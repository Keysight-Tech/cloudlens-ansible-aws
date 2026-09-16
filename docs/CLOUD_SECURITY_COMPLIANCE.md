# Cloud security compliance: what this repo leaves behind in an AWS account

Keysight IT scans every account against the CIS AWS Foundations Benchmark and
emails the owner the failures. This page records what the deploy scripts create,
which scanner checks they touch, and the two items that cannot be cleared by the
account owner at all.

Reference: Keysight Cloud Security Architecture Guide, sections 5.2 (CIS
Foundations) and 5.4 (S3 bucket access requirements).

## What the scripts now do on their own

| Resource | Hardening applied at creation |
| --- | --- |
| `keysight-cloudlens-templates` (public template bucket) | ACL public access blocked, public read scoped to `aws/*` and nothing else, plain HTTP denied, versioning on |
| `cloudlens-ssm-transfer-<account>` (Windows sensor staging) | All four public access blocks on, plain HTTP denied, versioning on, no bucket policy grant to anyone |
| `CloudLensZoneTap` IAM policy | Granted through the `cloudlens-zonetap` group, never attached straight to a user |
| Security groups for the appliances | Built from an admin CIDR the operator chooses. The Launch Stack form has always had the field; the CLI now asks for it too, offering this machine's address as a /32 rather than defaulting silently to 0.0.0.0/0 |

Both rows are repaired in place, not only at creation.
`deploy/scripts/sync-cfn-templates-to-s3.sh` brings the template bucket up to
these rules the next time templates are published, and the deploy script does
the same for the SSM bucket every time it adopts one from an earlier run. A
bucket policy neither script wrote is never overwritten: the operator is told
what statement is missing instead.

## The one deliberate exception: public read on the template bucket

AWS CloudFormation Quick-Create URLs accept an S3 URL and nothing else. A
GitHub raw URL is rejected with "TemplateURL must be a supported URL", and a
CloudFront URL is rejected the same way. The customer clicking Launch Stack is
in their own AWS account, so the object has to be readable without our
credentials.

That makes three scanner checks unavoidable while the Launch buttons exist:

- `s3_bucket_level_public_access_block` on the template bucket
- `s3_account_level_public_access_blocks` for the account
- the account-level `BlockPublicPolicy` and `RestrictPublicBuckets` halves

The exposure is four CloudFormation templates that are already public on GitHub,
read-only, under one prefix, with ACLs blocked and HTTP denied. Section 5.4 of
the Security Architecture Guide covers this case: justify the bucket to IT ISC
rather than remove the access. Keep that approval on file.

## What the account owner cannot fix

- **Hardware MFA on root.** Root is IT-managed (guide section 5.1, "IT
  Management of the root account"). Virtual MFA is already enabled. Replacing it
  with a hardware token needs a root login and a physical device.
- **MFA Delete on S3.** Only the root user with an MFA device can turn it on,
  through the CLI. Versioning, its prerequisite, is enabled on both buckets.
- **`AdministratorAccess` attachments.** The remaining ones are the SSO
  permission set and IT automation roles (`ks-org`, `ks-infosec-automations`,
  `ks-it-managed-automation`, the StackSets execution role). None belong to this
  project.

## Checking the account yourself

```bash
AWS_PROFILE=<admin-profile> bash deploy/scripts/sync-cfn-templates-to-s3.sh
AWS_PROFILE=<admin-profile> bash deploy/scripts/check-launch-buttons.sh
aws s3api get-bucket-policy-status --bucket keysight-cloudlens-templates
aws iam list-entities-for-policy --policy-arn arn:aws:iam::<account>:policy/CloudLensZoneTap
```

A healthy result: the policy status reports the template bucket public, the
zone-tap policy lists a group and a role and no users, and every Launch button
serves over HTTPS while plain HTTP returns 403.
