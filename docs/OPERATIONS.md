# CloudLens Operations Guide (AWS)

Single source of truth for every gotcha a customer or SE will hit when working
with **vController** (formerly CLMS), **KVO** (Keysight Vision One), and **vPB**
(Virtual Packet Broker) from the AWS Marketplace, plus the Ansible sensor
deployment on top.

If you hit any wall not described here, please open a PR adding it. The point
of this doc is that nobody should ever burn the same hour twice.

Everything below is written for **us-east-1**. The Marketplace AMIs are
region-locked, so if you deploy elsewhere you first have to look up the
equivalent AMI IDs for that region (see section 9).

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
| EC2 key pair | (none, required) | `CLOUDLENS_KEY_PAIR` | `--key-pair` |
| Admin ingress CIDR | `0.0.0.0/0` | `CLOUDLENS_ADMIN_CIDR` | `--admin-cidr` |
| Deploy KVO | `yes` | `CLOUDLENS_DEPLOY_KVO` | `--with-kvo` / `--no-kvo` |
| Deploy vPB | `yes` | `CLOUDLENS_DEPLOY_VPB` | `--with-vpb` / `--no-vpb` |
| VPC CIDR | `10.99.0.0/16` | `CLOUDLENS_VPC_CIDR` | `--vpc-cidr` |
| Discovery tag key | `cloudlens` | `CLOUDLENS_DISCOVERY_TAG_KEY` | `--discovery-tag-key` |
| Discovery tag value | `yes` | `CLOUDLENS_DISCOVERY_TAG_VALUE` | `--discovery-tag-value` |

For end-to-end verification with a custom discovery tag, run
`demo/setup-aws-visibility-demo.sh` first. It stands up Ubuntu + RHEL + Windows
EC2 instances tagged with your chosen pair, ready for a sensor-install test
pass.

---

## 1. Quick reference: every port and credential you will touch

| Component | Public port(s) | Default UI cred | Default CLI cred | First-boot wait |
|---|---|---|---|---|
| **vController** | TCP 443 (web UI), TCP 22 (Linux SSH) | `admin / Cl0udLens@dm!n` (force-change on first login) | n/a | ~15 min |
| **KVO** | TCP 443 (web UI), TCP 22 (Linux SSH) | See Keysight KVO docs (operator must change immediately) | n/a | ~15 min |
| **vPB** | TCP **9022** (KCOS SSH + CLI), TCP 443 (mgmt web), UDP 4789 (VXLAN), UDP 10800-10801 (Keysight VXLAN) | n/a (managed from KVO once adopted) | `admin / ixia` on port 9022 (force-change on first SSH) | 10 to 15 min |
| **Collector SVM** | n/a (auto-deployed by KVO Zone Tapping) | n/a | n/a | auto |
| **Sensor** | n/a | n/a | n/a | <1 min |

### Why these are not the obvious defaults

- **vPB OS SSH is on port 9022, NOT port 22.** Keysight CloudLens OS (KCOS)
  binds sshd to 9022. A connection to `:22` on the vPB will time out forever
  even after the instance is fully up. The Marketplace AMI plus the CloudFormation
  security group open 9022 automatically; if you build your own security group,
  add TCP/9022 by hand.
- **The vPB CLI is reached over the same 9022 SSH session.** SSH in as
  `admin / ixia`, and you land directly in the CloudLens vPB console. There is
  no separate CLI port on AWS and no localhost second hop.
- **vController and KVO use port 22 normally**, but their web UI gates new
  sessions behind an EULA plus first-login password change. Until you complete
  both in the browser, the REST API returns 405 and the product refuses to be
  adopted. The first manual UI session is unavoidable for these images.
- **Instance types are fixed by the Marketplace AMI.** vController and vPB run
  on `t3.xlarge`; KVO runs on `c5.2xlarge`. Choosing a different size breaks the
  Marketplace EULA binding and the launch fails with `UnsupportedOperation`
  (see section 8).

---

## 2. How to reach each device

AWS uses EC2 key pairs, not passwords, for the Linux OS login. The vController
and KVO UIs still use the shared default web credentials.

### vController (SSH port 22, web 443)

```bash
ssh -i ~/.ssh/<your-key>.pem ec2-user@<vcontroller-eip>
```

If SSH is refused right after launch, the image is still initialising. Open
`https://<vcontroller-eip>` in a browser, accept the EULA, sign in
`admin / Cl0udLens@dm!n`, complete the forced password change, then retry SSH.

### KVO (SSH port 22, web 443)

Same flow as vController. The EULA plus first-login is a one-time gate. KVO
blocks all access, including the REST API, until the EULA is accepted in a
browser.

```bash
ssh -i ~/.ssh/<your-key>.pem ec2-user@<kvo-eip>
```

### vPB (SSH + CLI on port 9022, NOT 22)

```bash
ssh -p 9022 admin@<vpb-eip>
# default password: ixia (forced change on first login)
```

You land straight in the CloudLens vPB console. There is no separate second-hop
SSH on AWS.

#### If `ssh -p 9022` itself times out

1. **Check the security group.** The vPB security group must allow inbound
   TCP/9022 from your admin CIDR. The CloudFormation stack opens 9022, 443,
   4789 and 10800-10801 automatically. For a hand-rolled deployment:

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
| vController OS SSH | `ec2-user` | EC2 key pair (no password) | n/a |
| KVO web UI | `admin` | See Keysight KVO docs | Forced on first login |
| KVO OS SSH | `ec2-user` | EC2 key pair | n/a |
| vPB SSH + CLI (port 9022) | `admin` | `ixia` | Forced on first SSH |
| Workload EC2 (Linux) | `ubuntu` (Ubuntu) / `ec2-user` (RHEL/AL) | EC2 key pair | n/a |
| Workload EC2 (Windows) | `Administrator` | EC2 key-pair-decrypted password | n/a |

The demo orchestrator (`demo/setup-aws-visibility-demo.sh`) reuses one EC2 key
pair everywhere and captures every EIP into a state file, so you only track one
key.

---

## 4. Adopting vPB and vController into KVO

This is the premium "single pane of glass" workflow that turns three separately
launched EC2 instances into one fleet view.

### 4a. Prerequisites

- KVO is launched and you have completed the EULA plus first-login on its web UI.
- A KVO user exists (for example `clms@keysight.com`) with the `KVO User` role
  or higher.
- vController, KVO and vPB share the same VPC (the stack puts all three in the
  `mgmt` subnet `10.99.1.0/24`), so KVO can reach them on private IPs. If you
  split them across VPCs, create VPC peering plus route-table entries first.

### 4b. Point vController at KVO

In the vController web UI:

`Settings > Management Server`

Enter:
- IP: KVO **private** IP (for example `10.99.1.x` from the stack outputs)
- Port: 443
- Credentials: the KVO user you created

Save. The vController device heartbeats to KVO within ~30s and appears in KVO
under `Inventory > Devices` (or `Adoptable`).

### 4c. Add vPB to KVO

**Step 1: SSH into the vPB console** (port **9022**, not 22)

```bash
ssh -p 9022 admin@<vpb-eip>
# password: ixia (set a new one when prompted)
```

You will see the Keysight EULA prompt the first time only. Accept it to reach
the `CloudLensVPB#` prompt.

**Step 2: Tell vPB where KVO lives, plus credentials**

Use the KVO **private** IP. Always set credentials, otherwise vPB shows
`disconnected` even after `enable` because it cannot authenticate to KVO during
the registration handshake.

```text
configure terminal
kvo
ip <kvo-private-ip>
port 443
username clms@keysight.com
password <kvo-user-password>
enable
monitored
end
write memory
```

If your build does not accept `kvo` as a verb, type `?` at the `CloudLensVPB#`
prompt to see what it does accept. Some builds use `management-server` or
`orchestrator`. The submode fields (`ip`, `port`, `username`, `password`,
`enable`, `monitored`) are the same across all of them.

**KVO side check.** Before this works, KVO must have:
- `Live Settings > Remote Access URL` set to `https://<kvo-private-ip>`
- A user (for example `clms@keysight.com`) created under `User Management` with
  the `KVO User` role or higher

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

**Step 5: Activate the license**

KVO UI: `Inventory > Licenses` (or `Live Settings > License Information`),
select the vPB, apply the **vPB** license credit. The `License Manager Error`
warning that appeared on the CLI disappears within ~30 seconds. Apply the
**vController** license to vController separately, not the vPB one.

---

## 5. AWS traffic path: VPC Traffic Mirroring and the Collector SVM

On AWS the packet path is native VPC Traffic Mirroring, orchestrated by KVO
Cloud Config, rather than a purely sensor-to-vPB VXLAN tunnel.

1. **Sensor** on each tagged EC2 instance taps the vNIC (captures both
   east-west and north-south traffic).
2. **KVO Cloud Config** (AWS) commits a mirror policy. KVO auto-deploys one or
   more **Collector SVMs** (`ami-0c22ade3667f8d35a`) as the VPC Traffic Mirror
   targets.
3. **VPC Traffic Mirroring** forwards mirrored copies to the Collector SVMs.
4. **vPB** filters, dedups, decapsulates GRE/VXLAN and forwards cleaned traffic
   out its `tool` interface to your analytics tool.

To create the AWS Cloud Config:

`KVO > Cloud Fabric > Cloud Configs > New > AWS`

Paste an IAM access key/secret scoped to `cloudlens:monitored:vpcid`, pick the
region + VPC + subnets, then commit. When the config shows `COMMITTED`, KVO
begins mirroring instances that match the workload selector.

For a pure out-of-band demo without VPC Traffic Mirroring, you can also point
sensors directly at a VXLAN receiver on UDP/4789 (see the SE demo playbook).

---

## 6. Sensor deployment (Ansible) troubleshooting matrix

| Symptom | Cause | Fix |
|---|---|---|
| `ssh admin@vpb -p 22` times out | KCOS does not expose 22 | Use port 9022 |
| `ssh -p 9022` times out | Security group missing TCP/9022 | Add SG ingress (see section 2) |
| `ssh -p 9022` fails after SG fix | KCOS still initialising | Wait 10 to 15 min after instance `running` |
| Inventory finds 0 instances | Tags missing | `aws ec2 create-tags --resources <id> --tags Key=cloudlens,Value=yes Key=os,Value=ubuntu Key=env,Value=prod` |
| SSH "Permission denied (publickey)" to Linux | Wrong key path or user | Set `aws.ssh_key_path` + correct `ssh_user_*` in `customer_input.yaml` |
| SSM "0 target instances" | Missing IAM role or SSM Agent stopped | Attach `AmazonSSMManagedInstanceCore`; confirm `aws ssm describe-instance-information` lists the instance |
| Windows sensor deploy hangs | SSM Agent stopped on the box | `Restart-Service AmazonSSMAgent` then re-run |
| vController REST API returns 405 | EULA + first-login not done in UI | Complete via browser once |
| KVO `Devices` empty | vPB/vController cannot reach KVO | Verify same VPC or VPC peering + routes |
| Adoption shows `Licensed` but vPB does not forward | License applied to vController, not vPB | Activate the **vPB** license |
| Sensor container starts but not in vController UI | Wrong project key or 443 blocked | Fresh key from `Settings > Projects > API Keys`; open egress to 443 |
| Mirror sessions never appear in EC2 console | Cloud Config not committed or source tags missing | Confirm Cloud Config `COMMITTED`; tag sources per the workload selector |
| "Mirror filter exceeded" | AWS hard limit of 10 mirror sessions per ENI | Scale the Cloud Config; KVO adds collector SVMs |

---

## 7. Security-group rules required per device (for CloudFormation / Terraform users)

| Device | Inbound rules required |
|---|---|
| **vController** | TCP/22 (SSH), TCP/443 (web) from admin CIDR only |
| **KVO** | TCP/22 (SSH), TCP/443 (web) from admin CIDR only |
| **vPB** | TCP/9022 (KCOS SSH + CLI), TCP/443 (mgmt web), UDP/4789 (VXLAN), UDP/10800-10801 (Keysight VXLAN) |
| **Collector SVM** | UDP/4789 from the mirror source subnets (managed by KVO) |
| **Workload EC2** | TCP/22 (Linux), TCP/5985-5986 (Windows WinRM, only if not using SSM), TCP/3389 (Windows RDP) from admin CIDR only |

The bundled `deploy/cloudformation/stack.yaml` opens all of the vController /
KVO / vPB rules for you, scoped to the `AdminIngressCidr` parameter. Narrow that
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
| `UnsupportedOperation` on stack create / terraform apply | Wrong instance type for a Marketplace AMI | KVO must be `c5.2xlarge`; vController and vPB must be `t3.xlarge` |
| Stack creates but instances fail to launch | Not subscribed to the Marketplace AMIs | Subscribe once per AWS account to Keysight Vision One, CloudLens Manager, and CloudLens Virtual Packet Broker |
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
| KVO (Keysight Vision One) | `c5.2xlarge` | `ami-017c0db8981569380` |
| vPB (Virtual Packet Broker) | `t3.xlarge` | `ami-0a561b450552b707d` |
| Collector SVM | (auto by KVO) | `ami-0c22ade3667f8d35a` |

To find the equivalent AMI in another region after you subscribe:

```bash
aws ec2 describe-images --region <region> --owners aws-marketplace \
  --filters "Name=name,Values=*CloudLens*Manager*" \
  --query "reverse(sort_by(Images,&CreationDate))[0].{id:ImageId,name:Name}"
```

Repeat with `*Vision*One*` and `*Virtual*Packet*Broker*`. Update the `RegionMap`
in `deploy/cloudformation/stack.yaml` or the AMI variable in the Terraform
module for that region.

---

## 10. The runbook scripts

| Script | Purpose |
|---|---|
| `quickstart.sh` | Customer-facing one-command sensor deploy. Reads `customer_input.yaml`, runs `deploy.yaml` Ansible playbook. |
| `deploy/deploy-stack.sh` | End-to-end AWS stack deploy: vController + KVO (optional) + vPB (optional) + sensors via CloudFormation. The "Launch Stack" button on the site points at the same template. |
| `demo/setup-aws-visibility-demo.sh` | Out-of-band visibility demo orchestrator: workload EC2 + vController + KVO + vPB + tool receiver + tags, then runs `quickstart.sh`. |
| `scripts/vcontroller_project_key.py` | Programmatic project + API key retrieval against the vController REST API (used by the demo orchestrator). |
| `deploy/shard.sh` | Shards large fleets into batches for very large sensor rollouts. |

---

## 11. Where to file feedback

- Site issues -> https://github.com/Keysight-Tech/cloudlens-ansible-aws/issues
- vController / vPB / KVO product issues -> Keysight TAC
- This document -> open a PR; the goal is that this file grows with every new
  gotcha discovered in the field.
