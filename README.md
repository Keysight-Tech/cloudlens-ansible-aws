# CloudLens Ansible for AWS

**Deploy CloudLens sensors to your AWS EC2 instances (Ubuntu, RHEL, Windows) via Ansible.**

🌐 **Live docs:** https://keysight-tech.github.io/cloudlens-ansible-aws/

Sibling product to [`cloudlens-ansible-azure`](https://github.com/Keysight-Tech/cloudlens-ansible-azure) — same playbook structure, AWS-native inventory + auth. The CloudLens sensor install tasks are identical across clouds; only the discovery and connection layers differ.

## Why Ansible for AWS?

[`cloudlens-autopilot`](https://github.com/Keysight-Tech/cloudlens-autopilot) uses AWS SSM Run Command for sensor deployment — that's optimal for AWS-native customers. This repo is for everyone who already has an Ansible workflow:

| You should use this if… | …else use AutoPilot SSM |
|---|---|
| You already run **Ansible Tower / AWX** | You're starting fresh on AWS |
| You manage **Azure + AWS + GCP** with one playbook | You're AWS-only |
| Your compliance team **disabled SSM** | SSM Agent is allowed |
| You're at **edge / Outposts / Wavelength** (SSM endpoints unreachable) | Standard AWS regions |

## Quick start

```bash
git clone https://github.com/Keysight-Tech/cloudlens-ansible-aws.git
cd cloudlens-ansible-aws

# 1. Set AWS auth (one of the following)
aws sso login --profile your-profile
# OR
export AWS_ACCESS_KEY_ID=AKIA...
export AWS_SECRET_ACCESS_KEY=...

# 2. Configure
cp customer_input.yaml.example customer_input.yaml
vim customer_input.yaml   # set CLMS IP + project_key + ssh_key_path

# 3. Tag your target EC2 instances:
#    cloudlens=yes   os=ubuntu|rhel|windows   env=prod

# 4. Run
bash quickstart.sh
```

## What it does

1. **Discovers** running EC2 instances tagged `cloudlens=yes` via the `amazon.aws.aws_ec2` dynamic inventory plugin
2. **Groups** them by `os` tag (ubuntu_prod_vms, redhat_prod_vms, windows_prod_vms)
3. **Connects** via SSH (Linux) or SSM Session Manager / WinRM (Windows)
4. **Installs** the CloudLens sensor — Docker on Ubuntu, Podman on RHEL, MSI on Windows
5. **Registers** each sensor with CLMS using the project key

## Prerequisites

- **AWS account** with CLMS Manager already deployed (use [`cloudlens-autopilot`](https://github.com/Keysight-Tech/cloudlens-autopilot-docs) to deploy CLMS via CloudFormation if needed)
- **Ansible 2.16+** with `amazon.aws`, `community.aws`, `ansible.windows` collections (auto-installed by `quickstart.sh`)
- **boto3** Python package
- **AWS CLI v2** authenticated (SSO or access keys)
- **EC2 key pair** (for SSH to Linux targets) OR **SSM Agent + IAM role** (for SSM mode)
- **EC2 instances tagged** with `cloudlens=yes` and `os=ubuntu|rhel|windows`

## Tag your VMs

CloudLens Ansible discovers VMs by tag. Apply these to every target:

| Tag | Value | Required? |
|---|---|---|
| `cloudlens` | `yes` | ✅ |
| `os` | `ubuntu` / `rhel` / `windows` | ✅ |
| `env` | `prod` / `dev` / `qa` | ✅ |

Bulk-tag a region:

```bash
# Ubuntu
aws ec2 describe-instances \
  --filters Name=instance-state-name,Values=running Name=image-id,Values=ami-* \
  --query "Reservations[].Instances[?Platform==null].InstanceId" --output text \
  | xargs -I {} aws ec2 create-tags --resources {} --tags Key=cloudlens,Value=yes Key=os,Value=ubuntu Key=env,Value=prod
```

## Connection modes

### Windows: SSM (recommended)
- No inbound ports open
- IAM-scoped via the instance's IAM role
- Requires SSM Agent (pre-installed on Windows Server 2016+)
- Set in `customer_input.yaml`: `aws.windows_connection: "ssm"`
- Uses the `community.aws.aws_ssm` Ansible connection plugin

### Windows: WinRM
- Standard Ansible Windows path
- Port 5985/5986 must be open in security group
- Username/password via `ANSIBLE_WINRM_PASSWORD` env var
- Set in `customer_input.yaml`: `aws.windows_connection: "winrm"`

### Linux: SSH (default)
- Uses EC2 key pair path from `aws.ssh_key_path`
- Default user: `ubuntu` (Ubuntu/Debian), `ec2-user` (RHEL/Amazon Linux)
- For private-only VMs: SSH ProxyCommand through bastion

### Linux: SSM (alternative)
- Set `aws.linux_connection: "ssm"` and remove SSH dependency
- Same IAM + SSM Agent requirements as Windows

## What's in this repo

```
cloudlens-ansible-aws/
├── ansible.cfg                       Ansible configuration
├── customer_input.yaml.example       Template config — copy to customer_input.yaml
├── deploy.yaml                       Master deploy playbook
├── cleanup.yaml                      Master remove playbook
├── quickstart.sh                     One-command bootstrap + deploy
├── requirements.yml                  Ansible collections to install
├── inventory/
│   ├── aws_ec2.yaml                  AWS EC2 dynamic inventory
│   └── group_vars/
│       ├── all.yaml
│       ├── ubuntu_prod_vms.yaml      SSH connection vars
│       ├── redhat_prod_vms.yaml      SSH connection vars
│       └── windows_prod_vms.yaml     SSM or WinRM connection vars
├── playbooks/
│   ├── ubuntu.yaml                   Docker install + CloudLens sensor
│   ├── ubuntu_cleanup.yaml
│   ├── redhat.yaml                   Podman install + CloudLens sensor
│   ├── redhat_cleanup.yaml
│   ├── windows.yaml                  MSI install + CloudLens sensor
│   └── windows_cleanup.yaml
├── vars/cloudlens.yaml               Sensor parameters
├── files/                            Sensor binaries (downloaded by quickstart)
└── docs/                             Public landing site (GitHub Pages)
```

## Documentation

| File | Purpose |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Discovery → connection → install pipeline |
| [`DEPLOYMENT_GUIDE.md`](DEPLOYMENT_GUIDE.md) | Step-by-step deploy |
| [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) | Common errors and fixes |
| [`SCALING.md`](SCALING.md) | Tuning Ansible forks for large fleets |

## Compare to cloudlens-autopilot

| Aspect | cloudlens-autopilot (SSM) | cloudlens-ansible-aws (this repo) |
|---|---|---|
| Deploys KVO/CLMS/vPB infra | ✅ via CFT/Terraform | ❌ assumes already-deployed |
| Deploys sensors to EC2 | ✅ via AWS SSM Run Command | ✅ via Ansible (SSH/SSM/WinRM) |
| Required tooling | AWS CLI only | Ansible + Python + boto3 |
| Multi-cloud-portable | AWS-only | ✅ same playbook on Azure |
| Best for | Self-service customers | Existing Ansible Tower shops |

You can run **both** in the same AWS account — they don't conflict.

## License

MIT — see [LICENSE](LICENSE).

## Contact

- 🐛 [Issues](https://github.com/Keysight-Tech/cloudlens-ansible-aws/issues)
- 🌐 [Keysight Technologies](https://www.keysight.com)
- 📦 [Sibling: cloudlens-ansible-azure](https://github.com/Keysight-Tech/cloudlens-ansible-azure)
- ☁️ [Sibling: cloudlens-autopilot](https://github.com/Keysight-Tech/cloudlens-autopilot-docs)
