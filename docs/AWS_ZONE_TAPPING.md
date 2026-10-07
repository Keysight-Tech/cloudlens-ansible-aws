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

- From the deploy: `bash deploy/deploy-stack.sh --enable-zone-tapping` (env
  `CLOUDLENS_ENABLE_ZONE_TAPPING=yes`) passes it to whichever engine runs
  (`deploy-stack.sh:4352`, `:4424`). Needs IAM-create rights; default no.
- CloudFormation directly: `EnableZoneTapping=yes` (needs `CAPABILITY_NAMED_IAM`).
- Terraform directly: `enable_zone_tapping = true`.

The instance profile does not replace the access key on the Cloud Config (see
Credentials below); it is supplementary.

Leave it off and the base suite still deploys with **no IAM required** - important
for SEs whose principal cannot create IAM roles.

### Collector security group ports (the documented set)

The collector security group(s) you pass (`--mgmt-sg` / `--ingress-sg` /
`--egress-sg` to the script, `--collector-mgmt-sg` / `--collector-ingress-sg` /
`--collector-egress-sg` to the deploy) must allow, inbound:

- **mgmt: TCP 443 from the VPC CIDR** (the control plane, CLMS and KVO, is
  in-VPC) plus **TCP 22 and 9022 from the VPC CIDR and from the admin CIDR**.
  KVO configures the collector over SSH (the KVO guide lists 22 and 9022
  inbound from KVO/vController on the vHub). With 22/9022 open to the admin
  only, the collector boots, registers its mirror target and cuts zero sessions
  for ever with no alert anywhere (seen live 2026-09-28).
- **ingress: UDP 4789 and GRE (IP protocol 47) from the VPC CIDR** (AWS VPC
  Traffic Mirroring is VXLAN; the mirrored traffic itself).
- **egress: GRE (IP protocol 47) and UDP 4789 from the VPC CIDR**, the tunnel
  return path toward the tool, whichever `--tool-encap` is in use.

`ensure_collector_sgs` in deploy-stack.sh creates exactly this set
(`deploy-stack.sh:5353-5373`); all three supplied means nothing is created and
no `ec2:CreateSecurityGroup` right is needed, and `--no-create-collector-sgs`
makes a missing one an input error.

**B. Brownfield - KVO already running.** Attach the profile in place:

```bash
scripts/kvo_enable_zonetap_iam.sh --instance-name <stack>-kvo --region <region>
# or: --instance-id i-0123... --profile <aws-cli-profile>
```

Idempotent. After it runs, reboot KVO (or restart its services) so the app picks
up the instance-role credentials.

**C. Access keys (required by the Cloud Config in practice).** Create a user
with the same policy, generate keys, and pass them to the deploy with
`--mirror-access-key` / `--mirror-secret-key` (env `CLOUDLENS_MIRROR_ACCESS_KEY`
/ `CLOUDLENS_MIRROR_SECRET_KEY`), or to the mirroring script with
`--aws-access-key` / `--aws-secret-key`. Long-lived keys, but `createAwsPresence`
rejects an empty `accessKeyId`, so A or B alone does not satisfy KVO (see
Credentials below).

## From the deploy

`deploy/deploy-stack.sh` runs this rail as the `mirror` phase (`--only mirror`
or `--from mirror` reruns it). Turn it on with `--with-mirror`, or with
`--tapping mirror` / `--tapping both`, which picks sensors and mirroring from
one answer; it needs KVO (`--with-kvo`). KVO's AWS credentials come from
`--mirror-access-key` / `--mirror-secret-key`. The deploy resolves the
selection, runs `kvo_aws_mirror.py` with
`--selection inventory/generated.workloads.json`, points the tool at the vPB
ingress IP (`--tool-remote-ip`, L2GRE, key `CLOUDLENS_GRE_KEY`, default 64) and
verifies the collector launches.

For an existing VPC name the collector placement: `--collector-zone AZ`,
`--collector-mgmt-subnet`, `--collector-ingress-subnet`,
`--collector-egress-subnet` (three distinct subnets in one AZ), optionally
`--collector-mgmt-sg` / `--collector-ingress-sg` / `--collector-egress-sg` (all
three supplied means none is created), `--no-create-collector-sgs`, and
`--collector-max N` for the fleet ceiling. To tap VPCs other than the CloudLens
one, repeat `--source-vpc-id ID` or
`--source-vpc vpc:az:mgmt:ingress:egress[:sgs]`, or let `--discover` (with
`--discover-regions`, `--discover-accounts organization`) find them from the
discovery tag (`docs/DISCOVERY.md`).

The `prove` phase (`--from prove`) then counts the VPC's mirror sessions,
generates traffic on the tapped workloads and measures what arrives at the
capture host by GRE key (collector key 64, vPB key 200); it changes no
configuration and retries for up to ten minutes while the collector registers.

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

`--source-tag cloudlens=yes` mirrors every ENI on instances carrying that tag.
Prefer `--selection inventory/generated.workloads.json` instead, written by
`scripts/resolve_workloads.py`: it carries the customer's FULL selection from
customer_input.yaml (ANDed tags, explicit instance ids, exclusions), resolved
per VPC, Nitro-classified, and already translated into the KVO selector. That
is what makes the mirror tap exactly the set the sensor path targets; the
single tag is the fallback when no selection can be resolved. deploy-stack.sh
runs the resolver and passes `--selection` on its own; the tag remains for
hand-run one-offs.

Tapping a VPC other than the CloudLens one, or several: give deploy-stack.sh
one `--source-vpc-id` (or `--source-vpc vpc:az:mgmt:ingress:egress`) per VPC,
or list them as `aws.source_vpc_ids` in customer_input.yaml. One fabric is
built per VPC (`aws-mirror-<vpcid>`), because a KVO cloud config is strictly
one VPC and editing one destroys its vHub. Each tapped VPC needs its own
collector placement (three distinct subnets in THAT VPC) and reachability to
the CLMS/KVO on 443 and to the tool from its egress subnet (peering or TGW).
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

The script runs the whole fabric, each step a committed change request and
idempotent on name (`scripts/kvo_aws_mirror.py:663-1070`):

0. Stuck-fabric guard: a config that exists with no collector ASG is incomplete
   and is rebuilt; a presence reused without its key never relaunches the
   collector, so the presence is recreated WITH the key. `--force-rebuild`
   tears the whole fabric down first (needs the access keys).
1. AWS presence.
2. AWS cloud config (what provisions and starts the AWS-side work;
   `availabilityZones` must be set or KVO launches no collector).
3. Cloud collection from `--selection` or `--source-tag`.
4. Remote tool (`--tool-remote-ip`, `--tool-encap`).
5. Monitoring policy collection -> tool.
6. Optional packet-capture receiver (`--tool-receiver-ip`).
7. Verify: wait up to `--verify-timeout` (300 s) for the collector ASG, then up
   to 15 minutes for the collector's mirror target, then count the sessions. No
   ASG is a loud failure (exit 12, with the `--force-rebuild` fix printed). Zero
   sessions after the target exists triggers the automated nudge described in
   the next section.

Before the cloud config is committed (the presence, idempotent on name, is
created first) the preflight validates the collector subnets (exist, in the
tapped VPC, one AZ, pairwise distinct) and reports Nitro eligibility per source,
AZ coverage (interfaces in an AZ with no collector are never tapped) and licence
demand per tapped interface, and derives `--max-size` from the interface count
(10 per Service VM, plus one spare).

## Why sessions can sit at zero, and what the script does about it

Every early stack ended with a complete, correct fabric (presence, Cloud Config,
Cloud Collection, collector running and registered, tool, monitoring policy,
zero open change requests, no alerts) and zero traffic mirror sessions until a
human re-committed the collection. The ordering is the cause: KVO cuts sessions
only when it acts on the collection AFTER the collector has registered its
mirror target, and the collection was committed before that.

Since 2026-09-28 the script does the nudge itself
(`scripts/kvo_aws_mirror.py:1000-1050`). After the collector registers its
target (the first AWS write it makes; allow 10 to 15 minutes, the vpb-svm image
is heavy), if `describe-traffic-mirror-sessions` still shows none for the VPC,
it deletes the monitoring policy, then the collection, then recreates the
collection and the policy, each in its own committed change request that leaves
KVO in a valid state, and waits up to 240 s for the sessions. Expect one session
per tapped source ENI about a minute later.

### If it still shows zero

Rerun the mirror phase (`--from mirror`): it repeats the recreate of the
collection and the policy. The KVO UI route is the last resort: open
Visibility Fabric > Cloud Collections > the collection (default
`aws-mirror-collect`), remove the workload selector and add it back, and commit
that as ONE change request. Do not clear the selector in one commit and restore
it in another: the collection is empty in between, the policy that references
it fails validation with "Monitoring policy ... has an empty cloud collection",
the change request sticks at InProgress and KVO serialises every later commit
behind it. Recovery is discarding that change request. The script avoids this
by recreating the objects rather than editing the selector.

Two other causes of zero sessions produce the same quiet symptom and are checked
by the script first: a cloud config with no availability zones (KVO launches no
collector, so no ASG ever appears), and a presence reused without its key (the
collector never relaunches). Both end in
`--force-rebuild --aws-access-key <AK> --aws-secret-key <SK>`.

The deploy's `prove` phase counts the sessions before measuring. A count of
zero means the recreate in the mirror phase has not taken yet: rerun it with
`--from mirror`, which repeats the recreate, rather than editing the collection
by hand.

## Verify (do not trust "done" - check AWS)

```bash
aws ec2 describe-traffic-mirror-sessions --region <region> \
  --query 'TrafficMirrorSessions[].{Src:NetworkInterfaceId,Tgt:TrafficMirrorTargetId}'
aws ec2 describe-traffic-mirror-targets  --region <region>
```

and in KVO: **Visibility Fabric > Cloud Configs** shows the Aws config, and the
Global Dashboard shows the collectors. A session per tagged source ENI = working.

## If you are sending through the vPB: the egress tool must be REMOTE

A vPB that receives everything and forwards nothing looks like this:

```bash
sudo vpb -c 'show traffic-rule-packet-counters'
TR1 | 9998 | Inspected 1,756,817 | Passed 0 | Denied 1,756,817
```

The cause was the egress tool. An earlier release created it as type LOCAL,
which is the bare port: the vPB put raw frames onto an AWS subnet and AWS
dropped every one not addressed to an ENI, so Inspected climbed into the
millions while Passed stayed at 0. `scripts/vpb_wire_path.py` now creates the
tool as REMOTE with `reachableFrom DEVICE_CONFIG` and binds eth2 with its ip,
netmask and gateway, so the vPB ORIGINATES an L2GRE tunnel out of eth2 to the
tool, which AWS routes normally (`vpb_wire_path.py:366-376`). Proven live:
Passed went 0 to 145,037 and the tool captured the tapped payload with the vPB
egress IP as the outer source. `--wire-vpb-path` in the deploy runs this
script. A pre-existing LOCAL tool is detected and the three-commit conversion
is printed (policy to the capture tool only, delete and recreate the tool as
REMOTE and rebind eth2, add it back to the policy); the script never deletes a
tool a policy still references.

After the fix, under generated load:

```bash
sudo vpb -c 'show traffic-rule-packet-counters'
T0   Inspected 25,059   Passed 12,526   Denied 12,533
T1   Inspected 31,426   Passed 15,709   Denied 15,717

sudo vpb -c 'show interface-pkt-counters'
eth1   RX 33,237 pkts / 36.3 MB    mirrored traffic arriving
eth2   TX 16,542 pkts / 17.4 MB    traffic leaving toward the tool
```

`Passed` climbing and `eth2 TX` climbing together mean traffic is flowing
through the broker end to end. The rule still reads `L2 Filter: VLAN ID: 1`
while passing traffic, so that filter is not itself a fault.

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
| Selection | the one selection in `customer_input.yaml` (ANDed tags, instance ids, exclusions, or an external inventory), default tag `cloudlens=yes` | the same selection, resolved per VPC by `scripts/resolve_workloads.py` and Nitro-filtered; `--source-tag` is the hand-run fallback |

Both register under the same CLM/KVO project, so you can mix them per workload.
