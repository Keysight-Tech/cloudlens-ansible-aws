# KVO AWS Zone Tapping: vHub deploys but is never adopted, so no Traffic Mirror sessions

## Summary

On KVO **2.13.0** (AMI `kvo-2.13.0-prod`) driving AWS Zone Tapping, the vHub
(collector Service VM, AMI `cloudlens-vpb-svm-6.14.1-6-prod`) is launched by the
Auto Scaling Group and boots healthy, but KVO never adopts it as a collection
point. As a result KVO creates the Traffic Mirror **target** and **filter** but
**no Traffic Mirror sessions**, and no traffic is ever tapped.

All documented prerequisites (KVO 3.0.1 User Guide, "AWS Cloud Configs") are met.
We would like to know what KVO does after launching the vHub to bring it into the
fabric, and what can prevent that step.

## Environment

- KVO 2.13.0, CloudLens vController (CLMS) 6.12.1, both in AWS us-east-1, adopted (CONNECTED).
- Region us-east-1, single AZ us-east-1a, one VPC (10.99.0.0/16).
- Cloud Config created with the adopted vController selected; AWS access keys from a
  user carrying the documented `iam-policy.json`.
- Nitro source instances tagged `cloudlens=yes`, remote L2GRE tool, monitoring policy
  source -> tool committed.

## What works

- Cloud Config, Cloud Collection, remote Tool, and Monitoring Policy all commit cleanly.
- KVO launches the vHub via an ASG; the vHub reaches `running`, EC2 status ok/ok, 3 ENIs
  in the mgmt/ingress/egress subnets.
- KVO creates the Traffic Mirror **target** (pointing at the vHub ingress ENI) and the
  **filter**.
- The vHub reads its config from EC2 instance tags via IMDS
  (`InstanceMetadataTags=enabled`): `cloudlens:ip` (vController), `cloudlens:projectKey`,
  `cloudlens:collector=true`, `cloudlens:monitored:vpcid`.
- The vHub's project key is a **real** project on the vController
  (`KVO_aws-mirror`), not a phantom key.
- The vHub can reach the vController on 443.

## What does not happen

- KVO never creates a **CollectionPoint** for the vHub, and the vHub never appears in KVO
  `devices`.
- **No Traffic Mirror sessions** are created (target=1, filter=1, sessions=0), so nothing
  is tapped.
- On the vHub, the KVO control API listener (`xfilter_kvoapp`, gunicorn `:8444`) only runs
  when `/var/lib/kcos/apps/packetstack/metadata.json` has `capabilities.KVO=true`; on the
  ASG-launched vHub that flag is **false**, so it exits immediately.
- The vHub logs, repeatedly:
  - `kvo._get_shim_config: Error reading config map: (404) configmaps "vpb-shim" not found`
  - `kvo._configure_shim: Shim configmap data not found`
  - `config.config: Tried to set invalid DTLS Config: 4 validation errors ... missing`
  - `driver_mapper.bind_to_dpdk: Failed to bind PCI NIC ...:07.0 to vfio-pci (exit 1)`
  - internal `GET /vpb/status -> 401 Missing Authorization header` in a loop
- The vHub's `:8444` gunicorn access log is **empty** — KVO never connects to push the
  shim/DTLS config, and the vHub never opens a connection to the vController.

## Questions for support

1. After the ASG launches the vHub, what is responsible for setting
   `metadata.capabilities.KVO=true` and creating the `vpb-shim` configmap — the vHub
   itself (from the `cloudlens:collector` tag) or a push from KVO to the vHub's `:8444`?
2. What conditions gate KVO from adopting a vHub it launched (creating the CollectionPoint
   and pushing `vpb-shim`/DTLS)? Is there a health/announce handshake the vHub must complete
   first, and where is its result logged on the KVO side?
3. Is the `vfio-pci` bind failure expected on the default vHub instance type, or does it
   block bring-up (and does the vHub type need IOMMU support)?
4. Is there a KVO-side log for the collection-point/vHub reconcile we can inspect to see why
   correlation is not happening?

## Notes on our automation (already corrected)

While reproducing this via API we found and fixed two deviations from the manual procedure;
neither resolved the adoption gap:
- The mgmt/ingress/egress security groups must be three DISTINCT SGs (UG requirement); we had
  passed one SG for all three. Now three separate SGs.
- The KVO access-key policy was missing `ec2:DescribeTags` vs the documented `iam-policy.json`.
  Now added.

Even with both corrected and a fresh vHub deployed, the vHub is still not adopted and no
sessions are created, which points the remaining cause at the KVO-side vHub adoption /
`vpb-shim` push rather than at the AWS-side configuration.
