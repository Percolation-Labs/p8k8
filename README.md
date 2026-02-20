# p8k8

Deployable K8s stack for the Hetzner `p8-w-1` cluster. API package forked from [remslim](https://github.com/Percolation-Labs/reminiscent), adapted for self-hosted deployment with nginx ingress, cert-manager TLS, and CloudNativePG.

## Quick Start (Local Dev)

```bash
# Install dependencies
uv sync

# Start local postgres + KMS
docker compose up -d

# Run migrations
P8_DATABASE_URL=postgresql://p8:p8_dev@localhost:5488/p8 uv run p8 migrate

# Start API server
P8_DATABASE_URL=postgresql://p8:p8_dev@localhost:5488/p8 uv run p8 serve
```

The API is at `http://localhost:8000`. MCP server at `/mcp`.

## Deploy to Hetzner

```bash
# Build and push container image
docker buildx build --platform linux/amd64 \
  -t percolationlabs/p8:latest --push -f Dockerfile .

# Create namespace and secrets (edit secrets.yaml first!)
kubectl --context=p8-w-1 apply -f manifests/application/p8-stack/overlays/hetzner/namespace.yaml
kubectl --context=p8-w-1 apply -f manifests/application/p8-stack/overlays/hetzner/secrets.yaml

# Generate postgres init ConfigMap from SQL files
kubectl --context=p8-w-1 create configmap p8-postgres-init-sql \
  --from-file=install_entities.sql=sql/install_entities.sql \
  --from-file=install.sql=sql/install.sql \
  -n p8

# Deploy full stack
kubectl --context=p8-w-1 apply -k manifests/application/p8-stack/overlays/hetzner/
```

## CLI

```bash
p8 serve [--port 8000] [--reload]
p8 migrate
p8 query 'LOOKUP "demo-project-planning"'
p8 upsert schemas data/agents.yaml
p8 schema list [--kind agent]
p8 chat [SESSION_ID] [--agent query-agent]
p8 moments [--type session_chunk]
```

## Configuration

All settings via environment variables with `P8_` prefix, or `.env` file. See `p8/settings.py`.

| Variable | Default | Description |
|----------|---------|-------------|
| `P8_DATABASE_URL` | `postgresql://p8:p8_dev@localhost:5488/p8` | Postgres connection |
| `P8_EMBEDDING_MODEL` | `openai:text-embedding-3-small` | Embeddings (1536d) |
| `P8_KMS_PROVIDER` | `local` | `local`, `vault`, or `aws` |
| `P8_OPENAI_API_KEY` | — | Required for embeddings |

## Project Structure

```
p8k8/
├── p8/                    # Python package
│   ├── api/               # FastAPI + MCP server + CLI
│   ├── services/          # Business logic
│   ├── ontology/          # Data models
│   ├── agentic/           # Agent framework
│   └── settings.py        # P8_ env prefix
├── sql/                   # PostgreSQL init scripts
├── docker/                # Dockerfile.pg18 (local dev)
├── tests/                 # Test suite
├── manifests/             # K8s manifests
│   ├── platform/          # cert-manager, cloudnative-pg
│   └── application/
│       └── p8-stack/      # API + Postgres + Workers
│           └── overlays/
│               ├── local/
│               └── hetzner/
├── Dockerfile             # Production container image
├── docker-compose.yml     # Local dev (postgres + KMS)
├── pyproject.toml         # name=p8
└── site -> percolation-site
```
