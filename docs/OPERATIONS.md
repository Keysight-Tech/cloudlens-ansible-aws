# CloudLens Operations Guide (AWS)

Single source of truth for every gotcha a customer or SE will hit when working
with **vController** (formerly CLMS), **KVO** (Keysight Vision Orchestrator), and **vPB**
(Virtual Packet Broker) from the AWS Marketplace, plus the Ansible sensor
deployment on top.

If you hit any wall not described here, please open a PR adding it. The point
of this doc is that nobody should ever burn the same hour twice.

Everything below is written for **us-east-1**. The template ships AMIs for
us-east-1, us-east-2, us-west-1, us-west-2, ca-central-1, eu-west-1, eu-west-2,
eu-central-1, ap-southeast-1, ap-southeast-2, ap-northeast-1 and ap-south-1:
pass `--region` and subscribe to the Marketplace AMIs in that region first.
Section 9 is only for a region outside that list.

---

## 0. Configuration knobs for `deploy-stack.sh`

Every default in the bash one-liner is overridable. Full reference table is in
[README.md "Configuration and overrides"](../README.md#configuration-and-overrides-deploy-stacksh).
Same precedence everywhere: **CLI flag wins over env var wins over hardcoded
default**. Run `bash deploy-stack.sh --help` for the in-script version.

Most common knobs:

| What | Default | Env var | Flag |
|---|---|---|---|
| Stack name | `cloudlens-stack` | `CLOUDLENS_STACK_NAME` | `--stack-name` |
| Region | `us-east-1` | `CLOUDLENS_REGION` | `--region` |
| EC2 key pair | prompted: the script lists the region's key pairs, or creates one | `CLOUDLENS_KEY_NAME` | `--key-name` (created if missing) |
| Admin ingress CIDR | interactive run offers this machine's public /32; template and non-interactive default `0.0.0.0/0` | `CLOUDLENS_ADMIN_CIDR` | `--admin-cidr` (ignored with `--existing-sg-id`) |
| Deploy KVO | prompted (template default `yes`) | `CLOUDLENS_DEPLOY_KVO` | `--with-kvo` / `--no-kvo` |
| Deploy vPB | prompted (template default `yes`) | `CLOUDLENS_DEPLOY_VPB` | `--with-vpb` / `--no-vpb` |
| VPC and subnet CIDRs | `10.99.0.0/16`, mgmt `10.99.1.0/24`, data `10.99.11.0/24`, tool `10.99.12.0/24` | n/a | Launch Stack form only (`VpcCidr`, `MgmtSubnetCidr`, `DataSubnetCidr`, `ToolSubnetCidr`); the CLI deploys into your own VPC with `--existing-vpc-id` instead |
| Discovery tag key | `cloudlens` | `CLOUDLENS_DISCOVERY_TAG_KEY` | `--discovery-tag-key` |
| Discovery tag value | `yes` | `CLOUDLENS_DISCOVERY_TAG_VALUE` | `--discovery-tag-value` |
| Engine | `cfn` | `CLOUDLENS_IAC` | `--iac cfn\|terraform` (Terraform refuses every `--existing-*` flag) |
| Existing VPC | (new VPC) | `CLOUDLENS_EXISTING_VPC_ID`, `_SUBNET_ID`, `_DATA_SUBNET_ID`, `_TOOL_SUBNET_ID`, `_SG_ID` | `--existing-vpc-id`, `--existing-subnet-id`, `--existing-data-subnet-id`, `--existing-tool-subnet-id`, `--existing-sg-id` |
| vController type | `t3.xlarge` | `CLOUDLENS_VCONTROLLER_TYPE` | `--vcontroller-type t3.xlarge\|m5.xlarge` |
| Tapping | prompted | `CLOUDLENS_TAPPING` | `--tapping sensors\|mirror\|both\|none` |
| vPB per area | every enabled area | `CLOUDLENS_VPB_RAILS` | `--vpb-rails mirror,k8s` |
| EKS | off | `CLOUDLENS_EKS_CLUSTER`, `CLOUDLENS_EKS_SAMPLE`, `CLOUDLENS_EKS_MODE`, `CLOUDLENS_EKS_POD_SELECTOR` | `--eks-cluster NAME`, `--eks-sample`, `--eks-mode daemonset\|sidecar`, `--eks-pod-selector REGEX` |
| Mirror | off | `CLOUDLENS_MIRROR_ACCESS_KEY` / `_SECRET_KEY`, `CLOUDLENS_SOURCE_VPCS` | `--with-mirror`, `--source-vpc-id`, `--source-vpc SPEC`, `--collector-*` |
| Discovery | off | `CLOUDLENS_DISCOVER`, `CLOUDLENS_DISCOVER_REGIONS`, `CLOUDLENS_DISCOVER_ACCOUNTS`, `CLOUDLENS_DISCOVER_ROLE` | `--discover`, `--discover-regions`, `--discover-accounts self\|organization`, `--discover-role` |
| Test workloads | none | `CLOUDLENS_TEST_VMS` | `--test-vms ubuntu:3,rhel:2,windows` |
| Pre-flight | n/a | n/a | `--doctor` (deploys nothing) |
| Replay | n/a | n/a | `--profile FILE` or `--profile https://URL` (deploy-profile-<stack>.env is written at the plan step) |
| Events | n/a | `CLOUDLENS_EVENTS_FILE`, `CLOUDLENS_PROMPT_PIPE` | `--events FILE` (+ `--prompt-pipe FIFO` for the console) |
| Resume | resume | n/a | `--resume`, `--fresh`, `--from PHASE`, `--only PHASE` |

For end-to-end verification, `--test-vms ubuntu,rhel,windows` deploys throwaway
workloads inside the stack, tagged with the discovery tag. To test a custom tag,
run `scripts/deploy-test-workload-vms.sh` first (default tag
`monitoring=enabled`, `TESTVMS_TAG_KEY` / `TESTVMS_TAG_VALUE` to change it) and
deploy with `--discovery-tag-key monitoring --discovery-tag-value enabled`. To
rehearse a brownfield deploy, `bash scripts/lab/brownfield-fixture.sh create`
builds what a customer already has (a 10.60/16 VPC with mgmt, data and tool
subnets in one zone, two tagged workloads talking HTTP, an EKS cluster running
the sample app); `env` prints the ids as shell exports for the `--existing-*`
flags and `--eks-cluster`; `status` and `destroy` find everything by the
`cloudlens:lab` tag.

---

## 0a. Re-runs, phases and brownfield

A re-run never starts from zero. Before anything is created the script probes
the real system (CloudFormation, the vController API, the KVO licensing API and
GraphQL, EC2 for the mirror resources) and skips every phase that is done; the
state file `.cloudlens-deploy-<stack>-<region>.state` (mode 600) only remembers
inputs. `--resume` is the default with no terminal, `--fresh` runs everything
again, `--from PHASE` and `--only PHASE` take one of the 12 stable names:
`stack wait bootstrap key license adopt sensors eks vpb mirror path prove`.
Nothing in the deploy deletes anything. Every run ends with
`deploy-report-<stack>-<region>.html` and `cloudlens-deploy-summary.txt`.

Brownfield: `--existing-vpc-id` + `--existing-subnet-id` put the appliances in
your VPC; add `--existing-data-subnet-id` AND `--existing-tool-subnet-id` (same
AZ) for a forwarding vPB; `--existing-sg-id` attaches your group and ignores
`--admin-cidr`. CloudFormation only. The deploy fills `ExistingVpcCidr` from the
VPC so the in-VPC rules match your range. Sensors inside the VPC register to the
vController's private address (`CLOUDLENS_SENSOR_MANAGER_ADDR` overrides).
Proven live 2026-10-07 with an existing EKS cluster and pre-existing tagged
workloads, both vPB rails passing. `--vpb-rails mirror,k8s` gives each area its
own vPB because KVO allows one cloud config per Cloud-to-Device Link;
`--eks-cluster NAME` taps an existing cluster (Kubernetes presence first,
DaemonSet on its key).

---

## 1. Quick reference: every port and credential you will touch

| Component | Public port(s) | Default UI cred | Default CLI cred | First-boot wait |
|---|---|---|---|---|
| **vController** | TCP 443 (web UI), TCP 22 (Linux SSH) | `admin / Cl0udLens@dm!n` (force-change on first login) | n/a | ~15 min |
| **KVO** | TCP 443 (web UI), TCP 9022 (KCOS SSH, EC2 key pair as `admin`; the stack also opens 22) | `admin / admin` (EULA first, then change it) | n/a | ~15 min |
| **vPB** | TCP **9022** (KCOS SSH), TCP 443 (mgmt web), UDP 4789 (VXLAN), UDP 10800-10801 (Keysight VXLAN), IP protocol 47 (L2GRE) | n/a (managed from KVO once adopted) | `ssh -i <key>.pem -p 9022 admin@<vpb-ip>` (EC2 key pair, no password), then `sudo vpb` for the `CloudLensVPB#` CLI. `admin / ixia` is the vPB device credential KVO uses at adoption (and the console login of the KVM, ESXi and generic-installer builds), not an SSH login on the AWS image | 10 to 15 min |
| **Collector SVM** | n/a (auto-deployed by KVO Zone Tapping) | n/a | n/a | auto |
| **Sensor** | n/a | n/a | n/a | <1 min |

### Why these are not the obvious defaults

- **vPB OS SSH is on port 9022, NOT port 22.** Keysight CloudLens OS (KCOS)
  binds sshd to 9022. A connection to `:22` on the vPB will time out forever
  even after the instance is fully up. The Marketplace AMI plus the CloudFormation
  security group open 9022 automatically; if you build your own security group,
  add TCP/9022 by hand.
- **The vPB CLI is reached over the same 9022 SSH session.** SSH in as `admin`
  with the EC2 key pair and you land in the KCOS shell; `sudo vpb` (installed by
  `scripts/bootstrap-vpb.sh` at first boot, re-run by phase 8 `--bootstrap-vpb`
  if needed) opens the `CloudLensVPB#` console, and `sudo vpb -c "show version"`
  runs one command. There is no password and no second hop.
- **vController is reached through its web UI; KVO SSH is on 9022 too**
  (`ssh -i <key>.pem -p 9022 admin@<kvo-ip>`). The vController forces a password
  change on the first login and KVO gates everything, API included, behind its
  EULA; the deploy completes both over the API (phase 9 sets a known vController
  admin password and mints the project key, `--accept-eula` in phases 11 and 12
  for KVO), so no manual browser session is needed.
- **Instance types are constrained by the Marketplace AMI.** vController runs
  on `t3.xlarge` (default) or `m5.xlarge` (`--vcontroller-type`, dedicated CPU
  for production); vPB on `t3.xlarge` only; KVO on `c5.2xlarge` only. Anything
  else is rejected at launch with `UnsupportedOperation` (see section 8).

---

## 2. How to reach each device

AWS uses EC2 key pairs, not passwords, for the Linux OS login. The vController
and KVO UIs still use the shared default web credentials.

### vController (SSH port 22, web 443)

```bash
# vController is appliance-managed: use the web UI (https://<vcontroller-eip>).
# Its shell uses password auth, not your key pair.
```

If SSH is refused right after launch, the image is still initialising. Open
`https://<vcontroller-eip>` in a browser, accept the EULA, sign in
`admin / Cl0udLens@dm!n`, complete the forced password change, then retry SSH.

### KVO (SSH port 9022 with the EC2 key pair, web 443)

Same flow as vController. The EULA plus first-login is a one-time gate. KVO
blocks all access, including the REST API, until the EULA is accepted (the
deploy does this over the API with `--accept-eula`; by hand, accept it once in
a browser).

```bash
ssh -i ~/.ssh/<your-key>.pem -p 9022 admin@<kvo-eip>
```

### vPB (SSH + CLI on port 9022, NOT 22)

```bash
ssh -i ~/.ssh/<your-key>.pem -p 9022 admin@<vpb-eip>   # EC2 key pair, no password
sudo vpb                                                # CloudLensVPB# console
sudo vpb -c "show version"                              # one command, non-interactive
```

You land in the KCOS shell; `sudo vpb` opens the console. There is no second-hop
SSH on AWS.

#### If `ssh -p 9022` itself times out

1. **Check the security group.** The vPB security group must allow inbound
   TCP/9022 from your admin CIDR. The CloudFormation stack opens 22, 9022,
   443, GRE (IP protocol 47), UDP 4789 and UDP 10800-10801 from the admin
   CIDR automatically. For a hand-rolled deployment:

   ```bash
   aws ec2 authorize-security-group-ingress \
     --group-id <vpb-sg-id> --protocol tcp --port 9022 \
     --cidr <your-admin-cidr> --region us-east-1
   ```

2. **Check the instance is fully booted.** vPB needs 10 to 15 min after the
   instance state reaches `running`. Confirm which listeners are up via SSM:

   ```bash
   aws ssm send-command --region us-east-1 \
     --document-name AWS-RunShellScript \
     --targets Key=instanceids,Values=<vpb-instance-id> \
     --parameters 'commands=["uptime","ss -tnl | grep :9022"]'
   ```

   If port 9022 is not listening yet, KCOS is still initialising. Wait 5 more
   minutes and try again.

---

## 3. Default credentials cheat-sheet

| Where | Username | Initial password | When does it change |
|---|---|---|---|
| vController web UI | `admin` | `Cl0udLens@dm!n` | Forced on first login |
| vController console / CLI (type `console` at the VM console) | `admin` | `transportation understanding manufacturing relationship` (vController UG v6.14.1 p.49); the appliance shell does not use the EC2 key pair | n/a |
| KVO web UI | `admin` | `admin` (after the EULA) | Not changed by the deploy, which signs in as `admin / admin` (`CLOUDLENS_KVO_ADMIN_PASS` if you rotated it); change it in the KVO UI |
| KVO KCOS SSH (port 9022) | `admin` | EC2 key pair | n/a |
| vPB KCOS SSH (port 9022) | `admin` | EC2 key pair, then `sudo vpb` for the CLI | n/a (`admin / ixia` is the vPB device credential KVO uses at adoption and the on-prem console login, not an SSH login on the AWS image) |
| Workload EC2 (Linux) | `ubuntu` (Ubuntu) / `ec2-user` (RHEL/AL) | EC2 key pair | n/a |
| Workload EC2 (Windows) | `Administrator` | EC2 key-pair-decrypted password | n/a |

The demo orchestrator (`demo/setup-aws-visibility-demo.sh`) reuses one EC2 key
pair everywhere and captures every EIP into a state file, so you only track one
key.

---

## 4. Adopting vPB and vController into KVO

This is the premium "single pane of glass" workflow that turns three separately
launched EC2 instances into one fleet view.

The deploy does all of this: phase 11 licenses the KVO
(`scripts/kvo_license.py --kvo <ip> --codes CODE[,QTY] --accept-eula`), phase 12
adopts the vController (`scripts/kvo_adopt_clms.py`: creates a non-admin KVO
user on the vController, accepts the KVO EULA, `createCloudLensManager`, commits
the change request, polls to CONNECTED) and creates the Cloud Config that
provisions the sensor project key, phase 14 adopts the vPB
(`scripts/vpb_kvo_adopt.py`: over SSH `sudo vpb -c` sends the `kvo` context
`ip <kvo-private-ip> / port 443 / enable / exit`, waits for the vPB to announce
itself, then adopts it with control enabled using the vPB device credential
`admin / ixia` and verifies it connects; KVO applies the vPB licence on
adoption, so the script sends no `license server` line and a licence warning
on the CLI before adoption is harmless). Re-run one step with `--only license`,
`--only adopt` or `--only vpb`. The manual route below is for reference; its
`license server <kvo-ip> type advanced` line is the by-hand route.

### 4a. Prerequisites

- KVO is launched and you have completed the EULA plus first-login on its web UI.
- A KVO user exists (for example `clms@keysight.com`) with the `KVO User` role
  or higher.
- vController, KVO and vPB share the same VPC (the stack puts all three in the
  `mgmt` subnet `10.99.1.0/24`), so KVO can reach them on private IPs. If you
  split them across VPCs, create VPC peering plus route-table entries first.

### 4b. Discover the vController from KVO

Create a non-admin KVO user on the vController (the deploy posts it to
`/cloudlens/api/v1/admin/kvo_users`), then in KVO: Inventory > CloudLens
Manager > Discover CloudLens Manager, enter a name, the vController **private**
IP and that user's credentials, and commit the change request KVO raises. Status
must reach CONNECTED. Then create the Cloud Config (Cloud Fabric > Cloud Configs
> New > Custom Cloud): that is what provisions the project on the vController
and returns the sensor key.

### 4c. Add vPB to KVO

**Step 1: SSH into the vPB console** (port **9022**, not 22)

```bash
ssh -i <key>.pem -p 9022 admin@<vpb-eip>
sudo vpb
```

You will see the Keysight EULA prompt the first time only. Accept it to reach
the `CloudLensVPB#` prompt.

**Step 2: Tell vPB where KVO lives**

Use the KVO **private** IP. Point the vPB at the KVO licence server first
(`license server <kvo-private-ip> type advanced`: this is the by-hand route;
the deploy sends no such line because KVO applies the licence on adoption),
then enter the `kvo` context. `username` and `password` are optional: the
proven automated sequence sets neither (`ip`, `port`, `enable`, `exit`) and
the vPB announces itself to KVO on `enable`. Add them only if your build stays
`disconnected` after `enable`.

```text
configure terminal
license server <kvo-private-ip> type advanced
kvo
ip <kvo-private-ip>
port 443
enable
exit
end
write memory
```

Optional fields inside the `kvo` context, entered before `enable`:
`username <kvo-user>`, `password <kvo-user-password>` and `monitored`.

If your build does not accept `kvo` as a verb, type `?` at the `CloudLensVPB#`
prompt to see what it does accept. Some builds use `management-server` or
`orchestrator`. The submode fields (`ip`, `port`, `enable`, and the optional
`username`, `password`, `monitored`) are the same across all of them.

**KVO side check.** Before this works, KVO must have:
- `Live Settings > Remote Access URL` set to `https://<kvo-private-ip>`
- A user (for example `clms@keysight.com`) created under `User Management` with
  the `KVO User` role or higher, only if you set `username` / `password` in
  the `kvo` context

Both happen one time at KVO bootstrap.

**Step 3: Confirm vPB is talking to KVO**

```text
CloudLensVPB# show kvo
```

You should see status transition to `connected` within ~30 seconds. If it stays
`disconnected`:
- Confirm all three are in the same VPC/subnet, or that VPC peering plus routes
  are in place.
- Confirm the vPB security group allows outbound to KVO on TCP/443.
- Confirm KVO's security group allows inbound on TCP/443 from the vPB subnet.

**Step 4: Adopt in KVO**

In the KVO UI (`https://<kvo-eip>`):

- Left nav: `Inventory > Devices` (or `Adopt Auto Discovered Device`)
- The vPB now appears in the `Devices Available` table
- Check the box next to it, and adopt with **"Control the adopted device"
  enabled** (this is what populates the port-binding dropdowns later)

**Step 5: Licence.** The vPB already points at the KVO licence server
(`license server <kvo-private-ip> type advanced` from Step 2) and KVO applies
the vPB licence when it adopts the device; the `License Manager Error` warning
on the CLI clears within about 30 seconds of adoption. No manual credit is
applied.

---

## 5. AWS traffic path: VPC Traffic Mirroring and the Collector SVM

On AWS the packet path is native VPC Traffic Mirroring, orchestrated by KVO
Cloud Config, rather than a purely sensor-to-vPB VXLAN tunnel.

1. **KVO AWS presence** holds the access key pair KVO calls AWS with
   (`--mirror-access-key` / `--mirror-secret-key`; an instance role is not
   enough, `createAwsPresence` fails with "accessKeyId cannot be empty"). The
   least-privilege policy is `deploy/iam/cloudlens-zonetap-policy.json`,
   attached with `EnableZoneTapping=yes` / `--enable-zone-tapping` or
   `scripts/kvo_enable_zonetap_iam.sh`.
2. **KVO Cloud Config + Cloud Collection** select the sources by the discovery
   tag (KVO's own `system.tags.<key>` identifier) and commit. KVO launches the
   **collector Service VMs** as an Auto Scaling group outside CloudFormation,
   from the newest `cloudlens-vpb-svm-*` Marketplace image in the region
   (resolved by name at run time, never a fixed AMI id), sized at 10 tapped
   interfaces per collector plus one spare (`--collector-max` overrides). The
   collector needs THREE DISTINCT subnets in ONE zone (mgmt, ingress, egress);
   the lab derives them from its own appliances, a customer VPC names them with
   `--collector-zone` and `--collector-*-subnet` or a
   `--source-vpc vpc:az:mgmt:ingress:egress` spec. One cloud config is strictly
   one VPC: repeat `--source-vpc-id` for more.
3. **VPC Traffic Mirroring**: KVO creates the mirror target, filter and one
   session per Nitro source ENI.
4. **Collector to vPB**: the collector tunnels the copies over L2GRE to the vPB
   ingress (the Cloud-to-Device Link), and the vPB forwards out its tool
   interface.

The deploy builds all of this with `--with-mirror` (phase 15) and verifies it;
`--only mirror` re-runs it. The collector launches only when the presence is
created with the key, so the script recreates the presence rather than reuse
one, otherwise the config commits cleanly and cuts zero sessions. Manual route:
KVO > Cloud Fabric > Cloud Configs > New > AWS.

For a pure out-of-band demo without VPC Traffic Mirroring, you can also point
sensors directly at a tool receiver. The SE demo playbook demonstrates a GRE
(IP protocol 47) receiver, `tcpdump proto 47` on the tool host, not a VXLAN
one: a `udp port 4789` filter captures nothing there.

---

## 6. Sensor deployment (Ansible) troubleshooting matrix

| Symptom | Cause | Fix |
|---|---|---|
| `ssh admin@vpb -p 22` times out | KCOS does not expose 22 | Use port 9022 |
| `ssh -p 9022` times out | Security group missing TCP/9022 | Add SG ingress (see section 2) |
| `ssh -p 9022` fails after SG fix | KCOS still initialising | Wait 10 to 15 min after instance `running` |
| Inventory finds 0 instances | Discovery tag missing | `aws ec2 create-tags --resources <id> --tags Key=cloudlens,Value=yes` (`cloudlens=yes` is the only required tag; `os`, `env` and `Platform` are optional grouping tags) |
| SSH "Permission denied (publickey)" to Linux | Wrong key path or user | Set `aws.ssh_key_path` + correct `ssh_user_*` in `customer_input.yaml` |
| SSM "0 target instances" | Missing IAM role or SSM Agent stopped | Attach `AmazonSSMManagedInstanceCore`; confirm `aws ssm describe-instance-information` lists the instance |
| Windows sensor deploy hangs | SSM Agent stopped on the box | `Restart-Service AmazonSSMAgent` then re-run |
| vController REST API returns 405 | EULA + first-login not done in UI | Complete via browser once |
| KVO `Devices` empty | vPB/vController cannot reach KVO | Verify same VPC or VPC peering + routes |
| Adoption shows `Licensed` but vPB does not forward | License applied to vController, not vPB | Activate the **vPB** license |
| Sensor container starts but not in vController UI | Wrong project key or 443 blocked | Fresh key from `Settings > Projects > API Keys`; open egress to 443 |
| Mirror sessions never appear in EC2 console | Cloud Config not committed or source tags missing | Confirm Cloud Config `COMMITTED`; tag sources per the workload selector |
| Sessions stop being created past a count | The collector fleet ceiling: the deploy sizes it at 10 tapped interfaces per collector Service VM plus one spare | `--collector-max N` raises the ceiling; KVO adds collectors up to it |
| `The Terraform engine does not support deploying into existing infrastructure` | `--iac terraform` with any `--existing-*` flag | Drop `--iac terraform`; brownfield is CloudFormation only |
| Nothing in an existing VPC reaches the appliances; KVO cannot discover the vController | The in-VPC security-group rules were opened to the template's `VpcCidr` (10.99/16), not the customer's range | `ExistingVpcCidr`: the CLI reads it from the VPC; set it on the Launch Stack form |
| `docker pull <public-ip>/sensor` times out on every workload | Sensors in the VPC were pointed at the vController's public address, which the SG refuses | The deploy writes the private address; `CLOUDLENS_SENSOR_MANAGER_ADDR` overrides it for workloads elsewhere |
| `galaxy.ansible.com unreachable; continuing with the collections already in ./collections` | Transient TLS or proxy failure | Nothing to do when every collection is present; only a missing collection is fatal |
| `CloudLens manager 10.x is a private address this machine cannot reach` | Expected: the hosts pull the image, not the operator machine | Continue |
| Stack lands in `DELETE_FAILED` on the security group | An extra `--vpb-rails` vPB still holds it | `teardown-stack.sh` terminates the rail vPBs first; re-run it |
| `The maximum number of addresses has been reached` | Elastic IP quota (5 per region by default; the stack needs 3) | Phase 4b names the addresses to free; or `--no-public-ip` |
| `kvo_license.py` sees 503 from the KVO | KVO still booting | The script waits for the token endpoint, up to ten minutes |
| vController rejects the password the deploy just set | The appliance applied the change after the client gave up | The new value is written to the creds file first and the file is ignored unless it names this appliance; re-run `--only key` |

---

## 7. Security-group rules required (for CloudFormation / Terraform users)

| Rule set | Inbound |
|---|---|
| From the admin CIDR (`AdminIngressCidr` / `--admin-cidr`) | TCP/22, TCP/9022 (KCOS SSH), TCP/443 (web UIs and APIs), UDP/4789, UDP/10800-10801, IP protocol 47 (L2GRE) |
| From inside the VPC (`VpcCidr`, or `ExistingVpcCidr` in brownfield) | TCP/443 (sensors and KVO to the vController), TCP/7443 (vController to KVO), UDP/4789, UDP/10800-10801, IP protocol 47 from collectors and sensors |
| Between the appliances | every port (self rule) |
| **Collector SVM** (created by KVO outside the stack) | the deploy adds 443, 22 and 9022 from the VPC and 22/9022 from the admin CIDR; KVO configures the collector over SSH |
| **Workload EC2** | TCP/22 (Linux), TCP/5985-5986 (Windows WinRM, only if not using SSM), TCP/3389 (Windows RDP) from the admin CIDR only |

`deploy/cloudformation/stack.yaml` creates ONE shared group with these rules and
attaches it to every appliance; narrowing the admin CIDR to one operator host is
correct and does not break the in-VPC paths. With `--existing-sg-id` /
`ExistingSecurityGroupId` nothing is created and you open 22, 9022, 443, 7443,
GRE (IP protocol 47), UDP 4789 and UDP 10800-10801 yourself. Narrow the admin
CIDR before any customer-facing deployment.

---

## 7a. Operator gotchas when running quickstart.sh from your laptop

The customer-facing path is **AWS CloudShell** (`curl quickstart.sh | bash`)
where AWS auth is automatic and Python deps land in a clean venv. SE operators
running the same flow from a laptop sometimes hit these:

| Symptom | Cause | Fix |
|---|---|---|
| `Unable to locate credentials` from the `aws_ec2` inventory | No AWS profile in the environment | `aws sso login --profile <p>` or export `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` |
| `NoRegionError` from boto3 | Region not set | Set `aws.regions` in `customer_input.yaml` or export `AWS_DEFAULT_REGION=us-east-1` |
| `ModuleNotFoundError: No module named 'boto3'` | Ansible venv missing boto3 | `pip install boto3 botocore` inside the same venv Ansible uses |
| `A worker was found in a dead state` on macOS for Windows targets | macOS `fork()` safety check + pywinrm | `export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` and add `--forks 1` |
| SSM connection plugin `command not found: session-manager-plugin` | AWS session-manager-plugin not installed | Install the plugin, or switch `linux_connection` back to `ssh` |
| WinRM open in SG but Ansible times out | Windows Firewall still blocking, or SSM would be cleaner | Prefer `windows_connection: ssm`; or open the host firewall for 5985 |

Quickstart.sh handles credential detection and collection install; the rest are
operator-environment specific so they live here in OPERATIONS.md.

---

## 8. Marketplace and instance-type gotchas

| Symptom | Cause | Fix |
|---|---|---|
| `UnsupportedOperation` on stack create / terraform apply | Wrong instance type for a Marketplace AMI | KVO `c5.2xlarge` only; vPB `t3.xlarge` only; vController `t3.xlarge` or `m5.xlarge` (`--vcontroller-type`) |
| Stack creates but instances fail to launch | Not subscribed to the Marketplace AMIs | Subscribe once per AWS account to Keysight Vision Orchestrator, Keysight CloudLens vController, and Keysight CloudLens Virtual Packet Broker |
| `AccessDenied` on stack create | IAM principal cannot create IAM resources | Deploy with `--capabilities CAPABILITY_NAMED_IAM` (CFN) or `AdministratorAccess` (Terraform) |
| Terraform state lock error | A prior apply was interrupted | `terraform force-unlock <lock-id>` |

Marketplace subscription **cannot** be automated; AWS requires interactive EULA
acceptance on the Marketplace listing pages.

---

## 9. The AMIs and how to look them up in another region

Default us-east-1 AMIs baked into the stack:

| Component | Instance type | us-east-1 AMI |
|---|---|---|
| vController (CLMS) | `t3.xlarge` | `ami-0bebd5e730315337e` |
| KVO (Keysight Vision Orchestrator) | `c5.2xlarge` | `ami-017c0db8981569380` |
| vPB (Virtual Packet Broker) | `t3.xlarge` | `ami-0d00b42a9748d580c` |

The collector Service VM has no fixed id: KVO launches it at run time from the
newest `cloudlens-vpb-svm-*` Marketplace image in the region (same subscription
as the vPB listing), so it is not in the table or the `RegionMap`.

To find the equivalent AMI in another region after you subscribe:

```bash
aws ec2 describe-images --region <region> --owners aws-marketplace \
  --filters "Name=name,Values=*CloudLens*Manager*" \
  --query "reverse(sort_by(Images,&CreationDate))[0].{id:ImageId,name:Name}"
```

The `RegionMap` in `deploy/cloudformation/stack.yaml` already carries
vController, KVO, vPB and the RHEL test AMI for 12 regions, and
`deploy-stack.sh` resolves them from `--region`. Only for a region outside that
list: repeat the lookup with the image names `deploy-stack.sh` resolves
(`KVO_AMI_NAME` and `VPB_AMI_NAME`, matched by exact name against the
Marketplace owner `679593333241` in `resolve_ami_by_name`), add the ids to the
`RegionMap`, and re-sync the templates to S3.

---

## 10. The runbook scripts

| Script | Purpose |
|---|---|
| `quickstart.sh` | Customer-facing one-command sensor deploy. Reads `customer_input.yaml`, runs `deploy.yaml` Ansible playbook. |
| `deploy/deploy-stack.sh` | The whole pipeline in 18 phase steps (12 resumable names: stack wait bootstrap key license adopt sensors eks vpb mirror path prove): stack via CloudFormation (default) or Terraform, vController key, KVO licensing and adoption, sensors, EKS, vPB adoption, mirror, vPB path, traffic proof, HTML report. `--doctor`, `--profile`, `--events`, `--discover`, `--resume`. |
| `deploy/teardown-stack.sh` | Deletes the stack and sweeps what it leaves, in order: rail vPBs, stamped instances, collector ASGs and launch templates, mirror targets and filter, security groups, volumes; offers to release the KVO licences while the KVO is alive. `--orphans`, `--dry-run`, `--sweep-only`, `--yes`, `--release-licences` with `--kvo-admin-user USER`, `--kvo-admin-pass PASS` (prefer the `CLOUDLENS_KVO_ADMIN_PASS` variable: a flag value sits in shell history and in `ps`) and `--kvo-address ADDR` when the stack cannot say where the KVO is. |
| `scripts/kvo_license.py`, `scripts/kvo_adopt_clms.py`, `scripts/vpb_kvo_adopt.py`, `scripts/vpb_wire_path.py`, `scripts/kvo_aws_mirror.py`, `scripts/kvo_k8s_config.py` | The KVO automation the phases call: licensing, vController adoption and Cloud Config, vPB adoption, vPB path and policy, AWS mirror fabric, Kubernetes presence. Each runs standalone. |
| `scripts/deploy-eks-tapping.sh` | The EKS rail: DaemonSet or sidecar sensors, image to ECR, sample cluster. |
| `scripts/bootstrap-vpb.sh` | vPB first-boot bootstrap (run by the stack's user data and by phase 8): kubeconfig and the `sudo vpb` wrapper. |
| `scripts/prove_traffic.sh` | Generates traffic on the tapped workloads and scores the sensor and mirror paths (phase 17, `--only prove`). |
| `scripts/deploy-test-workload-vms.sh` | Three tagged test instances with a custom discovery tag. |
| `scripts/lab/brownfield-fixture.sh` | `create` / `status` / `env` / `destroy`: a customer-shaped existing VPC, two tagged workloads and an EKS cluster for brownfield rehearsals. |
| `deploy/scripts/sync-cfn-templates-to-s3.sh`, `deploy/scripts/check-launch-buttons.sh` | Publish the templates behind the Launch Stack buttons; verify every button is live and in sync (no credentials). |
| `console/` (`python3 -m cloudlens_console`) | The operations console on localhost:8760: doctor, wizard to a plan, live deploy with prompts, read-only stack view, licensing, teardown. |
| `demo/setup-aws-visibility-demo.sh` | Out-of-band visibility demo orchestrator: workload EC2 + vController + KVO + vPB + tool receiver + tags, then runs `quickstart.sh`. |
| `scripts/vcontroller_project_key.py` | Programmatic project + API key retrieval against the vController REST API (used by the demo orchestrator). |
| `deploy/shard.sh` | Shards large fleets into batches for very large sensor rollouts. |

---

## 11. Where to file feedback

- Site issues -> https://github.com/Keysight-Tech/cloudlens-ansible-aws/issues
- vController / vPB / KVO product issues -> Keysight TAC
- This document -> open a PR; the goal is that this file grows with every new
  gotcha discovered in the field.
