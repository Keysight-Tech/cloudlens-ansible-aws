# Customer Email Templates

Copy-paste templates for SEs sending CloudLens Ansible intros, kickoff invites, and follow-ups to customers. All emails are pre-formatted with AWS-correct terminology.

---

## Initial introduction (after discovery call)

**Subject:** CloudLens Ansible for AWS - next steps for your visibility deployment

> Hi [Customer Name],
>
> Following our conversation, here's the next-step package for deploying Keysight CloudLens visibility on your AWS account using our AutoPilot platform.
>
> **What AutoPilot delivers:**
>
> - Full CloudLens stack (KVO + CLMS + vPB + collector SVMs) deployed via Ansible playbook - your choice
> - AWS-native sensor rollout to every tagged EC2 instance via Ansible (SSH/SSM/WinRM) (no SSH or WinRM access required)
> - VPC Traffic Mirroring auto-managed by KVO, with sub-second new-instance detection via our EventBridge Lambda
> - 45-minute typical deployment time (proven at AAA Financial Services with 847 VMs)
>
> **Public docs & live site:**
> https://keysight-tech.github.io/cloudlens-ansible-aws/
>
> **Customer runbook (DOCX):**
> https://github.com/Keysight-Tech/cloudlens-ansible-aws/raw/main/CloudLens-AutoPilot-Deployment-Runbook.docx
>
> **One-click deploy from your AWS Console:**
> https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https://keysight-cloudlens-templates.s3.us-east-1.amazonaws.com/aws/stack.yaml&stackName=cloudlens-ansible-aws
>
> Before deploying, please subscribe to the 3 Keysight Marketplace products (one-time per AWS account). Direct links are on the public docs page under "AWS Marketplace prerequisites."
>
> Want me to walk through deployment live? Reply with a 30-minute slot and I'll bring an engineer.
>
> Best,
> [Your name]
> Keysight Solutions Engineering

---

## Kickoff email (after they sign)

**Subject:** CloudLens Ansible deployment kickoff - checklist for [Customer Name]

> Hi [Customer Name],
>
> Great news - we're ready to deploy CloudLens on your AWS account. Here's what we need from you to start:
>
> **From your side (estimate 1 hour total):**
>
> 1. **AWS account access** for a Keysight SE (IAM role with `AdministratorAccess` is easiest; we can scope down later)
> 2. **Marketplace subscriptions accepted** for the 3 Keysight products in your target AWS account:
>    - Keysight Vision One
>    - Keysight CloudLens Manager
>    - Keysight CloudLens Virtual Packet Broker
>    (Direct links here: https://keysight-tech.github.io/cloudlens-ansible-aws/#prereq-deploys)
> 3. **EC2 key pair** created in the target region (we'll use this for emergency console access; not required for normal ops)
> 4. **Target VPC topology**: greenfield (we create a new VPC) or brownfield (we deploy into your existing VPC)?
> 5. **Tagged target instances** - the EC2 instances you want monitored need `cloudlens=true` + a `Platform` tag (`linux-docker`, `linux-podman`, or `windows`). Our `prep-targets.sh` script can auto-tag if needed.
>
> **From our side:**
>
> 1. SE-led CloudFormation or Terraform deployment (~10 min including waits)
> 2. KVO/CLMS/vPB EULA acceptance and license activation (~15 min)
> 3. Sensor rollout via Ansible (SSH/SSM/WinRM) (5-60 min depending on fleet size - see https://keysight-tech.github.io/cloudlens-ansible-aws/#scaling)
> 4. Verification + handoff to your team
>
> Shall we schedule the kickoff call? I have these slots available:
> - [Date / Time options]
>
> Best,
> [Your name]

---

## Day-of follow-up (during/after deployment)

**Subject:** CloudLens Ansible deployment - status update

> Hi [Customer Name],
>
> Quick status from today's session:
>
> ✅ **Phase 1 (Infrastructure)** - CloudFormation stack `cloudlens-ansible-aws` is in `CREATE_COMPLETE`. KVO, CLMS, vPB instances are up and reachable.
>
> ✅ **Phase 2 (Configuration)** - KVO EULA accepted, all 3 licenses activated, CLMS adopted, vPB onboarded via CLI, AWS Cloud Config committed.
>
> 🔄 **Phase 3 (Sensors)** - In progress. Ansible (SSH/SSM/WinRM) is currently deploying to [N] tagged instances. Estimated completion: [time].
>
> **Access URLs (private, don't share):**
> - KVO: https://[kvo-public-ip]/ (admin / admin - please change on first login)
> - CLMS: https://[clms-public-ip]/ (admin / Cl0udLens@dm!n - please change)
> - vPB CLI: `ssh -p 9022 admin@[vpb-public-ip]` (default password `ixia`)
>
> I'll send a final report once Phase 3 completes with the sensor registration count and CloudWatch dashboard link.
>
> Best,
> [Your name]

---

## Post-deployment summary

**Subject:** CloudLens Ansible - deployment complete + handoff to your team

> Hi [Customer Name],
>
> Deployment is complete. Summary:
>
> **Final numbers:**
> - **EC2 instances monitored:** [N] / [N target]
> - **Sensors registered in CLMS:** [N] / [N target]
> - **CFT stack:** `cloudlens-ansible-aws` (CREATE_COMPLETE)
> - **Auto-Mirror Lambda:** active, tagging new instances within ~1 second
> - **Total deployment time:** [HH:MM]
>
> **What to monitor day-to-day:**
> - CLMS > Sensors - should stay at 100% Connected
> - KVO > Cloud Fabric > Monitoring Policies - mirror session count
> - CloudWatch > VPC Flow Logs - traffic patterns
>
> **When to call us:**
> - Sensor stays Disconnected > 5 minutes
> - Mirror session count drops unexpectedly
> - New AWS region rollout
> - Scaling beyond 5,000 instances
>
> **Public docs & troubleshooting:**
> - Site: https://keysight-tech.github.io/cloudlens-ansible-aws/
> - Troubleshooting: https://github.com/Keysight-Tech/cloudlens-ansible-aws/blob/main/TROUBLESHOOTING.md
> - Open an issue: https://github.com/Keysight-Tech/cloudlens-ansible-aws/issues
>
> You're in good hands. Reach out any time.
>
> Best,
> [Your name]
> Keysight Solutions Engineering

---

*Templates updated 2026-06-09 to match runbook v2.0 facts (instance types, default passwords, ports, etc.).*
