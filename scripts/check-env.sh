#!/usr/bin/env bash
# check-env.sh — Validate that every .env key is covered by K8s manifests
#
# Parses keys from .env (falls back to .env.example), then checks each key
# against configMapGenerator literals (base + hetzner overlay) and the
# p8-app-secrets stringData. Reports any gaps.
#
# Exit 0 = all covered, exit 1 = gaps found.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# --- Locate .env source ---
ENV_FILE="${REPO_ROOT}/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  ENV_FILE="${REPO_ROOT}/.env.example"
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: No .env or .env.example found at repo root" >&2
  exit 2
fi
echo "Using env file: ${ENV_FILE##"$REPO_ROOT"/}"

# --- Parse .env keys (skip comments and blank lines) ---
env_keys=()
while IFS= read -r line; do
  [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
  key="${line%%=*}"
  key="$(echo "$key" | sed 's/^[[:space:]]*//')"
  [[ -n "$key" ]] && env_keys+=("$key")
done < "$ENV_FILE"

echo "Found ${#env_keys[@]} keys in env file"

# --- Parse configMapGenerator literals from kustomization files ---
configmap_keys=()
parse_configmap_literals() {
  local file="$1"
  [[ -f "$file" ]] || return 0
  local in_literals=false
  while IFS= read -r line; do
    if [[ "$line" =~ ^[[:space:]]+literals: ]]; then
      in_literals=true
      continue
    fi
    if $in_literals; then
      if [[ "$line" =~ ^[[:space:]]+-[[:space:]] ]]; then
        local item="${line#*- }"
        item="${item%%=*}"
        item="$(echo "$item" | sed 's/^[[:space:]]*//' | sed 's/[[:space:]]*#.*//')"
        [[ -n "$item" ]] && configmap_keys+=("$item")
      elif [[ "$line" =~ ^[[:space:]]*# ]]; then
        continue
      elif [[ -n "$line" && ! "$line" =~ ^[[:space:]]*$ ]]; then
        in_literals=false
      fi
    fi
  done < "$file"
}

BASE_KUSTOMIZATION="${REPO_ROOT}/manifests/application/p8-stack/base/kustomization.yaml"
HETZNER_KUSTOMIZATION="${REPO_ROOT}/manifests/application/p8-stack/overlays/hetzner/kustomization.yaml"

parse_configmap_literals "$BASE_KUSTOMIZATION"
parse_configmap_literals "$HETZNER_KUSTOMIZATION"

echo "Found ${#configmap_keys[@]} configMap literals"

# --- Parse p8-app-secrets stringData keys ---
secret_keys=()
SECRETS_FILE="${REPO_ROOT}/manifests/application/p8-stack/overlays/hetzner/secrets.yaml"
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
      [[ -n "$key" ]] && secret_keys+=("$key")
    fi
  done < "$SECRETS_FILE"
fi

echo "Found ${#secret_keys[@]} secret keys in p8-app-secrets"

# --- Dev-only keys (intentionally not in K8s manifests) ---
# P8_DATABASE_URL — constructed by CNPG operator from p8-database-credentials
# P8_KMS_LOCAL_KEYFILE — local dev only; production uses OpenBao vault
dev_only="P8_DATABASE_URL
P8_KMS_LOCAL_KEYFILE"

# --- Build newline-delimited covered keys list ---
covered=""
for k in "${configmap_keys[@]}"; do covered="${covered}${k}"$'\n'; done
for k in "${secret_keys[@]}"; do covered="${covered}${k}"$'\n'; done

# --- Check each .env key ---
gaps=()
skipped=()
for key in "${env_keys[@]}"; do
  if echo "$dev_only" | grep -qx "$key"; then
    skipped+=("$key")
  elif ! echo "$covered" | grep -qx "$key"; then
    gaps+=("$key")
  fi
done

echo ""
if [[ ${#skipped[@]} -gt 0 ]]; then
  echo "Skipped ${#skipped[@]} dev-only key(s): ${skipped[*]}"
fi

if [[ ${#gaps[@]} -eq 0 ]]; then
  echo "All env keys are covered by K8s manifests."
  exit 0
else
  echo "GAPS: ${#gaps[@]} env key(s) not found in configMap or secrets:"
  for g in "${gaps[@]}"; do
    echo "  - $g"
  done
  exit 1
fi
