# Customer Experience Implementation Plan (AWS)

**Goal:** Bring the AWS repo to parity with the mature Azure repo: a world-class
polished README, two Word/PDF runbooks, SE enablement docs, and a landing site,
so any customer or SE can fully automate CloudLens sensor deployment to AWS EC2
and stand up the vController + KVO + vPB stack.

**Architecture:** One source of truth. README is the technical entry (Launch
Stack + CloudShell + Docker, decision tree, compatibility matrix, CloudFormation
and Terraform sections, sensor quickstart, Marketplace AMI table). The runbooks
are the executive deliverables (Keysight-branded, AWS palette, printable).
Supporting SVG assets render the visual diagrams in both.

**Tech stack:** Markdown, SVG (hand-authored), python-docx (Word generation),
cairosvg (SVG rasterisation), LibreOffice/soffice (PDF export), GitHub badges
via shields.io.

**Design source:** `docs/plans/2026-06-02-customer-experience-design.md`

---

## Pre-flight

**Step 1:** Verify environment

```bash
cd ~/cloudlens-ansible-aws
git status
python3 -c "import docx, cairosvg" 2>&1 || pip install --user python-docx cairosvg Pillow
ls deploy/cloudformation/stack.yaml deploy/terraform/stack
```

Expected: stack template and terraform module exist, python-docx and cairosvg
importable.

---

### Task 1: Confirm the SVG assets exist and match AWS

**Files:** `docs/assets/architecture-diagram.svg`, `decision-tree.svg`,
`scenario-matrix.svg`, `deploy-demo.svg`

The AWS repo already ships these. Confirm the architecture diagram shows the AWS
path (sensor -> KVO -> VPC Traffic Mirroring -> Collector SVM -> vPB -> tool) and
the decision tree branches on CloudFormation / CloudShell / Docker. Re-author if
they still show Azure.

**Verify:** open each SVG in a browser.

---

### Task 2: Rewrite README.md

**Files:** Modify `README.md`

Sections: hero + badges (Launch Stack quick-create, CloudShell, ghcr Docker),
one-command deploy-stack.sh story + overrides table, decision tree, EC2
compatibility matrix, CloudFormation section, Terraform section, Ansible sensor
quickstart, scaling table, architecture summary, Marketplace AMI table, docs
links + footer. No em dashes. Keep the existing "Why Ansible for AWS" and
"Compare to autopilot" value framing.

**Verify:** preview the README on GitHub; all badges clickable, Launch Stack URL
resolves to the CloudFormation quick-create console.

---

### Task 3: Port OPERATIONS.md

**Files:** Create `docs/OPERATIONS.md`

Adapt the Azure operations guide to AWS: ports/creds table (vPB SSH 9022,
vController admin/Cl0udLens@dm!n), SSH via key pairs, security groups instead of
NSGs, KVO adoption flow, VPC Traffic Mirroring path, Marketplace instance-type
gotchas, the AMI lookup recipe, and the sensor deployment troubleshooting matrix.

---

### Task 4: Port the SE enablement docs

**Files:** Create `docs/SE_DEMO_PLAYBOOK.md`, `docs/SE_PROSPECT_EMAIL.md`,
`docs/SITE_README.md`

30-minute demo script (frame -> Launch Stack -> live sensors -> KVO Cloud Config
-> packets at the tool), three prospect email variants (inline-averse,
NDR-augmentation, EC2+EKS), and the landing-site maintenance readme.

---

### Task 5: Port the runbook generators and build the docs

**Files:** Create `docs/generate_runbook.py`, `docs/generate_stack_runbook.py`

python-docx generators with the AWS palette. Output
`CloudLens_Ansible_AWS_Customer_Runbook.docx` and
`CloudLens_Stack_Deployment_Runbook.docx`.

**Run:**

```bash
python3 docs/generate_runbook.py
python3 docs/generate_stack_runbook.py
```

**Export PDF:**

```bash
soffice --headless --convert-to pdf --outdir docs \
  docs/CloudLens_Ansible_AWS_Customer_Runbook.docx \
  docs/CloudLens_Stack_Deployment_Runbook.docx
```

**Verify:** open each .docx and .pdf; cover page, TOC, tables, embedded diagrams
render with AWS branding.

---

### Task 6: Push and polish

**Steps:**

1. `git add docs README.md && git commit -m "docs: AWS customer + SE enablement parity with Azure"`
2. `git push origin main`
3. Open https://github.com/Keysight-Tech/cloudlens-ansible-aws and confirm the
   README renders, badges click, and the docs links resolve.
4. Polish pass: typos, broken links, image scaling on mobile.

---

## Complete

- README has hero + Launch Stack/CloudShell/Docker + decision tree + matrix +
  CloudFormation + Terraform + sensor quickstart + scaling + architecture +
  Marketplace AMI table.
- `docs/OPERATIONS.md`, `SE_DEMO_PLAYBOOK.md`, `SE_PROSPECT_EMAIL.md`,
  `SITE_README.md` in place.
- `docs/CloudLens_Ansible_AWS_Customer_Runbook.docx/.pdf` and
  `docs/CloudLens_Stack_Deployment_Runbook.docx/.pdf` generated.
- Existing good AWS docs and screenshots preserved.
- Pushed to https://github.com/Keysight-Tech/cloudlens-ansible-aws
