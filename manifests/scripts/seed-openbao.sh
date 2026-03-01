#!/usr/bin/env bash
# seed-openbao.sh — Write secrets from .env into OpenBao KV v2.
#
# Usage:
#   ./manifests/scripts/seed-openbao.sh --context=p8-w-1          # seed from .env
#   ./manifests/scripts/seed-openbao.sh --context=p8-w-1 --dry-run  # show what would be written
#
# Prerequisites:
#   - OpenBao running, initialized, and unsealed
#   - .env file with secrets
#   - Root token stored in openbao-unseal-keys K8s secret
#
# Uses kubectl exec to run bao commands inside the pod (no local bao CLI needed).
# Writes secrets to KV v2 paths that ESO ExternalSecrets reference:
#   secret/p8/app-secrets           — all P8_* secret env vars
#   secret/p8/database-credentials  — username + password
#   secret/p8/keda-pg-connection    — connection string

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
ENV_FILE="${ROOT_DIR}/.env"
CONTEXT=""
DRY_RUN=false
NAMESPACE="p8"
POD="openbao-0"

for arg in "$@"; do
  case "$arg" in
    --context=*) CONTEXT="--context=${arg#--context=}" ;;
    --dry-run) DRY_RUN=true ;;
  esac
done

if [ ! -f "$ENV_FILE" ]; then
  echo "Error: .env file not found at $ENV_FILE"
  exit 1
fi

# Read a key from .env
env_val() {
  grep "^${1}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true
}

# Get root token from K8s secret
ROOT_TOKEN=$(kubectl $CONTEXT -n $NAMESPACE get secret openbao-unseal-keys \
  -o jsonpath='{.data.root_token}' 2>/dev/null | base64 -d 2>/dev/null || true)

if [ -z "$ROOT_TOKEN" ]; then
  echo "Error: Could not read root token from openbao-unseal-keys secret."
  echo "Run init-openbao.sh first."
  exit 1
fi

# Helper: run bao command inside the OpenBao pod
bao_exec() {
  kubectl $CONTEXT -n $NAMESPACE exec "$POD" -c openbao -- \
    env BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN="$ROOT_TOKEN" "$@"
}

# Verify connectivity + unsealed
echo "Checking OpenBao status..."
SEALED=$(bao_exec bao status -format=json 2>&1 | python3 -c "import sys,json; print(json.load(sys.stdin).get('sealed', True))" 2>/dev/null || echo "Error")
if [ "$SEALED" != "False" ]; then
  echo "Error: OpenBao is not unsealed (sealed=$SEALED). Unseal it first."
  exit 1
fi
echo "OpenBao is unsealed."

# --- Secret keys for p8-app-secrets (same list as sync-secrets.sh) ---
APP_SECRET_KEYS=(
  P8_OPENAI_API_KEY
  P8_API_KEY
  P8_AUTH_SECRET_KEY
  P8_GOOGLE_CLIENT_ID
  P8_GOOGLE_CLIENT_SECRET
  P8_APPLE_CLIENT_ID
  P8_APPLE_TEAM_ID
  P8_APPLE_KEY_ID
  P8_APPLE_PRIVATE_KEY_PATH
  P8_KMS_VAULT_TOKEN
  P8_S3_ACCESS_KEY_ID
  P8_S3_SECRET_ACCESS_KEY
  P8_STRIPE_SECRET_KEY
  P8_STRIPE_PUBLISHABLE_KEY
  P8_STRIPE_WEBHOOK_SECRET
  P8_SMTP_USERNAME
  P8_SMTP_PASSWORD
  P8_RESEND_API_KEY
  P8_MS_GRAPH_TENANT_ID
  P8_MS_GRAPH_CLIENT_ID
  P8_MS_GRAPH_CLIENT_SECRET
  P8_FCM_PROJECT_ID
  P8_FCM_SERVICE_ACCOUNT_FILE
  P8_TAVILY_API_KEY
  P8_SLACK_BOT_TOKEN
  P8_SLACK_APP_ID
  P8_SLACK_CLIENT_SECRET
  P8_SLACK_SIGNING_SECRET
  P8_SLACK_VERIFICATION_TOKEN
  PHOENIX_API_KEY
  PHOENIX_ENDPOINT
)

# --- 1. Write p8/app-secrets ---
echo ""
echo "=== secret/p8/app-secrets ==="
KV_ARGS=""
SET_COUNT=0
for key in "${APP_SECRET_KEYS[@]}"; do
  val=$(env_val "$key")
  if [ -n "$val" ]; then
    KV_ARGS="${KV_ARGS} ${key}=${val}"
    SET_COUNT=$((SET_COUNT + 1))
    if [ "$DRY_RUN" = true ]; then
      echo "  SET  ${key}=${val:0:12}..."
    fi
  else
    if [ "$DRY_RUN" = true ]; then
      echo "  SKIP ${key} (empty)"
    fi
  fi
done

if [ "$DRY_RUN" = false ]; then
  # shellcheck disable=SC2086
  bao_exec bao kv put secret/p8/app-secrets $KV_ARGS
  echo "Written $SET_COUNT keys."
fi

# --- 2. Write p8/database-credentials ---
echo ""
echo "=== secret/p8/database-credentials ==="
DB_USER=$(env_val P8_DB_USERNAME)
DB_PASS=$(env_val P8_DB_PASSWORD)
# Fallback: try parsing DATABASE_URL
if [ -z "$DB_USER" ] || [ -z "$DB_PASS" ]; then
  DB_URL=$(env_val P8_DATABASE_URL)
  if [ -z "$DB_URL" ]; then
    DB_URL=$(env_val DATABASE_URL)
  fi
  if [ -n "$DB_URL" ]; then
    # postgresql://user:pass@host:port/dbname
    DB_USER=${DB_USER:-$(echo "$DB_URL" | sed -n 's|.*://\([^:]*\):.*|\1|p')}
    DB_PASS=${DB_PASS:-$(echo "$DB_URL" | sed -n 's|.*://[^:]*:\([^@]*\)@.*|\1|p')}
  fi
fi

if [ "$DRY_RUN" = true ]; then
  echo "  username=${DB_USER:-<empty>}"
  echo "  password=${DB_PASS:+${DB_PASS:0:8}...}"
else
  if [ -n "$DB_USER" ] && [ -n "$DB_PASS" ]; then
    bao_exec bao kv put secret/p8/database-credentials username="$DB_USER" password="$DB_PASS"
    echo "Written."
  else
    echo "Warning: Could not determine DB credentials from .env (tried P8_DB_USERNAME/P8_DB_PASSWORD and DATABASE_URL). Skipping."
  fi
fi

# --- 3. Write p8/keda-pg-connection ---
echo ""
echo "=== secret/p8/keda-pg-connection ==="
KEDA_CONN=$(env_val P8_KEDA_PG_CONNECTION)
if [ -z "$KEDA_CONN" ]; then
  # Build from database URL if available
  DB_URL=$(env_val P8_DATABASE_URL)
  if [ -z "$DB_URL" ]; then
    DB_URL=$(env_val DATABASE_URL)
  fi
  KEDA_CONN="$DB_URL"
fi

if [ "$DRY_RUN" = true ]; then
  echo "  connection=${KEDA_CONN:+${KEDA_CONN:0:30}...}"
else
  if [ -n "$KEDA_CONN" ]; then
    bao_exec bao kv put secret/p8/keda-pg-connection connection="$KEDA_CONN"
    echo "Written."
  else
    echo "Warning: No connection string found (tried P8_KEDA_PG_CONNECTION, P8_DATABASE_URL, DATABASE_URL). Skipping."
  fi
fi

echo ""
if [ "$DRY_RUN" = true ]; then
  echo "Dry run complete. Run without --dry-run to write to OpenBao."
else
  echo "All secrets seeded. ESO will sync them to K8s on next refresh (up to 1h)."
  echo "Force immediate sync: kubectl $CONTEXT -n $NAMESPACE annotate externalsecrets --all force-sync=$(date +%s) --overwrite"
fi
