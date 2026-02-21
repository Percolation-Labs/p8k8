# Manifests

K8s manifests for the p8-w-1 Hetzner cluster.

## TODO

- [ ] Setup cluster cert renewals
- [ ] Add `.env → Secret` loader script (e.g. `scripts/sync-secrets.sh` that reads `.env` and runs `kubectl create secret generic --from-env-file`)
- [ ] Enable etcd encryption at rest for Secrets on the cluster
- [ ] Evaluate sealed-secrets or external-secrets-operator for git-safe secret storage
- [ ] Add secret rotation procedure for API keys and DB credentials
