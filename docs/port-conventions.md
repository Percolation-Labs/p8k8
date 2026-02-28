# Port Conventions

Reserved port ranges for the p8 stack. Each environment gets its own port to
avoid conflicts between local Docker containers, test databases, and kubectl
port-forwards to the Hetzner cluster.

## PostgreSQL

| Port | Environment | Container / Source | Connection String |
|------|------------|-------------------|-------------------|
| 5489 | **Local dev** | `p8k8-db` (docker-compose) | `postgresql://p8:p8_dev@localhost:5489/p8` |
| 5490 | **Test** | Dedicated test container or tmpfs PG | `postgresql://p8:p8_test@localhost:5490/p8_test` |
| 5491 | **K8s port-forward** | `kubectl port-forward svc/p8-rw 5491:5432` | `postgresql://p8user:...@localhost:5491/p8` |

### Why not 5488?

Port 5488 is used by the legacy `remslim` stack (`p8-local` container). Do not
reuse it — both stacks may run simultaneously during migration.

## API Server

| Port | Environment | Notes |
|------|------------|-------|
| 8000 | Local dev | `p8 serve --port 8000` |
| 8001 | Other services | Reserved for non-p8 services (e.g. siggy) |

## KMS (OpenBao)

| Port | Environment | Notes |
|------|------------|-------|
| 8200 | Local dev | `docker-compose` KMS service |

## Rules

1. **Never share ports between environments.** A kubectl port-forward bound to
   `localhost` shadows Docker's `0.0.0.0` binding on the same port, causing
   silent connection misrouting.
2. **Kill port-forwards before local dev.** If `lsof -i :5489` shows kubectl,
   kill it before running `p8 mcp` or `p8 serve`.
3. **Test databases get their own port.** Never run tests against the dev
   database — use port 5490 with a disposable container.
4. **Document new ports here.** Any new service port must be added to this table.
