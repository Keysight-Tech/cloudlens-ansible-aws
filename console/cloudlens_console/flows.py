"""The four deployment flows, as data: what each one is called, what it
asks for, and the diagram that stands for it.

Two readers, and they read the same seven keys - id, name, script,
subtitle, inputs, nodes, wires:

  GET /flows        the console page draws the four cards under the
                    operations screens from this
  build_site.py     the published demo page (docs/console.html) is built
                    from this plus the captured fixtures next door, and
                    replays them client-side

Nothing here starts anything. The console runs deploy-stack.sh through
one engine (orchestrator.run_engine), started by the operations API and
by nothing else; the four "quick flows" that used to run from here - a
second, unlocked way to start a real deploy, with three of the four
commands broken - are gone, and with them the per-flow log matchers and
argv builders that only they used.
"""
from __future__ import annotations


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
}

FLOWS = {f["id"]: f for f in (STACK, SENSORS, KVO, MIRROR)}
ORDER = ["stack", "sensors", "kvo", "mirror"]
