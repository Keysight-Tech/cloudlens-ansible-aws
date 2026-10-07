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
> - Full CloudLens stack (KVO, CLMS (now vController) and vPB; collector SVMs are launched by KVO) deployed by CloudFormation or Terraform, your choice, then configured end to end by `deploy-stack.sh`
> - Sensor rollout to every tagged EC2 instance via Ansible over SSH (Linux), SSM Session Manager or WinRM (Windows)
> - VPC Traffic Mirroring managed by the KVO Cloud Config: the deploy creates and commits the Cloud Collection and monitoring policy, with no manual commit step left for you
> - 45 minutes for the sensor rollout to an 847-VM fleet (AAA Financial Services; timing bands per fleet size are on the scaling page)
>
> **Public docs & live site:**
> https://keysight-tech.github.io/cloudlens-ansible-aws/
>
> **Customer runbook (DOCX):**
> https://github.com/Keysight-Tech/cloudlens-ansible-aws/raw/main/docs/CloudLens_Stack_Deployment_Runbook.docx
>
> **One-click deploy from your AWS Console:**
> https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https://keysight-cloudlens-templates.s3.us-east-1.amazonaws.com/aws/stack.yaml&stackName=cloudlens-stack
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
>    - Keysight Vision Orchestrator
>    - Keysight CloudLens Manager
>    - Keysight CloudLens Virtual Packet Broker
>    (Direct links here: https://keysight-tech.github.io/cloudlens-ansible-aws/#prereq-deploys)
> 3. **EC2 key pair** in the target region. The Launch Stack form defaults to an existing key pair chosen from the dropdown; "create a new key pair for me" is the alternative, and the dropdown still needs a value either way. The deploy uses it for the Linux SSH sensor rollout and for vPB CLI access on port 9022
> 4. **Target VPC topology**: greenfield (we create a new VPC) or brownfield (we deploy into your existing VPC)?
> 5. **Tagged target instances** - the EC2 instances you want monitored need the tag `cloudlens=yes`. OS classification is automatic; an optional `os=ubuntu`, `os=rhel` or `os=windows` tag overrides it. Bulk-tagging commands are in the README.
>
> **From our side:**
>
> 1. SE-led CloudFormation or Terraform deployment (~5 min to `CREATE_COMPLETE`, then ~15 min of first-boot wait)
> 2. KVO EULA acceptance, license activation and CLMS adoption into KVO (~15 min); vPB adoption by KVO, which applies the vPB license, follows the sensor rollout
> 3. Sensor rollout via Ansible (SSH/SSM/WinRM): 5 to 60 min for fleets up to 2,000 instances, 1 to 4+ hours above that with sharded runs - see https://keysight-tech.github.io/cloudlens-ansible-aws/#scaling
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
> ✅ **Stack and first boot** (`stack`, `wait`, `bootstrap` phases) - CloudFormation stack `cloudlens-stack` is in `CREATE_COMPLETE`. KVO, CLMS and vPB instances are up and reachable.
>
> ✅ **Licensing and adoption** (`key`, `license`, `adopt` phases) - KVO EULA accepted, licenses activated, CLMS adopted into KVO.
>
> 🔄 **Sensors** (`sensors` phase) - In progress. Ansible (SSH/SSM/WinRM) is currently deploying to [N] tagged instances. Estimated completion: [time].
>
> ⏳ **Still to run** (`vpb`, `mirror`, `path`, `prove` phases) - vPB adoption by KVO with its license applied on adoption (`vpb`), the AWS mirror fabric: the Cloud Config for VPC Traffic Mirroring with collection and policy committed by the deploy (`mirror`), the vPB traffic path (`path`), and the traffic proof (`prove`).
>
> **Access URLs (private, don't share):**
> - KVO: https://[kvo-public-ip]/ (admin / admin - please change on first login)
> - CLMS: https://[clms-public-ip]/ (admin / Cl0udLens@dm!n - please change)
> - vPB CLI: `ssh -i <key>.pem -p 9022 admin@[vpb-public-ip]` (the EC2 key pair, no password), then `sudo vpb` for the CLI
>
> I'll send a final report once the remaining phases complete, with the sensor registration count, the mirror session count and the HTML deploy report.
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
> - **CFT stack:** `cloudlens-stack` (CREATE_COMPLETE)
> - **Mirror sessions:** [N] cut by the KVO Cloud Config (collection and policy committed by the deploy)
> - **Total deployment time:** [HH:MM]
>
> **What to monitor day-to-day:**
> - CLMS > Sensors - should stay at 100% Connected
> - KVO > Cloud Fabric > Cloud Configs - mirror session count
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
> - Troubleshooting: https://github.com/Keysight-Tech/cloudlens-ansible-aws/blob/main/docs/TROUBLESHOOTING.md
> - Open an issue: https://github.com/Keysight-Tech/cloudlens-ansible-aws/issues
>
> You're in good hands. Reach out any time.
>
> Best,
> [Your name]
> Keysight Solutions Engineering

---

*Templates updated 2026-06-09 to match runbook v2.0 facts (instance types, default passwords, ports, etc.).*
