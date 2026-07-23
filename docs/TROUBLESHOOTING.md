# Troubleshooting

Common issues encountered during CloudLens Ansible AWS deployments and their fixes. Pulled from real customer engagements (AAA, Nokia, Airtel) and the official deployment runbook section 9.

🌐 **Live FAQ:** https://keysight-tech.github.io/cloudlens-ansible-aws/#faq

---

## Deployment errors

### `UnsupportedOperation` on `terraform apply` or CFT stack creation

**Cause:** Wrong EC2 instance type for the AWS Marketplace AMI.

**Fix:**
- KVO **must** use `c5.2xlarge` (Marketplace restriction)
- vPB-KVO **must** use `t3.xlarge` (Marketplace restriction)
- CLMS is flexible - `t3.xlarge` recommended

Cannot be changed in `customer_input.yaml` or CFT parameters without breaking the Marketplace EULA binding.

### `AccessDenied` on stack creation

**Cause:** Your IAM principal lacks permissions to create IAM resources.

**Fix:** Run with `--capabilities CAPABILITY_NAMED_IAM` (CFT) or attach `AdministratorAccess` to the principal (Terraform). The stack creates an IAM user with the v6.13.0 CloudLens policy scoped to `cloudlens:monitored:vpcid`.

### Stack creates but instances fail to launch

**Cause:** Not subscribed to AWS Marketplace AMIs.

**Fix:** Subscribe to all 3 Keysight products **once per AWS account** before deploying:

1. [Keysight Vision One](https://aws.amazon.com/marketplace/search/results?searchTerms=Keysight+Vision+One)
2. [Keysight CloudLens Manager](https://aws.amazon.com/marketplace/search/results?searchTerms=Keysight+CloudLens+Manager)
3. [Keysight CloudLens Virtual Packet Broker](https://aws.amazon.com/marketplace/search/results?searchTerms=Keysight+CloudLens+Virtual+Packet+Broker)

This step **cannot** be automated - AWS requires interactive EULA acceptance.

### Terraform state lock error

**Cause:** A previous `terraform apply` was interrupted before releasing the lock.

**Fix:** `terraform force-unlock <lock-id>` (lock ID is shown in the error).

---

## KVO / CLMS / vPB issues

### KVO shows EULA page on every request

**Cause:** EULA not accepted in browser.

**Fix:** Open `https://<kvo-public-ip>/` in a browser, read the Keysight Software EULA, click **Agree**. The EULA blocks the API too - you must do this in a browser, the CLI cannot accept on your behalf.

### vPB not appearing in KVO Inventory > Devices

**Cause:** vPB doesn't get pulled by KVO. It must push itself outbound from its own CLI.

**Fix:** SSH to the vPB **on port 9022 (NOT 22)** with `admin / ixia`, then:

```
configure terminal
kvo
ip <kvo-private-ip>
port 443
enable
monitored
end
write memory
```

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

**Fix:** Attach the AWS managed policy `AmazonSSMManagedInstanceCore` to the instance's IAM role. The `prep-targets.sh` script can do this automatically with `--auto-fix`.

### Sensor container starts but doesn't appear in CLMS

**Cause:** Target VM can't reach CLMS on port 443, OR the project key is wrong.

**Fix:**
1. Verify outbound HTTPS (443) from target VM to CLMS public IP - check security groups, NACLs, VPC endpoints
2. Get a fresh project key from CLMS UI: **Settings > Projects > API Keys**
3. Redeploy the sensor with the correct key

### SSM shows "0 target instances" when running deploy script

**Cause:** Missing tags or SSM Agent not running.

**Fix:**
1. Verify tags on each target: `cloudlens=true` AND `Platform=linux-docker` (or `linux-podman` or `windows`)
2. Verify SSM Agent is registered: `aws ssm describe-instance-information --region us-east-1`
3. If empty, run `./scripts/prep-targets.sh --auto-fix`

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
2. Tag source instances with whatever the Cloud Collection workload selector specifies (typically `cloudlens-mirror=true`)
3. Auto-Mirror Lambda should pick them up within 1-2 seconds via EventBridge

### "Mirror filter exceeded" error

**Cause:** AWS hard limit of 10 Mirror sessions per ENI.

**Fix:** Add more collector SVMs by scaling the KVO Cloud Config - KVO auto-distributes mirror sessions across collectors.

---

## When nothing else works

1. **Tear down and redeploy** is often faster than debugging. AutoPilot's `terraform destroy` + reapply takes 5 minutes.
2. **Check the deployment runbook** [section 9 Troubleshooting](https://github.com/Keysight-Tech/cloudlens-ansible-aws/raw/main/CloudLens-AutoPilot-Deployment-Runbook.docx) for the canonical issue/cause/solution table.
3. **Open an issue:** https://github.com/Keysight-Tech/cloudlens-ansible-aws/issues

---

*Last updated: 2026-06-09. Maintained against runbook v2.0 (March 2026).*
