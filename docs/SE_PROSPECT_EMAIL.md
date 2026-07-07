# SE Outreach Email Templates (AWS)

Copy-paste templates for SE-to-prospect outreach on AWS network visibility.
Three variants by prospect profile. Each ends with the same call-to-action:
30-minute live demo.

Pair with `docs/SE_DEMO_PLAYBOOK.md` for the demo itself. For post-signature
customer comms (kickoff, day-of, handoff), see `docs/CUSTOMER_EMAIL.md`.

---

## Variant A: Inline-averse prospect (most common)

Use when the prospect has already said "no inline" or "we are not inserting
anything into our production data path."

```
Subject: 30-min demo: AWS packet visibility without inline placement

Hi [Name],

You mentioned [last week / last call] that you need packet-level
visibility into your AWS workloads but cannot accept an inline
appliance in the production data path. That is exactly the constraint
CloudLens was built for.

Quick recap of what we can do:

- Host-resident sensor on every Linux + Windows EC2 instance, deployed
  in parallel via Ansible. Out-of-band by design; your application path
  never depends on us.
- KVO orchestrates AWS VPC Traffic Mirroring natively and feeds a vPB
  that filters, dedups, and forwards to any tool that speaks VXLAN or
  GRE (Vectra, Corelight, ExtraHop, custom).
- Captures both east-west and north-south traffic. The sensor taps at
  the vNIC, so nothing east-west is invisible.
- One Ansible playbook covers 1 sensor or 5,000 sensors, over SSH, SSM,
  or WinRM. Works even if your account has SSM locked down.

I have a live demo environment standing in us-east-1: sensors already
registered on Linux and Windows. I can show you the full flow end-to-end
in 30 minutes:

  - The Launch Stack deploy your Ops team would actually do
  - Sensor inventory in CloudLens vController
  - KVO committing an AWS Cloud Config and standing up mirroring
  - Mirrored packets arriving at a tcpdump receiver in real-time
  - The honest blind-spots conversation (EKS pod-to-pod, double-TLS)
    and what covers each

What is your week looking like? I can run [Tue / Wed / Thu] at any
time that works for you.

Reference materials in advance:

- Architecture diagram: [attach PNG]
- Open-source runbook: https://keysight-tech.github.io/cloudlens-ansible-aws/
- Operations guide:
  https://github.com/Keysight-Tech/cloudlens-ansible-aws/blob/main/docs/OPERATIONS.md

Thanks,
[Your name]
[Your title]
Keysight Technologies
```

---

## Variant B: NDR-augmentation prospect

Use when the prospect already owns a tool (Vectra, ExtraHop, Corelight,
Darktrace) and the question is how to feed it from AWS.

```
Subject: Feed [their NDR] from AWS - 30-min demo

Hi [Name],

You said your [Vectra / Corelight / ExtraHop / Darktrace] cluster is
getting starved of AWS-side telemetry. That is the most common question
we get, and the architecture is straightforward.

The shape of the answer:

- CloudLens sensor on every EC2 instance (Linux + Windows). One Ansible
  playbook. Scales to thousands of sensors without changing anything in
  your tool config.
- KVO drives AWS VPC Traffic Mirroring; the vPB sits between the
  collector SVMs and your tool for dedup, header-slicing, and fleet-wide
  load balancing to multiple tool nodes with session affinity.
- Your tool sees real packets encapsulated as VXLAN or GRE. No agent on
  the tool, no proxy.

I have a working lab with sensors on Linux + Windows instances and a
VXLAN-receiving tool simulator. 30 minutes is enough to show:

  - Sensors deploying via Ansible (live)
  - KVO Cloud Config commit and mirror sessions appearing (live)
  - Packets at the tool side, decoded (live)
  - The session-affinity LB story at the vPB layer (slide)

If you have [tool] running somewhere reachable, we can point the vPB at
it directly during the demo: real packets into your real console within
the 30 minutes.

What slot works for you?

Reference in advance:

- Architecture: [attach PNG]
- Runbook: https://keysight-tech.github.io/cloudlens-ansible-aws/

Thanks,
[Your name]
```

---

## Variant C: Hybrid EC2 + EKS prospect

Use when the prospect has a complex AWS estate with both EC2 instances and
containerized workloads on EKS.

```
Subject: Visibility across your AWS EC2 + EKS estate - the honest answer

Hi [Name],

You asked whether CloudLens can give you full visibility across your
AWS estate including EKS. The honest answer has three parts, and I want
to walk you through all three in a 30-min demo.

Part 1: EC2 is solved.
Host sensor on every instance (Linux + Windows). Out-of-band. One
Ansible playbook. Tag-driven discovery. Same deploy on 1 instance or
5,000. KVO drives VPC Traffic Mirroring into a vPB.

Part 2: EKS node traffic is covered by the same path.
VPC Traffic Mirroring taps the node ENI, so any pod traffic that leaves
the node is mirrored just like a VM. Your existing vPB and tool see it
with no extra moving parts.

Part 3: Pod-to-pod inside a node needs the DaemonSet.
For east-west traffic that never leaves a single node, deploy the
CloudLens Kubernetes DaemonSet. It taps the pod veth pairs and forwards
into the same vPB. One tool, one policy surface, full coverage.

I will show the EC2 path live in 30 minutes against a working lab and
we can scope which of your namespaces need the DaemonSet.

Best windows: [your availability]

In the meantime:

- Architecture diagram: [attach PNG]
- Public runbook: https://keysight-tech.github.io/cloudlens-ansible-aws/
- Operations doc:
  https://github.com/Keysight-Tech/cloudlens-ansible-aws/blob/main/docs/OPERATIONS.md

Thanks,
[Your name]
```

---

## Post-demo follow-up template

Send within 24 hours of the demo. The goal is to keep the technical buyer warm
while the procurement conversation starts.

```
Subject: Recap + next steps after today

Hi [Name],

Thanks for the time today. Quick recap of what we covered:

- The architecture (out-of-band sensors, KVO-driven VPC Traffic
  Mirroring, vPB to your tool, no inline). [Attach diagram PNG]
- Live deploy via Ansible against tagged EC2 instances.
- Sensors registering to vController in under 90 seconds.
- KVO committing an AWS Cloud Config and mirror sessions appearing.
- Packets at the tool side, decoded, with the original L2 frame intact.

What I have for you in advance of the technical evaluation:

1. Public runbook with one-click Launch Stack deploy:
   https://keysight-tech.github.io/cloudlens-ansible-aws/

2. The full operations guide, including every gotcha we have hit in
   real engagements:
   https://github.com/Keysight-Tech/cloudlens-ansible-aws/blob/main/docs/OPERATIONS.md

3. The Ansible repo for your security team to audit:
   https://github.com/Keysight-Tech/cloudlens-ansible-aws

Three concrete next steps to suggest:

  - Stand up vController + one sensor in your own AWS account using the
    Launch Stack button on the site above. ~30 minutes, no Keysight
    commitment. We can pair on a screen-share if helpful.
  - Pick one VPC and let us scope the KVO Cloud Config and mirror policy
    together. ~1 hour of joint architecture time.
  - Define the scale target (1k? 10k instances?) so we can size the vPB
    fleet and discuss the LB strategy.

I am available [your slots] for any of these.

Thanks,
[Your name]
```

---

## What to attach to every prospect email

These three things in every initial outreach:

1. **PNG of the architecture diagram** (the vendor-neutral kit version)
   - Source: `docs/assets/architecture-diagram.svg` in this repo (export to PNG)

2. **Direct link to the operations doc**
   - https://github.com/Keysight-Tech/cloudlens-ansible-aws/blob/main/docs/OPERATIONS.md

3. **Direct link to the public site**
   - https://keysight-tech.github.io/cloudlens-ansible-aws/

Customers consistently say "I was able to read about CloudLens before the call"
is the thing that makes them comfortable buying. Lead with the artifacts.
