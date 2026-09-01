#!/usr/bin/env python3
"""
The ONE implementation of "customer_input.yaml -> which workloads".

Why this exists
---------------
The sensor path (render_inventory.py -> the aws_ec2 dynamic inventory) and the
agentless mirror path (kvo_aws_mirror.py -> a KVO cloud collection) each grew
their own answer to "which hosts did the customer select". The sensor path
honoured aws.tag_filters / aws.instance_ids / aws.vpc_id; the mirror path took
one --source-tag and ignored the file entirely. A customer selecting on two
ANDed tags got sensors on the right hosts and mirror sessions on a different
set, with nothing anywhere saying the two had diverged.

Both consumers now import THIS module, so the selection can only be computed
one way. render_inventory.py turns the result into aws_ec2 inventory filters;
resolve_workloads.py turns the same result into EC2 describe-instances filters
and a KVO resourceSelector.

What customer_input.yaml can set (all under "aws:"):

  regions:          list of regions to scan.
  tag_filters:      mapping of tag name -> value. ALL must match (AND).
  instance_ids:     explicit instance ids. Wins over tag_filters.
  inventory_file:   pre-existing Ansible inventory; skips AWS discovery.
  vpc_id:           the VPC the stack built (written by deploy-stack.sh).
  source_vpc_ids:   the VPC(s) to TAP with mirroring. Defaults to [vpc_id].
                    An existing-environment customer whose workloads live in
                    a different VPC than the CloudLens appliances names it
                    here (or with deploy-stack.sh --source-vpc-id).
  tapping:
    exclude_instance_ids:  instances to leave alone on BOTH paths.
"""
from __future__ import annotations


def tag_value(value):
    """YAML 1.1 reads bare yes/no as booleans; AWS tag values are strings.
    Render them back the way the operator typed them."""
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "" if value is None else str(value)


def as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v is not None and str(v) != ""]
    return [str(value)]


def load_aws_block(path):
    """The aws: mapping from customer_input.yaml, or {} when absent/unparseable."""
    import os
    import yaml
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path) as fh:
            doc = yaml.safe_load(fh)
    except Exception:
        return {}
    if not isinstance(doc, dict):
        return {}
    aws = doc.get("aws")
    return aws if isinstance(aws, dict) else {}


def build_selection(aws, base_filters=None):
    """Resolve the aws: block into one selection description.

    Returns a dict:
      mode           'static' | 'instance-ids' | 'tags' | 'default'
      static_path    only for mode 'static': the operator's inventory file
      filters        {filter-name: value-or-list} in EC2 filter vocabulary
                     (tag:K, instance-id, instance-state-name, vpc-id)
      described      one human line saying what was selected
      regions        list (possibly empty)
      source_vpc_ids VPC(s) to tap with mirroring ([] = wherever discovery
                     already looks). Defaults to [vpc_id] when vpc_id is set.
      exclude_ids    instance ids to leave alone on both paths

    base_filters is the checked-in inventory's filters mapping; mode 'default'
    means "no customer selection at all: behave exactly like that file".
    """
    if not isinstance(aws, dict):
        aws = {}
    base_filters = dict(base_filters or {})

    static = aws.get("inventory_file")
    if static:
        return {"mode": "static", "static_path": str(static), "filters": {},
                "described": "the inventory file %s" % static,
                "regions": as_list(aws.get("regions")),
                "source_vpc_ids": [], "exclude_ids": []}

    tapping = aws.get("tapping") if isinstance(aws.get("tapping"), dict) else {}
    exclude_ids = as_list(tapping.get("exclude_instance_ids"))

    filters = {"instance-state-name": base_filters.get("instance-state-name", "running")}
    instance_ids = as_list(aws.get("instance_ids"))
    tag_filters = aws.get("tag_filters")
    if not isinstance(tag_filters, dict):
        tag_filters = {}

    if instance_ids:
        mode = "instance-ids"
        filters["instance-id"] = instance_ids
        described = " ".join(instance_ids)
    elif tag_filters:
        mode = "tags"
        described_parts = []
        for key, value in tag_filters.items():
            rendered = tag_value(value)
            filters["tag:%s" % key] = rendered
            described_parts.append("tag:%s=%s" % (key, rendered))
        described = " ".join(described_parts)
    else:
        mode = "default"
        filters = dict(base_filters)
        described = " ".join(
            "%s=%s" % (k, tag_value(v)) for k, v in base_filters.items() if k.startswith("tag:")
        )

    # Confine discovery to the VPC(s) the workloads live in. Two sources:
    #   aws.vpc_id          the VPC the stack built (written by deploy-stack.sh)
    #   aws.source_vpc_ids  where the customer's workloads actually are
    # source_vpc_ids wins when set: an existing-environment customer whose
    # workloads sit outside the CloudLens VPC needs BOTH paths, sensors and
    # mirror alike, to look there, or the two tap different hosts.
    # (Why confinement exists at all: tag discovery is region-wide and two
    # stacks share the default tag; a second stack's run found the FIRST
    # stack's workloads live.)
    vpc_id = aws.get("vpc_id")
    source_vpc_ids = as_list(aws.get("source_vpc_ids"))
    if not source_vpc_ids and vpc_id:
        source_vpc_ids = [str(vpc_id)]
    if source_vpc_ids and mode != "instance-ids":
        filters["vpc-id"] = list(source_vpc_ids) if len(source_vpc_ids) > 1 else source_vpc_ids[0]
        described += " vpc-id=%s" % ",".join(source_vpc_ids)

    if exclude_ids:
        described += " excluding %s" % ",".join(exclude_ids)

    return {"mode": mode, "static_path": "", "filters": filters,
            "described": described, "regions": as_list(aws.get("regions")),
            "source_vpc_ids": source_vpc_ids, "exclude_ids": exclude_ids}


def to_cli_filters(filters, extra=None):
    """EC2 filter mapping -> aws CLI --filters arguments (Name=...,Values=...)."""
    out = []
    merged = dict(filters)
    if extra:
        merged.update(extra)
    for name, value in merged.items():
        values = value if isinstance(value, (list, tuple)) else [value]
        out.append("Name=%s,Values=%s" % (name, ",".join(str(v) for v in values)))
    return out


def _tag_regex(value):
    """EC2 tag-filter semantics rendered as a KVO regex.

    EC2 filters match EXACTLY, with * as a wildcard. KVO takes an unanchored
    regex, so the raw value 'yes' would also match 'yes-legacy' and '.' would
    match any character: anchor and escape, translating only * to .*, so the
    two paths keep matching the same hosts."""
    import re
    return "^" + ".*".join(re.escape(p) for p in str(value).split("*")) + "$"


def kvo_selector(selection, instance_ids, single_vpc):
    """The KVO cloud-collection resourceSelector for one tapped VPC.

    Two forms, chosen by what is PROVABLY equivalent to the resolved set:

    - The tag form {field: K, tag: K, regex: V}, proven live (commit history:
      field must be the TAG KEY, not the literal "tag"). Only safe when the
      selection is exactly one tag, nothing was excluded, and discovery is
      confined to this one VPC: KVO applies the selector inside the cloud
      config's VPC on its own, so the vpc filter is implicit.
    - The instance-id form over the literal resolved ids. Correct regardless
      of how KVO combines multiple selector entries (AND vs OR is not
      documented), because ONE entry carries the whole set as an anchored
      alternation. Used for everything else: multiple ANDed tags, explicit
      instance lists, exclusions, or a non-Nitro subset.

    Long alternations are chunked into multiple entries at ~4KB. That relies
    on entries OR-ing; the KVO QSG lists selectors as additive rows, and a
    chunked list is the one case where OR is what we want. A chunked selector
    is logged by the caller so a live run can confirm the count.
    """
    # Zero resolved instances means zero mirrorable interfaces in this VPC,
    # whatever form the selection took: return the empty selector so the
    # consumer refuses to build a fabric that cannot cut a session. The tag
    # form must not bypass this: a wrong VPC or an untagged fleet would
    # otherwise commit a collector that taps nothing, silently.
    ids = sorted(instance_ids)
    if not ids:
        return []

    filters = selection["filters"]
    tag_keys = [k for k in filters if k.startswith("tag:")]
    if (selection["mode"] in ("tags", "default") and len(tag_keys) == 1
            and not selection["exclude_ids"] and single_vpc):
        key = tag_keys[0][len("tag:"):]
        return [{"field": key, "tag": key,
                 "regex": _tag_regex(str(filters[tag_keys[0]]))}]
    chunks, cur, cur_len = [], [], 0
    for i in ids:
        if cur and cur_len + len(i) + 1 > 4000:
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.append(i)
        cur_len += len(i) + 1
    chunks.append(cur)
    return [{"field": "instance-id", "tag": "instance-id",
             "regex": "^(%s)$" % "|".join(c)} for c in chunks]
