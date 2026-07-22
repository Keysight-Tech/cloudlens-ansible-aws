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

## Verify (do not trust "done" - check AWS)

```bash
aws ec2 describe-traffic-mirror-sessions --region <region> \
  --query 'TrafficMirrorSessions[].{Src:NetworkInterfaceId,Tgt:TrafficMirrorTargetId}'
aws ec2 describe-traffic-mirror-targets  --region <region>
```

and in KVO: **Visibility Fabric > Cloud Configs** shows the Aws config, and the
Global Dashboard shows the collectors. A session per tagged source ENI = working.

## How this relates to the sensor path

| | Sensor (agent) | Zone Tapping (agentless) |
|---|---|---|
| Where capture happens | inside the VM | AWS mirrors the ENI |
| Install per VM | yes (docker/podman/Windows svc) | none |
| Needs KVO AWS IAM | no | yes (this doc) |
| Selection | `cloudlens=yes` tag | `cloudlens=yes` tag |

Both register under the same CLM/KVO project, so you can mix them per workload.
