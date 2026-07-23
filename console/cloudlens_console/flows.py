"""The four deployment flows, as data.

Each flow is the SINGLE source both the orchestrator and the UI read:
  - inputs:  fields the user fills in
  - nodes:   the diagram (id -> position/icon/label); pre-rendered as ghosts
  - wires:   which nodes connect
  - source:  how real progress is obtained -
       {"kind":"cfn", ...}    poll CloudFormation stack events (real AWS state)
       {"kind":"script", ...} run a repo script; parse its stdout into events

For "script" sources, `patterns` maps a real stdout substring/regex to an event:
  (matcher, node, status, narration, tone)
so a real log line lights the right node and streams a human explanation.

Every flow shells out to the EXISTING, proven automation - the console is a live
wrapper, never a reimplementation.
"""
from __future__ import annotations
import re
from . import events as E

# repo root is two levels up from this package (…/cloudlens-ansible-aws)
REPO = "{repo}"  # substituted by orchestrator at runtime


def _field(key, label, default="", placeholder=""):
    return {"key": key, "label": label, "default": default, "placeholder": placeholder}


# ---------------------------------------------------------------- FLOW 01: stack
STACK = {
    "id": "stack",
    "name": "Launch full stack",
    "script": "deploy-stack.sh",
    "subtitle": "VPC + vController + KVO + vPB, one CloudFormation deploy",
    "inputs": [
        _field("stack", "Stack name", "cloudlens-live", "cloudlens-live"),
        _field("region", "Region", "us-east-1", "us-east-1"),
        _field("key", "EC2 key pair", "", "my-ec2-key"),
        _field("kvo", "Deploy KVO", "yes", "yes / no"),
        _field("vpb", "Deploy vPB", "yes", "yes / no"),
    ],
    "nodes": {
        "vpc": {"x": 50, "y": 20, "ic": "vpc", "lab": "VPC", "sub": "network"},
        "clms": {"x": 22, "y": 66, "ic": "clms", "lab": "vController", "sub": "CLMS"},
        "kvo": {"x": 50, "y": 80, "ic": "kvo", "lab": "KVO", "sub": "orchestrator"},
        "vpb": {"x": 78, "y": 66, "ic": "vpb", "lab": "vPB", "sub": "packet broker"},
    },
    "wires": [["vpc", "clms"], ["vpc", "kvo"], ["vpc", "vpb"]],
    "source": {
        "kind": "cfn",
        "stack_input": "stack",
        # CloudFormation logical-id fragment -> diagram node
        "resource_map": {
            "Vpc": "vpc", "InternetGateway": "vpc", "MgmtSubnet": "vpc",
            "VcontrollerInstance": "clms",
            "KvoInstance": "kvo",
            "VpbInstance": "vpb",
        },
        # (logical fragment, status fragment) -> narration
        "narrate": {
            ("Vpc", "CREATE_COMPLETE"): ("Network foundation is up - the VPC and gateway exist before anything lands in it.", "good"),
            ("VcontrollerEip", "CREATE_IN_PROGRESS"): ("Elastic IPs are reserved first, so an over-quota account fails in seconds - not six minutes in with instance-hours burned.", "note"),
            ("VcontrollerInstance", "CREATE_IN_PROGRESS"): ("Launching the CloudLens Manager - the control plane that every sensor registers to.", "info"),
            ("VcontrollerInstance", "CREATE_COMPLETE"): ("vController is up. It still needs ~15 min to initialize before you can log in.", "good"),
            ("KvoInstance", "CREATE_IN_PROGRESS"): ("Launching KVO - the single pane that adopts the manager, the vPB, and drives AWS mirroring.", "info"),
            ("KvoInstance", "CREATE_COMPLETE"): ("KVO is up.", "good"),
            ("VpbInstance", "CREATE_IN_PROGRESS"): ("Launching the virtual packet broker - filters and forwards tapped traffic to your tools.", "info"),
            ("VpbInstance", "CREATE_COMPLETE"): ("vPB is up.", "good"),
        },
    },
}

# ------------------------------------------------------------- FLOW 02: sensors
SENSORS = {
    "id": "sensors",
    "name": "CLMS + sensors",
    "script": "ansible-playbook",
    "subtitle": "Register sensors straight into CloudLens Manager - no KVO",
    "inputs": [
        _field("clms", "vController IP", "", "20.84.115.190"),
        _field("key", "Project API key", "", "af9aa122…"),
        _field("tag", "Source tag", "cloudlens=yes", "cloudlens=yes"),
        _field("region", "Region", "us-east-1", "us-east-1"),
    ],
    "nodes": {
        "clms": {"x": 50, "y": 18, "ic": "clms", "lab": "vController", "sub": "CLMS"},
        "u": {"x": 20, "y": 72, "ic": "vm", "lab": "Ubuntu", "sub": "docker"},
        "r": {"x": 50, "y": 80, "ic": "vm", "lab": "RHEL", "sub": "podman"},
        "w": {"x": 80, "y": 72, "ic": "vm", "lab": "Windows", "sub": "service"},
    },
    "wires": [["u", "clms"], ["r", "clms"], ["w", "clms"]],
    "source": {
        "kind": "script",
        "patterns": [
            (r"project key", "clms", E.LIVE, "Project key retrieved - the forced first-login password change was handled automatically.", "good"),
            (r"[Uu]buntu.*(TASK|sensor|docker)", "u", E.BUSY, "Installing the sensor on Ubuntu via Docker.", "info"),
            (r"WebServerLB1|ubuntu.*ok=", "u", E.LIVE, "Ubuntu sensor registered - Register status 200.", "good"),
            (r"[Rr]hel|[Rr]ed ?[Hh]at", "r", E.BUSY, "Installing the sensor on RHEL via Podman.", "info"),
            (r"rhel.*ok=|WebServerLB2", "r", E.LIVE, "RHEL sensor registered.", "good"),
            (r"[Ww]indows", "w", E.BUSY, "Installing the CloudLens Windows service.", "info"),
            (r"win.*ok=|brine-winvm.*ok=", "w", E.LIVE, "Windows sensor registered.", "good"),
        ],
    },
}

# ------------------------------------------------------------ FLOW 03: kvo + vpb
KVO = {
    "id": "kvo",
    "name": "KVO + vPB + sensors",
    "script": "kvo_adopt_clms.py / vpb_kvo_adopt.py",
    "subtitle": "KVO as the single pane: adopt the manager and the packet broker",
    "inputs": [
        _field("clms", "CLMS IP", "", "10.99.1.25"),
        _field("kvo", "KVO IP", "", "10.99.1.26"),
        _field("vpb", "vPB IP", "", "10.99.1.30"),
        _field("cloud", "Cloud config", "prod-cloud", "prod-cloud"),
    ],
    "nodes": {
        "kvo": {"x": 50, "y": 16, "ic": "kvo", "lab": "KVO", "sub": "single pane"},
        "clms": {"x": 20, "y": 50, "ic": "clms", "lab": "CLMS", "sub": "adopted"},
        "vpb": {"x": 80, "y": 50, "ic": "vpb", "lab": "vPB", "sub": "Online"},
        "tool": {"x": 80, "y": 84, "ic": "tool", "lab": "Tool", "sub": "analyzer"},
        "vm": {"x": 22, "y": 84, "ic": "vm", "lab": "Sensors", "sub": "hosts"},
    },
    "wires": [["clms", "kvo"], ["vpb", "kvo"], ["vpb", "tool"], ["vm", "clms"]],
    "source": {
        # Matchers key on message CONTENT, never on the [kvo-adopt]/[vpb-adopt] log
        # prefixes (which appear on every line) - order matters, first hit wins.
        "kind": "script",
        "patterns": [
            (r"licenses active|EULA accepted|is licensed", "kvo", E.LIVE, "EULA accepted and licenses active - every KVO write is unblocked.", "good"),
            (r"createCloudLensManager|committing change request", "clms", E.BUSY, "Adopting the CLMS into KVO - committing the change request.", "info"),
            (r"status: CONNECTED|is CONNECTED", "clms", E.LIVE, "CLMS is CONNECTED; the Cloud Config provisions the working project key.", "good"),
            (r"KVO enabled|vPB announced", "vpb", E.BUSY, "vPB announced itself - adopting it with control.", "info"),
            (r"availability: Online|is Online", "vpb", E.LIVE, "vPB is Online and auto-licensed - KVO built its Device Config.", "good"),
            (r"ports bound|Cloud[- ]to[- ]Device Link|C2DL", "tool", E.LIVE, "Ports bound: ingress to the Cloud-to-Device Link, egress to the tool.", "good"),
            (r"monitoring policy committed|registered under KVO", "vm", E.LIVE, "Sensors registered under KVO management.", "good"),
        ],
    },
}

# ----------------------------------------------------------- FLOW 04: aws mirror
MIRROR = {
    "id": "mirror",
    "name": "AWS mirror session",
    "script": "kvo_aws_mirror.py",
    "subtitle": "Agentless: KVO deploys collectors and drives VPC Traffic Mirroring",
    "inputs": [
        _field("vpc", "Source VPC", "", "vpc-0ebe57a…"),
        _field("tag", "Source tag", "cloudlens=yes", "cloudlens=yes"),
        _field("az", "Zone", "us-east-1a", "us-east-1a"),
        _field("tool", "Tool IP", "", "10.99.12.146"),
    ],
    "nodes": {
        "src": {"x": 20, "y": 22, "ic": "vm", "lab": "Nitro srcs", "sub": "cloudlens=yes"},
        "mir": {"x": 50, "y": 22, "ic": "mirror", "lab": "Mirror", "sub": "per ENI"},
        "coll": {"x": 50, "y": 58, "ic": "coll", "lab": "Collector", "sub": "Service VM"},
        "kvo": {"x": 82, "y": 38, "ic": "kvo", "lab": "KVO", "sub": "orchestrates"},
        "tool": {"x": 50, "y": 88, "ic": "tool", "lab": "Tool", "sub": "analyzer"},
    },
    "wires": [["src", "mir"], ["mir", "coll"], ["coll", "tool"], ["kvo", "coll"]],
    "source": {
        "kind": "script",
        "patterns": [
            (r"Zone[- ]?Tapping IAM|IAM attached", "kvo", E.LIVE, "Least-privilege Zone-Tapping IAM is attached - KVO can call AWS on your behalf.", "good"),
            (r"AWS presence|cloud config|createCloudCollection", "kvo", E.LIVE, "AWS presence, cloud config and collection created.", "info"),
            (r"Nitro sources matched|sources matched", "src", E.LIVE, "Nitro sources matched by tag - only Nitro instances can be mirrored.", "good"),
            (r"collector up|target \+ filter|filter created", "coll", E.LIVE, "Collector up; traffic mirror target and filter created.", "good"),
            (r"collector Service VM|RunInstances|deploying collector", "coll", E.BUSY, "Deploying the collector Service VM and the mirror target.", "info"),
            (r"tool bound|monitoring policy committed", "tool", E.LIVE, "Tool bound and the monitoring policy committed.", "good"),
            (r"CreateTrafficMirrorSession|mirror session", "mir", E.LIVE, "VPC Traffic Mirror sessions created - one per source ENI.", "good"),
        ],
    },
}

FLOWS = {f["id"]: f for f in (STACK, SENSORS, KVO, MIRROR)}
ORDER = ["stack", "sensors", "kvo", "mirror"]


def match(patterns, line):
    """Return the first (node,status,text,tone) whose matcher hits `line`, else None.
    Case-insensitive; matchers key on message content, not the log prefix."""
    for matcher, node, status, text, tone in patterns:
        if re.search(matcher, line, re.IGNORECASE):
            return node, status, text, tone
    return None
