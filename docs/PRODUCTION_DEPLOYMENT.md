# CloudLens Production Deployment: the automation tracks

Naming: CLMS is now the vController (AWS Marketplace: Keysight CloudLens Manager). The scripts keep the `--clms` spelling; the deploy's flags and outputs say vController.

Fully-automated deployment tracks, each building on the previous. Pick the
one that matches the customer's visibility model. Every manual "button" from the
UI (EULA, licensing, adoption, cloud config, mirror path) is scripted here.

| Track | Model | Agent in guest? | When to use |
|---|---|---|---|
| **1. CLMS standalone + sensors** | Direct CloudLens | Yes | Simplest. CLMS manages sensors directly, no KVO. |
| **2. CLMS + KVO + KVO-managed sensors** | KVO-orchestrated agent | Yes | KVO is the single pane; sensors register to a KVO-provisioned project. |
| **3. CLMS + KVO + AWS mirror sessions** | Agentless (VPC Traffic Mirroring) | **No** | **The big one for customers.** No in-guest agent; AWS mirrors Nitro sources to KVO collectors. |
| **3b. vPB adopted into KVO as the tool** | Agentless, vPB as the KVO-managed tool | **No** | KVO collects, filters and forwards through the vPB; one vPB per area with `--vpb-rails`. |
| **4. EKS pods** | Sensor DaemonSet (or sidecar) under a KVO Kubernetes presence | Yes (one per node) | Pod traffic on an EKS cluster; its own vPB under `--vpb-rails`. |

Every track is one `deploy/deploy-stack.sh` run. The scripts named under each track are what its phases call, and can be run by hand against a stack that is already up:

```bash
bash deploy/deploy-stack.sh --doctor                                        # check first, deploys nothing
bash deploy/deploy-stack.sh --no-kvo --with-vpb                             # Track 1: standalone sensors (--sensor-mode standalone, the default)
bash deploy/deploy-stack.sh --with-kvo --sensor-mode kvo --kvo-codes CODE   # Track 2: KVO-managed sensors
bash deploy/deploy-stack.sh --with-kvo --with-mirror \
     --mirror-access-key AK --mirror-secret-key SK                          # Track 3: agentless mirroring (--tapping mirror or both)
bash deploy/deploy-stack.sh --with-kvo --with-vpb --adopt-vpb --wire-vpb-path   # Track 3b: the vPB as the tool
bash deploy/deploy-stack.sh --with-kvo --with-eks --eks-cluster NAME        # Track 4: EKS pods
# Terraform builds the infrastructure only (no existing-VPC support):
cd deploy/terraform && terraform apply   # vars: deploy_kvo, deploy_vpb, enable_zone_tapping
```
The vController needs ~15 min to initialize after the instance is up; the wait phase (`--from wait`) waits for it. A re-run resumes from the first unfinished phase (`--resume`, `--from`, `--only`: `stack wait bootstrap key license adopt sensors eks vpb mirror path prove`).

---

## Track 1: CLMS standalone with sensors

1. **Deploy** the stack (KVO optional/off).
2. **Project key**: handles the forced first-login password change automatically:
   ```bash
   python3 scripts/vcontroller_project_key.py --host <vcontroller-ip> --project <name> --creds-file creds.txt --insecure   # or --ca-bundle <file>
   # prints the working login (URL/user/password/project/api_key)
   ```
3. **Install sensors** on every tagged VM (Docker/Podman/Windows) with that key:
   ```bash
   # customer_input.yaml carries cloudlens.manager_ip_or_fqdn and cloudlens.project_key;
   # deploy.yaml and inventory/group_vars/all.yaml read them from there.
   bash quickstart.sh      # discovery, classification (playbooks/classify.yaml), then the per-OS lanes
   # per-OS lanes: playbooks/ubuntu.yaml, redhat.yaml, windows.yaml (Windows only with
   # CLOUDLENS_WINDOWS_SENSOR=true or windows: {enabled: true}; it is off by default)
   ```
   Manual equivalent (one host): `docker run … <clms>/sensor --accept_eula yes --project_key <key> --server <clms> --ssl_verify no`
4. **Verify**: CLMS project shows the sensors registered (`agent/register` → 200).

---

## Track 2: CLMS with KVO adopted + KVO-managed sensor deploy

Everything in Track 1's sensor step, but the project is created *through KVO* so
KVO is the single management plane. All KVO buttons scripted in
`scripts/kvo_adopt_clms.py`.

**KVO one-time setup (buttons):**
1. **Accept EULA**: KVO 302s every path until this is done (legal acceptance, gated behind a flag):
   `POST /eula/v1/eula/<id> {"accepted":true}`  (script: `--accept-eula`)
2. **Activate licenses**: a KVO with no license refuses *every* write. Per activation code:
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
1. **CLMS: create KVO user** - `POST /admin/kvo_users` (one non-admin user per CLM).
2. **KVO: adopt CLMS** - `createCloudLensManager(name, {ip,user,password})`, **commit the change request**, wait for status **CONNECTED**.
3. **KVO: Custom Cloud** - `createCustomCloud` (cloud presence; shows in Global Dashboard).
4. **KVO: Cloud Config** - `createCloudConfig(CustomCloudConfig)`. This **provisions the real CLM project** `KVO_<name>` and returns the working sensor key. (Skipping this = phantom key + empty Cloud Configs page.)

Then **install sensors** with the printed key exactly as Track 1 step 3.

**Verify**: KVO Visibility Fabric > Cloud Configs shows the cloud; the custom cloud `status.sensorCount` rises as sensors register.

---

## Track 3: CLMS + KVO with AWS mirror sessions (agentless)  ★ the customer big one

No agent in the guest. KVO deploys collector Service VMs and drives **AWS VPC
Traffic Mirroring**: AWS copies traffic from every selected Nitro source ENI to
the collectors, which forward it (L2GRE/VXLAN) to a destination tool.

**Hard requirements (each one silently blocks mirroring if missed):**
- **Nitro source instances**: AWS only mirrors Nitro (t3/m5/c5/… ; check `describe-instance-types … Hypervisor`). Non-Nitro → use Track 1/2 sensors.
- **KVO AWS IAM**: greenfield `deploy-stack.sh --enable-zone-tapping` (sets `EnableZoneTapping=yes` in CloudFormation; `enable_zone_tapping="true"` in Terraform; needs IAM-create rights), or brownfield `scripts/kvo_enable_zonetap_iam.sh`. Least-privilege policy: `deploy/iam/cloudlens-zonetap-policy.json`. The mirror fabric still needs an access key pair (`--mirror-access-key` / `--mirror-secret-key`): the instance role alone is not accepted by createAwsPresence.
- **Collector security groups**: 443 from the VPC, and 22 and 9022 from the VPC and the admin CIDR. The deploy creates them (`--collector-*-sg` names pre-approved groups instead). TCP 8443 is not required.
- **A destination tool**: an analyzer/NPB VM (or vPB) reachable from the collector egress, receiving L2GRE/VXLAN.

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
     # collector AMI auto-resolves per region (newest cloudlens-vpb-svm image).
     # --aws-access-key/--aws-secret-key are required: an instance role alone
     # makes createAwsPresence fail with 'accessKeyId cannot be empty'. The deploy
     # passes them as --mirror-access-key/--mirror-secret-key. --force-rebuild tears
     # the fabric down (presence included) when it exists without a collector ASG;
     # reusing a presence never relaunches the collector.
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

**The second commit of the Cloud Collection (automated):** the collector does
bootstrap and register, and the sessions do get created, but only when KVO acts
on the **Cloud Collection after the collector has registered its mirror
target**, and the collection was committed before that. Until then a stack sits
at zero sessions with a complete, correct fabric and nothing reporting an error:
no alert, no open change request, no failed task. Confirmed on four separate
stacks, and confirmed through the vPB as well, with `Passed 12,526` on the
traffic rule and `eth2 TX 16,542` packets leaving toward the tool.

Since 2026-09-28 `kvo_aws_mirror.py` does this itself. After the collector
registers its target (allow 10 to 15 minutes, the vpb-svm image is heavy), if
`aws ec2 describe-traffic-mirror-sessions` still shows none for the VPC, it
deletes the monitoring policy, then the collection, then recreates the
collection and the policy, each in its own committed change request that leaves
KVO in a valid state, and waits up to 240 s for the sessions: one per tapped
source ENI, about a minute later. It recreates the objects rather than editing
the selector on purpose: clearing the selector in one commit and restoring it in
another leaves the collection empty in between, the monitoring policy fails
validation, and the stuck change request wedges every later commit on that KVO.

Fallback, only if the count is still zero afterwards: in KVO open Visibility
Fabric > Cloud Collections, remove the workload selector from the collection and
add it back, and commit that as ONE change request. The prove phase prompts for
this when it counts zero sessions.

What the deploy does around it: the path phase (`--from path`) terminates the collector once the fabric is complete so the Auto Scaling Group relaunches a fresh one against the finished config (a collector reads its config once, at boot; a reboot keeps the stale state), and `kvo_aws_mirror.py` recreates the whole fabric, presence included, when it finds a fabric with no collector ASG. The prove phase (`--from prove`) reads the session count, prompts for the fallback re-commit above when it is zero, then runs `scripts/prove_traffic.sh`, retrying for up to 10 minutes while the collector registers, and reports which leg failed. Mirroring a VPC that is not the stack's own needs the collector placement named explicitly: `--collector-zone`, `--collector-mgmt-subnet`, `--collector-ingress-subnet`, `--collector-egress-subnet` (and the three `--collector-*-sg` ids to create nothing), or one `--source-vpc vpc:az:mgmt:ingress:egress` spec per VPC. KVO needs three distinct subnets in one AZ or the fabric commits cleanly and cuts zero sessions. `--discover` proposes that placement from subnets tagged `cloudlens:role=mgmt/ingress/egress`.

Full sequence, the ordering behind it, and the vPB numbers:
[`AWS_ZONE_TAPPING.md`](AWS_ZONE_TAPPING.md).

---

## Track 3b: vPB adopted into KVO as the tool (automated)

The vpb and path phases of the deploy (`--adopt-vpb`, `--wire-vpb-path`; resume with `--from vpb` or `--from path`). `scripts/vpb_kvo_adopt.py` enters the `kvo` context on the vPB over SSH (`ip <kvo-private-ip>`, `port 443`, `enable`, `exit`) and adopts the announced device in KVO with control enabled; KVO applies the vPB licence on adoption. The by-hand route adds `license server <kvo-private-ip> type advanced` before the `kvo` context. `scripts/vpb_wire_path.py` then syncs the device ports, creates the Cloud to Device Link, binds eth1 as ingress, creates a REMOTE tool reachable from the device config (a LOCAL tool forwards nothing: raw frames on an AWS subnet are dropped), binds eth2 as egress with an address so it can source the tunnel, attaches the link to the cloud config (`--link-config` names which one) and creates the monitoring policy. Requires a 3-NIC vPB; the stack wires the NICs. KVO allows one cloud config per link, so `--vpb-rails mirror,k8s` launches, adopts and wires one more vPB per further area and the teardown sweeps them before the stack delete. Proven live on the AWS path with the capture host counting packets, and on 2026-10-07 in an existing VPC with both rail vPBs adopted and wired and the traffic proof passing on the vPB path. The device counters are the truth: `sudo vpb -c 'show traffic-rule-packet-counters'` with Inspected climbing while Passed stays at 0 means no forwarding rule, not a DPDK or reachability problem.

---

## Track 4: EKS pods (`--with-eks`)

`--eks-cluster NAME` taps an existing cluster; `--eks-sample` creates a 2x t3.medium test cluster plus a sample traffic app (~15 min). `--eks-mode daemonset` (default, one sensor per node, automated) or `sidecar` (customer pods get a rendered snippet, never an automatic restart). `--eks-sensor-image URI` for an image the nodes already reach, or `--eks-sensor-tar PATH` to push the CloudLens-Sensor tar to ECR. `--eks-pod-selector REGEX` limits which pods the KVO collection taps; every tapped pod costs a credit. Order matters and is enforced: `scripts/kvo_k8s_config.py` creates the Kubernetes presence and its cloud config first, the DaemonSet (`scripts/deploy-eks-tapping.sh`) registers with the presence key, and an in-VPC cluster is pointed at the vController's private address. The Kubernetes area gets its own vPB and Cloud to Device Link under `--vpb-rails`. Details: [`KUBERNETES_RAIL.md`](KUBERNETES_RAIL.md).

---

## Existing VPC, discovery, teardown

- **Existing VPC**: `--existing-vpc-id` + `--existing-subnet-id`; `--existing-data-subnet-id` + `--existing-tool-subnet-id` for the vPB data plane (same AZ); `--existing-sg-id` for a pre-approved group. The script reads the VPC CIDR and fills the template's `ExistingVpcCidr` so the in-VPC ports open to the customer's range. CloudFormation only: Terraform refuses `--existing-*`. Sensors register to the vController's private address (`CLOUDLENS_SENSOR_MANAGER_ADDR` overrides). Proven live 2026-10-07 with an existing EKS cluster and pre-existing tagged workloads: both vPBs adopted and wired (one per rail), the sensor DaemonSet on every node, and the traffic proof passing on the mirror path and the vPB path.
- **Discovery**: `--discover` with `--discover-regions`, `--discover-accounts organization` and `--discover-role` finds tagged instances, proposes collector placement per VPC and writes `inventory/discovered.json` plus a replayable `--profile`. Read-only.
- **Rehearsal**: `bash scripts/lab/brownfield-fixture.sh create|status|env|destroy` builds a customer-shaped VPC (management, data and tool subnets in one zone, two tagged workloads, an EKS cluster) with nothing CloudLens in it.
- **Teardown**: `bash deploy/teardown-stack.sh --stack-name NAME --region REGION` audits, releases the KVO's licences while the KVO is alive (`--release-licences` without a terminal), terminates the rail vPBs and their Elastic IPs, deletes the stack, then sweeps the volumes, security groups, collector ASG, mirror targets and the deploy-stamped resources inside a customer VPC. In a pre-existing VPC only the VPC itself and the resources the deploy did not stamp are left alone. `--orphans` is the read-only audit.
