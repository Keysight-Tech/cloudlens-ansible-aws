# Brownfield readiness: deploying into a customer's existing environment

Status: **proven live 2026-10-07** against a customer-shaped environment the
deploy did not build (VPC 10.60.0.0/16 with management, data and tool subnets in
one zone, two tagged workloads, an existing EKS cluster,
`--vpb-rails mirror,k8s`): both vPB rails and the VM sensors passed (5,516 GRE
packets at the tool from the mirror and stack-vPB paths, 3,929 passed on the k8s
vPB with pod-to-pod HTTP inside its tunnel). Stages 1-4 are done (section 7);
stage 5 (sensor TLS with a customer certificate) still needs a lab proof.
Brownfield is CloudFormation only: `--iac terraform` with any `--existing-*` flag
is refused. Sections 2 to 5 are kept as the record of what was wrong and what
fixed it. Originally written 2026-09-01 from a full audit of
`deploy/deploy-stack.sh`, `deploy/cloudformation/stack.yaml`,
`scripts/render_inventory.py`, `scripts/kvo_aws_mirror.py`, `quickstart.sh`, the
playbooks and group_vars, cross-read against the CloudLens vController User Guide
v6.14.1, the KVO 3.0.1 User Guide, and the CloudLens-KVO Integration Quick Start
Guide v1.13.0.

"Brownfield" here means the customer already has a VPC, subnets, security groups
and running EC2 workloads. We deploy visibility into what they have. We do not
build them a lab.

---

## 1. What already works

Worth stating first, because the plumbing is further along than it looks and
none of it needs rebuilding.

**CloudFormation genuinely honours an existing network.** `ExistingVPC` is true
when `ExistingVpcId` is non-empty and `CreateVpc` is its negation
(`stack.yaml:426-428`). Every network resource carries `Condition: CreateVpc`, so
a brownfield deploy creates no VPC, no internet gateway, no subnets, no route
table and no associations (conditions on lines 467 through 561). Appliances are
placed with `SubnetId: !If [CreateVpc, !Ref MgmtSubnet, !Ref ExistingSubnetId]`
(`stack.yaml:678`, `:724`) and the vPB ingress ENI uses `ExistingDataSubnetId`
the same way (`:1004`).

**The template takes the existing VPC's CIDR.** `ExistingVpcCidr` makes the
security group the template creates open the in-VPC ports (443, 7443, GRE (IP
protocol 47), UDP 4789, UDP 10800-10801) to that range instead of the new-VPC
`VpcCidr` (`stack.yaml:296-306`, `:626-646`). `deploy-stack.sh` reads it from
the VPC with `describe-vpcs` and fails if it cannot (`:4360-4369`). Seen live 2026-10-07:
nothing in a 10.60/16 VPC could reach appliances whose rules said 10.99/16, and
KVO could not discover the vController.

**The flags exist** and each has a `CLOUDLENS_*` environment variable form so the
`curl | bash` path works non-interactively: `--existing-vpc-id`,
`--existing-subnet-id`, `--existing-data-subnet-id`, `--existing-tool-subnet-id`,
`--existing-sg-id`, `--no-public-ip` (`deploy-stack.sh:2343-2348`, defaults at
`:278-284`). Validation before anything touches AWS rejects a VPC without a
subnet, a data subnet without a tool subnet (and the reverse), and the Terraform
engine with any `--existing-*` flag (`:2504-2540`). All three subnets must be in
the same availability zone; nothing checks that yet (section 8).

**A customer-supplied security group is honoured** for the appliances. Setting
`ExistingSecurityGroupId` makes `CreateSecurityGroup` false and every appliance
ENI gets the customer's SG. The template documents the ports they then own:
22, 9022, 443, 7443, GRE (IP protocol 47), UDP 4789 and UDP 10800-10801 (the `ExistingSecurityGroupId` description in `stack.yaml`). With `--existing-sg-id`
the template's Admin source CIDR no longer applies to the appliances and the
interview does not ask for it (`deploy-stack.sh:3502-3509`); `--admin-cidr` still
scopes the SSH rules on the collector and capture-host groups the deploy creates
later (`:5362-5363`, `:5434`).

**Workload discovery already has three real methods**, with precedence, in
`render_inventory.py`: an external inventory file wins, then explicit
`instance_ids`, then `tag_filters` (a mapping, all ANDed), narrowed by
`aws.regions`. `quickstart.sh` diagnoses a zero-match run rather than proceeding
against an empty inventory: it reports what was searched, how many instances
exist ignoring every filter, and up to 25 real tag pairs present, then fails.

**Proven live in an existing VPC on 2026-10-07.** The shape that passed, against
an existing EKS cluster and pre-existing tagged workloads (VM sensors were proven
afterwards with `--only sensors`, so start with `--tapping both`):

```bash
CLOUDLENS_WORKLOAD_CHOICE=existing CLOUDLENS_IAC=cfn \
CLOUDLENS_MIRROR_ACCESS_KEY=<AK> CLOUDLENS_MIRROR_SECRET_KEY=<SK> \
bash deploy/deploy-stack.sh --stack-name <stack> --region us-east-1 \
  --key-name <key> --admin-cidr A.B.C.D/32 \
  --existing-vpc-id vpc-... --existing-subnet-id <mgmt> \
  --existing-data-subnet-id <data> --existing-tool-subnet-id <tool> \
  --with-kvo --with-vpb --kvo-codes <code> --enable-zone-tapping \
  --tapping both --with-mirror --sensor-mode kvo \
  --eks-cluster <cluster> --eks-mode daemonset --eks-sensor-tar CloudLens-Sensor-<ver>.tar \
  --bootstrap-vpb --adopt-vpb --wire-vpb-path --vpb-rails mirror,k8s
```

`--wire-vpb-path` must be passed: interactively it defaults to No, and without it
the vPB path is never wired.

**Sensors in an existing VPC register to the vController's PRIVATE address.**
`customer_input.yaml` used to carry the public one; from inside the customer's
VPC `docker pull <public>/sensor` leaves through the internet gateway and returns
from the workload's public IP, which the security group refuses, while the
private address answered at once. The deploy now writes `CLMS_PRIVATE_IP` and
`CLOUDLENS_SENSOR_MANAGER_ADDR` overrides it for workloads that live elsewhere
(`deploy-stack.sh:6137-6139`). An EKS cluster in the same VPC gets the private
address the same way (`:6618-6625`). `quickstart.sh` treats a private manager
address it cannot reach from the operator machine as a warning, not a stop: the
hosts do the pull.

---

## 2. Three defects that misled the operator (all fixed)

Each of these made the tool report one thing and do another. They are recorded
here with what fixed them.

### 2.1 Terraform refuses brownfield (fixed)

`--iac terraform` combined with any `--existing-*` flag used to pass validation,
print `Existing VPC: vpc-x`, and then build a brand new VPC, IGW, subnets and
route table in the customer's account, because the Terraform module has no
existing-network variables. Argument validation now fails hard: "The Terraform
engine does not support deploying into existing infrastructure. It would ignore
the --existing-* flags and create a new VPC instead. Use the CloudFormation
engine for brownfield" (`deploy-stack.sh:2511-2520`). Brownfield is
CloudFormation only.

### 2.2 The brownfield vPB gets its data plane when both subnets are named (fixed)

`VpbMultiNic` used to be ANDed with `CreateVpc`, so every existing-VPC deploy got
a management-only vPB: nothing for `--wire-vpb-path` to bind, an empty
`VPB_INGRESS_IP`, a mirror phase that ran `kvo_aws_mirror.py` with no
`--tool-remote-ip` and cut zero sessions, and a run that reported success.

It is now `DeployVPB AND (CreateVpc OR BrownfieldDataPlane)`, where
`BrownfieldDataPlane` is true when both `ExistingDataSubnetId` and
`ExistingToolSubnetId` are set (`stack.yaml:437-460`). Supply
`--existing-data-subnet-id` and `--existing-tool-subnet-id` together and the
ingress and egress ENIs land in those subnets. The deploy refuses one without the
other as an input error (`deploy-stack.sh:2528-2537`), and the help states that
without both the vPB comes up management-only and cannot forward traffic. The
three subnets must share one availability zone.

### 2.3 `--resume` keeps the network choice (fixed)

A resumed run used to re-enter the stack phase with blank network parameters
against the same stack name, which would have redeployed it as greenfield and
built a new VPC in the customer's account. The state file now records
`EXISTING_VPC_ID`, `EXISTING_SUBNET_ID`, `EXISTING_DATA_SUBNET_ID`,
`EXISTING_TOOL_SUBNET_ID`, `EXISTING_SG_ID` and `ASSIGN_PUBLIC_IP`, written even
when empty so a resume cannot inherit a value from an earlier, different run,
together with the collector placement (`COLLECTOR_ZONE`, the three collector
subnets and the three collector SGs) (`deploy-stack.sh:3988-4000`);
`resume_load_inputs` reads them back (`:1857-1865`).

Still open: `state_get SENSOR_MODE` (`:1871`) has no matching `state_set`
anywhere in the file, so a resumed run with sensors on and a KVO asks the sensor
mode again in the sensor-mode step (Phase 10, which runs before the sensors
phase, Phase 13) unless `--sensor-mode` or `CLOUDLENS_SENSOR_MODE` (which a
saved profile carries) supplies it. `--tapping` does not set it.

---

## 3. Discovery finds hosts the playbook can target (fixed)

A real fleet has no `os` tag and no `env` tag. Discovery used to find the
customer's 200 instances, `deploy.yaml` then targeted `ubuntu_prod_vms`,
`redhat_prod_vms` and `windows_prod_vms` (defined on `tags.os` and `tags.env`),
every host landed in `os_untagged` with no `group_vars`, and the run reported
success having installed nothing.

`inventory/aws_ec2.yaml` now classifies without customer tags: `os_windows` from
the AWS `platform` attribute, `os_rhel` and `os_ubuntu` from `platform_details`,
and every other Linux host into `cloudlens_linux_unknown`, which
`playbooks/classify.yaml` probes and sorts into `os_ubuntu` or `os_rhel` with the
right user. A `tags.os` value still wins when present, and the AWS-classified
hosts join the same `os_*` groups so they inherit the connection vars in
`inventory/group_vars/os_*.yaml`. `deploy.yaml` imports `classify.yaml` first
and targets `os_ubuntu`, `os_rhel` and `os_windows`; the `*_prod_vms`,
`*_dev_vms` and `*_test_vms` groups are kept for existing labs
(`deploy.yaml:27-57`). `env` no longer decides anything.

Related, and smaller: `aws.ssh_key_path` is one path applied to every Linux host.
Real fleets use different keys per application or per team. There is no per-host
override, no ssh-agent path, no bastion or `ProxyJump`, and no reachability
preflight, so one unreachable host fails the whole run.

---

## 4. One question decides how to tap (done)

There used to be three separate gates several phases apart: "Chain to sensor
deployment?" in Phase 3, the sensor mode in the sensor-mode step (Phase 10,
ahead of the sensors phase, Phase 13), and "Set up the AWS mirror fabric?" in
the mirror phase (Phase 15, `--from mirror`)
with a default of No. An operator who declined KVO was never offered agentless
mirroring at all, and never told why it vanished.

`--tapping sensors|mirror|both|none` (env `CLOUDLENS_TAPPING`,
`deploy-stack.sh:2279`) derives `CHAIN_SENSORS` and `WITH_MIRROR` from one
answer (`:2426-2437`). Interactively it is a single question in Phase 3
(`:3565-3572`):

```
How should the workloads be tapped?
  1) sensors    an agent inside each VM. Works on any instance type,
                Linux and Windows. This is the default.
  2) mirroring  agentless AWS VPC Traffic Mirroring. Nitro instances
                only; needs KVO and an AWS access key for it.
  3) both       sensors where possible plus the mirror fabric.
  4) none       infrastructure only, no tapping.
```

The specific flags (`--sensor-mode`, `--with-mirror`, `--no-sensors`) still win
when given, and the management plane (standalone or KVO-managed) is a separate
question asked right after. Agentless mirroring needs KVO: `phase_applicable`
keeps `mirror` inapplicable when `DEPLOY_KVO=false`, so pass `--with-kvo` with
`--tapping mirror` or `both`.

---

## 5. Agentless mirroring against an existing environment

### 5.1 The collector's three subnets are named explicitly (done)

`kvo_aws_mirror.py` used to derive the collector's management, ingress and egress
subnets from the lab stack's own instances, and with no vPB deployed ingress and
egress collapsed onto the management subnet. That collapse is deleted.
`--collector-zone`, `--collector-mgmt-subnet`, `--collector-ingress-subnet` and
`--collector-egress-subnet` name the placement for an existing VPC (env
`CLOUDLENS_COLLECTOR_*`), the interview lists the VPC's subnets with AZ, CIDR and
public-IP flag to pick from when mirroring into an existing VPC
(`deploy-stack.sh:3792-3810`), and the script validates the trio (exists, in the
tapped VPC, one AZ, pairwise distinct) before the cloud config is committed
(`scripts/kvo_aws_mirror.py:143-192`; the presence, idempotent on name, is
created first). KVO requires three distinct subnets in one availability zone (KVO
UG p.257, QSG p.70); with fewer the fabric commits cleanly and cuts zero
sessions.

### 5.2 Source selection is one tag, and it disagrees with the sensor side

> **DONE.** `scripts/workload_selection.py` is now the one implementation both
> paths import; `scripts/resolve_workloads.py` resolves it into the shared
> manifest (`inventory/generated.workloads.json`) the mirror consumes via
> `--selection`. The rest of this section describes the state it replaced.

Mirroring forwards a single `--source-tag KEY=VALUE` built from the command line.
KVO itself supports four selectors, all regex-capable: **Instance Name, Instance
ID, Interface ID, Subnet ID** (QSG p.72).

Meanwhile sensors select from `customer_input.yaml`, which supports multiple
ANDed tags, instance ids, or an external inventory. So a customer who selects
workloads by two tags gets sensors on the right hosts and mirror sessions cut
against a different set. `deploy-stack.sh` already computes a `DISCOVERY_MODE` of
`tags|instance-ids|static`; the mirror phase simply ignores it.

**Fix:** one selector definition, consumed by both paths.

### 5.3 Collector management security group (resolved)

TCP 8443 is not required. No Keysight document lists it for the collector (a
scan of the indexed corpus finds 8443 only in the physical Vision E10S/E40
guides), and both proven causes of the zero-sessions symptom were elsewhere: an
incomplete cloud config with no availability zones, and a presence reused
without its key so the collector never relaunched (`docs/AWS_ZONE_TAPPING.md`,
collector security group ports).

`ensure_collector_sgs` now opens 443 from the VPC CIDR only, since the control
plane is in-VPC, and 22 and 9022 from the VPC CIDR and the admin CIDR, because
KVO configures the collector over SSH from inside the VPC: with 22/9022
admin-only the collector booted, registered its mirror target and cut zero
sessions for ever with no alert (seen live 2026-09-28)
(`deploy-stack.sh:5353-5363`). The ingress and egress SGs admit UDP 4789 and GRE
from the VPC CIDR only. One residue: when neither `--admin-cidr` nor the
interview supplies a CIDR, 22 and 9022 fall back to `0.0.0.0/0`; an interactive
run asks and offers this machine's /32.

### 5.4 Other items, now in place

- `--enable-zone-tapping` (env `CLOUDLENS_ENABLE_ZONE_TAPPING`) passes
  `EnableZoneTapping=yes` to CloudFormation and `enable_zone_tapping=true` to
  Terraform (`deploy-stack.sh:4352`, `:4424`). The AWS presence still needs an
  access key pair (`--mirror-access-key` / `--mirror-secret-key`):
  `createAwsPresence` rejects an empty `accessKeyId`, so the instance profile is
  supplementary, not a replacement. Customers who forbid static IAM users remain
  blocked on the KVO side.
- Nitro is checked. `kvo_aws_mirror.py` classifies every matched source with
  `describe-instance-types` Hypervisor and lists the non-Nitro hosts that need a
  sensor instead (`scripts/kvo_aws_mirror.py:233-245`).
- Licence demand is reported per tapped interface, not per VM (QSG p.72),
  together with AZ coverage: interfaces in an AZ with no collector are named as
  untapped (`:195-250`).
- The collector fleet ceiling is derived from the matched interface count (10
  per Service VM, plus one spare); `--collector-max N` overrides it and a
  too-small value warns with the number to pass (`:776-786`).
- Multi-VPC: `--source-vpc-id` and
  `--source-vpc vpc:az:mgmt:ingress:egress[:sgs]` are repeatable,
  `aws.source_vpc_ids` in customer_input.yaml does the same, and one presence,
  config and collection is built per VPC (`aws-mirror-<vpcid>`; the stack VPC
  keeps `aws-mirror`).

---

## 6. Securing the sensor to manager connection

The playbooks and the orchestrator are now built for this. `--secure-sensors`
writes `registry_type: secure` and `ssl_verify: yes` into `customer_input.yaml`,
`--ca-cert PATH` adds the CA path and implies `--secure-sensors`, and the
certificate is preflighted (parse and expiry hard-fail, name-mismatch warning)
(`deploy-stack.sh:6238-6243`). The default stays `registry_type: insecure`. The
Linux plays consume `cloudlens.ssl_verify` directly instead of deriving it from
`registry_type`. What blocks a customer recommendation is the missing lab proof
in 6.2, not the plumbing.

### 6.1 What the documentation specifies

- **Linux:** mount the CA and turn verification on.
  `-v <path/to/ca>:/usr/local/share/ca-certificates:ro` (vController UG p.66) and
  `--ssl_verify yes` (p.68). **The default is already `yes`.**
- **And on the manager:** import an X.509 certificate plus private key on the
  admin page "Import TLS Certificate" (p.176). The sensor documentation makes
  this an explicit prerequisite. Hard constraint: **the certificate must use TLS
  1.2 or less.**
- **Windows:** `SSL_Verify="yes"` in the silent install (pp.61-62). Note the
  fork: a standalone vController deployment uses `Project_Key`, a KVO-managed
  Custom Cloud uses `Access_Key` (KVO UG pp.240-241).
- **Ports:** sensor to manager is **TCP 443 only**, inbound and outbound (p.73).

### 6.2 Documentation gaps that must be closed in the lab first

Being straight about this: the parameter is documented, the flow is not.

- **There is not one worked example of `--ssl_verify yes` in the entire Keysight
  corpus.** Every example in every guide and every version uses `no`.
- The CA source is never specified: not the format, not the filename, not even
  whether `<path/to/ca>` is a file or a directory. The container target is a
  directory following Debian convention, which implies a directory of PEM `.crt`
  files, but the documentation never says so.
- **Windows has no documented CA procedure at all.** One sentence, no certificate
  store named, no command, and `agent.yml` has no CA-path key.
- The **Helm chart exposes no key** for a manager CA. `customDtlsCa` is the DTLS
  data-tunnel CA, a different thing.
- The failure mode is never stated for the default configuration: verification on
  with no CA mounted. No log signature is documented.
- Nothing documents the appliance's out-of-box certificate, its CN or SAN, or
  whether verification can work against an IP rather than an FQDN. The guide's
  advice to "use FQDN for sensors whenever possible" (p.58) is almost certainly
  the workaround, but the two are never linked.

**Therefore:** implement the plumbing, but do not ship the certificate path to a
customer until it has been proven end to end in the lab, including a deliberate
negative test where verification is expected to fail.

### 6.3 What is implemented

- `--secure-sensors` plus `--ca-cert <path>` write `registry_type: secure`,
  `ssl_verify: yes` and the CA path into `customer_input.yaml`.
- The Linux path consumes `cloudlens.ssl_verify` directly
  (`inventory/group_vars/all.yaml:29`), deriving it from `registry_type` only
  when it is unset, so the two can differ.
- Every generated sensor command passes verification explicitly:
  `--ssl_verify {{ ssl_verify }}` in `playbooks/ubuntu.yaml:198` and
  `playbooks/redhat.yaml:258`, `:310`; `SSL_Verify` in the Windows silent install
  (`playbooks/windows.yaml:273`). The EKS DaemonSet still hardcodes
  `--ssl_verify no` (`scripts/deploy-eks-tapping.sh:345`).
- The certificate is preflighted: a parse or expiry failure stops the run, a
  subject or SAN that does not match the manager address warns
  (`deploy-stack.sh:6095-6126`).
- Not yet done: the lab proof including the deliberate negative test. Do not
  recommend the certificate path to a customer until that proof exists.

---

## 7. Suggested order

Each stage is independently shippable and useful on its own.

**Stage 1, safety.** DONE: the three defects in section 2 are fixed (Terraform
refusal, brownfield data plane, resume persistence).

**Stage 2, discovery.** DONE: OS classification from AWS attributes plus
`playbooks/classify.yaml` (section 3), and the interview lists the VPC's subnets
with AZ, CIDR and public-IP flag to pick from (`deploy-stack.sh:3409-3411`,
`:3532-3535`). Still open from section 3: one SSH key for every Linux host, no
bastion or per-host override, no reachability preflight.

**Stage 3, the tapping question.** DONE: `--tapping sensors|mirror|both|none`
(env `CLOUDLENS_TAPPING`) sets both paths from one answer; interactively it is
a single question replacing the sensor gate and the mirror-phase gate. The
specific flags still win. Selector unification (5.2) is DONE: both paths
resolve the selection through `scripts/workload_selection.py`, and the mirror
consumes the resolved per-VPC manifest via `--selection` (ANDed tags, explicit
ids and exclusions become a literal instance-id selector; the proven single-tag
form is kept where exactly equivalent).

**Stage 4, mirroring for existing environments.** CORE DONE:
`--collector-zone/-mgmt-subnet/-ingress-subnet/-egress-subnet` and the three
`--collector-*-sg` flags name the placement explicitly and always beat
appliance-derived values; supplied SGs skip creation (no
ec2:CreateSecurityGroup needed) and `--no-create-collector-sgs` makes a gap an
input error. kvo_aws_mirror.py validates the trio (exists, tapped VPC, AZ
match, pairwise distinct) before any KVO write, refuses a shared SG, supports
multi-AZ via repeatable `--zone-spec`, derives `--max-size` from the matched
interface count, and reports Nitro eligibility, AZ coverage and licence demand
up front. The silent mgmt-subnet collapse is deleted. The per-VPC loop is
DONE too: `--source-vpc-id` / `--source-vpc vpc:az:mgmt:ingress:egress[:sgs]`
(or `aws.source_vpc_ids` in customer_input.yaml) build one fabric per tapped
VPC (`aws-mirror-<vpcid>`; the stack VPC keeps `aws-mirror`), each with its own
placement and VPC-local SGs, and `vpb_wire_path.py` links the C2DL to every
AWS config. The instance-id selector form ships with the resolver.

**Stage 5, the certificate path.** PLUMBING DONE, still gated on lab proof:
`--secure-sensors` / `--ca-cert` write the TLS choice into customer_input.yaml
with a certificate preflight (parse/expiry hard-fail, name-mismatch warning),
and the Linux plays consume `ssl_verify` directly instead of deriving it from
`registry_type`. Do not recommend to a customer until proven end to end,
including a deliberate verification failure.

---

## 8. Preflight checks

Done:

- The region is validated. `--doctor` checks it is reachable with the
  credentials and present in the CloudFormation RegionMap
  (`deploy-stack.sh:2782-2789`), and the stack phase fails hard on a region with
  no RegionMap entry instead of falling back to us-east-1 AMI ids (`:4384`).
- `--doctor` also checks credentials, Marketplace subscriptions, Elastic IP
  headroom, the key pair, tooling and network before anything is created, and
  prints the fix for each gap. Run it first.
- An `--admin-cidr` answered interactively is parsed with `valid_cidr` (`:3483`).
  A value given by flag or `CLOUDLENS_ADMIN_CIDR` is still spliced into
  `authorize-security-group-ingress` unvalidated (`:2335`).

Still missing for an existing VPC:

- The named subnets are actually in the named VPC.
- The data and tool subnets are in the same AZ as the management subnet. The
  help states the rule; nothing checks it, and an AZ mismatch makes multi-NIC
  attachment impossible.
- The supplied security group belongs to that VPC and opens the ports the
  template says the customer now owns.
- The subnet has a route to the internet, or the required VPC endpoints exist,
  since `--no-public-ip` with no NAT leaves the appliances unable to reach
  anything.
- The caller has permission to create security groups in that VPC.

One more, from section 3: when discovery matches nothing, the deploy still offers
(default No) to launch three throwaway EC2 instances into `--existing-subnet-id`
when one is set (`deploy-stack.sh:5274`), with a security group opening 22, 5985
and 3389 from `0.0.0.0/0` (`scripts/deploy-test-workload-vms.sh:167-171`). That
behaviour belongs in a lab and should be disabled whenever an existing VPC is in
use.

---

## 9. Rehearsal and teardown

`scripts/lab/brownfield-fixture.sh create|status|env|destroy` builds the
customer side for a repeatable proof: a VPC (default 10.60.0.0/16) with
management, data and tool subnets in one zone plus a second-zone subnet for EKS,
a key pair, two tagged workloads talking HTTP to each other, and an EKS cluster
running the same web + loadgen app the sample cluster uses. Nothing in it is
CloudLens. Every resource carries `cloudlens:lab=<name>` so `status` and
`destroy` find it by tag; `create` is idempotent; `env` prints the ids as shell
exports for the `--existing-*` and `--eks-cluster` flags. `destroy` only when
the operator says so.

`deploy/teardown-stack.sh` runs in a fixed order. The extra `--vpb-rails` vPBs
and their Elastic IPs are terminated by tag BEFORE the stack delete, because
they hold the stack security group and otherwise leave the stack in
DELETE_FAILED (seen 2026-10-07) (`:1349-1388`). The sweep follows the delete
in a fixed order: any rail vPB that appeared in between, the instances the
deploy stamped, the collector ASGs and their launch templates, the mirror
targets and filter, the security groups, and last the volumes (only those now
`available`). A stamped resource is one tagged with the stack name or named
`cloudlens-*-<stack>`, and the sweep finds it inside a customer VPC as much as
in a stack-built one. In a pre-existing VPC only the VPC itself and the
resources the deploy did not stamp are left alone: a customer security group,
an unattached volume or an ASG that carries no stack tag is reported, never
deleted, and that VPC is recorded for reporting only (`teardown-stack.sh:424`,
`:625-662`).

`bash deploy/deploy-stack.sh --profile FILE` replays a saved interview so a
second operator deploys the same shape; the plan step writes
`deploy-profile-<stack>.env` after any interview.
