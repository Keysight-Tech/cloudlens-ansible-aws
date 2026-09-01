# AWS Zone Tapping (agentless VPC Traffic Mirroring)

The agentless alternative to installing sensors in each VM. KVO deploys collector
Service VMs and drives **AWS VPC Traffic Mirroring** so AWS copies traffic from
every selected source ENI to the collectors. No agent runs inside the workloads.

Use it when you cannot (or do not want to) put a sensor in the guest: appliances,
locked-down images, third-party VMs, or anywhere the customer prefers cloud-native
mirroring over an in-guest agent.

## Prerequisite: source instances must be Nitro (instance type matters)

AWS VPC Traffic Mirroring only supports **Nitro-based** instances as mirror
sources - CloudLens inherits this AWS limitation (CloudLens AWS deployment guide
913-3373-01: *"only certain instance types can be tapped, which are inherited in
the solution that CloudLens provides"*). Non-Nitro instances (older t2, m4, c4,
etc.) cannot be tapped agentlessly - put a **sensor** on those instead; both land
under the same CLM/KVO project.

- Nitro families: t3/t3a, m5/m6, c5/c6, r5/r6, and newer. Check with:
  `aws ec2 describe-instance-types --instance-types <type> --query 'InstanceTypes[].Hypervisor'`
  (`nitro` = mirrorable, `xen` = not).
- The **collector** (`--collector-type`) should also be a Nitro type sized for the
  mirrored throughput (t3.xlarge in the reference deployment).

## What KVO creates in your AWS account

When the automation commits, KVO calls the AWS API and creates real resources
(visible under **VPC > Traffic Mirroring**):

- **Traffic Mirror Target** - the collector Service VM ENI
- **Traffic Mirror Filter** (+ rules)
- **Traffic Mirror Session** - one per selected source ENI

Everything KVO creates is tagged `cloudlens:monitored:vpcid`, and the IAM policy
denies any create that is not so tagged.

## Prerequisite: give KVO AWS permissions (one-time)

Mirroring is KVO calling AWS on your behalf, so KVO needs credentials. The
least-privilege policy is `deploy/iam/cloudlens-zonetap-policy.json` (verbatim
from the KVO 3.0.1 User Guide, Ch.10). Pick ONE:

**A. Greenfield - bake it into the stack (recommended).** The KVO instance
launches with the profile already attached, so every deployment is ready.

- CloudFormation: deploy with `EnableZoneTapping=yes` (needs `CAPABILITY_NAMED_IAM`).
- Terraform: set `enable_zone_tapping = "true"`.

Leave it off and the base suite still deploys with **no IAM required** - important
for SEs whose principal cannot create IAM roles.

### Collector security group ports (the documented set)

The collector security group(s) you pass (`--mgmt-sg` etc.) must allow, inbound:

- **mgmt: TCP 443 from the control plane** (CLMS/KVO drive the collector; KVO
  UG, AWS Cloud Configs) plus **TCP 22 and 9022 from the admin** for
  troubleshooting SSH.
- **ingress: UDP 4789 from the sources** (AWS VPC Traffic Mirroring is VXLAN;
  the mirrored traffic itself).
- **egress: GRE (IP protocol 47) or UDP 4789**, matching `--tool-encap`, for
  the tunnel toward the tool.

`ensure_collector_sgs` in deploy-stack.sh creates exactly this set. Field note:
TCP 8443 was once suspected in a zero-sessions case, but no Keysight document
requires it for the collector (a KB scan of all 108 indexed documents finds
8443 only in the physical Vision E10S/E40 guides), and both proven causes of
that symptom were elsewhere: an incomplete cloud config (no availabilityZones),
and a presence reused without its key so the collector never relaunched. Chase
those first, not a firewall.

**B. Brownfield - KVO already running.** Attach the profile in place:

```bash
scripts/kvo_enable_zonetap_iam.sh --instance-name <stack>-kvo --region <region>
# or: --instance-id i-0123... --profile <aws-cli-profile>
```

Idempotent. After it runs, reboot KVO (or restart its services) so the app picks
up the instance-role credentials.

**C. No IAM at all - access keys.** Create a user with the same policy, generate
keys, and pass them to the mirroring script with `--aws-access-key/--aws-secret-key`.
Simpler, but long-lived keys; A or B is preferred.

## Configure the mirroring (any SE runs this)

After the CLM is adopted into KVO and CONNECTED (see `kvo_adopt_clms.py`):

```bash
scripts/kvo_aws_mirror.py \
  --kvo <kvo-ip> --clm-name <name-in-kvo> \
  --region <region> --vpc-id <source-vpc> \
  --source-tag cloudlens=yes \
  --ssh-key <keypair> --cloudlens-ip <clms-ip> \
  --mgmt-sg <sg> --ingress-sg <sg> --egress-sg <sg> \
  --zone us-east-1a --mgmt-subnet <id> --ingress-subnet <id> --egress-subnet <id> \
  --tool-remote-ip <tool-ip> \
  --accept-eula --insecure
# multi-AZ: replace the --zone/--*-subnet quartet with one --zone-spec per AZ:
#   --zone-spec us-east-1a:subnet-m1:subnet-i1:subnet-e1 \
#   --zone-spec us-east-1b:subnet-m2:subnet-i2:subnet-e2
```

`--source-tag cloudlens=yes` mirrors every ENI on instances carrying that tag -
the same convention the sensor path uses, so one tag drives both models.
The three SG flags and `--tool-remote-ip` are required in practice: the SGs are
non-null in the KVO schema, and without a tool and monitoring policy KVO never
creates sessions (KVO UG: the tapping infrastructure is created "only after a
monitoring policy is created").

Credentials: pass `--aws-access-key/--aws-secret-key` for an IAM user carrying
the zone-tapping policy. That is the model Keysight documents (the KVO Cloud
Config dialog has exactly two credential fields, Access Key ID and Secret
Access Key), and the live failure `createAwsPresence: accessKeyId cannot be
empty` is server-side input validation, so KVO's instance profile (options A/B)
does NOT satisfy the Cloud Config on its own. Until a live run proves
otherwise, treat A/B as supplementary and the access key as required.

The script runs the three-object KVO chain (AWS presence -> Aws Cloud Config ->
Cloud Collection), each in a committed change request.

## REQUIRED MANUAL STEP: re-create the Cloud Collection

**The automation builds the whole fabric, but KVO does not cut the mirror
sessions until the Cloud Collection is committed again. This step is yours, and
without it you will sit at zero sessions with nothing reporting an error.**

Confirmed on four separate stacks. Every one of them ended with a complete,
correct fabric - AWS presence, Cloud Config, Cloud Collection, collector running
and registered, tool, monitoring policy, zero open change requests, no alerts -
and **zero traffic mirror sessions**, until a human re-committed the collection.

### Do it in this order

1. **Wait for the collector to register its mirror target.** Before that exists
   there is nothing for a session to attach to, and re-committing early achieves
   nothing:

   ```bash
   aws ec2 describe-traffic-mirror-targets --region <region>
   ```

   The collector boots from a heavy image; allow 10 to 15 minutes.

2. **In KVO:** Cloud Fabric > Cloud Collections > select the collection (default
   `aws-mirror-collect`) > re-create or re-edit it > **commit that ONE change
   request**.

3. **Sessions appear about a minute later:**

   ```bash
   aws ec2 describe-traffic-mirror-sessions --region <region>
   ```

   Expect one session per tagged source ENI.

### Do it as a SINGLE change request

Clearing the workload selector in one commit and restoring it in another leaves
the collection empty in between. The monitoring policy that references it then
fails validation with *"Monitoring policy ... has an empty cloud collection"*,
the change request sticks at **InProgress**, and KVO serialises every later
commit behind it. That wedges the deployment and needs the change request
discarded to recover. Make the edit and commit once.

### Why this is not automated

It was, briefly, and it was reverted. Automating the clear-then-restore produced
exactly the wedge described above on a live deployment. Doing it safely requires
one change request carrying both edits, which the API path did not reliably
produce. Until that is solved, this stays a deliberate manual step rather than an
automation that can break a working stack.

## Verify (do not trust "done" - check AWS)

```bash
aws ec2 describe-traffic-mirror-sessions --region <region> \
  --query 'TrafficMirrorSessions[].{Src:NetworkInterfaceId,Tgt:TrafficMirrorTargetId}'
aws ec2 describe-traffic-mirror-targets  --region <region>
```

and in KVO: **Visibility Fabric > Cloud Configs** shows the Aws config, and the
Global Dashboard shows the collectors. A session per tagged source ENI = working.

## If you are sending through the vPB: the same re-commit fixes it

**Confirmed on a live stack: re-committing the Cloud Collection also makes the
vPB forward traffic.** One action fixes both symptoms. You do not need to
re-create the C2DL, rebuild the monitoring policy, or configure anything on the
vPB by hand.

Before the re-commit the vPB receives everything and forwards nothing:

```bash
sudo vpb -c 'show traffic-rule-packet-counters'
TR1 | 9998 | Inspected 1,756,817 | Passed 0 | Denied 1,756,817
```

After it, measured under generated load:

```bash
sudo vpb -c 'show traffic-rule-packet-counters'
T0   Inspected 25,059   Passed 12,526   Denied 12,533
T1   Inspected 31,426   Passed 15,709   Denied 15,717

sudo vpb -c 'show interface-pkt-counters'
eth1   RX 33,237 pkts / 36.3 MB    mirrored traffic arriving
eth2   TX 16,542 pkts / 17.4 MB    traffic leaving toward the tool
```

`Passed` climbing and `eth2 TX` climbing together mean traffic is flowing through
the broker end to end.

**Why it works:** ordering. The C2DL, the tools and the monitoring policy are all
created before the Cloud Collection has live sources and before the collector has
registered, so KVO computes the device rule against an incomplete picture.
Re-committing the collection makes KVO recompute everything downstream, the
device rule included.

**Two things worth knowing:**

The rule still reads `L2 Filter: VLAN ID: 1` while passing traffic, so that
filter is not itself a fault, and roughly half the packets are still denied in a
near-exact 50/50 split. That is worth understanding but it does not block
anything: traffic flows.

The KVO alert *"Tunnel of type GRE: Remote destination not reachable"* is a
**false alarm**. The security group permits GRE but not ICMP, so KVO's
reachability probe fails while the data path carries traffic normally. Check the
counters, never the alert.

**Diagnosing a vPB that is not forwarding, in order:**

```bash
sudo vpb -c 'show traffic-rule-packet-counters'   # Inspected vs Passed vs Denied
sudo vpb -c 'show traffic-rule-status'            # WHY: the filter and egress
sudo vpb -c 'show interface-pkt-counters'         # is eth2 transmitting
sudo vpb -c 'show license-status'                 # expect CL.vPB.ADVKVO, Server State up
```

Counters tell you traffic is denied. Only `show traffic-rule-status` tells you why.

## How this relates to the sensor path

| | Sensor (agent) | Zone Tapping (agentless) |
|---|---|---|
| Where capture happens | inside the VM | AWS mirrors the ENI |
| Install per VM | yes (docker/podman/Windows svc) | none |
| Needs KVO AWS IAM | no | yes (this doc) |
| Selection | `cloudlens=yes` tag | `cloudlens=yes` tag |

Both register under the same CLM/KVO project, so you can mix them per workload.
