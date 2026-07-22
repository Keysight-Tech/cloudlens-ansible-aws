# CloudLens Production Deployment — the 3 automation tracks

Three fully-automated deployment tracks, each building on the previous. Pick the
one that matches the customer's visibility model. Every manual "button" from the
UI (EULA, licensing, adoption, cloud config, mirror path) is scripted here.

| Track | Model | Agent in guest? | When to use |
|---|---|---|---|
| **1. CLMS standalone + sensors** | Direct CloudLens | Yes | Simplest. CLMS manages sensors directly, no KVO. |
| **2. CLMS + KVO + KVO-managed sensors** | KVO-orchestrated agent | Yes | KVO is the single pane; sensors register to a KVO-provisioned project. |
| **3. CLMS + KVO + AWS mirror sessions** | Agentless (VPC Traffic Mirroring) | **No** | **The big one for customers.** No in-guest agent; AWS mirrors Nitro sources to KVO collectors. |

Common first step for all three — deploy the stack (same-VPC CLMS[+KVO][+vPB]+workloads):

```bash
# CloudFormation
deploy/deploy-stack.sh            # interactive; toggles DeployKVO / DeployVPB / EnableZoneTapping
# or Terraform
cd deploy/terraform && terraform apply   # vars: deploy_kvo, deploy_vpb, enable_zone_tapping
```
CLMS needs ~15 min to initialize after the instance is up.

---

## Track 1 — CLMS standalone with sensors

1. **Deploy** the stack (KVO optional/off).
2. **Project key** — handles the forced first-login password change automatically:
   ```bash
   python3 scripts/vcontroller_project_key.py --clms <clms-ip> --project <name> --creds-file creds.txt
   # prints the working login (URL/user/password/project/api_key)
   ```
3. **Install sensors** on every tagged VM (Docker/Podman/Windows) with that key:
   ```bash
   ansible-playbook -i inventory/aws_ec2.yaml deploy.yaml \
     -e project_key=<api_key> -e cloudlens_ip=<clms-ip>
   # per-OS lanes: playbooks/ubuntu.yaml, redhat.yaml, windows.yaml
   ```
   Manual equivalent (one host): `docker run … <clms>/sensor --accept_eula yes --project_key <key> --server <clms> --ssl_verify no`
4. **Verify**: CLMS project shows the sensors registered (`agent/register` → 200).

---

## Track 2 — CLMS with KVO adopted + KVO-managed sensor deploy

Everything in Track 1's sensor step, but the project is created *through KVO* so
KVO is the single management plane. All KVO buttons scripted in
`scripts/kvo_adopt_clms.py`.

**KVO one-time setup (buttons):**
1. **Accept EULA** — KVO 302s every path until this is done (legal acceptance, gated behind a flag):
   `POST /eula/v1/eula/<id> {"accepted":true}`  (script: `--accept-eula`)
2. **Activate licenses** — a KVO with no license refuses *every* write. Per activation code:
   ```
   POST /api/v2/licensing/operations/retrieve-activation-code-info {"activationCode":"<code>"}
   POST /api/v2/licensing/operations/activate  {activationCode, quantity}
   GET  /api/v2/licensing/licenses             # confirm installed > 0
   ```
   Codes seen: `CL.vPB.ADVPERM` (vPB), `CL-CREDIT` (sensor credits), `KVO-DEVICE` (device adoption).

**Adopt + provision (per CLM), one command:**
```bash
python3 scripts/kvo_adopt_clms.py \
  --clms <clms-ip> --clms-admin-pass <pw> --kvo <kvo-ip> \
  --name clms-prod --cloud-config prod-cloud --accept-eula --insecure
```
This runs, in order (each in a committed KVO change request):
1. **CLMS: create KVO user** — `POST /admin/kvo_users` (one non-admin user per CLM).
2. **KVO: adopt CLMS** — `createCloudLensManager(name, {ip,user,password})`, **commit the change request**, wait for status **CONNECTED**.
3. **KVO: Custom Cloud** — `createCustomCloud` (cloud presence; shows in Global Dashboard).
4. **KVO: Cloud Config** — `createCloudConfig(CustomCloudConfig)` — **this provisions the real CLM project** `KVO_<name>` and returns the working sensor key. (Skipping this = phantom key + empty Cloud Configs page.)

Then **install sensors** with the printed key exactly as Track 1 step 3.

**Verify**: KVO Visibility Fabric > Cloud Configs shows the cloud; the custom cloud `status.sensorCount` rises as sensors register.

---

## Track 3 — CLMS + KVO with AWS mirror sessions (agentless)  ★ the customer big one

No agent in the guest. KVO deploys collector Service VMs and drives **AWS VPC
Traffic Mirroring**: AWS copies traffic from every selected Nitro source ENI to
the collectors, which forward it (L2GRE/VXLAN) to a destination tool.

**Hard requirements (each one silently blocks mirroring if missed):**
- **Nitro source instances** — AWS only mirrors Nitro (t3/m5/c5/… ; check `describe-instance-types … Hypervisor`). Non-Nitro → use Track 1/2 sensors.
- **KVO AWS IAM** — greenfield `EnableZoneTapping=yes` (CFN) / `enable_zone_tapping="true"` (TF), or brownfield `scripts/kvo_enable_zonetap_iam.sh`. Least-privilege policy: `deploy/iam/cloudlens-zonetap-policy.json`.
- **8443 security group** — KVO must reach the collector on 8443.
- **A destination tool** — an analyzer/NPB VM (or vPB) reachable from the collector egress, receiving L2GRE/VXLAN.

**Steps:**
1. Do Track 2's KVO setup (EULA, licenses, adopt CLMS → CONNECTED).
2. Tag the source instances `cloudlens=yes` (same tag as the sensor path).
3. One command runs the whole fabric (presence → AWS cloud config → collection → tool → monitoring policy):
   ```bash
   python3 scripts/kvo_aws_mirror.py \
     --kvo <kvo-ip> --clm-name clms-prod \
     --region <region> --vpc-id <source-vpc> --source-tag cloudlens=yes \
     --ssh-key <keypair> --cloudlens-ip <clms-ip> \
     --zone <az> --mgmt-subnet <s> --ingress-subnet <s> --egress-subnet <s> \
     --tool-remote-ip <analyzer-or-vpb-ip> --tool-encap L2GRE \
     --accept-eula --insecure
     # collector AMI auto-resolves per region; add --aws-access-key/--aws-secret-key
     # only if KVO has no instance role.
   ```
   The commit-time gotchas are baked in so they can't bite: the remote tool is
   created with `vlanStripping:false` ("Strip Path Solver VLAN" OFF) and
   `reachableFrom:CLOUD_CONFIG` ("Traffic source is a cloud").
4. KVO creates: collector **ASG + SVM(s)**, Traffic Mirror **target + filter**, and one **session per source ENI**.
5. **Verify**:
   ```bash
   aws ec2 describe-traffic-mirror-sessions --region <region>
   aws ec2 describe-traffic-mirror-targets  --region <region>
   ```
   plus KVO: Visibility Fabric > Cloud Configs / Tools / Monitoring Policies.

**KNOWN OPEN ITEM (Keysight):** in the reference test env the collector SVM
never bootstrapped — its launch-template user-data was empty and KVO made no
8443 push, so it never registered and no sessions were created, **even with the
whole fabric (tool + policy) correctly committed.** The automation and config are
complete; the collector-provisioning handshake is the missing link. Question for
Keysight: how does the AWS collector SVM receive its CLMS IP + project key
(user-data vs 8443 push vs SVM polling)? See
`~/keysight-kb/digests/kvo-clms-adoption-api.md`.

---

## Coming next — Track 3b: vPB adopted into KVO as the tool

Automate adopting the **vPB** into KVO as a managed device and wiring the full
traffic path (ingress → filter → egress → tool) so KVO collects, filters, and
dumps traffic to a tool, all inside KVO. Requires a **licensed, 3-NIC** vPB.
Procedure (vPB `kvo ip/enable` + KVO Inventory adoption + Device Config + Cloud
to Device Link) is documented in the KB digest; automation is the next build.
