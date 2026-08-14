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

### Collector security group MUST allow KVO on 8443 (easy to miss)

KVO deploys each collector Service VM, then configures and registers it by
calling the collector's **management plane on TCP 8443**. If the collector's
security group does not allow 8443 from KVO, the collector boots healthy but
never registers, KVO never creates the Traffic Mirror **sessions**, and the
Cloud Collection change request hangs `InProgress` with no error. Symptom: ASG +
collector + target + filter all exist, `describe-traffic-mirror-sessions` stays
empty, and CloudTrail shows zero `CreateTrafficMirrorSession` attempts.

The collector security group(s) you pass (`--mgmt-sg` etc.) must allow, inbound:

- **TCP 8443 from KVO** (mgmt/registration) - a self-referencing rule works when
  KVO shares the SG, else allow KVO's SG or private IP.
- **UDP 4789 from the sources** (VXLAN - the mirrored traffic itself).
- plus the usual 22/9022/443.

If you reuse a generic stack SG, add 8443 explicitly:
`aws ec2 authorize-security-group-ingress --group-id <sg> --protocol tcp --port 8443 --source-group <kvo-sg>`

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
  --availability-zones us-east-1a,us-east-1b \
  --accept-eula --insecure
```

`--source-tag cloudlens=yes` mirrors every ENI on instances carrying that tag -
the same convention the sensor path uses, so one tag drives both models. Omit
`--aws-access-key/--aws-secret-key` to use KVO's instance role (option A/B).

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

## If you are sending through the vPB: the same re-commit applies

The vPB path has the same shape of problem, and the same likely remedy.

**Symptom:** the vPB receives the mirrored traffic and forwards none of it.

```bash
sudo vpb -c 'show traffic-rule-packet-counters'
TR1 | 9998 | Inspected 1,756,817 | Passed 0 | Denied 1,756,817
```

**What the device is actually doing** - this is the command that explains it, and
counters alone never will:

```bash
sudo vpb -c 'show traffic-rule-status'
Traffic Rule TR1
  Operation: PASS
  Ingress Interface: eth1
  Filter Configuration:
    L2 Filter:
      VLAN ID: 1          <-- the rule only matches VLAN 1
  Egress Configuration:
    Interface: eth2
```

KVO **does** program a correct PASS rule from ingress to egress. It just filters
on VLAN 1, and mirrored L2GRE traffic from the collector carries no such tag, so
nothing matches.

**The likely cause is ordering, exactly as with the Cloud Collection.** The C2DL,
the tools and the monitoring policy are all created *before* the collection has
live sources and before the collector is registered. KVO computes the device rule
against that incomplete picture. Re-committing the collection is what makes KVO
recompute for the mirror sessions; the same recompute is what the device rule
needs.

**So when you do the manual re-commit above, check the vPB immediately after:**

```bash
sudo vpb -c 'show traffic-rule-status'          # has the VLAN 1 filter changed?
sudo vpb -c 'show traffic-rule-packet-counters' # is Passed above zero?
```

`Passed` climbing is the only acceptable proof. If the filter still reads
VLAN ID 1 after the re-commit, re-create the **C2DL and the monitoring policy**
last, after the collection has live sources, and check again.

**Ruled out already, each by measurement, so do not spend time on them:** the
data ports are up and eth1 holds an IP; the tunnel reaches the device (over a
million packets arrived); the licence is present (`show license-status` reports
`CL.vPB.ADVKVO`, Server State up); the device is in Control Mode with a
DeviceConfig binding; and the KVO alert *"Tunnel of type GRE: Remote destination
not reachable"* is a **false alarm** caused by the security group permitting GRE
but not ICMP, so the reachability probe fails while traffic flows normally.

**The capture path does not depend on any of this.** The collector tunnels
straight to the capture host, which is why packet-level proof is obtainable even
while the vPB forwards nothing.

## How this relates to the sensor path

| | Sensor (agent) | Zone Tapping (agentless) |
|---|---|---|
| Where capture happens | inside the VM | AWS mirrors the ENI |
| Install per VM | yes (docker/podman/Windows svc) | none |
| Needs KVO AWS IAM | no | yes (this doc) |
| Selection | `cloudlens=yes` tag | `cloudlens=yes` tag |

Both register under the same CLM/KVO project, so you can mix them per workload.
