# Discovering what to tap: `--discover`

Every `--source-vpc` spec used to be typed by hand:
`vpc:az:mgmt-subnet:ingress-subnet:egress-subnet`. That is exact, and it is
the one step that stops a 100-VPC estate from being automated. Discovery
produces those specs from the discovery tag, so a new VPC or a new account is
one re-run, not a support call.

## What it does (read-only)

`scripts/discover_workloads.py` runs describe and list calls only. For each
account in scope and each region:

1. Running instances carrying the discovery tag (default `cloudlens=yes`),
   grouped by VPC. Nitro or not comes from `describe-instance-types`, because
   VPC Traffic Mirroring silently skips non-Nitro sources. Non-Nitro hosts are
   listed by id: they are the sensor path's job.
2. For each VPC with matches, the collector placement:
   - subnets tagged `cloudlens:role=mgmt`, `ingress` and `egress` in one
     availability zone win;
   - otherwise the AZ holding most tapped instances, and three distinct
     subnets in it, private ones first.
   Fewer than three subnets in that AZ means the VPC is reported as
   INCOMPLETE with what to create. It is never guessed into a broken fabric.
3. `inventory/discovered.json`, a table on the terminal, and one replayable
   profile per account and region (`deploy-profile-discovered-<account>-<region>.env`,
   mode 600, allowlisted keys only: `CLOUDLENS_REGION`,
   `CLOUDLENS_DISCOVERY_TAG_KEY`, `CLOUDLENS_DISCOVERY_TAG_VALUE`,
   `CLOUDLENS_SOURCE_VPCS`). Replay one with
   `bash deploy/deploy-stack.sh --profile deploy-profile-discovered-<account>-<region>.env`;
   every question the profile answers is skipped, and the rest are asked as
   usual.

## From the deploy

```bash
# the interview: answer "find" when asked which VPCs the workloads live in
curl -sSL https://raw.githubusercontent.com/Keysight-Tech/cloudlens-ansible-aws/main/deploy/deploy-stack.sh | bash

# no questions: scan two regions of this account and tap every complete proposal
curl -sSL https://raw.githubusercontent.com/Keysight-Tech/cloudlens-ansible-aws/main/deploy/deploy-stack.sh | \
  bash -s -- --region us-east-1 --with-kvo --with-vpb --tapping mirror --with-mirror \
             --discover --discover-regions us-east-1,us-east-2

# the whole organization, through the role the management account can assume
... --discover --discover-accounts organization --discover-role OrganizationAccountAccessRole
```

Every complete proposal becomes a `--source-vpc` spec; a VPC the operator
already named is never duplicated. Incomplete VPCs are listed with the reason,
and the deploy continues with the rest.

## Standalone

```bash
python3 scripts/discover_workloads.py --regions us-east-1,us-east-2 --tag cloudlens=yes \
    --accounts organization --role OrganizationAccountAccessRole \
    --out inventory/discovered.json --profile-dir .
```

Other flags: `--subnet-role-tag KEY` changes the subnet tag that names the
collector role (default `cloudlens:role`); `--print-specs` prints one complete
`--source-vpc` spec per line on stdout and nothing else, for piping into
another tool.

Exit codes: 0 at least one complete spec, 3 nothing tappable, 2 bad input,
4 AWS unreachable for the calling identity.

## What it needs

- `ec2:Describe*` in every scanned account and region; `sts:GetCallerIdentity`.
- Organization mode: `organizations:ListAccounts` (management account or a
  delegated administrator) and `sts:AssumeRole` on the role in each member
  account. A refused ListAccounts scans this account only and says so; an
  account whose role cannot be assumed is reported, not skipped silently.
- KVO still needs its own credentials per account for the mirror phase (an
  instance role is not enough: `createAwsPresence` fails with "accessKeyId
  cannot be empty"). Pass them to the deploy as `--mirror-access-key` /
  `--mirror-secret-key` (env `CLOUDLENS_MIRROR_ACCESS_KEY` /
  `CLOUDLENS_MIRROR_SECRET_KEY`). Discovery finds the VPCs; it does not
  create the presence.

## Test

`bash deploy/tests/test_discover.sh` (hermetic: a stubbed `aws` CLI, nothing
leaves the machine). It pins every rule above and the `run_discovery` hook in
`deploy-stack.sh`.
