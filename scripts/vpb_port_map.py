#!/usr/bin/env python3
"""Work out which vPB port name is physically the ingress NIC, and which is the egress.

DO NOT ASSUME eth1 IS INGRESS. The vPB enumerates its data ports in whatever
order the guest brings them up, and that order does NOT have to match the AWS
attachment device index. Measured on two stacks built from the same template on
the same day:

    stack A   vPB eth1 MAC 02:d2:52:dc:95:79  ==  AWS device index 1   correct
    stack B   vPB eth1 MAC 02:4f:2d:d9:c7:1f  ==  AWS device index 2   SWAPPED

Stack A forwarded 145,037 packets. Stack B silently forwarded nothing: the
mirrored traffic physically arrived on the NIC the vPB calls eth2, which had
been bound as the EGRESS port, so the ingress rule never matched. eth1 sat at
471 packets while eth2 climbed past 700,000.

The MAC address is the only reliable key. The IP shown by `show interface-status`
is NOT: those addresses are the ones KVO wrote, so a mis-mapped port cheerfully
displays the address it was wrongly given.

Prints shell-evalable assignments:

    VPB_INGRESS_PORT=eth2
    VPB_EGRESS_PORT=eth1

Exit: 0 ok, 2 ssh/CLI failure, 3 could not determine the mapping.
"""
from __future__ import annotations
import argparse, re, subprocess, sys


def log(m): print(f"[vpb-portmap] {m}", file=sys.stderr, flush=True)


def vpb_interface_macs(host, port, user, key, timeout=25):
    """port name -> MAC, as the DEVICE reports them.

    The CLI truncates: on a 3-NIC vPB the last interface's MAC line is often
    missing entirely, so callers must tolerate a port with no MAC and resolve it
    by elimination. Returns (macs, port_order).
    """
    cmd = ["ssh", "-i", key, "-p", str(port), "-o", "StrictHostKeyChecking=no",
           "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}",
           f"{user}@{host}", "sudo vpb -c 'show interface-status'"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 20).stdout
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"cannot reach the vPB CLI: {e}"); return None, None

    macs, order, current = {}, [], None
    for line in out.splitlines():
        m = re.search(r"Interface\s+(eth\d+)", line)
        if m:
            current = m.group(1)
            order.append(current)
            macs.setdefault(current, None)
            continue
        if current:
            m = re.search(r"\b([0-9a-f]{2}(?::[0-9a-f]{2}){5})\b", line, re.I)
            if m and macs.get(current) is None:
                macs[current] = m.group(1).lower()
    if not order:
        log("could not parse any interface from 'show interface-status'"); return None, None
    return macs, order


def resolve(macs, order, ingress_mac, egress_mac, mgmt_mac=None):
    """Map the two data ports to roles by MAC, filling one gap by elimination."""
    known = {p: m for p, m in macs.items() if m}
    ingress_port = next((p for p, m in known.items() if m == ingress_mac), None)
    egress_port = next((p for p, m in known.items() if m == egress_mac), None)

    # The CLI dropped a MAC. If exactly one data port is unidentified and exactly
    # one role is unfilled, the remainder is forced.
    data_ports = [p for p in order if p != "eth0"] if mgmt_mac is None else \
                 [p for p in order if known.get(p) != mgmt_mac]
    unknown = [p for p in data_ports if p not in (ingress_port, egress_port)]
    if ingress_port and not egress_port and len(unknown) == 1:
        egress_port = unknown[0]
        log(f"egress MAC not reported by the CLI; {egress_port} is the only port left")
    elif egress_port and not ingress_port and len(unknown) == 1:
        ingress_port = unknown[0]
        log(f"ingress MAC not reported by the CLI; {ingress_port} is the only port left")
    return ingress_port, egress_port


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vpb", required=True, help="vPB management IP")
    ap.add_argument("--port", type=int, default=9022)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--key", required=True, help="SSH private key")
    ap.add_argument("--ingress-mac", required=True, help="MAC of the AWS ingress ENI")
    ap.add_argument("--egress-mac", required=True, help="MAC of the AWS egress ENI")
    ap.add_argument("--mgmt-mac", help="MAC of the management ENI, so it is never a candidate")
    a = ap.parse_args()

    ingress_mac = a.ingress_mac.lower()
    egress_mac = a.egress_mac.lower()
    macs, order = vpb_interface_macs(a.vpb, a.port, a.user, a.key)
    if not macs:
        return 2

    ing, egr = resolve(macs, order, ingress_mac, egress_mac,
                       a.mgmt_mac.lower() if a.mgmt_mac else None)
    if not ing or not egr or ing == egr:
        log("could not map the vPB ports to the AWS interfaces by MAC.")
        log(f"  device reported: {macs}")
        log(f"  looking for ingress {ingress_mac} / egress {egress_mac}")
        log("  Binding by position would be a guess, and a wrong guess silently")
        log("  forwards nothing, so refusing to guess.")
        return 3

    # Say plainly when the device does not agree with AWS's attachment order,
    # because this is the case that used to fail without a single error message.
    if ing != "eth1" or egr != "eth2":
        log(f"NOTE: this vPB does NOT follow AWS device-index order.")
        log(f"  ingress ENI is the port the device calls {ing}")
        log(f"  egress  ENI is the port the device calls {egr}")
        log("  Binding to eth1/eth2 by position here would forward nothing.")
    print(f"VPB_INGRESS_PORT={ing}")
    print(f"VPB_EGRESS_PORT={egr}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
