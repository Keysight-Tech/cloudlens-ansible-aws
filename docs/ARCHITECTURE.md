# CloudLens Ansible Architecture

🌐 **Live diagrams:** https://keysight-tech.github.io/cloudlens-ansible-aws/#architecture

## The 3-phase approach

AutoPilot deploys CloudLens on AWS in three distinct phases:

| Phase | Tool | What it does | Time |
|---|---|---|---|
| **Phase 1** | CloudFormation **or** Terraform | Deploys VPC, KVO, CLMS, vPB, IAM, security groups, SSM Documents (~35 AWS resources) | 5 min |
| **Phase 2** | Manual (web UI) | Accept EULAs, activate licenses, adopt CLMS into KVO, onboard vPB, create Cloud Config | 15 min |
| **Phase 3** | AWS Ansible (SSH/SSM/WinRM) | Push CloudLens sensors to every tagged EC2 instance (Docker, Podman, or Windows) | 5–60 min depending on fleet size |

## Products deployed

| Product | Role | Instance type | Source |
|---|---|---|---|
| **KVO** (Keysight Vision One) | Master orchestrator, analytics, CloudConfig pipeline | `c5.2xlarge` only | AWS Marketplace `ami-017c0db8981569380` |
| **CLMS** (CloudLens Manager) | Sensor management and registration | `t3.xlarge` | AWS Marketplace `ami-0bebd5e730315337e` |
| **vPB-KVO** (Virtual Packet Broker) | Traffic filtering, dedup, load balancing | `t3.xlarge` only, SSH on port 9022 | AWS Marketplace `ami-0a561b450552b707d` |
| **Collector SVM** (Service VM) | AWS VPC Traffic Mirror collector | Auto-deployed by KVO Zone Tapping | `ami-0c22ade3667f8d35a` v6.13.0 |
| **Auto-Mirror Lambda** | EventBridge-driven instance tagger | python3.12, 128 MB | `deploy/cloudformation/stack.yaml` |

## Network topology

```
┌─────────────────────────────────────────────────────────────┐
│ VPC 10.0.0.0/16                                              │
│                                                              │
│  ┌──────────────────────┐                                    │
│  │ Mgmt 10.0.1.0/24     │ ← KVO, CLMS, vPB eth0 (mgmt)       │
│  │   SSH, HTTPS, API    │                                    │
│  └──────────────────────┘                                    │
│                                                              │
│  ┌──────────────────────┐                                    │
│  │ Data 10.0.2.0/24     │ ← vPB eth1 (ingress)               │
│  │   Sensor traffic     │   GRE / VXLAN from sensors         │
│  │   (mirrored copies)  │                                    │
│  └──────────────────────┘                                    │
│                                                              │
│  ┌──────────────────────┐                                    │
│  │ Tool 10.0.3.0/24     │ ← vPB eth2 (egress)                │
│  │   To analytics tools │   Cleaned + filtered traffic       │
│  └──────────────────────┘                                    │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

All CIDRs are configurable in `customer_input.yaml` and CFT parameters.

## Data flow - east-west AND north-south

```
                     ┌─────────────────┐
                     │  Target EC2     │
                     │  + CloudLens    │
                     │    Sensor       │
                     └────────┬────────┘
                              │ port mirror via SSM-deployed sensor
                              ▼
                   ┌──────────────────────┐
                   │  AWS VPC Traffic     │  ← managed natively
                   │  Mirroring           │     by KVO Cloud Config
                   └──────────┬───────────┘
                              ▼
                   ┌──────────────────────┐
                   │  Collector SVM       │  ← auto-deployed by KVO
                   │  (Service VM)        │     when Cloud Config commits
                   └──────────┬───────────┘
                              ▼
                   ┌──────────────────────┐
                   │  vPB-KVO             │  ← filter, dedup, GREoUDP
                   │  Virtual Packet      │     decap, SSL Payload
                   │  Broker              │     ingress
                   └──────────┬───────────┘
                              ▼
                   ┌──────────────────────┐
                   │  Analytics tools     │
                   │  (SIEM, DPI, etc.)   │
                   └──────────────────────┘
```

**Both east-west (pod-to-pod, VM-to-VM within VPC) and north-south (VM-to-internet) traffic is captured** - the sensor lives in the target VM and taps at the vNIC level. AWS VPC Traffic Mirroring then forwards mirrored copies to the Collector SVMs.

## Two deployment paths

```
┌─────────────────────────┐         ┌─────────────────────────┐
│  CloudFormation         │         │  Terraform              │
│                         │         │                         │
│  • AWS-native           │         │  • Multi-cloud ready    │
│  • No tools to install  │         │  • S3 state backend     │
│  • Auto-Mirror included │         │  • Modular              │
│  • Customer self-serve  │         │  • SE / DevOps preferred│
└────────────┬────────────┘         └────────────┬────────────┘
             └──────────────┬──────────────────┘
                            ▼
                ┌───────────────────────┐
                │  Identical AWS state  │
                │  (~35 resources)      │
                └───────────────────────┘
```

Choose based on your team's existing tooling - both produce **identical infrastructure**. CloudFormation includes the Auto-Mirror Lambda in the template; Terraform deploys it as a separate module (planned).

## Why Auto-Mirror beats Gigamon ATS

| | Gigamon GigaVUE-FM ATS | CloudLens Ansible |
|---|---|---|
| Mechanism | Poll AWS APIs on interval | EventBridge fires on `RunInstances` |
| Detection latency | Polling interval (1–5 min) | < 1 second |
| New VM coverage | Bounded by poll cycle | Instant |
| Cost | Continuous API calls | Pay-per-event |

Auto-Mirror auto-tags new EC2 instances with `cloudlens-mirror=true` in watched VPCs. KVO picks up the tag and starts mirroring immediately - no polling overhead.

## Customer case study

**AAA Financial Services:**
- 847 VMware VMs migrated to AWS EC2
- Existing Ansible Tower for orchestration
- AutoPilot deployed CloudLens stack in 45 minutes end-to-end
- 100% sensor registration rate via Ansible (SSH/SSM/WinRM)
- Zero SSH/WinRM access required by SEs

See the [Executive Summary](CloudLens-AutoPilot-Executive-Summary.docx) for the full case study.

---

*Architecture reference for CloudLens Ansible v2.0 (March 2026). For implementation details, see the [Deployment Runbook](CloudLens-AutoPilot-Deployment-Runbook.docx).*
