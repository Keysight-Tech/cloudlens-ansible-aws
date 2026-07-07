# CloudLens on AWS: Terraform

This directory offers two ways to provision the CloudLens control and data
plane on AWS. Pick the one that matches how you work. Both use the same
CloudLens Marketplace AMIs and produce the same running stack.

## 1. One-shot root module (this directory)

The `.tf` files directly in `deploy/terraform/` are a single flat module that
stands up the whole stack (vController, plus optional KVO and vPB) in one
`apply`. This is the Terraform counterpart of `deploy/cloudformation/stack.yaml`,
and it is what `deploy/deploy-stack.sh --iac terraform` drives.

```bash
cd deploy/terraform
terraform init
terraform apply \
  -var key_name=my-ec2-keypair \
  -var deploy_kvo=true \
  -var deploy_vpb=true
```

Key variables: `region`, `key_name`, `deploy_kvo`, `deploy_vpb`,
`controller_ami`/`controller_type`, `kvo_ami`/`kvo_type`,
`vpb_ami`/`vpb_type`, `vpb_ingress_nics`, `vpb_egress_nics`, `admin_cidr`.
See `variables.tf` for defaults (the AMIs default to the qualified
us-east-1 Marketplace images).

## 2. Per-component modules (clms/, kvo/, vpb/, stack/)

For granular or reusable deployments, each component has its own module with
its own variables, outputs, and README:

- `clms/`  vController (CloudLens control plane, formerly CLMS)
- `kvo/`   Keysight Vision Orchestrator
- `vpb/`   Virtual Packet Broker (data plane)
- `stack/` composes clms + kvo + vpb, with a `shared_vpc` toggle

Use these when you want to manage a single component, wire CloudLens into an
existing Terraform project, or keep separate state per component.

```bash
cd deploy/terraform/stack
terraform init
terraform apply -var key_name=my-ec2-keypair
```

## Instance-type restrictions (enforced)

The CloudLens Marketplace AMIs are qualified on specific instance types, and
the modules enforce this with `validation` blocks:

| Component            | AMI (us-east-1)        | Instance type   |
|----------------------|------------------------|-----------------|
| vController (CLMS)   | ami-0bebd5e730315337e  | t3.xlarge       |
| KVO                  | ami-017c0db8981569380  | c5.2xlarge only |
| vPB                  | ami-0a561b450552b707d  | t3.xlarge only  |

Each AMI requires an active AWS Marketplace subscription before launch.
