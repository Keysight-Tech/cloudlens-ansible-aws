# Troubleshooting

Common issues encountered during CloudLens Ansible AWS deployments and their fixes, pulled from real customer engagements and the live lab runs.

🌐 **Live FAQ:** https://keysight-tech.github.io/cloudlens-ansible-aws/#faq

---

## Deployment errors

### My deploy stopped partway

**Fix:** Re-run the same command with `--resume` (the default with no terminal). Nothing already built is lost; `--from PHASE` / `--only PHASE` take `stack wait bootstrap key license adopt sensors eks vpb mirror path prove`.

### `The Terraform engine does not support deploying into existing infrastructure`

**Cause:** `--iac terraform` with an `--existing-*` flag.

**Fix:** Use the CloudFormation engine (the default) for brownfield.

### Existing VPC: KVO reports `Could not discover CLMS`, sensors and collectors cannot reach the appliances

**Cause:** The in-VPC security-group rules were opened to the template's own `VpcCidr` (10.99/16), not your range.

**Fix:** `deploy-stack.sh` fills `ExistingVpcCidr` from the VPC; on the Launch Stack form set it to your VPC's CIDR.

### Stack delete ends in `DELETE_FAILED` on the security group

**Cause:** An extra vPB launched per area (`--vpb-rails`) still holds the group.

**Fix:** `teardown-stack.sh` terminates the rail vPBs before the delete; re-run it.

### `The maximum number of addresses has been reached`

**Cause:** Elastic IP quota (5 per region by default; the stack needs 3).

**Fix:** Release orphaned addresses the deploy names in Phase 4b, request a quota increase, or deploy with `--no-public-ip`.

### `galaxy.ansible.com unreachable; continuing with the collections already in ./collections`

**Cause:** A transient TLS, proxy or routing failure.

**Fix:** None when every collection is already installed; only a missing collection stops the run.

### `CloudLens manager 10.x.x.x is a private address this machine cannot reach`

**Cause:** Expected. The sensor hosts inside the VPC do the pull, not the operator machine.

**Fix:** None; the run continues.

### Licensing fails with 503 right after the stack came up

**Cause:** The KVO is still booting.

**Fix:** `kvo_license.py` now waits for the token endpoint for up to ten minutes; re-run `--only license` if it timed out.

### `UnsupportedOperation` on `terraform apply` or CFT stack creation

**Cause:** Wrong EC2 instance type for the AWS Marketplace AMI.

**Fix:**
- KVO: `c5.2xlarge` only (`KvoInstanceType`, `--kvo-type`)
- vPB: `t3.xlarge` only (`VpbInstanceType`, `--vpb-type`)
- vController: `t3.xlarge` (default) or `m5.xlarge` (`VcontrollerInstanceType`, `--vcontroller-type`); m5 gives dedicated CPU for production

These are the only values the template accepts (AllowedValues) and the CLI rejects anything else before it calls AWS. `customer_input.yaml` does not control them.

### `AccessDenied` on stack creation

**Cause:** Your IAM principal lacks permissions to create IAM resources.

**Fix:** Run with `--capabilities CAPABILITY_NAMED_IAM` (CloudFormation) or a principal that may create roles (Terraform). The only IAM the stack creates is the `<stack>-kvo-zonetap` role and instance profile for KVO, gated on `EnableZoneTapping=yes` (`--enable-zone-tapping`), with every create scoped to the `cloudlens:monitored:vpcid` tag (`deploy/iam/cloudlens-zonetap-policy.json`). Leave it at `no` and the base suite deploys with no IAM rights; attach the policy later with `scripts/kvo_enable_zonetap_iam.sh`.

### Stack creates but instances fail to launch

**Cause:** Not subscribed to AWS Marketplace AMIs.

**Fix:** Subscribe to all 3 Keysight products **once per AWS account** before deploying:

1. [Keysight Vision Orchestrator](https://aws.amazon.com/marketplace/search/results?searchTerms=Keysight+Vision+Orchestrator)
2. [Keysight CloudLens Manager](https://aws.amazon.com/marketplace/search/results?searchTerms=Keysight+CloudLens+Manager)
3. [Keysight CloudLens Virtual Packet Broker](https://aws.amazon.com/marketplace/search/results?searchTerms=Keysight+CloudLens+Virtual+Packet+Broker)

This step **cannot** be automated - AWS requires interactive EULA acceptance.

### Terraform state lock error

**Cause:** A previous `terraform apply` was interrupted before releasing the lock.

**Fix:** `terraform force-unlock <lock-id>` (lock ID is shown in the error).

---

## KVO / CLMS / vPB issues

### KVO shows EULA page on every request

**Cause:** EULA not yet accepted.

**Fix:** By hand, open `https://<kvo-public-ip>/`, read the Keysight Software EULA and click **Agree**. The deploy does this for you in phase 11 (`scripts/kvo_license.py --accept-eula`, stated before it acts); the API answers 302 to `/eula/static/` until then, which is what the deploy's detection reports as "KVO is still on the EULA page".

### vPB not appearing in KVO Inventory > Devices

**Cause:** vPB doesn't get pulled by KVO. It must push itself outbound from its own CLI.

**Fix:** Re-run the adoption phase: `bash deploy/deploy-stack.sh --stack-name <stack> --only vpb`. It SSHes in with the EC2 key pair (`ssh -i <key>.pem -p 9022 admin@<vpb-ip>`), runs the `kvo` context (`ip <kvo-private-ip>`, `port 443`, `enable`, `exit`) through `sudo vpb -c`, waits for the announcement and adopts the device with control enabled; KVO applies the licence on adoption. By hand, `license server <kvo-private-ip> type advanced` first and then the same `kvo` context in `sudo vpb`; `kvo` is an interactive context, flat `kvo ip` / `kvo enable` commands do not enable it.

The vPB now appears in **KVO > Inventory > Devices**. Adopt it with **"Control the adopted device" enabled** (default).

### vPB adopted but missing from port-binding dropdowns

**Cause:** Adopted without the "Control the adopted device" checkbox.

**Fix:** Delete the vPB from Inventory, re-discover, re-adopt with control enabled. KVO will auto-create the Device Config which is what populates the port-binding dropdowns.

### CLMS stuck "DISCOVERING" forever

**Cause:** KVO using the CLMS **public** IP. CLMS only accepts adoption on its private IP from inside the VPC.

**Fix:** In KVO **Inventory > CloudLens Manager > Discover**, use the CLMS **private** IP (the `10.99.1.x` address from Terraform/CFT outputs), not the public IP.

---

## SSM / Sensor deployment issues

### SSM command fails with `AccessDenied`

**Cause:** Target VM is missing the SSM IAM role.

**Fix:** Attach the AWS managed policy `AmazonSSMManagedInstanceCore` to the instance's IAM role (`aws iam attach-role-policy --role-name <role> --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore`). For the Windows test VM the deploy takes the profile name with `--windows-instance-profile`.

### Sensor container starts but doesn't appear in CLMS

**Cause:** Target VM can't reach CLMS on port 443, OR the project key is wrong.

**Fix:**
1. Verify HTTPS (443) from the target VM to the vController address the sensor was given. Inside the CloudLens VPC that must be the **private** address: the public one leaves through the internet gateway and returns from the workload's public IP, which the security group refuses (seen live 2026-10-07: `docker pull <public-ip>/sensor` timed out on every workload while the private address answered at once). The deploy writes the private address into `customer_input.yaml`; set `CLOUDLENS_SENSOR_MANAGER_ADDR` for workloads in another VPC or on-prem.
2. Get a fresh project key from CLMS UI: **Settings > Projects > API Keys**
3. Redeploy the sensor with the correct key

### SSM shows "0 target instances" when running deploy script

**Cause:** Missing tags or SSM Agent not running.

**Fix:**
1. Verify the discovery tag on each target: `cloudlens=yes` (or the pair you passed with `--discovery-tag-key` / `--discovery-tag-value`). That is the only required tag. `os`, `env` and `Platform` are optional: the inventory reads the OS from AWS (`platform` / `platform_details`) and probes any Linux host it cannot name, so `os=ubuntu|rhel|windows` only overrides that, and `env` and `Platform` only group hosts
2. Verify SSM Agent is registered: `aws ssm describe-instance-information --region us-east-1`
3. If empty, attach the policy as above, restart the agent, and wait 60 seconds.

### Windows sensor deploy hangs

**Cause:** Windows Server 2016+ has SSM Agent pre-installed but it might be stopped.

**Fix:** RDP to the box and run (PowerShell as admin):
```powershell
Restart-Service AmazonSSMAgent
Get-Service AmazonSSMAgent  # should show "Running"
```
Wait 60 seconds, then re-run the SSM command.

---

## VPC Traffic Mirroring issues

### Traffic Mirror sessions never appear in EC2 console

**Cause:** KVO Cloud Config not configured, or tags missing on source instances.

**Fix:**
1. KVO > **Cloud Fabric > Cloud Configs** - verify the AWS Cloud Config status is "COMMITTED"
2. Tag source instances with the discovery tag (`cloudlens=yes` by default); the deploy builds the Cloud Collection selector from KVO's own identifier for that tag (`system.tags.<key>`). Only Nitro instances can be mirrored.
3. KVO creates the mirror target, filter and sessions when the Monitoring Policy commits. If the config shows COMMITTED with zero sessions, the collector booted before the Cloud-to-Device Link existed; it relaunches only when the AWS presence is recreated with the key, which `--only mirror` does for you (reusing the presence never relaunches it).

### "Mirror filter exceeded" error

**Cause:** AWS hard limit of 10 Mirror sessions per ENI.

**Fix:** Add more collector SVMs by scaling the KVO Cloud Config - KVO auto-distributes mirror sessions across collectors.

---

## When nothing else works

1. **Re-run with `--resume` first**: it probes the real system and continues from the first unfinished phase in about a minute. **Tear down and redeploy** only when that fails: `bash deploy/teardown-stack.sh --stack-name <stack> --region <region>` audits, confirms, releases the KVO licences while the KVO is alive (a bare `delete-stack` strands them for good), terminates any rail vPBs, deletes the stack and sweeps the leftovers; then run the deploy again.
2. **Check the operations guide** [docs/OPERATIONS.md](OPERATIONS.md) section 6 and the [stack runbook](CloudLens_Stack_Deployment_Runbook.pdf) for the issue/cause/fix tables.
3. **Open an issue:** https://github.com/Keysight-Tech/cloudlens-ansible-aws/issues

---

*Last updated: 2026-10-07, against the brownfield proof (existing VPC, existing EKS cluster, both vPB rails) and the current deploy-stack.sh phase list.*
