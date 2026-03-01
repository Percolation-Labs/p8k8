#!/usr/bin/env bash
# init-openbao.sh — Initialize OpenBao and store unseal keys + root token.
#
# ONE-TIME script. Run after deploying OpenBao in production mode.
# Creates K8s secret `openbao-unseal-keys` with unseal keys + root token.
# Also creates `openbao-eso-token` for ESO ClusterSecretStore.
#
# Usage:
#   ./manifests/scripts/init-openbao.sh --context=p8-w-1
#
# What it does:
#   1. kubectl exec into the OpenBao pod (no local bao/vault CLI needed)
#   2. Runs `bao operator init` (key-shares=3, key-threshold=2)
#   3. Saves unseal keys + root token to K8s secret `openbao-unseal-keys`
#   4. Unseals OpenBao using the generated keys
#   5. Creates `openbao-eso-token` secret for ESO bootstrap
#   6. Updates P8_KMS_VAULT_TOKEN in .env with the new root token
#
# After running this, restart the OpenBao pod so the auto-unseal init
# container picks up the keys:
#   kubectl --context=p8-w-1 -n p8 rollout restart statefulset/openbao

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
ENV_FILE="${ROOT_DIR}/.env"
NAMESPACE="p8"
CONTEXT=""
POD="openbao-0"

KEY_SHARES=3
KEY_THRESHOLD=2

for arg in "$@"; do
  case "$arg" in
    --context=*) CONTEXT="--context=${arg#--context=}" ;;
  esac
done

# Helper: run bao command inside the OpenBao pod
bao_exec() {
  kubectl $CONTEXT -n $NAMESPACE exec "$POD" -c openbao -- env BAO_ADDR=http://127.0.0.1:8200 "$@"
}

# --- Check pod is running ---
echo "Checking OpenBao pod..."
POD_STATUS=$(kubectl $CONTEXT -n $NAMESPACE get pod "$POD" -o jsonpath='{.status.phase}' 2>&1)
if [ "$POD_STATUS" != "Running" ]; then
  echo "Error: Pod $POD is not running (status: $POD_STATUS)"
  exit 1
fi
echo "Pod $POD is running."

# --- Check if already initialized ---
HEALTH=$(bao_exec bao status -format=json 2>&1 || true)

if echo "$HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('initialized') else 1)" 2>/dev/null; then
  echo "OpenBao is already initialized."
  SEALED=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('sealed', True))")
  if [ "$SEALED" = "True" ]; then
    echo "It's sealed — unseal keys should be in openbao-unseal-keys secret."
    echo "If you lost the keys, you need to re-deploy OpenBao with a fresh PVC."
  else
    echo "It's already unsealed. Nothing to do."
  fi
  exit 0
fi

# --- Initialize ---
echo ""
echo "Initializing OpenBao (shares=$KEY_SHARES, threshold=$KEY_THRESHOLD)..."
INIT_OUTPUT=$(bao_exec bao operator init \
  -key-shares=$KEY_SHARES \
  -key-threshold=$KEY_THRESHOLD \
  -format=json)

echo "$INIT_OUTPUT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'Root token: {d[\"root_token\"][:12]}...')
print(f'Unseal keys: {len(d[\"unseal_keys_b64\"])} keys generated')
"

# Extract values
ROOT_TOKEN=$(echo "$INIT_OUTPUT" | python3 -c "import sys,json; print(json.load(sys.stdin)['root_token'])")
UNSEAL_KEYS=$(echo "$INIT_OUTPUT" | python3 -c "
import sys, json
keys = json.load(sys.stdin)['unseal_keys_b64']
for i, k in enumerate(keys):
    print(f'--from-literal=unseal_key_{i}={k}')
" | tr '\n' ' ')

# --- Save unseal keys + root token to K8s secret ---
echo ""
echo "Saving unseal keys + root token to openbao-unseal-keys secret..."
eval "kubectl $CONTEXT -n $NAMESPACE create secret generic openbao-unseal-keys \
  $UNSEAL_KEYS \
  --from-literal=root_token=$ROOT_TOKEN \
  --dry-run=client -o yaml | kubectl $CONTEXT apply -f -"

# --- Create ESO bootstrap token ---
echo "Creating openbao-eso-token secret..."
kubectl $CONTEXT -n $NAMESPACE create secret generic openbao-eso-token \
  --from-literal=token="$ROOT_TOKEN" \
  --dry-run=client -o yaml | kubectl $CONTEXT apply -f -

# --- Unseal ---
echo ""
echo "Unsealing OpenBao..."
KEYS_B64=$(echo "$INIT_OUTPUT" | python3 -c "
import sys, json
for k in json.load(sys.stdin)['unseal_keys_b64']:
    print(k)
")

APPLIED=0
while IFS= read -r key; do
  echo "Applying unseal key $((APPLIED + 1))..."
  bao_exec bao operator unseal "$key" > /dev/null
  APPLIED=$((APPLIED + 1))
  if [ $APPLIED -ge $KEY_THRESHOLD ]; then
    break
  fi
done <<< "$KEYS_B64"

# Verify unsealed
SEALED=$(bao_exec bao status -format=json 2>&1 | python3 -c "import sys,json; print(json.load(sys.stdin).get('sealed', True))")
if [ "$SEALED" = "False" ]; then
  echo "OpenBao unsealed successfully."
else
  echo "Error: OpenBao still sealed after unseal attempt."
  exit 1
fi

# --- Update .env ---
echo ""
if [ -f "$ENV_FILE" ]; then
  if grep -q "^P8_KMS_VAULT_TOKEN=" "$ENV_FILE"; then
    OLD_TOKEN=$(grep "^P8_KMS_VAULT_TOKEN=" "$ENV_FILE" | cut -d= -f2-)
    if [ "$OLD_TOKEN" != "$ROOT_TOKEN" ]; then
      sed -i.bak "s|^P8_KMS_VAULT_TOKEN=.*|P8_KMS_VAULT_TOKEN=${ROOT_TOKEN}|" "$ENV_FILE"
      rm -f "${ENV_FILE}.bak"
      echo "Updated P8_KMS_VAULT_TOKEN in .env"
    fi
  else
    echo "P8_KMS_VAULT_TOKEN=${ROOT_TOKEN}" >> "$ENV_FILE"
    echo "Added P8_KMS_VAULT_TOKEN to .env"
  fi
fi

echo ""
echo "=== Done ==="
echo ""
echo "Next steps:"
echo "  1. Restart OpenBao so auto-unseal picks up the keys:"
echo "     kubectl $CONTEXT -n $NAMESPACE rollout restart statefulset/openbao"
echo ""
echo "  2. Re-run the init job to enable Transit + KV v2 engines:"
echo "     kubectl $CONTEXT -n $NAMESPACE delete job openbao-init-transit --ignore-not-found"
echo "     kubectl $CONTEXT apply -k manifests/platform/openbao/"
echo ""
echo "  3. Seed secrets into KV v2:"
echo "     ./manifests/scripts/seed-openbao.sh $CONTEXT"
echo ""
echo "  4. Update p8-app-secrets with new root token:"
echo "     ./manifests/scripts/sync-secrets.sh --apply $CONTEXT"
echo ""
echo "IMPORTANT: The unseal keys are stored in openbao-unseal-keys secret."
echo "Back up the root token and unseal keys securely."
echo "Root token: ${ROOT_TOKEN}"
