#!/usr/bin/env bash
# =====================================================================
# Azure Service Principal Setup Helper
# =====================================================================
# Creates a Service Principal with the required roles for Ansible
# dynamic inventory and VM management.
# =====================================================================
set -euo pipefail

SP_NAME="${SP_NAME:-cloudlens-ansible-sp}"

# --- Verify Azure CLI login ---
if ! az account show >/dev/null 2>&1; then
  echo "Logging into Azure..."
  az login
fi

# --- Prompt for subscription if not provided ---
SUBSCRIPTION_ID="${AZURE_SUBSCRIPTION_ID:-$(az account show --query id -o tsv)}"
echo "Subscription: $SUBSCRIPTION_ID"
read -rp "Use this subscription? [Y/n] " confirm
if [[ "${confirm,,}" == "n" ]]; then
  az account list -o table
  read -rp "Enter subscription ID: " SUBSCRIPTION_ID
  az account set --subscription "$SUBSCRIPTION_ID"
fi

# --- Create SP ---
echo ""
echo "Creating Service Principal: $SP_NAME"
echo "Scope: /subscriptions/$SUBSCRIPTION_ID"
echo "Role: Virtual Machine Contributor + Reader"
echo ""

SP_JSON=$(az ad sp create-for-rbac \
  --name "$SP_NAME" \
  --role "Virtual Machine Contributor" \
  --scopes "/subscriptions/$SUBSCRIPTION_ID")

# Add Reader role for inventory discovery
SP_APP_ID=$(echo "$SP_JSON" | python3 -c "import sys, json; print(json.load(sys.stdin)['appId'])")
az role assignment create \
  --assignee "$SP_APP_ID" \
  --role "Reader" \
  --scope "/subscriptions/$SUBSCRIPTION_ID" >/dev/null

# --- Save creds ---
CREDS_FILE="azure_sp_creds.json"
echo "$SP_JSON" > "$CREDS_FILE"
chmod 600 "$CREDS_FILE"

# --- Generate env-export script ---
TENANT=$(echo "$SP_JSON" | python3 -c "import sys, json; print(json.load(sys.stdin)['tenant'])")
SECRET=$(echo "$SP_JSON" | python3 -c "import sys, json; print(json.load(sys.stdin)['password'])")

cat > scripts/load_sp_creds.sh <<EOF
#!/usr/bin/env bash
# Source this file: source scripts/load_sp_creds.sh
export AZURE_SUBSCRIPTION_ID="$SUBSCRIPTION_ID"
export AZURE_TENANT="$TENANT"
export AZURE_CLIENT_ID="$SP_APP_ID"
export AZURE_SECRET="$SECRET"
echo "✓ Azure SP credentials loaded"
EOF
chmod 600 scripts/load_sp_creds.sh

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "✓ Service Principal created successfully"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "Credentials saved to:"
echo "  $CREDS_FILE                  (JSON, do NOT commit)"
echo "  scripts/load_sp_creds.sh     (env export script)"
echo ""
echo "Load credentials in your shell:"
echo "  source scripts/load_sp_creds.sh"
echo ""
echo "Or set manually:"
echo "  export AZURE_SUBSCRIPTION_ID=$SUBSCRIPTION_ID"
echo "  export AZURE_TENANT=$TENANT"
echo "  export AZURE_CLIENT_ID=$SP_APP_ID"
echo "  export AZURE_SECRET='<see $CREDS_FILE>'"
echo ""
