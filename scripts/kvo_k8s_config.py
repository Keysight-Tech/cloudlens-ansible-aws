#!/usr/bin/env python3
"""
Wire a Kubernetes cluster into KVO end to end: the Kubernetes Cluster Cloud
Config, a Cloud Collection selecting pods, and the monitoring policy binding
that collection to the vPB tool. The KVO-side half of the EKS tapping rail;
scripts/deploy-eks-tapping.sh is the cluster-side half.

What the KVO UG (913-3037-01, "Kubernetes Cluster Cloud Configs") says this
object is: name + a vController picked from the ones KVO has discovered +
optionally a Cloud to Device Link. It never touches the cluster API and never
deploys sensors ("the creation of the Cloud Config ... was the condition to
populate the deployment data for the sensor"); the sensors register to the
vController on their own. The collection then lists the cluster's pods and a
Workload Selector narrows them: "the selector 'pod-name' and 'nginx' value,
will select all pods that start with 'nginx' prefix."

One honest unknown, handled instead of guessed: no document prints the
GraphQL input shape for the Kubernetes config type. This script INTROSPECTS
_CloudConfigInput at run time, builds the settings from what the schema
actually offers, and when nothing matches it prints the real field list plus
the exact manual UI steps, rather than committing a wrong object. The first
live run records what worked (the same way the resourceSelector 'field is
the tag key' fact was pinned by commit b301357).

Usage:
  python3 scripts/kvo_k8s_config.py --kvo <ip> \
      --name k8s-mycluster --vcontroller <clm-name-in-kvo> \
      [--device-link vpb-c2dl] [--pod-selector 'pod-name=^(web|loadgen)'] \
      [--tool vpb-egress-tool] [--policy k8s-traffic-policy] \
      [--kvo-admin-user admin] [--kvo-admin-pass admin] [--insecure]

Exit codes: 0 wired; 2 bad input; 5 auth/CR failed; 6 the schema offered no
Kubernetes shape (manual steps printed); 7 collection failed; 10 policy failed.
"""
from __future__ import annotations
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kvo_aws_mirror import (gql, open_cr, commit_cr, cluster_uid, exists_named,
                            create_collection, create_monitoring_policy,
                            kvo_token, kvo_accept_eula, log)


def introspect_input(base, tok, verify, type_name):
    q = ('{ __type(name:"%s"){ inputFields { name type { name kind '
         'ofType { name } } } } }' % type_name)
    d = gql(base, tok, q, None, verify)
    t = (d.get("data", {}) or {}).get("__type") or {}
    return {f["name"]: f for f in (t.get("inputFields") or [])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kvo", required=True)
    ap.add_argument("--name", required=True,
                    help="the Kubernetes Cloud Config name (one per cluster)")
    ap.add_argument("--vcontroller", required=True,
                    help="the vController AS KVO KNOWS IT (the name shown in "
                         "Cloud Fabric > CloudLens vController)")
    ap.add_argument("--device-link", default="vpb-c2dl",
                    help="C2DL to attach so tapped pod traffic reaches the vPB; "
                         "attached only if it already exists in KVO")
    ap.add_argument("--pod-selector", action="append", default=[],
                    help="field=regex, repeatable (default pod-name=.* with a "
                         "licence warning: every selected pod consumes a credit)")
    ap.add_argument("--tool", default="vpb-egress-tool",
                    help="existing KVO tool to bind the collection to")
    ap.add_argument("--policy", default="")
    ap.add_argument("--no-policy", action="store_true",
                    help="stop after the collection (no tool exists yet)")
    ap.add_argument("--kvo-admin-user", default=os.environ.get("KVO_ADMIN_USER", "admin"))
    ap.add_argument("--kvo-admin-pass", default=os.environ.get("KVO_ADMIN_PASS", "admin"))
    ap.add_argument("--accept-eula", action="store_true")
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()
    verify = not args.insecure
    kvo = args.kvo
    policy_name = args.policy or f"{args.name}-policy"
    coll_name = f"{args.name}-collect"

    selectors = []
    for s in (args.pod_selector or []):
        f, _, r = s.partition("=")
        if not f or not r:
            log(f"--pod-selector must be field=regex (got '{s}')"); return 2
        selectors.append({"field": f, "tag": f, "regex": r})
    if not selectors:
        selectors = [{"field": "pod-name", "tag": "pod-name", "regex": ".*"}]
        log("WARNING: no --pod-selector given; selecting EVERY pod (pod-name .*).")
        log("  Each selected pod consumes one licence credit (vTAP UG). Narrow it:")
        log("  --pod-selector 'pod-name=^(web|loadgen)'")

    tok = kvo_token(kvo, args.kvo_admin_user, args.kvo_admin_pass, verify)
    if not tok and args.accept_eula:
        kvo_accept_eula(kvo, verify)
        tok = kvo_token(kvo, args.kvo_admin_user, args.kvo_admin_pass, verify)
    if not tok:
        log("could not authenticate to KVO"); return 5
    cluster = cluster_uid(kvo, tok, verify)
    if not cluster:
        log("no KVO cluster uid"); return 5

    # 1. The Kubernetes Cloud Config, idempotent by name. NEVER edited when it
    # exists: the KVO UG documents config edits as destructive for the AWS
    # type, and there is no documented promise the Kubernetes type is safer.
    have = any(c.get("name") == args.name for c in
               (gql(kvo, tok, "{ cloudConfigs { name } }", None, verify)
                .get("data", {}).get("cloudConfigs") or []))
    if have:
        log(f"Kubernetes cloud config '{args.name}' already exists; reusing")
    else:
        fields = introspect_input(kvo, tok, verify, "_CloudConfigInput")
        if not fields:
            log("could not introspect _CloudConfigInput; is this KVO reachable?"); return 5
        # The dialog's vController dropdown must map to SOME input field; find
        # it by name instead of guessing a spelling.
        vc_field = next((n for n in fields
                         if "controller" in n.lower() or n.lower() in ("vcontroller", "clm")), "")
        if not vc_field:
            log("The KVO schema offers no vController field on _CloudConfigInput.")
            log("Its actual input fields are:")
            for n in sorted(fields):
                log(f"  {n}")
            log("Create the config manually instead (KVO UG, Kubernetes Cluster")
            log("Cloud Configs): Cloud Fabric > Cloud Configs > New Cloud Config >")
            log(f"Kubernetes Cluster, name '{args.name}', vController")
            log(f"'{args.vcontroller}', device link '{args.device_link}'. Then")
            log("re-run this script: the collection and policy steps are schema-safe.")
            return 6
        settings = {vc_field: {"name": args.vcontroller}}
        want_links = []
        if args.device_link and "deviceLinks" in fields:
            have_c2dl = {c["name"] for c in
                         (gql(kvo, tok, "{ c2DLinks { name } }", None, verify)
                          .get("data", {}).get("c2DLinks") or [])}
            if args.device_link in have_c2dl:
                want_links = [{"name": args.device_link}]
                settings["deviceLinks"] = want_links
            else:
                log(f"device link '{args.device_link}' is not in KVO yet; the config "
                    "is created without it (the vPB path attaches it to every "
                    "cloud config when it runs).")
        cr = open_cr(kvo, tok, "k8s-cloud-config", verify)
        if not cr: return 5
        r = gql(kvo, tok,
                "mutation($n:String!,$c:String!,$cl:String!,$s:_CloudConfigInput!){ "
                "createCloudConfig(name:$n, changeID:$c, clusterID:$cl, settings:$s){ uid name } }",
                {"n": args.name, "c": cr, "cl": cluster, "s": settings}, verify)
        if "errors" in r:
            log(f"createCloudConfig failed: {r['errors'][0]['message'][:220]}")
            log(f"settings sent: {settings}")
            log("If the message names a missing/unknown field, the schema differs")
            log("from the doc dialog; create it in the UI (steps above under exit 6)")
            log("and re-run for the collection + policy.")
            return 6
        if not commit_cr(kvo, tok, cr, verify): return 5
        log(f"Kubernetes cloud config '{args.name}' live "
            f"(vController '{args.vcontroller}' via field '{vc_field}'"
            + (f", device link {args.device_link}" if want_links else "") + ")")

    # 2. The Cloud Collection with the pod selector. create_collection is the
    # SAME function the AWS rail uses; the pod-name/pod-label field names come
    # from the KVO UG's Kubernetes selector examples.
    have_coll = exists_named(kvo, tok, "cloudCollections", coll_name, verify)
    if have_coll:
        log(f"cloud collection '{coll_name}' already exists; reusing")
    else:
        cr = open_cr(kvo, tok, "k8s-collection", verify)
        if not cr: return 7
        coll = create_collection(kvo, tok, cr, coll_name, cluster, args.name,
                                 selectors, verify)
        if not coll:
            return 7
        if not commit_cr(kvo, tok, cr, verify): return 7
        log(f"cloud collection '{coll_name}' selecting "
            + ", ".join(f"{s['field']}~{s['regex']}" for s in selectors))

    # 3. The monitoring policy: collection -> the vPB tool. Without it KVO has
    # nowhere to send the tapped pod traffic, exactly like the AWS rail.
    if args.no_policy:
        log("stopping before the policy (--no-policy): no tool exists yet. "
            "Re-run without it after the vPB path creates the tool.")
        return 0
    if not exists_named(kvo, tok, "tools", args.tool, verify):
        log(f"tool '{args.tool}' does not exist in KVO yet, so no policy was "
            "created. The vPB traffic-path step creates it; re-run this script "
            "afterwards (deploy-stack.sh does this automatically), or pass "
            "--tool <existing>.")
        return 0
    if exists_named(kvo, tok, "monitoringPolicies", policy_name, verify):
        log(f"monitoring policy '{policy_name}' already exists; reusing")
    else:
        cr = open_cr(kvo, tok, "k8s-policy", verify)
        if not cr: return 10
        pol = create_monitoring_policy(kvo, tok, cr, policy_name, cluster,
                                       coll_name, args.tool, verify)
        if not pol:
            return 10
        if not commit_cr(kvo, tok, cr, verify): return 10
        log(f"monitoring policy '{policy_name}' ({coll_name} -> {args.tool}) committed")

    log("")
    log("Kubernetes rail wired in KVO: config -> collection (pod selector) -> "
        f"policy -> {args.tool}. Tapped pod traffic follows the same path the "
        "VM sensors use.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
