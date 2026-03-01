# Manifests

K8s manifests for the p8-w-1 Hetzner cluster.

## Secret Management

Production secrets are managed by **External Secrets Operator (ESO)** pulling from **OpenBao KV v2**.

| K8s Secret | ESO Source | Keys |
|------------|-----------|------|
| `p8-app-secrets` | `secret/p8/app-secrets` | 22 (API keys, OAuth, Stripe, etc.) |
| `p8-database-credentials` | `secret/p8/database-credentials` | username, password |
| `p8-keda-pg-connection` | `secret/p8/keda-pg-connection` | connection string |

### Adding a new secret

```bash
# 1. Get the root token
ROOT_TOKEN=$(kubectl --context=p8-w-1 -n p8 get secret openbao-unseal-keys \
  -o jsonpath='{.data.root_token}' | base64 -d)

# 2. Write to OpenBao (adds key to existing secret, preserves others)
kubectl --context=p8-w-1 -n p8 exec openbao-0 -c openbao -- env \
  BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN="$ROOT_TOKEN" \
  bao kv patch secret/p8/app-secrets NEW_KEY=new_value

# 3. Force ESO sync (otherwise waits up to 1h)
kubectl --context=p8-w-1 -n p8 annotate externalsecrets p8-app-secrets \
  force-sync=$(date +%s) --overwrite

# 4. Restart to pick up new secret
kubectl --context=p8-w-1 -n p8 rollout restart deploy/p8-api
```

### Bulk seed from .env

```bash
./manifests/scripts/seed-openbao.sh --context=p8-w-1
```

### Scripts

| Script | Purpose |
|--------|---------|
| `scripts/init-openbao.sh` | One-time: initialize OpenBao, generate unseal keys, create bootstrap secrets |
| `scripts/seed-openbao.sh` | Seed all secrets from `.env` into OpenBao KV v2 |

## Architecture

```
.env (local)
  │
  ▼  seed-openbao.sh
OpenBao KV v2 (in-cluster, persistent PVC)
  │
  ▼  ESO (1h refresh)
K8s Secrets (p8-app-secrets, p8-database-credentials, p8-keda-pg-connection)
  │
  ▼  envFrom / secretKeyRef
Pods (p8-api, workers, openbao)
```

## TODO

- [ ] Setup cluster cert renewals
- [ ] Enable etcd encryption at rest for Secrets on the cluster
- [ ] Migrate OpenBao to dedicated node or external service
- [ ] Rotate DB password (currently bootstrap placeholder)
- [ ] Add secret rotation procedure for API keys
