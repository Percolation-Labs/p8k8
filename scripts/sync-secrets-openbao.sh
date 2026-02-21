#!/usr/bin/env bash
# sync-secrets-openbao.sh — Load p8-app-secrets from .env into OpenBao KV v2
#
# Reads .env, filters to only keys that exist in p8-app-secrets stringData,
# then writes them to secret/p8/app-secrets.
#
# Uses `bao` CLI if available, otherwise falls back to curl + HTTP API.
#
# Environment:
#   BAO_ADDR   — OpenBao address (default: http://127.0.0.1:8200)
#   BAO_TOKEN  — OpenBao token (default: reads from VAULT_TOKEN)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

export BAO_ADDR="${BAO_ADDR:-http://127.0.0.1:8200}"
export BAO_TOKEN="${BAO_TOKEN:-${VAULT_TOKEN:-}}"

if [[ -z "$BAO_TOKEN" ]]; then
  echo "ERROR: BAO_TOKEN (or VAULT_TOKEN) must be set" >&2
  exit 1
fi

# --- Pick transport: bao CLI or curl ---
USE_CLI=false
if command -v bao &>/dev/null; then
  USE_CLI=true
  echo "Using: bao CLI"
else
  echo "Using: curl (bao CLI not found)"
fi

# --- Locate .env ---
ENV_FILE="${REPO_ROOT}/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: No .env found at repo root" >&2
  exit 1
fi

# --- Parse p8-app-secrets stringData keys from secrets.yaml ---
SECRETS_FILE="${REPO_ROOT}/manifests/application/p8-stack/overlays/hetzner/secrets.yaml"
allowed_keys=""

if [[ -f "$SECRETS_FILE" ]]; then
  in_app_secrets=false
  in_stringdata=false
  while IFS= read -r line; do
    if [[ "$line" =~ name:[[:space:]]*p8-app-secrets ]]; then
      in_app_secrets=true
      continue
    fi
    if [[ "$line" == "---" ]]; then
      in_app_secrets=false
      in_stringdata=false
      continue
    fi
    if $in_app_secrets && [[ "$line" =~ ^stringData: ]]; then
      in_stringdata=true
      continue
    fi
    if $in_stringdata; then
      if [[ "$line" =~ ^[a-zA-Z] ]]; then
        in_stringdata=false
        in_app_secrets=false
        continue
      fi
      [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
      key="${line%%:*}"
      key="$(echo "$key" | sed 's/^[[:space:]]*//')"
      [[ -n "$key" ]] && allowed_keys="${allowed_keys}${key}"$'\n'
    fi
  done < "$SECRETS_FILE"
fi

allowed_count=$(echo -n "$allowed_keys" | grep -c '.' || true)
echo "Allowed keys from p8-app-secrets: ${allowed_count}"

# --- Read .env and collect matching key=value pairs ---
kv_pairs=()
json_data="{"
first=true
while IFS= read -r line; do
  [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
  key="${line%%=*}"
  key="$(echo "$key" | sed 's/^[[:space:]]*//')"
  value="${line#*=}"
  if echo "$allowed_keys" | grep -qx "$key"; then
    kv_pairs+=("${key}=${value}")
    # Build JSON for curl path — escape double quotes in value
    escaped_value="$(echo "$value" | sed 's/\\/\\\\/g; s/"/\\"/g')"
    if $first; then
      first=false
    else
      json_data="${json_data},"
    fi
    json_data="${json_data}\"${key}\":\"${escaped_value}\""
  fi
done < "$ENV_FILE"
json_data="${json_data}}"

if [[ ${#kv_pairs[@]} -eq 0 ]]; then
  echo "No matching keys found in .env — nothing to sync."
  exit 0
fi

echo "Syncing ${#kv_pairs[@]} key(s) to OpenBao at ${BAO_ADDR}..."

if $USE_CLI; then
  # --- CLI path ---
  if ! bao secrets list -format=json 2>/dev/null | grep -q '"secret/"'; then
    echo "Enabling KV v2 engine at secret/..."
    bao secrets enable -path=secret -version=2 kv 2>/dev/null || true
  fi
  bao kv put secret/p8/app-secrets "${kv_pairs[@]}"
else
  # --- curl path ---
  # Enable KV v2 at secret/ (409 = already exists, that's fine)
  http_code=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "${BAO_ADDR}/v1/sys/mounts/secret" \
    -H "X-Vault-Token: ${BAO_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"type":"kv","options":{"version":"2"}}')
  if [[ "$http_code" == "204" ]]; then
    echo "Enabled KV v2 engine at secret/"
  elif [[ "$http_code" == "400" ]]; then
    echo "KV v2 engine already mounted at secret/"
  else
    echo "Mount check returned HTTP ${http_code} (continuing)"
  fi

  # Write secrets
  response=$(curl -s -w "\n%{http_code}" \
    -X POST "${BAO_ADDR}/v1/secret/data/p8/app-secrets" \
    -H "X-Vault-Token: ${BAO_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"data\":${json_data}}")

  http_code=$(echo "$response" | tail -1)
  body=$(echo "$response" | sed '$d')

  if [[ "$http_code" != "200" && "$http_code" != "204" ]]; then
    echo "ERROR: OpenBao returned HTTP ${http_code}" >&2
    echo "$body" >&2
    exit 1
  fi
fi

echo "Done. Wrote ${#kv_pairs[@]} key(s) to secret/p8/app-secrets"
