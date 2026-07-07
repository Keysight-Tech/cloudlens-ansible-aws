# Keysight CloudLens AWS Visibility : SE Demo Playbook

A reusable 30-minute demonstration for any enterprise prospect evaluating AWS
network visibility. Built and stress-tested against a real AWS account.
Customize the talking points per industry; the technical flow stays the same.

This guide is for **field SEs** running customer or prospect demos. Pair it with
`docs/OPERATIONS.md` for the underlying technical reference.

---

## When to run this

Pull this kit out for any prospect who fits one of these patterns:

- "We need packet-level visibility into AWS EC2 workloads but VPC Traffic
  Mirroring alone does not give us the filtering and dedup we need."
- "We want to feed NDR (Vectra, ExtraHop, Corelight, Darktrace) from AWS
  workloads without inserting an inline appliance."
- "Our security team needs traffic mirroring but Cloud Ops will not approve
  anything that touches the production data path."
- "We are running a hybrid Linux + Windows estate on AWS and need a single
  sensor strategy, and we already run Ansible."

If you hear any of those, this kit gives them a live answer in 30 min.

---

## The story you are telling

In one sentence: **"Drop a CloudLens sensor on every EC2 instance, register it
to vController in 90 seconds, and let KVO orchestrate VPC Traffic Mirroring into
a vPB that filters and dedups before your tool ever sees a packet. No inline
placement, works across east-west and north-south."**

Three things the prospect will care about:

| Concern | The CloudLens answer |
|---|---|
| Inline risk | Sensors are out-of-band by design; production never depends on Keysight |
| AWS-native | KVO drives VPC Traffic Mirroring; no third-party data plane in the path |
| Scale | One Ansible playbook covers 1 sensor or 5,000 sensors |
| Mixed OS estate | Same sensor on Ubuntu, RHEL, Windows. One inventory, one workflow |
| Tool agnostic | vPB forwards to any tool that speaks VXLAN or GRE (Vectra, Corelight, ExtraHop, custom) |

---

## The 30-minute demo structure

| Minute | What you do | What the prospect sees |
|---|---|---|
| 0-3 | Frame the problem, share the diagram | Architecture diagram, the three "no's" |
| 3-8 | Show the public site, click Launch Stack | CloudFormation quick-create, vController + KVO + vPB |
| 8-15 | Show your live demo environment, walk the sensor inventory | Sensors registered (mixed Ubuntu + Windows), green status |
| 15-22 | Click through KVO: commit an AWS Cloud Config, show the vPB config | Live mirror policy, collector SVM auto-deploy |
| 22-27 | Switch to the tool side, tcpdump streams real mirrored packets | Live capture, inner L2 frames decoded |
| 27-30 | Close with the upgrade path | KVO single-pane, eBPF kTLS for payload, K8s DaemonSet for EKS |

The whole thing runs against a persistent demo stack in your AWS account
(us-east-1). Nothing needs to be torn down between demos.

---

## What is already deployed in the lab (reuse for every demo)

All resources live in one CloudFormation stack (`cloudlens-demo`) in the demo
VPC `10.99.0.0/16`, us-east-1. Record your real EIPs and private IPs in the
state file that `demo/setup-aws-visibility-demo.sh` writes; the table below is
the shape you fill in.

| Component | Public IP (EIP) | Private IP | Notes |
|---|---|---|---|
| KVO | `<eip>` | `10.99.1.x` | EULA accepted, admin user set |
| vController | `<eip>` | `10.99.1.x` | Project `cloudlens-demo` active |
| vPB | `<eip>` | `10.99.1.x` (mgmt) | SSH + CLI on port 9022, adopted into KVO |
| Tool receiver | `<eip>` | `10.99.12.x` | nginx + tcpdump ready for UDP/4789 |
| app01-ubuntu | `<eip>` | `10.99.1.x` | Sensor running, registered |
| app02-ubuntu | `<eip>` | `10.99.1.x` | Sensor running, registered |
| win01 | `<eip>` | `10.99.1.x` | Sensor running, registered |
| win02 | `<eip>` | `10.99.1.x` | Sensor running, registered |

The shared EC2 key pair used by the orchestrator is the single credential you
carry to a demo.

---

## Pre-demo checklist (do 30 minutes before the call)

1. **Confirm AWS auth is fresh**

   ```bash
   aws sts get-caller-identity --query "{account:Account,arn:Arn}" --output table
   ```

   If the session is expired, run `aws sso login --profile <your-profile>` and
   re-auth.

2. **Confirm the sensors are still healthy** (example against an Ubuntu box)

   ```bash
   ssh -i ~/.ssh/<demo-key>.pem ubuntu@<app01-eip> \
     'sudo docker ps --filter name=cloudlens-agent --format "{{.Names}}: {{.Status}}"'
   ```

   Expect `cloudlens-agent: Up N hours`.

3. **Open these tabs in your browser**

   - `https://<vcontroller-eip>` (vController)
   - `https://<kvo-eip>` (KVO)
   - `https://keysight-tech.github.io/cloudlens-ansible-aws/` (public site for
     the Launch Stack click-through)
   - `https://github.com/Keysight-Tech/cloudlens-ansible-aws/blob/main/docs/OPERATIONS.md`
     (operator gotchas)

4. **Pre-warm vController and KVO**: log in once so the EULA plus password
   change is out of the way for the live call. Open the Sensors view so the
   green agents are visible the moment you screen-share.

5. **Start tcpdump on the tool receiver** in a background terminal so packets
   are pre-buffered:

   ```bash
   ssh -i ~/.ssh/<demo-key>.pem ec2-user@<tool-eip> \
     'sudo nohup timeout 1800 tcpdump -i any -nn -w /tmp/vxlan-capture.pcap udp port 4789 > /tmp/tcpdump.log 2>&1 &'
   ```

---

## The demo, beat by beat

### Beat 1 (0:00 to 0:03) : Frame the problem

Open the architecture diagram from the public site (or
`docs/assets/architecture-diagram.svg`). The architecture has three layers:

- Sensors on every EC2 instance (Linux + Windows)
- KVO-orchestrated VPC Traffic Mirroring into Collector SVMs
- vPB filtering / dedup before the tool, out-of-band from production

Say: **"This is the architecture. No inline anything. AWS-native mirroring
driven by KVO. Let me show you the live build."**

### Beat 2 (0:03 to 0:08) : The customer-facing deploy path

Open https://keysight-tech.github.io/cloudlens-ansible-aws/. Scroll to the
Launch Stack button. Hover over it. Say: **"This is what your team clicks. One
CloudFormation quick-create deploys vController, KVO and vPB from the Marketplace
AMIs. The template is open source, go pick at it."**

If they care about IaC: also click the Terraform disclosure and show
`deploy/terraform/stack/`.

### Beat 3 (0:08 to 0:15) : The live lab

Switch to vController (`https://<vcontroller-eip>`). Show the `cloudlens-demo`
project. Click Sensors. Green sensors:

| Hostname | OS | Status |
|---|---|---|
| app01-ubuntu | Linux | Connected |
| app02-ubuntu | Linux | Connected |
| win01 | Windows | Connected |
| win02 | Windows | Connected |

Say: **"These agents were deployed in minutes by a single Ansible playbook.
Same playbook scales to 5,000 instances."**

If they ask "how does Ansible know which instances are which OS?" open the
inventory YAML and show the tag-based discovery:

```bash
sed -n '1,25p' inventory/aws_ec2.yaml
```

Point out the `cloudlens=yes` tag filter and the OS-based grouping.

### Beat 4 (0:15 to 0:22) : Commit a Cloud Config and show the vPB

In KVO, navigate to `Cloud Fabric > Cloud Configs > New > AWS`. Show the shape:
- IAM keys scoped to `cloudlens:monitored:vpcid`
- Region `us-east-1`, the demo VPC, and the mgmt/data subnets
- Commit

Say: **"This is the only screen the security team touches. Point KVO at the VPC,
commit, and KVO stands up the collector SVMs and the mirror sessions for you."**

Then SSH into the vPB console to show the data-plane config is real:

```bash
ssh -p 9022 admin@<vpb-eip>
CloudLensVPB# show kvo
CloudLensVPB# show statistics
```

### Beat 5 (0:22 to 0:27) : The packets arrive

Switch to a terminal that is SSHed to the tool receiver. Run:

```bash
sudo wc -c /tmp/vxlan-capture.pcap        # file growing
sudo tcpdump -nn -r /tmp/vxlan-capture.pcap -c 5
```

Expected output: VXLAN UDP/4789 packets with an inner IP layer. Decode shows the
original L2 frames carrying the workload traffic.

Say: **"Real packets, real-time. No inline appliance. The workload instances do
not know they are being mirrored."**

### Beat 6 (0:27 to 0:30) : Close + roadmap

Three honest upsell cards:

1. **KVO** for single pane of glass across many vPBs and many vControllers
2. **eBPF kTLS hook** for TLS payload visibility on Linux (no cert distribution,
   no MITM)
3. **CloudLens K8s DaemonSet on EKS** for full-packet visibility into
   containerized workloads that VPC Traffic Mirroring alone does not reach

Hand them three artifacts:

- The architecture diagram (PNG in their email)
- A link to https://keysight-tech.github.io/cloudlens-ansible-aws/
- The PR-ready GitHub repo for their security team to audit

---

## If the prospect asks any of these...

**Q: "What if a vPB dies?"**
A: Production keeps serving. Mirroring just stops flowing to the tool. No
customer impact. Run vPB in two AZs for fast failover.

**Q: "Why not just use raw VPC Traffic Mirroring?"**
A: You can, for a small estate. The moment you need filtering, dedup across
overlapping mirror sessions, header slicing, or load balancing to multiple tool
nodes with session affinity, that is the vPB. KVO also gives you fleet-wide
policy instead of per-ENI clicking.

**Q: "What about EKS / containers?"**
A: VPC Traffic Mirroring taps the ENI, so it sees pod traffic that egresses the
node ENI. For pod-to-pod inside a node, deploy the CloudLens K8s DaemonSet. Both
land in the same vPB.

**Q: "Can we audit your code?"**
A: All open source. https://github.com/Keysight-Tech/cloudlens-ansible-aws.
PRs welcome.

**Q: "What if we already own ExtraHop / Corelight / Darktrace?"**
A: Anything that accepts VXLAN or GRE works. The tool receiver in our demo is
just tcpdump on port 4789; your tool is the same shape with a real NDR engine
behind it.

**Q: "SSM is disabled in our account. Does this still work?"**
A: Yes. The Ansible layer connects over SSH (Linux) or WinRM (Windows) as well
as SSM. That is exactly why this repo exists alongside the SSM-based AutoPilot
path.

---

## The 5-minute resync after every demo

The lab is idempotent. After a demo:

```bash
# Stop any test traffic generators on the workload boxes
ssh -i ~/.ssh/<demo-key>.pem ubuntu@<app01-eip> 'pkill -f "while true; do curl"' || true

# Clear the tcpdump capture so the next demo starts at 0 bytes
ssh -i ~/.ssh/<demo-key>.pem ec2-user@<tool-eip> 'sudo truncate -s 0 /tmp/vxlan-capture.pcap'

# In KVO: leave the Cloud Config committed, or un-commit it so the next demo
# starts dark and you can show the commit live again.
```

Sensors stay registered, project stays alive, lab is ready for the next
prospect call.

---

## Teardown (only if you want to nuke everything)

Costs run in the low tens of dollars per day with the full lab up (the
`c5.2xlarge` KVO is the biggest line item). If the demo cycle pauses for more
than two weeks, tear down to save cost; rebuild in ~30 min when needed.

```bash
bash demo/teardown.sh                       # deletes the demo stack + workload instances
aws cloudformation delete-stack --stack-name cloudlens-demo --region us-east-1
```

To rebuild fresh:

```bash
bash demo/setup-aws-visibility-demo.sh
```

That orchestrator deploys everything from scratch, reuses one EC2 key pair, and
prints all the EIPs in a state file.

---

## Where this came from

This kit mirrors the mature Azure demo kit, ported to AWS: vController is the
new name for CLMS, KVO is Keysight Vision One, and the data path is AWS-native
VPC Traffic Mirroring instead of Azure vTAP. Every wall hit during a build is
documented in `docs/OPERATIONS.md`. Every command in this playbook has been run
against a real AWS account.

Author the next chapter when you take it to your next prospect.
