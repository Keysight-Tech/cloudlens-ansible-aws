#!/usr/bin/env bash
# deploy/tests/test_kvo_selector.sh: the KVO cloud-collection selector must
# carry KVO's own tag identifiers.
#
# Why this exists. On KVO 3.1.0 a resourceSelector whose `tag` is the bare
# key (the KVO 2.13 form, commit b301357) matches nothing and says nothing:
# the fabric commits, the presence is Online, the collector registers, and
# zero mirror sessions ever appear, with no alert and 0 credits reserved.
# KVO names a tag key `system.tags.<key>` and the cloud metadata fields
# `system.cloud_metadata.<key>` (cloudPresenceTagsForPresence returns them);
# `field` stays the bare key. Sessions appeared 90 s after the repoint on
# 2026-09-28. This pins the shape for the three builders.
#
# Hermetic: pure Python, no network.
#
# Usage: bash deploy/tests/test_kvo_selector.sh
set -u
cd "$(dirname "$0")/../.."
PASS=0; FAIL=0
ok_()  { echo "PASS $*"; PASS=$((PASS+1)); }
bad_() { echo "FAIL $*"; FAIL=$((FAIL+1)); }

out=$(python3 - <<'PY'
import sys, json
sys.path.insert(0, "scripts")
from workload_selection import kvo_selector
tag = kvo_selector({"mode": "tags", "filters": {"tag:cloudlens": "yes"}, "exclude_ids": []},
                   ["i-1", "i-2"], True)
ids = kvo_selector({"mode": "instance-ids", "filters": {}, "exclude_ids": []},
                   ["i-2", "i-1"], True)
print(json.dumps({"tag": tag, "ids": ids}))
PY
)
python3 - "$out" <<'PY' && ok_ "tag form: field=key, tag=system.tags.key, anchored regex" || bad_ "tag form wrong: $out"
import sys, json
d = json.loads(sys.argv[1])["tag"]
assert d == [{"field": "cloudlens", "tag": "system.tags.cloudlens", "regex": "^yes$"}], d
PY
python3 - "$out" <<'PY' && ok_ "instance-id form: tag=system.cloud_metadata.instance-id over the sorted ids" || bad_ "instance-id form wrong: $out"
import sys, json
d = json.loads(sys.argv[1])["ids"]
assert d == [{"field": "instance-id", "tag": "system.cloud_metadata.instance-id", "regex": "^(i-1|i-2)$"}], d
PY
# The two scripts that build a selector by hand must use the same prefix.
if grep -q '"tag": "system.tags." + tag_key' scripts/kvo_aws_mirror.py; then
  ok_ "kvo_aws_mirror.py builds the tag form with the system.tags. prefix"
else
  bad_ "kvo_aws_mirror.py builds the selector without the system.tags. prefix"
fi
if grep -q '"tag": "system.tags." + f' scripts/kvo_k8s_config.py && grep -q '"tag": "system.tags.pod-name"' scripts/kvo_k8s_config.py; then
  ok_ "kvo_k8s_config.py builds pod selectors with the system.tags. prefix"
else
  bad_ "kvo_k8s_config.py pod selector lacks the system.tags. prefix"
fi
echo; echo "$PASS PASS, $FAIL FAIL"
[[ $FAIL -eq 0 ]]
