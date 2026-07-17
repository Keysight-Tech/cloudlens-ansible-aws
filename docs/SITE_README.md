# CloudLens Ansible for AWS: Landing Site

This directory contains the static GitHub Pages site published at:

**https://keysight-tech.github.io/cloudlens-ansible-aws/**

## Files

| File | Purpose |
|---|---|
| `index.html` | Single-page landing site, all sections |
| `styles.css` | Theming, layout, dark mode, responsive |
| `glass.css` | Glassmorphism surface treatment layered over `styles.css` |
| `script.js` | Wizard, scaling slider, theme toggle, smooth scroll |
| `assets/*.svg` | Architecture, decision-tree, scenario-matrix, deploy-demo diagrams |
| `assets/*.png` | Product UI screenshots used in the deployment walkthrough |
| `.nojekyll` | Prevents GitHub Pages from running Jekyll on this folder |

## Enable GitHub Pages

1. Open the repo on GitHub: `https://github.com/Keysight-Tech/cloudlens-ansible-aws`
2. Go to **Settings** -> **Pages**
3. Under **Build and deployment**, set:
   - **Source:** Deploy from a branch
   - **Branch:** `main`
   - **Folder:** `/docs`
4. Click **Save**
5. Wait 60 to 120 seconds. The site goes live at `https://keysight-tech.github.io/cloudlens-ansible-aws/`

The `.nojekyll` file in this folder tells GitHub Pages to serve the HTML
directly without Jekyll processing (so paths beginning with an underscore are
not stripped).

## Local preview

Open `index.html` directly in a browser, or run a quick local server:

```bash
cd docs
python3 -m http.server 8080
# Visit http://localhost:8080
```

## Updating the site

- Hero copy and the Launch Stack button are in `index.html` near the top
- Brand colors and dark-mode tokens are CSS variables at the top of `styles.css`
- The glass surface tuning lives in `glass.css`
- The scaling-slider bands are defined in `script.js`
- If you regenerate the SVGs, keep them in `docs/assets/`; the docx runbook
  generator (`docs/generate_runbook.py`) rasterises the same SVGs via cairosvg

## The Launch Stack link

The site's primary call to action is a CloudFormation quick-create deep link:

```
https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/quickcreate?templateURL=https://keysight-cloudlens-templates.s3.us-east-1.amazonaws.com/aws/stack.yaml&stackName=cloudlens-stack
```

The `templateURL` points at the public S3 bucket
`keysight-cloudlens-templates` (account 466778915280), not GitHub raw -
CloudFormation refuses `raw.githubusercontent.com` URLs. Re-sync S3 after
any template change:

```bash
AWS_PROFILE=AdministratorAccess-466778915280 \
  bash deploy/scripts/sync-cfn-templates-to-s3.sh
```

If you cut a release branch and want the button to pin to that branch's
templates, add a matching prefix (e.g. `aws/v1.2/`) in the sync script and
update the URLs in `index.html`.

## Self-contained, no build step

The site is pure HTML, CSS, and vanilla JavaScript. No npm, no bundler, no
framework. It renders straight from the `/docs` folder on GitHub Pages.
