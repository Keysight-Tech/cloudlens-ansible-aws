# Scaling

🌐 **Interactive sizing slider:** https://keysight-tech.github.io/cloudlens-ansible-aws/#scaling

CloudLens Ansible scales linearly across thousands of EC2 instances using Ansible's native parallelism: SSH for Linux targets, SSM Session Manager or WinRM for Windows targets. No custom orchestration, no agent push servers - just Ansible forks over AWS-native transports.

## Sensor deployment time vs fleet size

| Fleet size | Ansible forks | Sharded execution | Deployment time |
|---|---|---|---|
| 1–50 instances | 50 | No | 5–10 min |
| 51–200 | 100 | No | 10–20 min |
| 201–800 | 200 | No | 15–30 min |
| 801–2,000 | 400 | No | 30–60 min |
| 2,001–5,000 | 800 | Yes | 1–2 hours |
| 5,001–10,000 | 1,500 | Yes | 2–4 hours |
| 10,000+ | 2,500+ | Yes | 4+ hours |

Parallelism is the Ansible `forks` setting. The deploy reads it from `ANSIBLE_FORKS` in the environment, then `deploy.forks` in `customer_input.yaml`, and otherwise picks a value from the fleet size (20 forks up to 50 instances, 50 up to 500, 200 up to 2,000, 500 beyond). Pass `--forks` to `ansible-playbook` to override a single run. The site's interactive slider models the same bands.

## KVO infrastructure sizing

| Customer scale | KVO instance | CLMS instance | vPB instance | Collector SVMs |
|---|---|---|---|---|
| Demo / POC | `c5.2xlarge` | `t3.xlarge` | `t3.xlarge` | 1 auto |
| ≤ 1,000 sensors | `c5.2xlarge` | `t3.xlarge` | `t3.xlarge` | 2–4 auto |
| ≤ 5,000 sensors | `c5.2xlarge` | `t3.xlarge` | `c5.4xlarge`† | 4–8 auto |
| ≤ 10,000 sensors | `c5.4xlarge`* | `m5.2xlarge`‡ | `c5.4xlarge`† | 8–16 auto |
| 10,000+ sensors | Federated KVOs | Multi-CLMS | Multi-vPB | Cross-region |

\* KVO Marketplace AMI is locked to `c5.2xlarge` - going larger requires a custom Keysight build.

† vPB Marketplace AMI is qualified on `t3.xlarge` only; the stack rejects any other size (`VpbInstanceType` in `deploy/cloudformation/stack.yaml`). Deploy `t3.xlarge`; a larger vPB is a custom AMI request (see When to call Keysight Professional Services).

‡ CLMS (vController) Marketplace AMI permits `t3.xlarge` and `m5.xlarge` only; the Marketplace product rejects other sizes (`VcontrollerInstanceType` in `stack.yaml`). Deploy `m5.xlarge`; a larger CLMS is a custom AMI request.

## VPC Traffic Mirroring at scale

| Limit | Value | Notes |
|---|---|---|
| Mirror sessions per ENI | **10 (AWS hard limit)** | Beyond this → add more collector SVMs |
| Mirror filter rules per filter | 50 | Includes inbound + outbound + custom protocols |
| Mirror target bandwidth | Limited by collector ENI throughput | `c5.2xlarge` ≈ 10 Gbps |
| Concurrent KVO Cloud Configs | Per-region | Federate KVOs for global fleets |

KVO distributes mirror sessions across collector SVMs as source instances are added. New instances that match the Cloud Config selector are picked up on KVO's next inventory sync; no extra tagging step is needed beyond the discovery tag `cloudlens=yes`.

## Real-world deployment proof points

### AAA Financial Services
- **847 VMware VMs migrated to AWS EC2**
- Mix: 312 Ubuntu, 280 RHEL, 255 Windows Server 2019
- Single CFT stack deployment
- **45 minutes** for the sensor phase (the `sensors` step) at 400 forks, the band table's setting for 801–2,000 instances; that sits inside the 30–60 min band. Stack creation and the phases before `sensors` are not in that figure
- 100% sensor registration rate

### Airtel (in flight)
- **Projected 1,700 sites** with E1S/E50 edge appliances
- Distributed across India + adjacent regions
- AutoPilot orchestrates per-site CFT stacks via AWS Service Catalog (roadmap)
- Estimated full rollout: 2–3 quarters

### Nokia
- 4G LTE private network - CMU containerized workloads
- E40 packet broker integration
- VIAVI TSA downstream analytics
- AutoPilot CFT + manual K8s sensor deployment for hybrid stack

## When to shard

The deploy shards above **2,000 instances**. `deploy/shard.sh` splits the discovered inventory into shards of `SHARD_SIZE` (default 500) and runs one `ansible-playbook` per shard in parallel, each with its own `--forks`. Every shard runs against the same inventory with `--limit`, so the OS groups and their group_vars stay intact. A failure in one shard does not stop the others; any failed or empty shard makes the script exit non-zero. Each shard writes its own log under `./logs/shards/`.

The container entrypoint shards on its own once the discovered count passes 2,000, reading `deploy.shard_size` and `deploy.forks` from `customer_input.yaml` (or `SHARD_SIZE` and `ANSIBLE_FORKS` from the environment). To run it directly:

```bash
# shards of 500 instances, 200 forks per shard
# first argument: count hint (the real count comes from the inventory)
# second argument: forks per shard
SHARD_SIZE=500 bash deploy/shard.sh 5000 200
```

## What to monitor

- **Ansible play recap and `./logs/ansible.log`** - per-instance success/failure, one log per shard under `./logs/shards/` when sharded
- **CLMS > Sensors** - registration rate over time
- **CloudWatch > VPC Flow Logs** - traffic patterns, drops
- **KVO > Cloud Fabric > Monitoring Policies** - mirror session count, collector load

## When to call Keysight Professional Services

- Federating multiple KVOs across regions
- 10,000+ sensor deployments
- Custom AMI requirements beyond Marketplace SKUs
- Service Catalog packaging for customer self-service portals
- 5G core network visibility (NSCP, SBI)
- SSL Payload decryption at fleet scale

Contact: https://www.keysight.com/find/cloudlens

---

*Numbers from production deployments and the deployment runbook v2.0 (March 2026). Updated 2026-10-07.*
