#!/usr/bin/env bash
# sync-secrets.sh — Create/update p8-app-secrets from .env file.
#
# Usage:
#   ./manifests/scripts/sync-secrets.sh                    # dry-run (shows diff)
#   ./manifests/scripts/sync-secrets.sh --apply            # apply to cluster
#   ./manifests/scripts/sync-secrets.sh --apply --context=p8-w-1  # explicit context
#
# This replaces the old approach of including secrets.yaml in kustomize,
# which overwrote patched values with placeholders on every apply -k.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
ENV_FILE="${ROOT_DIR}/.env"
NAMESPACE="p8"
SECRET_NAME="p8-app-secrets"
CONTEXT=""
APPLY=false

for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=true ;;
    --context=*) CONTEXT="--context=${arg#--context=}" ;;
  esac
done

if [ ! -f "$ENV_FILE" ]; then
  echo "Error: .env file not found at $ENV_FILE"
  exit 1
fi

# Secret keys that belong in p8-app-secrets (not configmap)
SECRET_KEYS=(
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
  P8_FCM_PROJECT_ID
  P8_FCM_SERVICE_ACCOUNT_FILE
  PHOENIX_API_KEY
  PHOENIX_ENDPOINT
)

# Build --from-literal args from .env
ARGS=()
for key in "${SECRET_KEYS[@]}"; do
  # Extract value from .env (skip comments, handle empty values)
  val=$(grep "^${key}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true)
  ARGS+=("--from-literal=${key}=${val}")
done

CMD="kubectl ${CONTEXT} -n ${NAMESPACE} create secret generic ${SECRET_NAME} ${ARGS[*]} --dry-run=client -o yaml | kubectl ${CONTEXT} apply -f -"

if [ "$APPLY" = true ]; then
  echo "Syncing ${SECRET_NAME} from .env → cluster..."
  eval "$CMD"
  echo "Done. Restart deployments to pick up changes:"
  echo "  kubectl ${CONTEXT} -n ${NAMESPACE} rollout restart deploy/p8-api"
else
  echo "Dry run — would create/update ${SECRET_NAME} with these keys:"
  echo ""
  for key in "${SECRET_KEYS[@]}"; do
    val=$(grep "^${key}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true)
    if [ -z "$val" ]; then
      echo "  EMPTY  $key"
    else
      echo "  SET    $key=${val:0:12}..."
    fi
  done
  echo ""
  echo "Run with --apply to update the cluster."
fi
