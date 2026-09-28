# The Kubernetes rail: pods to a vPB to a tool

Proven live on 2026-09-28 (KVO 3.1.0, vController 6.12.1, vPB 3.13.0-7,
EKS 1.33): a pod-to-pod HTTP session inside the cluster arrived at a packet
capture host inside the vPB's GRE copy. This page is the repeatable chain
and the two product rules that decide the design.

## Two rules that shape it

1. **One cloud config per Cloud to Device Link, one link per vPB ingress
   port.** KVO refuses a second config on a link ("There can only be one
   Cloud Config associated to a Cloud To Device Link") and refuses a policy
   from a config that has no link. The AWS mirror config owns the first
   vPB's link, so the Kubernetes config needs its own link on its own vPB
   (a 3-interface vPB has one ingress port). Size the vPB tier per cloud
   config, not per region.
2. **Selectors use KVO's own tag identifiers.** A cloud collection's
   `resourceSelector.tag` is `system.tags.<key>` (`system.tags.pod-name`,
   `system.tags.pod-namespace`, `system.tags.app`, ...), `field` is the
   bare key. A bare `tag` matches nothing and raises no alert.

## The chain

1. **KVO first.** `scripts/kvo_k8s_config.py --kvo <kvo> --name k8s-<cluster>
   --vcontroller <vController name in KVO> --pod-selector 'pod-name=^(web|loadgen)'
   --no-policy --key-out <file> --insecure` creates the KubernetesCluster
   presence (the vController lives on the presence), the cloud config of
   type K8s, and the pod collection. KVO provisions the presence's own
   vController project; the key lands in `<file>` (mode 600).
2. **DaemonSet on that key.** `scripts/deploy-eks-tapping.sh --clms-ip
   <vController PRIVATE ip> --project-key <key> --mode daemonset ...`.
   The sensors register under the Kubernetes presence (sensorCount = nodes).
   Pods in the stack VPC must use the private address: the stack security
   group refuses the public one.
3. **A vPB of its own.** Launch from the vPB AMI with three interfaces
   (management, ingress, egress) and no `AssociatePublicIpAddress` (refused
   with several interfaces), then attach an Elastic IP to the management
   interface. First boot to the EULA prompt takes about 15 minutes.
4. **Adopt it.** `scripts/vpb_kvo_adopt.py --vpb <eip> --vpb-port 9022
   --vpb-user admin --key <pem> --kvo <kvo> --kvo-internal-ip <kvo private>
   --vpb-mgmt-ip <mgmt private> --device-name cloudlens-vpb2 --wait-cli
   --accept-eula --insecure`.
5. **Wire it to the Kubernetes config.** `scripts/vpb_wire_path.py --kvo <kvo>
   --device cloudlens-vpb2 --collection k8s-<cluster>-collect --cloud-config
   k8s-<cluster> --link-config k8s-<cluster> --c2dl k8s-c2dl --gre-key 101
   --ingress-ip <vpb2 eth1> --gateway <ingress gw> --egress-ip <vpb2 eth2>
   --egress-gateway <egress gw> --egress-gre-key 201 --tool vpb2-egress-tool
   --policy k8s-traffic-policy --capture-ip <capture host> --capture-tool
   vpb2-capture-tool --capture-gre-key 300 --insecure`. `--link-config`
   attaches the link to that one config instead of the AWS mirror configs.
6. **Wait two to three minutes.** KVO projects the groups, connections and
   static destinations into the vController project `KVO_k8s-<cluster>`
   some time after the commit reports Success; the sensors pick them up
   within ten seconds of that and build their GRE tunnels.

## How to tell it works

- vController, logged in as the KVO user: `GET /cloudlens/api/v1/mgmt/accounts/{account}/projects`,
  project `KVO_k8s-<cluster>` shows groups, connections and static
  destinations (the vPB ingress IP and the capture host).
- vPB: `show traffic-rule-packet-counters` inspects and passes packets;
  `show interface-pkt-counters` shows `gre_eth2` TX rising.
- Capture host: `tcpdump -i any -nn -v 'proto 47 and src host <vpb2 egress>'`
  shows pod IPs and the HTTP inside the GRE copy.
- Sensor logs: `kubectl -n cloudlens logs ds/cloudlens-sensor` shows
  "Control says to rebalance" and a `GR_...` interface to the vPB ingress IP.

## Automated in deploy-stack.sh: one vPB per area

`deploy/deploy-stack.sh` does the chain above by itself. `--vpb-rails LIST`
names the areas that get a vPB of their own (`mirror`, `k8s`); the stack's
vPB serves the first area, and one more vPB is launched, adopted and wired
per further area. With no `--vpb-rails` every enabled area gets one, so
`--with-vpb --with-mirror --with-eks` means two vPBs; the interview asks the
same question when both areas land on a vPB. `--vpb-rails mirror` keeps the
pods off any vPB; `--vpb-rails k8s` gives the stack vPB to the pods.

What the deploy does for an extra area:

1. Right after the stack exists, launches `<stack>-vpb-<area>` (same image,
   type, key, bootstrap and security group as the stack vPB, three
   interfaces on its subnets) and attaches an Elastic IP; tagged
   `cloudlens:stack=<stack>` and `cloudlens:vpb-rail=<area>`. It boots while
   the sensor, EKS and mirror phases run. A vPB with those tags is reused.
2. Phase 14 adopts it as `<device>-<area>` (a device KVO already lists at
   that management address is reused under its existing name).
3. Phase 16 wires it: `--link-config` on the area's cloud config, its own
   link (`k8s-c2dl`), tools `vpb-<area>-egress-tool` and
   `vpb-<area>-capture-tool`, policy `<area>-traffic-policy`, GRE keys
   offset per area. The state ledger records the instance, device and wiring.
4. The final summary lists each extra vPB; `teardown-stack.sh` terminates
   them by tag and releases their Elastic IPs.

`bash deploy/tests/test_vpb_rails.sh` pins the area computation and the
launch shape (three interfaces, no `AssociatePublicIpAddress`, Elastic IP
attached after, tags, reuse).
