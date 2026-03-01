# Security — Encryption at Rest

Per-tenant envelope encryption with pluggable KMS backends. Every tenant chooses an encryption mode that controls how their data is stored and who can read it.

## Architecture

```
                    KMS (OpenBao Transit / Local / AWS)
                         |
                    Master Key
                         |
                   wrap / unwrap
                         |
              +----- Tenant DEK -----+
              |                      |
         AES-256-GCM            AES-256-GCM
         (randomized)           (deterministic)
              |                      |
     content, bios, etc.         emails
```

**Envelope encryption**: a master key in the KMS wraps per-tenant Data Encryption Keys (DEKs). DEKs are stored wrapped in the `tenant_keys` table — never plaintext. Each field is encrypted with the tenant's DEK using AES-256-GCM with Additional Authenticated Data (AAD) binding `tenant_id:entity_id` to prevent ciphertext swapping.

## Encryption Modes

| Mode | Who encrypts | Who decrypts | DB sees | API returns | Use case |
|------|-------------|-------------|---------|-------------|----------|
| **platform** | Server | Server | Ciphertext | Plaintext | Default — transparent encryption |
| **client** | Server | Client | Ciphertext | Ciphertext | Client controls decryption |
| **sealed** | Server (public key) | Client (private key) | Ciphertext | Ciphertext | Zero-trust — server can never decrypt |
| **disabled** | — | — | Plaintext | Plaintext | Opt-out (tenant accepts risk) |

## Encrypted Fields

| Entity | Encrypted fields | Mode |
|--------|-----------------|------|
| Message | `content` | randomized |
| Resource | `content` | randomized |
| User | `content`, `email` | content: randomized, email: deterministic |
| File | `parsed_content` | randomized |
| Moment | `summary` | randomized |
| Feedback | `comment` | randomized |
| Ontology | `content` | randomized |
| Schema, Session, Server, Tool, Tenant | — | not encrypted |

**Randomized** — different ciphertext each time (no equality queries possible).
**Deterministic** — same plaintext + same key = same ciphertext, enabling `WHERE email = enc(x)` lookups.

## API

### Create tenant with encryption mode

```bash
POST /auth/tenants
{
  "name": "acme-corp",
  "encryption_mode": "platform",
  "own_key": true
}
```

`own_key: true` generates a dedicated DEK for the tenant. `own_key: false` (default) falls back to the system DEK.

### Change encryption mode

```bash
POST /auth/tenants/{tenant_id}/encryption
{
  "mode": "client"
}
```

### Sealed mode — server generates key pair

```bash
POST /auth/tenants/{tenant_id}/encryption
{
  "mode": "sealed"
}
```

Response includes `private_key_pem` (RSA-4096) — returned **once**, never stored by the server. The client must save this key. The server only keeps the public key for encrypting future data.

### Sealed mode — tenant provides their own public key

```bash
POST /auth/tenants/{tenant_id}/encryption
{
  "mode": "sealed",
  "public_key_pem": "-----BEGIN PUBLIC KEY-----\n..."
}
```

The server never sees the private key. Only the tenant can decrypt their data.

### Disable encryption

```bash
POST /auth/tenants/{tenant_id}/encryption
{
  "mode": "disabled"
}
```

## Example: Four Tenants, Four Modes

Below shows how the same message content appears at each layer for tenants configured with different encryption modes.

### Setup

```bash
# Create four tenants, each with a different mode
curl -X POST /auth/tenants -d '{"name": "Acme Corp", "encryption_mode": "platform", "own_key": true}'
curl -X POST /auth/tenants -d '{"name": "Secretive Inc", "encryption_mode": "client", "own_key": true}'
curl -X POST /auth/tenants -d '{"name": "Hospital Systems", "encryption_mode": "platform"}'
# then switch to sealed:
curl -X POST /auth/tenants/{hospital_id}/encryption -d '{"mode": "sealed"}'
curl -X POST /auth/tenants -d '{"name": "Yolo Startup", "encryption_mode": "disabled"}'
```

### What each tenant sees

All four tenants store the same message: `"My SSN is 123-45-6789 and my salary is $185,000."`

**Acme Corp** — platform mode (own key)
```
DB column:    PBVcL9k4qBWM3r7YDK0MaSjqcS/g8DXl+KqYClaoX87s...  (ciphertext)
API response: My SSN is 123-45-6789 and my salary is $185,000.   (plaintext — server decrypts)
```

**Secretive Inc** — client mode
```
DB column:    bMBqsST0LChPbTYkyCef0XZVTIzPdcCcc/QAwmjjuYGv...  (ciphertext)
API response: bMBqsST0LChPbTYkyCef0XZVTIzPdcCcc/QAwmjjuYGv...  (ciphertext — client decrypts)
```

**Hospital Systems** — sealed mode
```
DB column:    AgAkXukt0uJ2uEtyO1VD2ZM/3f2qVxeVYCnEmDFcPPnS...  (hybrid ciphertext)
API response: AgAkXukt0uJ2uEtyO1VD2ZM/3f2qVxeVYCnEmDFcPPnS...  (ciphertext — only private key decrypts)
Server can decrypt? NO — only the public key is stored
```

**Yolo Startup** — disabled
```
DB column:    My SSN is 123-45-6789 and my salary is $185,000.   (plaintext)
API response: My SSN is 123-45-6789 and my salary is $185,000.   (plaintext)
```

### DB operator view

What a database administrator with `SELECT` access sees:

```sql
SELECT tenant_id, LEFT(content, 60) FROM messages;
```

```
 tenant_id       | content
-----------------+--------------------------------------------------------------
 acme-corp       | PBVcL9k4qBWM3r7YDK0MaSjqcS/g8DXl+KqYClaoX87s4AijO4vPrT...
 secretive-inc   | bMBqsST0LChPbTYkyCef0XZVTIzPdcCcc/QAwmjjuYGvZHI1ZOqwbXg...
 hospital-sys    | AgAkXukt0uJ2uEtyO1VD2ZM/3f2qVxeVYCnEmDFcPPnSl2hwP/PgbGI...
 yolo-startup    | My SSN is 123-45-6789 and my salary is $185,000.
```

Only the disabled tenant's data is readable. All others show ciphertext.

### Tenant isolation

Two hospital tenants with their own keys cannot read each other's data:

```
hospital-a stores: "Patient diagnosed with condition XYZ."
hospital-a reads:  Patient diagnosed with condition XYZ.        (correct key + AAD)
hospital-b reads:  gWQpODG2Px2iX34ec+MhizIvX60FxlgXo6eIeq...  (wrong key, AAD mismatch)
```

AAD is `"{tenant_id}:{entity_id}"` — even if the raw ciphertext were copied between tenants, decryption fails because the authenticated data doesn't match.

### File uploads

Files follow the same encryption path. When a file is uploaded:

1. File is stored in S3 (object storage) — unencrypted at the storage layer
2. Parsed text content is extracted by the file worker
3. `parsed_content` is encrypted with the tenant's DEK before being written to PostgreSQL
4. Chunked resources created from the file also have their `content` field encrypted

```
Upload PDF → S3 → Worker extracts text → encrypt(parsed_content) → PostgreSQL
                                        → chunk into Resources → encrypt(content) → PostgreSQL
```

## KMS Backends

| Backend | Config | Use case |
|---------|--------|----------|
| **OpenBao Transit** | `P8_KMS_PROVIDER=vault` | Production (Hetzner, self-hosted) |
| **AWS KMS** | `P8_KMS_PROVIDER=aws` | Production (AWS) |
| **Local file** | `P8_KMS_PROVIDER=local` | Development only |

### OpenBao (Hetzner)

OpenBao runs as a StatefulSet in the `p8` namespace with **file storage** on a persistent volume (1Gi PVC). It serves two functions:

1. **Transit engine** — envelope encryption for per-tenant DEKs
2. **KV v2 engine** — source of truth for all K8s secrets (synced by ESO)

```
Pod: openbao-0  →  Transit engine  →  p8-master key
                                    →  p8-master-{tenant_id} per-tenant keys
                →  KV v2 engine    →  secret/p8/app-secrets
                                    →  secret/p8/database-credentials
                                    →  secret/p8/keda-pg-connection
```

OpenBao runs in **production mode** (not `-dev`). On pod restart, an `auto-unseal` init container reads unseal keys from `openbao-unseal-keys` K8s secret and automatically unseals. A `fix-permissions` init container ensures PVC ownership is correct.

The API and workers connect to `http://openbao.p8.svc.cluster.local:8200`. The vault token is stored in `openbao-unseal-keys` (root token) and `openbao-eso-token` (for ESO auth).

In production, OpenBao should run on a dedicated cluster or external service, isolated from the workload plane. The in-cluster deployment is a convenience for single-cluster setups.

### Configuration

```bash
# Hetzner (vault)
P8_KMS_PROVIDER=vault
P8_KMS_VAULT_URL=http://openbao.p8.svc.cluster.local:8200
P8_KMS_VAULT_TOKEN=<token from p8-app-secrets>
P8_KMS_VAULT_TRANSIT_KEY=p8-master

# Local dev (file)
P8_KMS_PROVIDER=local
P8_KMS_LOCAL_KEYFILE=.keys/.dev-master.key

# AWS
P8_KMS_PROVIDER=aws
P8_KMS_AWS_KEY_ID=<KMS key ARN>
P8_KMS_AWS_REGION=us-east-1
```

## Running the Encryption Sim

The encryption demo exercises all 9 scenarios against a live database and KMS:

```bash
# Against local docker-compose (OpenBao dev mode)
P8_KMS_PROVIDER=vault \
P8_KMS_VAULT_URL=http://localhost:8200 \
P8_KMS_VAULT_TOKEN=dev-root-token \
P8_KMS_VAULT_TRANSIT_KEY=p8-master \
python tests/.sim/encryption_demo.py

# Against Hetzner (via port-forward)
kubectl --context=p8-w-1 -n p8 port-forward svc/p8-postgres-rw 5491:5432 &
kubectl --context=p8-w-1 -n p8 port-forward svc/openbao 8201:8200 &

P8_DATABASE_URL="postgresql://p8user:<password>@localhost:5491/p8db?sslmode=disable" \
P8_KMS_PROVIDER=vault \
P8_KMS_VAULT_URL=http://localhost:8201 \
P8_KMS_VAULT_TOKEN=<token> \
P8_KMS_VAULT_TRANSIT_KEY=p8-master \
python tests/.sim/encryption_demo.py
```

## Client Decryption

Every record carries an `encryption_level` field (`platform`, `client`, `sealed`, `disabled`, or `none`) stamped by the repository at write time. This tells the client whether the content is plaintext or ciphertext and, if encrypted, how to decrypt it.

### Who decrypts what

| Mode | Server decrypts? | Client decrypts? | Key source |
|------|-----------------|-----------------|------------|
| **platform** | Yes — transparent | No — receives plaintext | Server holds DEK via KMS |
| **client** | No — returns ciphertext | Yes | Client fetches DEK from KMS (`POST /auth/tenants/{id}/key`) |
| **sealed** | No — cannot decrypt | Yes | Client's own private key (RSA-4096, never on server) |
| **disabled** | N/A | N/A | No encryption — plaintext everywhere |

In **platform** mode the API returns plaintext — the client needs no crypto. In **client** mode the server *has* the DEK in KMS but returns ciphertext by design — the client retrieves the same DEK from KMS to decrypt locally. In **sealed** mode the server only holds the public key and literally cannot decrypt — only the client's private key works.

### How the client knows

Every API response includes `encryption_level` on each record:

**Moment feed** (`GET /moments/feed`):
```json
{
  "event_type": "moment",
  "event_id": "...",
  "summary": "PBVcL9k4qBWM3r7Y...",
  "encryption_level": "client",
  "metadata": { ... }
}
```

**Session timeline** (`GET /moments/session/{id}`):
```json
{
  "event_type": "message",
  "content_or_summary": "bMBqsST0LChPbTYk...",
  "encryption_level": "client",
  "metadata": { ... }
}
```

**Entity endpoints** (`GET /moments/{id}`, `GET /query`, etc.):
```json
{
  "id": "...",
  "summary": "PBVcL9k4qBWM3r7Y...",
  "encryption_level": "client"
}
```

The client checks `encryption_level` per record:
- `null`, `none`, `disabled`, `platform` → content is plaintext, display directly
- `client` → content is base64 AES-256-GCM ciphertext, decrypt with tenant DEK
- `sealed` → content is base64 hybrid ciphertext (RSA-wrapped ephemeral DEK + AES), decrypt with private key

### Client decryption — AES-256-GCM (client mode)

```
1. Fetch DEK from KMS:  POST /auth/tenants/{tenant_id}/key → { "dek": "<base64>" }
2. For each ciphertext field:
   raw    = base64_decode(field_value)
   nonce  = raw[0:12]
   ct     = raw[12:]
   aad    = "{tenant_id}:{entity_id}".encode()
   plain  = AES-256-GCM.decrypt(dek, nonce, ct, aad)
```

### Client decryption — hybrid (sealed mode)

```
1. Load the private key (RSA-4096 PEM, stored client-side only)
2. For each ciphertext field:
   raw        = base64_decode(field_value)
   dek_len    = int(raw[0:2], big-endian)
   wrapped    = raw[2 : 2+dek_len]
   nonce      = raw[2+dek_len : 2+dek_len+12]
   ct         = raw[2+dek_len+12:]
   aad        = "{tenant_id}:{entity_id}".encode()
   eph_dek    = RSA-OAEP-SHA256.decrypt(private_key, wrapped)
   plain      = AES-256-GCM.decrypt(eph_dek, nonce, ct, aad)
```

### Chat — encryption and history

Chat has two encryption paths: the **live stream** and **persisted messages**.

**Live stream** — The SSE stream (`POST /chat/{id}`) always returns plaintext deltas. The LLM produces plaintext; the client renders it in real time. No client-side decryption is needed during a live conversation.

**Persisted messages** — When the stream completes, the user and assistant messages are encrypted with the tenant's DEK and written to the `messages` table with `encryption_level` stamped. On the next turn, the server loads historical messages, decrypts them, and feeds them to the LLM as context.

**Sealed mode cap** — Because the server must decrypt historical messages to feed the LLM, sealed mode is **automatically capped to platform** for chat messages. The server cannot decrypt sealed ciphertext (it only holds the public key), so chat messages are encrypted with the tenant's DEK instead. This means:

| Tenant mode | Chat messages stored as | Server can decrypt for LLM? |
|-------------|------------------------|----------------------------|
| **platform** | platform-encrypted | Yes |
| **client** | client-encrypted | Yes (server has DEK via KMS) |
| **sealed** | platform-encrypted (capped) | Yes (sealed would break history) |
| **disabled** | plaintext | N/A |

Non-chat entities (resources, files, moments, ontologies) still use the tenant's configured mode including full sealed encryption. Only the chat message path enforces this cap.

**Two persistence paths** — Messages persisted via Repository (file processing, resource creation, etc.) go through `Repository.upsert()` which handles encryption and `encryption_level` stamping. Chat messages go through `rem_persist_turn()` which encrypts at the Python layer before calling the SQL function. Both paths produce the same result: encrypted content + `encryption_level` in the `messages` table.

**Chat history** — All chat messages are stored in the `messages` table and loaded via `MemoryService.load_context()` with token budgeting and compaction. There is no secondary copy of message history in session metadata.

### Mixed history

A tenant can change encryption mode over time. Historical records retain the `encryption_level` they were written with. A single feed page may contain:

```
moment-1:  encryption_level=null       → plaintext (written before encryption)
moment-2:  encryption_level=platform   → plaintext (server decrypted)
moment-3:  encryption_level=client     → ciphertext (client must decrypt)
moment-4:  encryption_level=sealed     → ciphertext (client must decrypt)
```

The client handles each record independently based on its `encryption_level`.

## Security Properties

- **Envelope encryption** — master keys never leave the KMS; only wrapped DEKs are stored
- **Per-tenant isolation** — each tenant can have their own DEK; cross-tenant reads fail via AAD
- **Field-level granularity** — only sensitive fields are encrypted, not entire rows
- **Sealed mode** — true zero-knowledge for non-chat entities: even a compromised server cannot decrypt resources, files, moments, or ontologies. Chat messages are capped to platform encryption so the LLM can read history (see [Chat — encryption and history](#chat--encryption-and-history))
- **Deterministic mode** — enables equality queries on encrypted fields (emails) without exposing plaintext
- **Key rotation** — rotate the KMS master key; re-wrap DEKs without re-encrypting data
- **AAD binding** — `tenant_id:entity_id` prevents ciphertext relocation attacks; chat messages pre-generate UUIDs before INSERT so the AAD is bound correctly
