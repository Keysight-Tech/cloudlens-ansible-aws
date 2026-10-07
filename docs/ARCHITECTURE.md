# CloudLens Ansible Architecture

🌐 **Live diagrams:** https://keysight-tech.github.io/cloudlens-ansible-aws/#architecture

## The 3-phase approach

CloudLens Ansible deploys CloudLens on AWS in three phases. `deploy/deploy-stack.sh` runs all three in one pass (18 phase steps); the Launch Stack button and the Terraform module run Phase 1 only and stop.

| Phase | Tool | What it does | Time |
|---|---|---|---|
| **Phase 1: infrastructure** | CloudFormation (`deploy/cloudformation/stack.yaml`) **or** Terraform (`deploy/terraform/stack`) | VPC with three subnets, internet gateway, security group, vController, KVO (optional), vPB with three network interfaces (optional), Elastic IPs, the KVO Zone Tapping IAM role (optional) and optional test workloads: up to 34 AWS resources | 5 min to create, then the wait phase while the vController initializes |
| **Phase 2: product configuration** | `deploy-stack.sh`: the bootstrap, key, license, adopt, vpb, mirror and path phases | Bootstraps the vPB, mints the project key and sets the vController login, licenses KVO, adopts the vController into KVO and creates the Cloud Config, adopts the vPB, builds the mirror fabric, wires the vPB traffic path | 15 min |
| **Phase 3: sensors** | Ansible over SSH, SSM Session Manager or WinRM: the sensors and eks phases | Pushes the CloudLens sensor to every tagged EC2 instance (Docker on Ubuntu, Podman on RHEL, MSI on Windows) and, with `--eks-cluster`, a DaemonSet or sidecar to the cluster | 5 to 10 min for up to 50 instances, 4+ hours above 10,000; the bands are in [SCALING.md](SCALING.md) |

Phase 2 is manual only after a Launch Stack or Terraform deploy, and even then re-running `deploy-stack.sh` with the same `--stack-name` and `--region` finds the stack and finishes it. The by-hand clicks are in [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md). In the script's run order the sensor phases come between adopt and vpb; the three-phase view groups the work by kind, not by order. Large fleets are sharded with `deploy/shard.sh` plus the Ansible `forks` setting (see [SCALING.md](SCALING.md)).

### Script phases and resume names

The script has 12 resumable phases, used with `--from NAME` and `--only NAME`. Names, not numbers: the numbers shift when phases are added.

| Name | Script banner | What it does |
|---|---|---|
| `stack` | 6 | Creates the CloudFormation stack (or runs Terraform) |
| `wait` | 7 | Waits for the vController to initialize |
| `bootstrap` | 8 | vPB post-deploy bootstrap over SSH on port 9022 |
| `key` | 9 | Mints the project key and completes the vController first-login password change |
| `license` | 11 | Activates the KVO product licences |
| `adopt` | 12 | Creates a KVO user on the vController, adopts the vController into KVO with that user (never the admin factory login), then creates the Cloud Config |
| `sensors` | 13 | Runs the Ansible sensor deployment |
| `eks` | 13b | EKS pod tapping (DaemonSet or sidecar) |
| `vpb` | 14 | Adopts the vPB into KVO; KVO applies the vPB licence on adoption. By hand the vPB CLI needs `license server <kvo-private-ip> type advanced` before `kvo ip <kvo-private-ip>` and `kvo enable` |
| `mirror` | 15 | Builds the AWS mirror fabric: presence, cloud collection, monitoring policy, collector SVMs, mirror sessions |
| `path` | 16 and 16b | Wires the vPB traffic path and monitoring policy, then one path per extra rail vPB (`--vpb-rails`) |
| `prove` | 17 | Proves the traffic path end to end |

Banners 1 to 5 (environment, pre-flight, customer input, Marketplace subscriptions, engine choice), 10 (sensor mode) and 18 (final summary) have no resume name.

## Products deployed

| Product | Role | Instance type | us-east-1 AMI |
|---|---|---|---|
| **vController** (CloudLens Manager, CLMS) | Sensor management and registration; every sensor registers to it on first start | `t3.xlarge` (default) or `m5.xlarge` | `ami-0bebd5e730315337e` |
| **KVO** (Keysight Vision Orchestrator) | Orchestrator: licensing, Cloud Config, vPB control, AWS Zone Tapping | `c5.2xlarge` only | `ami-017c0db8981569380` |
| **vPB** (Virtual Packet Broker) | Filter, dedup, load balance; three network interfaces (mgmt, ingress, egress). CLI login is the EC2 key pair on port 9022, then `sudo vpb`; there is no password | `t3.xlarge` only | `ami-0d00b42a9748d580c` |
| **Collector SVM** (Service VM) | AWS VPC Traffic Mirror target; KVO launches it into an Auto Scaling Group during the mirror phase | `t3.xlarge`: the default of `--collector-type` on `scripts/kvo_aws_mirror.py`, not a `deploy-stack.sh` flag | Resolved per region by name at run time (`cloudlens-vpb-svm-*`, newest), never a fixed id |

The AMI ids are the us-east-1 entries of the template's `RegionMap`; `stack.yaml` carries 12 regions and `--region` picks the right set. All three Marketplace listings need an active subscription in the account before launch.

## Network topology

```
┌──────────────────────────────────────────────────────────────┐
│ VPC 10.99.0.0/16                                             │
│                                                              │
│  ┌──────────────────────┐                                    │
│  │ Mgmt 10.99.1.0/24    │ ← vController, KVO, vPB eth0 (mgmt)│
│  │   SSH, HTTPS, API    │                                    │
│  └──────────────────────┘                                    │
│                                                              │
│  ┌──────────────────────┐                                    │
│  │ Data 10.99.11.0/24   │ ← vPB eth1 (ingress)               │
│  │   Sensor traffic     │   GRE / VXLAN from sensors,        │
│  │   (mirrored copies)  │   mirror traffic from collectors   │
│  └──────────────────────┘                                    │
│                                                              │
│  ┌──────────────────────┐                                    │
│  │ Tool 10.99.12.0/24   │ ← vPB eth2 (egress)                │
│  │   To analytics tools │   Filtered traffic                 │
│  └──────────────────────┘                                    │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

The CIDRs are the template parameters `VpcCidr`, `MgmtSubnetCidr`, `DataSubnetCidr` and `ToolSubnetCidr` (the Launch Stack form; `vpc_cidr` in the Terraform stack module), used only when no existing VPC is given. `customer_input.yaml` is the sensor run's input and sets no CIDRs. With `--existing-vpc-id` the script deploys into your own VPC, subnets and (optionally) security group instead, CloudFormation engine only, and fills `ExistingVpcCidr` from the VPC so the in-VPC ports open to your range.

### Ports

The stack security group (created unless `--existing-sg-id` supplies one):

| Source | Ports |
|---|---|
| Admin CIDR (`AdminIngressCidr`, default 0.0.0.0/0; the interactive run offers your public /32) | TCP 22 (SSH), TCP 9022 (vPB SSH), TCP 443 (HTTPS), UDP 4789 and UDP 10800-10801 (VXLAN), GRE (IP protocol 47) |
| The VPC (`VpcCidr`, or `ExistingVpcCidr` in an existing VPC) | TCP 443, TCP 7443, GRE (IP protocol 47), UDP 4789, UDP 10800-10801 |
| The group itself | All traffic between stack members |

The collector security groups (three, created by the mirror phase unless `--collector-mgmt-sg`, `--collector-ingress-sg` and `--collector-egress-sg` supply them):

| Group | Ports |
|---|---|
| mgmt | TCP 443 from the VPC; TCP 22 and TCP 9022 from the VPC and from the admin CIDR (KVO configures the collector over SSH) |
| ingress and egress | UDP 4789 and GRE (IP protocol 47) from the VPC |

## Data flow: east-west AND north-south

Two paths carry packets to the vPB. `--tapping sensors`, `mirror` or `both` picks them.

**Sensor path** (any instance, Nitro or not):

```
┌──────────────────────────────┐
│  Target EC2 + CloudLens      │  sensor pushed by Ansible over
│  sensor (taps at the vNIC)   │  SSH, SSM Session Manager or WinRM
└──────────────┬───────────────┘
               │ registers to the vController, then sends mirrored
               │ copies over GRE / VXLAN (UDP 4789, 10800-10801)
               ▼
┌──────────────────────────────┐
│  vPB eth1 (ingress)          │  ← Cloud to Device Link from the
│  filter, dedup, policy       │     Cloud Config (the path phase)
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│  vPB eth2 (egress) → REMOTE  │
│  tool (SIEM, DPI, ...)       │
└──────────────────────────────┘
```

**Mirror path** (agentless, Nitro instances only):

```
┌──────────────────────────────┐
│  Nitro EC2 instance          │  no sensor installed
└──────────────┬───────────────┘
               │ AWS VPC Traffic Mirror session, cut by KVO from
               │ the Cloud Config's AWS presence (the mirror phase)
               ▼
┌──────────────────────────────┐
│  Collector SVM               │  ← launched by KVO into an
│  (Service VM)                │     Auto Scaling Group
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│  vPB eth1 (ingress) → filter,│
│  dedup → vPB eth2 → tool     │
└──────────────────────────────┘
```

**Both east-west (VM to VM inside the VPC) and north-south (VM to internet) traffic is captured on either path**: the sensor taps at the vNIC, and a mirror session copies everything the ENI sees. VPC Traffic Mirroring silently skips non-Nitro instances, so those are the sensor path's job.

The mirror phase creates the AWS presence, the cloud collection and the monitoring policy, waits for the collector to register and for sessions to be cut, and recreates the collection and policy itself when no sessions appear; there is no manual second commit. The phase runs `scripts/kvo_aws_mirror.py`; the collector instance type is that script's `--collector-type` option (default `t3.xlarge`) and `deploy-stack.sh` has no flag for it, so a deploy run always launches `t3.xlarge` collectors and a different type means running the script directly. The deploy's `--collector-zone`, `--collector-*-subnet`, `--collector-*-sg` and `--collector-max` flags set where the collectors go and the fleet ceiling, not their size. A collector reads its config once at boot, so the path phase relaunches it through the Auto Scaling Group once the fabric is complete. `--vpb-rails` launches one extra vPB per tapped area outside CloudFormation and phase 16b wires a path to each.

## Two deployment engines

```
┌─────────────────────────┐         ┌─────────────────────────┐
│  CloudFormation         │         │  Terraform              │
│                         │         │                         │
│  • AWS-native           │         │  • stack module wraps   │
│  • Launch Stack button, │         │    the clms, kvo and    │
│    no tools to install  │         │    vpb child modules    │
│  • New or existing VPC  │         │  • New VPC only         │
│  • Customer self-serve  │         │  • SE / DevOps preferred│
└────────────┬────────────┘         └────────────┬────────────┘
             └──────────────┬──────────────────┘
                            ▼
                ┌───────────────────────┐
                │  Same Marketplace     │
                │  AMIs, same network   │
                │  and appliances       │
                └───────────┬───────────┘
                            ▼
                ┌───────────────────────┐
                │  deploy-stack.sh      │
                │  continues: key,      │
                │  licence, adoption,   │
                │  sensors, vPB path,   │
                │  mirror, proof        │
                └───────────────────────┘
```

Choose by your team's existing tooling. `deploy-stack.sh` drives either engine (`--iac cfn`, the default, or `--iac terraform`) and is the only path that carries on past the infrastructure; Terraform refuses the `--existing-*` flags, so an existing VPC is CloudFormation only. On the Launch Stack form the key pair defaults to an existing one chosen from the dropdown; "Create a new key pair for me" is the alternative, and the dropdown still needs a value either way.

## Discovery

Sensor targets are found by tag: `cloudlens=yes` by default (`DiscoveryTagKey` and `DiscoveryTagValue` on the template, `--discovery-tag-key` and `--discovery-tag-value` on the script). No OS tag is needed: untagged hosts are classified from AWS platform details and, for Linux, a probe; `os=ubuntu|rhel|windows` is optional. `--discover` scans regions (and, with `--discover-accounts organization`, every account the role reaches) for the tag, proposes the collector placement per VPC and writes a replayable profile; it is read-only. A VPC or account added later is one re-run of the deploy (for example the sensors phase, `--from sensors`, or the mirror phase, `--from mirror`), not a support call. Details: [DISCOVERY.md](DISCOVERY.md).

## What lives outside the stack

CloudFormation owns the resources in Phase 1. The deploy also creates, outside the stack: the extra rail vPBs and their Elastic IPs (`--vpb-rails`, tagged `cloudlens:stack=<stack>` and `cloudlens:vpb-rail=<area>`), the three collector security groups, the collector Auto Scaling Group and launch templates KVO creates, and the mirror targets, filters and sessions.

`deploy/teardown-stack.sh` terminates the rail vPBs and releases their Elastic IPs before the stack delete (they hold the stack security group open), deletes the stack, then sweeps what the delete leaves behind: the volumes (the Marketplace AMIs keep their root volumes on termination), the non-stack security groups, the collector Auto Scaling Group, the mirror targets and the deploy-stamped resources inside a customer VPC. In a pre-existing VPC the VPC itself and anything the deploy did not stamp are left alone. Release the KVO's licences before the delete; the teardown asks, or takes `--release-licences`.

## Customer case study

**AAA Financial Services:**
- 847 VMware VMs migrated to AWS EC2
- Existing Ansible Tower for orchestration
- Sensor rollout to 847 VMs in 45 minutes (the `sensors` step at 400 forks; stack creation and the earlier phases excluded)
- 100% sensor registration rate via Ansible (SSH/SSM/WinRM)
- Zero SSH/WinRM access required by SEs

The proof points, with the OS mix, are in [SCALING.md](SCALING.md#real-world-deployment-proof-points).

---

*Architecture reference for CloudLens Ansible v2.0 (March 2026). For the step-by-step procedure see the [Stack Deployment Runbook](CloudLens_Stack_Deployment_Runbook.docx) and [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md); for failures see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).*
