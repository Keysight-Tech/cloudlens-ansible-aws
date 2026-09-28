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

The GraphQL shape, pinned live on KVO 3.1.0 (2026-09-28): a KubernetesCluster
PRESENCE carries the vController (cloudLensManagerId) and KVO provisions its
vController project + key on creation; the Cloud Config is then
{cloudConfigType: K8s, cloudPresence: {name}, deviceLinks}. Nothing on
_CloudConfigInput names a vController. The presence key is what the
DaemonSet must register with (--key-out hands it to deploy-eks-tapping.sh).

Usage:
  python3 scripts/kvo_k8s_config.py --kvo <ip> \
      --name k8s-mycluster --vcontroller <clm-name-in-kvo> \
      [--device-link vpb-c2dl] [--pod-selector 'pod-name=^(web|loadgen)'] \
      [--tool vpb-egress-tool] [--policy k8s-traffic-policy] \
      [--kvo-admin-user admin] [--kvo-admin-pass admin] [--insecure]

Exit codes: 0 wired; 2 bad input; 5 auth/CR failed; 6 presence/config refused
(manual steps printed); 7 collection failed; 10 policy failed; 11 the config has
no device link so the policy was not attempted.
"""
from __future__ import annotations
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kvo_aws_mirror import (gql, open_cr, commit_cr, cluster_uid, exists_named, clm_info,
                            create_collection, create_monitoring_policy,
                            kvo_token, kvo_accept_eula, log)


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
    ap.add_argument("--key-out", default="",
                    help="write the presence's vController project key here (0600); "
                         "the DaemonSet must register with it")
    ap.add_argument("--accept-eula", action="store_true")
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()
    verify = not args.insecure
    # Accept a bare address as well as a URL: the deploy passes the KVO IP, and
    # requests refuses a URL with no scheme (MissingSchema, seen live 2026-09-28).
    kvo = args.kvo if args.kvo.startswith(("http://", "https://")) else "https://" + args.kvo
    kvo = kvo.rstrip("/")
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

    # 1. The presence, then the config. In the KVO 3.1.0 schema CloudPresence
    # is an interface (AwsPresence, CustomCloud, KubernetesCluster, ...) and
    # _CloudConfigInput has no vController field at all: the vController the
    # UG's dialog asks for goes on the KubernetesCluster PRESENCE as
    # cloudLensManagerId, and the Cloud Config only references the presence by
    # name with cloudConfigType K8s. Same shape as the AWS and Custom Cloud
    # rails. Found live 2026-09-28 after the introspection guess found nothing.
    # KVO provisions the vController project for the presence and hands back
    # its key: the UG says "the creation of the Cloud Config ... was the
    # condition to populate the deployment data for the sensor", so the
    # DaemonSet must register with THIS key, not the VM sensors' one.
    clm = clm_info(kvo, tok, args.vcontroller, verify)
    if not clm:
        log(f"vController '{args.vcontroller}' is not known to KVO (Cloud Fabric > "
            "CloudLens vController). Adopt it first (kvo_adopt_clms.py)."); return 6
    pres = next((k for k in (gql(kvo, tok, "{ kubernetesClusters { name clmsProjectId clmsProjectApiKey } }",
                                 None, verify).get("data", {}) or {}).get("kubernetesClusters") or []
                 if k.get("name") == args.name), None)
    have_cfg = any(c.get("name") == args.name for c in
                   (gql(kvo, tok, "{ cloudConfigs { name } }", None, verify)
                    .get("data", {}).get("cloudConfigs") or []))
    if pres and have_cfg:
        log(f"Kubernetes cluster presence + cloud config '{args.name}' already exist; reusing")
    else:
        cr = open_cr(kvo, tok, "k8s-cloud-config", verify)
        if not cr: return 5
        if not pres:
            r = gql(kvo, tok,
                    "mutation($n:String!,$c:String!,$s:_KubernetesClusterInput!){ "
                    "createKubernetesCluster(name:$n, changeID:$c, settings:$s)"
                    "{ uid name clmsProjectId clmsProjectApiKey } }",
                    {"n": args.name, "c": cr,
                     "s": {"cloudLensManagerId": clm["uid"],
                           "description": "CloudLens Autopilot Kubernetes"}}, verify)
            if "errors" in r:
                log(f"createKubernetesCluster failed: {r['errors'][0]['message'][:220]}")
                log("Create it in the UI instead (KVO UG, Kubernetes Cluster Cloud Configs):")
                log("Cloud Fabric > Cloud Configs > New Cloud Config > Kubernetes Cluster,")
                log(f"name '{args.name}', vController '{args.vcontroller}', device link "
                    f"'{args.device_link}'; then re-run this script.")
                return 6
            rows = r.get("data", {}).get("createKubernetesCluster") or []
            pres = rows[0] if rows else None
            if not pres:
                log("createKubernetesCluster returned no rows"); return 6
        if not have_cfg:
            settings = {"cloudConfigType": "K8s", "cloudPresence": {"name": args.name}}
            if args.device_link:
                have_c2dl = {c["name"] for c in
                             (gql(kvo, tok, "{ c2DLinks { name } }", None, verify)
                              .get("data", {}).get("c2DLinks") or [])}
                # KVO enforces ONE cloud config per Cloud to Device Link: the
                # commit is refused upfront with "There can only be one Cloud
                # Config associated to a Cloud To Device Link" (KVO 3.1.0, seen
                # live 2026-09-28 when the AWS mirror config already owned
                # vpb-c2dl). A link another config owns is therefore not
                # attached; the K8s rail needs its own link, on its own vPB
                # ingress port, to reach the vPB.
                owner = next((c["name"] for c in
                              (gql(kvo, tok, "{ cloudConfigs { name settings { deviceLinks { name } } } }",
                                   None, verify).get("data", {}).get("cloudConfigs") or [])
                              if args.device_link in
                              [l.get("name") for l in ((c.get("settings") or {}).get("deviceLinks") or [])]), "")
                if args.device_link not in have_c2dl:
                    log(f"device link '{args.device_link}' is not in KVO yet; the config "
                        "is created without it (the vPB path attaches it to every "
                        "cloud config when it runs).")
                elif owner:
                    log(f"device link '{args.device_link}' is owned by cloud config "
                        f"'{owner}' and KVO allows one cloud config per link, so the "
                        "Kubernetes config is created WITHOUT it. Pod traffic cannot "
                        "reach the vPB until this cluster gets its own link (a second "
                        "vPB ingress port, or a second vPB).")
                else:
                    settings["deviceLinks"] = [{"name": args.device_link}]
            r = gql(kvo, tok,
                    "mutation($n:String!,$c:String!,$cl:String!,$s:_CloudConfigInput!){ "
                    "createCloudConfig(name:$n, changeID:$c, clusterID:$cl, settings:$s){ uid name } }",
                    {"n": args.name, "c": cr, "cl": cluster, "s": settings}, verify)
            if "errors" in r:
                log(f"createCloudConfig failed: {r['errors'][0]['message'][:220]}")
                log(f"settings sent: {settings}")
                return 6
        if not commit_cr(kvo, tok, cr, verify): return 5
        log(f"Kubernetes cluster '{args.name}' live in KVO: presence on vController "
            f"'{args.vcontroller}', cloud config type K8s"
            + (", device link " + args.device_link if "deviceLinks" in (settings if not have_cfg else {}) else "") + ")")
    key = (pres or {}).get("clmsProjectApiKey") or ""
    if args.key_out:
        # The DaemonSet registers with this key. Written 0600, never printed.
        fd = os.open(args.key_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(key + "\n")
        log(f"project key for the Kubernetes sensors written to {args.key_out} (mode 600)"
            if key else f"WARNING: KVO returned no project key for '{args.name}'; {args.key_out} is empty")

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
        # KVO refuses the policy upfront when the config has no link: "Cloud
        # Config ... of Cloud Collection ... associated to monitoring policy
        # ... does not have a device link associated to it" (live 2026-09-28).
        # Say so and stop instead of leaving an invalid change request behind.
        links = next(((c.get("settings") or {}).get("deviceLinks") or []
                      for c in (gql(kvo, tok, "{ cloudConfigs { name settings { deviceLinks { name } } } }",
                                    None, verify).get("data", {}).get("cloudConfigs") or [])
                      if c.get("name") == args.name), [])
        if not links:
            log(f"cloud config '{args.name}' has no device link, so no policy to "
                f"'{args.tool}' can be committed (KVO requires one). Give this "
                "cluster its own Cloud to Device Link on a free vPB ingress port "
                "(KVO allows one cloud config per link), then re-run.")
            return 11
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
