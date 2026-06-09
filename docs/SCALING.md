# Scaling

🌐 **Interactive sizing slider:** https://keysight-tech.github.io/cloudlens-ansible-aws/#scaling

CloudLens Ansible scales linearly across thousands of EC2 instances using AWS Ansible (SSH/SSM/WinRM)'s native parallelism. No custom orchestration, no agent push servers — just AWS-native concurrency.

## Sensor deployment time vs fleet size

| Fleet size | SSM parallelism | Sharded execution | Deployment time |
|---|---|---|---|
| 1–50 instances | 50 concurrent | No | 5–10 min |
| 51–200 | 100 concurrent | No | 10–20 min |
| 201–800 | 200 concurrent | No | 15–30 min |
| 801–2,000 | 400 concurrent | **Yes** (chunked) | 30–60 min |
| 2,001–5,000 | 800 concurrent | Yes | 1–2 hours |
| 5,001–10,000 | 1,500 concurrent | Yes | 2–4 hours |
| 10,000+ | 2,500+ concurrent | Yes | 4+ hours |

Ansible (SSH/SSM/WinRM)'s `--max-concurrency` and `--max-errors` flags let you tune the concurrency profile per customer. The site's interactive slider models the same bands.

## KVO infrastructure sizing

| Customer scale | KVO instance | CLMS instance | vPB instance | Collector SVMs |
|---|---|---|---|---|
| Demo / POC | `c5.2xlarge` | `t3.xlarge` | `t3.xlarge` | 1 auto |
| ≤ 1,000 sensors | `c5.2xlarge` | `t3.xlarge` | `t3.xlarge` | 2–4 auto |
| ≤ 5,000 sensors | `c5.2xlarge` | `t3.xlarge` | `c5.4xlarge` | 4–8 auto |
| ≤ 10,000 sensors | `c5.4xlarge`* | `m5.2xlarge` | `c5.4xlarge` | 8–16 auto |
| 10,000+ sensors | Federated KVOs | Multi-CLMS | Multi-vPB | Cross-region |

\* KVO Marketplace AMI is locked to `c5.2xlarge` — going larger requires a custom Keysight build.

## VPC Traffic Mirroring at scale

| Limit | Value | Notes |
|---|---|---|
| Mirror sessions per ENI | **10 (AWS hard limit)** | Beyond this → add more collector SVMs |
| Mirror filter rules per filter | 50 | Includes inbound + outbound + custom protocols |
| Mirror target bandwidth | Limited by collector ENI throughput | `c5.2xlarge` ≈ 10 Gbps |
| Concurrent KVO Cloud Configs | Per-region | Federate KVOs for global fleets |

KVO auto-distributes mirror sessions across collector SVMs as you scale source instances. Auto-Mirror Lambda's EventBridge rules tag new instances within seconds — no polling lag.

## Real-world deployment proof points

### AAA Financial Services
- **847 VMware VMs migrated to AWS EC2**
- Mix: 312 Ubuntu, 280 RHEL, 255 Windows Server 2019
- Single CFT stack deployment
- **45 minutes** end-to-end (Phase 1 + Phase 2 + Phase 3)
- 100% sensor registration rate

### Airtel (in flight)
- **Projected 1,700 sites** with E1S/E50 edge appliances
- Distributed across India + adjacent regions
- AutoPilot orchestrates per-site CFT stacks via AWS Service Catalog (roadmap)
- Estimated full rollout: 2–3 quarters

### Nokia
- 4G LTE private network — CMU containerized workloads
- E40 packet broker integration
- VIAVI TSA downstream analytics
- AutoPilot CFT + manual K8s sensor deployment for hybrid stack

## When to shard

AutoPilot auto-shards above **2,000 sensors**. Sharding chunks the fleet into batches and processes them serially within a batch, in parallel across batches. This keeps any single SSM API call below AWS throttling limits.

You can tune sharding behavior in `deploy-sensors.sh`:

```bash
./scripts/deploy-sensors.sh \
  --region us-east-1 \
  --profile autopilot \
  --batch-size 500 \
  --max-concurrent-batches 4
```

## What to monitor

- **AWS Systems Manager > Run Command > Command History** — per-instance success/failure
- **CLMS > Sensors** — registration rate over time
- **CloudWatch > VPC Flow Logs** — traffic patterns, drops
- **KVO > Cloud Fabric > Monitoring Policies** — mirror session count, collector load
- **CloudWatch > Auto-Mirror Lambda invocations** — should match new EC2 launch rate

## When to call Keysight Professional Services

- Federating multiple KVOs across regions
- 10,000+ sensor deployments
- Custom AMI requirements beyond Marketplace SKUs
- Service Catalog packaging for customer self-service portals
- 5G core network visibility (NSCP, SBI)
- SSL Payload decryption at fleet scale

Contact: https://www.keysight.com/find/cloudlens

---

*Numbers from production deployments and the deployment runbook v2.0 (March 2026). Updated 2026-06-09.*
