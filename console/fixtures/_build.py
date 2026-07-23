"""Generate the four replay fixtures - realistic event streams that mirror the
actual deploy sequences proven in the reference AWS account. Run:  python3 _build.py
Each fixture is a list of {"_delay": seconds, "event": {...}} frames the console
replays with no AWS calls."""
import json, os

HERE = os.path.dirname(__file__)

def st(node, status, label=None):
    e = {"type": "state", "node": node, "status": status}
    if label: e["label"] = label
    return e
def log(t): return {"type": "log", "text": t}
def nar(t, tone="info"): return {"type": "narrate", "text": t, "tone": tone}
def stat(**k): k["type"] = "stat"; return k
def done(s, **o): return {"type": "done", "summary": s, "outputs": o}

def frames(seq):
    out = []
    for f in seq:
        d, e = (f if isinstance(f, tuple) else (0.7, f))
        out.append({"_delay": d, "event": e})
    return out

# ---------------- FLOW 01: full stack (CloudFormation) ----------------
stack = frames([
    (0.2, st("vpc","ghost")), (0.15, st("clms","ghost")), (0.15, st("kvo","ghost")), (0.2, st("vpb","ghost")),
    (0.6, log("$ aws cloudformation deploy --stack-name cloudlens-live --region us-east-1")),
    (0.9, log("Waiting for changeset to be created..")),
    (0.7, st("vpc","busy")), (0.5, log("CREATE_IN_PROGRESS  AWS::EC2::VPC  Vpc")),
    (1.1, st("vpc","live","live")), (0.2, log("CREATE_COMPLETE     AWS::EC2::VPC  Vpc")),
    (0.4, nar("Network foundation is up: the VPC and gateway exist before anything lands in it.","good")),
    (0.6, log("CREATE_COMPLETE     AWS::EC2::Subnet  MgmtSubnet / DataSubnet / ToolSubnet")),
    (0.6, stat(created=1)),
    (0.7, log("CREATE_IN_PROGRESS  AWS::EC2::EIP  VcontrollerEip")),
    (0.5, nar("Elastic IPs are reserved first, so an over-quota account fails in seconds, not six minutes in with instance-hours burned.","note")),
    (0.8, st("clms","busy")), (0.4, log("CREATE_IN_PROGRESS  AWS::EC2::Instance  VcontrollerInstance")),
    (0.5, nar("Launching the CloudLens Manager: the control plane every sensor registers to.","info")),
    (1.4, st("clms","live","live")), (0.2, log("CREATE_COMPLETE     AWS::EC2::Instance  VcontrollerInstance")),
    (0.4, nar("vController is up. It still needs ~15 min to initialize before you can log in.","good")),
    (0.5, stat(created=2)),
    (0.7, st("kvo","busy")), (0.4, log("CREATE_IN_PROGRESS  AWS::EC2::Instance  KvoInstance")),
    (0.5, nar("Launching KVO: the single pane that adopts the manager, the vPB, and drives AWS mirroring.","info")),
    (1.3, st("kvo","live","live")), (0.2, log("CREATE_COMPLETE     AWS::EC2::Instance  KvoInstance")),
    (0.5, stat(created=3)),
    (0.7, st("vpb","busy")), (0.4, log("CREATE_IN_PROGRESS  AWS::EC2::Instance  VpbInstance")),
    (0.5, nar("Launching the virtual packet broker: filters and forwards tapped traffic to your tools.","info")),
    (1.3, st("vpb","live","live")), (0.2, log("CREATE_COMPLETE     AWS::EC2::Instance  VpbInstance")),
    (0.5, stat(created=4)),
    (0.6, log("CREATE_COMPLETE     AWS::CloudFormation::Stack  cloudlens-live")),
    (0.4, done("Stack CREATE_COMPLETE: 3 appliances up. They need ~15 min to initialize.",
               note="Log in at the vController URL once initialized, then run Flow 02 or 03.")),
])

# ---------------- FLOW 02: CLMS + sensors ----------------
sensors = frames([
    (0.2, st("clms","ghost")), (0.15, st("u","ghost")), (0.15, st("r","ghost")), (0.2, st("w","ghost")),
    (0.6, log("$ vcontroller_project_key.py --clms 20.84.115.190")),
    (0.9, st("clms","busy")), (0.6, log("[vctl] login ok · rotating first-login password")),
    (0.8, st("clms","live","project")), (0.3, nar("Project key retrieved. The forced first-login password change was handled automatically.","good")),
    (0.6, log("$ ansible-playbook -i inventory/aws_ec2.yaml deploy.yaml")),
    (0.7, st("u","busy")), (0.5, log("TASK [ubuntu : docker run …/sensor --accept_eula yes]")),
    (1.0, st("u","live","200")), (0.3, nar("Ubuntu sensor registered: Register status 200.","good")),
    (0.7, st("r","busy")), (0.5, log("TASK [redhat : podman run …/sensor]")),
    (1.0, st("r","live","200")), (0.3, nar("RHEL sensor registered.","good")),
    (0.7, st("w","busy")), (0.5, log("TASK [windows : cloudlens-win-sensor.exe /install /quiet]")),
    (1.1, st("w","live","200")), (0.3, nar("Windows sensor registered: service CloudLens running.","good")),
    (0.6, log("PLAY RECAP  WebServerLB1 ok=27  WebServerLB2 ok=25  brine-winvm ok=25  failed=0")),
    (0.4, done("3 sensors registered · sensorCount = 3.", note="Watch them in the vController UI under your project.")),
])

# ---------------- FLOW 03: KVO + vPB + sensors ----------------
kvo = frames([
    (0.2, st("kvo","ghost")), (0.15, st("clms","ghost")), (0.15, st("vpb","ghost")), (0.15, st("tool","ghost")), (0.2, st("vm","ghost")),
    (0.6, log("[kvo-adopt] KVO EULA accepted · licenses active")),
    (0.7, st("kvo","live","licensed")), (0.3, nar("EULA accepted and licenses active: every KVO write is unblocked.","good")),
    (0.6, st("clms","busy")), (0.5, log("[kvo-adopt] createCloudLensManager · committing change request")),
    (1.1, log("[kvo-adopt]   clms-live status: CONNECTED")),
    (0.5, st("clms","live","CONNECTED")), (0.3, nar("CLMS is CONNECTED; the Cloud Config provisions the working project key.","good")),
    (0.6, log("[vpb-adopt] vPB: kvo ▸ ip ▸ port 443 ▸ enable → KVO enabled")),
    (0.6, st("vpb","busy")), (0.6, log("[vpb-adopt] vPB announced: STANDARD_VPB · adopting with control")),
    (1.2, log("[vpb-adopt]   vpb-prod availability: Online · auto-licensed")),
    (0.5, st("vpb","live","Online")), (0.3, nar("vPB is Online and auto-licensed: KVO built its Device Config.","good")),
    (0.7, log("[vpb-adopt] ports bound · eth1 → C2DL · eth2 → tool")),
    (0.5, st("tool","live","bound")), (0.3, nar("Ports bound: ingress to the Cloud-to-Device Link, egress to the tool.","good")),
    (0.7, log("[kvo-adopt] monitoring policy committed · source → vPB → tool")),
    (0.5, st("vm","live","3 hosts")), (0.3, nar("Sensors registered under KVO management.","good")),
    (0.4, done("Fabric live in KVO: CLMS CONNECTED, vPB Online, sensors managed.")),
])

# ---------------- FLOW 04: AWS mirror session ----------------
mirror = frames([
    (0.2, st("src","ghost")), (0.15, st("mir","ghost")), (0.15, st("coll","ghost")), (0.15, st("kvo","ghost")), (0.2, st("tool","ghost")),
    (0.6, log("[kvo-mirror] Zone-Tapping IAM attached: least privilege")),
    (0.6, st("kvo","live","IAM ok")), (0.3, nar("Least-privilege Zone-Tapping IAM is attached: KVO can call AWS on your behalf.","good")),
    (0.7, log("[kvo-mirror] AWS presence → cloud config → collection (tag cloudlens=yes)")),
    (0.6, st("src","busy")), (0.6, log("[kvo-mirror] DescribeInstances: matching Nitro sources")),
    (0.9, st("src","live","3 Nitro")), (0.3, nar("Nitro sources matched by tag. Only Nitro instances can be mirrored; put sensors on the rest.","good")),
    (0.7, st("coll","busy")), (0.6, log("[kvo-mirror] RunInstances: collector Service VM · launch template + ASG")),
    (1.3, log("[kvo-mirror] collector up · CreateTrafficMirrorTarget · CreateTrafficMirrorFilter")),
    (0.5, st("coll","live","up")), (0.3, nar("Collector up; traffic-mirror target and filter created.","good")),
    (0.6, st("tool","live","bound")), (0.4, log("[kvo-mirror] tool bound · monitoring policy committed")),
    (0.3, nar("Tool bound and the monitoring policy committed.","good")),
    (0.7, st("mir","busy")), (0.6, log("[kvo-mirror] CreateTrafficMirrorSession × 3")),
    (0.9, st("mir","live","3 sessions")), (0.3, nar("VPC Traffic Mirror sessions created, one per source ENI. Packets are flowing to the tool.","good")),
    (0.4, done("3 mirror sessions live. Verify in VPC → Traffic Mirroring.")),
])

for name, seq in [("stack", stack), ("sensors", sensors), ("kvo", kvo), ("mirror", mirror)]:
    with open(os.path.join(HERE, name + ".json"), "w") as fh:
        json.dump(seq, fh, indent=1)
    print("wrote", name + ".json", "(", len(seq), "frames )")
