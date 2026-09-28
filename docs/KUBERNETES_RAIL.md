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

## Not automated yet

`deploy/deploy-stack.sh` deploys one vPB. A stack that runs both the mirror
rail and the Kubernetes rail needs a second vPB wired as above; the steps
are the scripts listed here, in that order.
