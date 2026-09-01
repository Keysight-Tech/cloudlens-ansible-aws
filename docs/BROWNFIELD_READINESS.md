# Brownfield readiness: deploying into a customer's existing environment

Status: **plan, not yet implemented.** Written 2026-09-01 from a full audit of
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
(`stack.yaml:402-403`). Every network resource carries `Condition: CreateVpc`, so
a brownfield deploy creates no VPC, no internet gateway, no subnets, no route
table and no associations (conditions on lines 432 through 526). Appliances are
placed with `SubnetId: !If [CreateVpc, !Ref MgmtSubnet, !Ref ExistingSubnetId]`.

**The flags exist** and each has a `CLOUDLENS_*` environment variable form so the
`curl | bash` path works non-interactively: `--existing-vpc-id`,
`--existing-subnet-id`, `--existing-data-subnet-id`, `--existing-sg-id`,
`--no-public-ip` (`deploy-stack.sh:1779-1785`, defaults at `:149-153`). Paired
validation rejects a VPC without a subnet before anything touches AWS.

**A customer-supplied security group is honoured** for the appliances. Setting
`ExistingSecurityGroupId` makes `CreateSecurityGroup` false and every appliance
ENI gets the customer's SG. The template documents the ports they then own:
22, 9022, 443, 4789, 10800-10801 (`stack.yaml:308-316`).

**Workload discovery already has three real methods**, with precedence, in
`render_inventory.py`: an external inventory file wins, then explicit
`instance_ids`, then `tag_filters` (a mapping, all ANDed), narrowed by
`aws.regions`. `quickstart.sh` diagnoses a zero-match run rather than proceeding
against an empty inventory: it reports what was searched, how many instances
exist ignoring every filter, and up to 25 real tag pairs present, then fails.

---

## 2. Three defects that actively mislead the operator

These are not missing features. In each case the tool tells the operator one
thing and does another, which is worse than not supporting brownfield at all.

### 2.1 Terraform ignores brownfield entirely

`--iac terraform --existing-vpc-id vpc-x --existing-subnet-id subnet-y` passes
the pairing validation, prints `Existing VPC: vpc-x` in the resolved-configuration
block, and then builds a **brand new VPC, IGW, subnets and route table** in the
customer's account. `deploy_terraform` passes region, stack name, key name, the
KVO/vPB toggles, AMI and instance-type vars, the vPB SSH port, NIC counts and
admin CIDR. It passes no existing-network variable of any kind.

Silent, wrong and billable.

**Fix:** refuse the combination until the Terraform module supports it. A hard
`fail` in argument validation naming the CloudFormation path as the supported
one. Cheap, and it converts a silent wrong outcome into a clear message.

### 2.2 The brownfield vPB has no data plane

`VpbMultiNic` is ANDed with `CreateVpc`. The moment an existing VPC is used, the
vPB's ingress and egress ENIs are not created and it comes up with a single
management NIC.

The consequences cascade, and none of them announce themselves:

- The vPB cannot receive mirrored traffic or forward to a tool.
- `--wire-vpb-path` has nothing to bind.
- `VPB_INGRESS_IP` is empty, so the mirror phase runs `kvo_aws_mirror.py` with no
  `--tool-remote-ip`, which by design stops after the collection and creates
  **zero mirror sessions**.

A customer buying "vPB plus agentless mirroring" gets a deployment that cannot
work, and the run reports success.

**Fix:** decouple the multi-NIC condition from `CreateVpc`. In brownfield the
operator must supply the ingress and egress subnets (see 3.1), and the ENIs are
created in those. Until then, the combination should warn loudly rather than
proceed quietly.

### 2.3 `--resume` reverts to greenfield

None of `EXISTING_VPC_ID`, `EXISTING_SUBNET_ID`, `EXISTING_DATA_SUBNET_ID`,
`EXISTING_SG_ID` or `ASSIGN_PUBLIC_IP` are written to the state file
(`deploy-stack.sh:2584-2595` records region, stack name, IaC, key pair, the KVO
and vPB toggles, admin user, cloud config, CLM name, vPB device name, discovery
tag and last run, and nothing else).

A resumed run therefore re-enters the stack phase with blank network parameters
against the same stack name.

**Fix:** persist all five, and read them back in `resume_load_inputs`. Note there
is already a dead read there: `state_get SENSOR_MODE` at `:1465-1467` has no
corresponding `state_set` anywhere in the file, so sensor mode is re-asked on
every resume. Do not copy that block when adding new keys.

---

## 3. The practical blocker: discovery finds hosts the playbook cannot target

This is the single biggest gap between "works in the lab" and "works at a
customer".

Discovery by tag or instance id can correctly find a customer's 200 EC2
instances, and `quickstart.sh` will report `Discovered 200 EC2 instance(s)`.
`deploy.yaml` then runs against `ubuntu_prod_vms`, `redhat_prod_vms` and
`windows_prod_vms`, which are defined as `tags.os == 'ubuntu' and tags.env ==
'prod'` and equivalents.

A real fleet has neither tag. Every host lands in `os_untagged`, which has no
`group_vars`: no `ansible_user`, no key, no connection settings. **Discovery
succeeds and the deployment matches nothing.**

**Fix:** stop requiring customer-authored tags to determine OS.

- Derive Windows from the AWS `platform` attribute, which AWS sets itself.
- Derive the Linux flavour from the AMI description or from a cheap connection
  probe, falling back to a prompt rather than to an empty group.
- Drop `env` from the group definitions entirely. It is our lab convention, not
  a customer's.
- Give `os_untagged` real `group_vars` so an unclassified host produces a clear
  error instead of a silent skip.

Related, and smaller: `aws.ssh_key_path` is one path applied to every Linux host.
Real fleets use different keys per application or per team. There is no per-host
override, no ssh-agent path, no bastion or `ProxyJump`, and no reachability
preflight, so one unreachable host fails the whole run.

---

## 4. Let the operator choose how to tap

There is no single question. Three separate gates, roughly 700 lines and several
phases apart:

| Question | Where |
|---|---|
| "Chain to sensor deployment?" | Phase 3, `:2549-2552` |
| Sensor mode: standalone / KVO-managed / none | Phase 10, `:4020-4058` |
| "Set up the AWS mirror fabric?" | Phase 15, `:4725-4731`, default No |

Worse, `phase_applicable` makes `mirror` inapplicable when `DEPLOY_KVO=false`,
and the whole Phase 15 block is wrapped in a KVO check. An operator who answers
"no" to the KVO question is never offered agentless mirroring at all, and is
never told why it vanished.

**Fix:** one question, asked early in Phase 3, with the consequences stated:

```
How should these workloads be tapped?
  1) Sensors        an agent in each guest. Any instance type. Needs SSH or WinRM.
  2) Agentless      AWS VPC Traffic Mirroring. No agent. Nitro instances only.
  3) Both
```

Then derive `CHAIN_SENSORS`, `SENSOR_MODE` and `WITH_MIRROR` from that one
answer, persist it to state, and let the later prompts be confirmations rather
than the first time the operator hears about the option. Choosing agentless must
imply KVO rather than silently removing the option.

---

## 5. Agentless mirroring against an existing environment

### 5.1 The collector needs three real subnets

`kvo_aws_mirror.py` derives the collector's management, ingress and egress
subnets from the lab stack's own instances. With no vPB deployed, ingress and
egress **collapse to the management subnet**, which contradicts the documented
requirement that they be separate with separate IPs (KVO UG p.257, QSG p.70).

There is no `--existing-mgmt-subnet` / `--existing-ingress-subnet` /
`--existing-egress-subnet` input anywhere. For a customer VPC this is the first
thing to add.

### 5.2 Source selection is one tag, and it disagrees with the sensor side

Mirroring forwards a single `--source-tag KEY=VALUE` built from the command line.
KVO itself supports four selectors, all regex-capable: **Instance Name, Instance
ID, Interface ID, Subnet ID** (QSG p.72).

Meanwhile sensors select from `customer_input.yaml`, which supports multiple
ANDed tags, instance ids, or an external inventory. So a customer who selects
workloads by two tags gets sensors on the right hosts and mirror sessions cut
against a different set. `deploy-stack.sh` already computes a `DISCOVERY_MODE` of
`tags|instance-ids|static`; the mirror phase simply ignores it.

**Fix:** one selector definition, consumed by both paths.

### 5.3 The collector management SG is missing TCP 8443

`ensure_collector_sgs` opens 443 and 22 on the management SG. This repo's own
`docs/AWS_ZONE_TAPPING.md:53-71` states that without 8443 from KVO the collector
boots healthy but never registers, KVO never creates the sessions, and the Cloud
Collection change request hangs `InProgress` with no error. The ingress and
egress SGs get a blanket intra-VPC allow that happens to cover it; the management
SG, which is the one KVO calls, does not.

Also: that SG is opened to `ADMIN_CIDR`, whose default is `0.0.0.0/0`. A customer
who does not pass `--admin-cidr` gets 443 and 22 world-open on a collector.

### 5.4 Other items before this is customer-ready

- `EnableZoneTapping` is never passed to CloudFormation, so KVO always needs a
  long-lived static access key even though the template has a working IAM role.
  Blocking for customers who forbid static IAM users.
- The Nitro requirement is never checked. A fleet on t2/m4/c4 produces a complete
  green fabric and zero sessions with no explanation. Add a
  `describe-instance-types` preflight that names which workloads need a sensor
  instead.
- Licence credits on AWS are consumed **per interface**, not per VM (QSG p.72).
  A credit estimate built from a VM count under-provisions on any multi-NIC
  fleet. Nothing checks this.
- Collector fleet size is a fixed `--max-size 4`, capping coverage at 40
  interfaces, rather than being derived from the discovered interface count.
- One Cloud Config equals one VPC and one AZ. Multi-VPC customers, which is the
  normal enterprise case, need one presence, config and collection per VPC and
  the tool cannot express that yet.

---

## 6. Securing the sensor to manager connection

The playbooks are already built for this. **The orchestrator is what blocks it.**
`deploy-stack.sh` hardcodes `registry_type: "insecure"` in both write paths
(`:4296` fresh, `:4354` on merge) and never writes `ssl_verify`, `local_ca_path`
or `ca_cert_dir`. `cloudlens.ssl_verify` is defined in group_vars and consumed by
nothing, because on Linux the sensor's flag is derived from `registry_type`.

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

### 6.3 What to implement

- A `--secure-sensors` flag plus `--ca-cert <path>`, writing `registry_type:
  secure`, `ssl_verify: yes` and the CA path into `customer_input.yaml`.
- Consume `cloudlens.ssl_verify` directly on the Linux path rather than deriving
  it from `registry_type`, so the two can differ.
- Always emit `--ssl_verify` explicitly in generated commands. Never rely on the
  default, given the failure mode is undocumented.
- Preflight the certificate: verify it parses, is not expired, and that its
  subject or SAN matches the manager address the sensors will actually use.

---

## 7. Suggested order

Each stage is independently shippable and useful on its own.

**Stage 1, safety.** The three defects in section 2. These make the tool honest
about what it is doing. Smallest change, highest value, no new features.

**Stage 2, discovery.** Section 3 plus interactive VPC and subnet selection with
a real listing. This is what makes a customer deployment possible at all.

**Stage 3, the tapping question.** DONE: `--tapping sensors|mirror|both|none`
(env `CLOUDLENS_TAPPING`) sets both paths from one answer; interactively it is
a single question replacing the sensor gate and the Phase 15 mirror gate. The
specific flags still win. Selector unification (5.2) remains open: mirror still
takes one `--source-tag` while sensors use the full `customer_input.yaml`
filters. The mirror preflight now reports the Nitro/sensor split, so the gap is
at least stated per run instead of silent.

**Stage 4, mirroring for existing environments.** CORE DONE:
`--collector-zone/-mgmt-subnet/-ingress-subnet/-egress-subnet` and the three
`--collector-*-sg` flags name the placement explicitly and always beat
appliance-derived values; supplied SGs skip creation (no
ec2:CreateSecurityGroup needed) and `--no-create-collector-sgs` makes a gap an
input error. kvo_aws_mirror.py validates the trio (exists, tapped VPC, AZ
match, pairwise distinct) before any KVO write, refuses a shared SG, supports
multi-AZ via repeatable `--zone-spec`, derives `--max-size` from the matched
interface count, and reports Nitro eligibility, AZ coverage and licence demand
up front. The silent mgmt-subnet collapse is deleted. Open: per-VPC config
loops for tapping a VPC other than the stack's own (`source_vpc_ids`), and the
instance-id selector form for ANDed tag filters.

**Stage 5, the certificate path.** PLUMBING DONE, still gated on lab proof:
`--secure-sensors` / `--ca-cert` write the TLS choice into customer_input.yaml
with a certificate preflight (parse/expiry hard-fail, name-mismatch warning),
and the Linux plays consume `ssl_verify` directly instead of deriving it from
`registry_type`. Do not recommend to a customer until proven end to end,
including a deliberate verification failure.

---

## 8. Preflight checks worth adding regardless

Nothing today validates the customer's environment before creating anything:

- The named subnet is actually in the named VPC.
- The data subnet is in the same AZ as the management subnet. An AZ mismatch
  makes multi-NIC attachment impossible and puts the collector in the wrong zone.
- The supplied security group belongs to that VPC and opens the ports the
  template says the customer now owns.
- The subnet has a route to the internet, or the required VPC endpoints exist,
  since `--no-public-ip` with no NAT leaves the appliances unable to reach
  anything.
- The caller has permission to create security groups in that VPC.
- The region is real. Today an unsupported region only warns and a typo is caught
  much later, or falls back to hardcoded us-east-1 AMI ids.
- `--admin-cidr` parses as a CIDR. It is currently spliced verbatim into
  `authorize-security-group-ingress` calls.

One more, from section 3: when discovery matches nothing, the current remedy is
to offer to launch three throwaway EC2 instances **into the customer's own
subnet**, with a security group opening 22 and 3389 from a CIDR that defaults to
`0.0.0.0/0`. That behaviour belongs in a lab and must be disabled whenever an
existing VPC is in use.
