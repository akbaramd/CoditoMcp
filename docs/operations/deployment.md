# Relay deployment runbook

Target: Ubuntu Docker host `192.168.200.39`, bind `192.168.200.39:8094`, public URL
`https://codito.akbaramd.ir`, private registry
`docker.wa-nezam.org/codito/relay`.

The repository does not manage DNS, TLS, Nginx, or OPNsense. The owner configures
those layers after the internal service passes health checks. Never expose port 8094
directly to the Internet.

## Preconditions

- Reviewed commit on `main`; clean tree; CI/security gates green.
- `uv.lock` committed and `uv sync --frozen` succeeds.
- Version selected (for example `0.1.0`) and 12-character Git SHA known.
- Registry authentication available without printing credentials.
- Server has Docker Engine/Compose, sufficient disk, time sync, and the fixed port
  remains free. The existing `postgres` and `redis` containers are healthy and
  attached to external `postgres-network` and `redis-network` networks.
- Production Django/OAuth keys and random credentials generated out of band.
- A current PostgreSQL backup and tested rollback image tag are recorded.

## Build and push

From the repository root:

```powershell
$version = "0.1.0"
$sha = (git rev-parse --short=12 HEAD)
$image = "docker.wa-nezam.org/codito/relay:$version-$sha"
docker buildx build --platform linux/amd64 `
  --file deploy/Dockerfile `
  --build-arg VERSION=$version `
  --build-arg VCS_REF=$sha `
  --tag $image --push .
```

Never publish or deploy `latest`. Capture the pushed manifest digest and SBOM in
the release evidence; production may pin `CODITO_RELAY_IMAGE` by digest after the
human-readable version/SHA tag is published.

## First server setup

On `192.168.200.39`:

```sh
install -d -m 0750 /data/projects/codito
install -d -m 0700 /data/backups/codito/daily /data/backups/codito/weekly
```

Copy `deploy/compose.yaml` and `deploy/scripts/` into that exact directory. Codito
reuses the server's existing PostgreSQL 15 and authenticated Redis 7 containers via
their external Docker networks. It creates a dedicated PostgreSQL database/role and
uses an otherwise-empty Redis logical database; it does not create or upgrade either
shared service.

Run the one-time helper below. It refuses to overwrite an existing `.env`, generates
all random values and the RSA signing key on the server, extracts and URL-encodes the
existing Redis credential without printing it, and refuses to use a non-empty Redis
database. It also refuses to proceed if a role or database named `codito` already
exists, so it cannot silently take ownership of another deployment:

```sh
./scripts/bootstrap-host.sh \
  docker.wa-nezam.org/codito/relay:0.1.0-<git-sha> \
  192.168.200.34
```

If logical database 1 is already in use, select an empty database explicitly with
`CODITO_REDIS_DB=<number>` for the bootstrap command. Do not share that logical
database with another application. Set `chmod 0750 scripts/*.sh`; the helper creates
`.env` with mode 0600.

```dotenv
BIND_ADDRESS=192.168.200.39
PUBLIC_BASE_URL=https://codito.akbaramd.ir
FORWARDED_ALLOW_IPS=<exact Nginx/OPNsense source IP or CIDR>
```

Never set `FORWARDED_ALLOW_IPS=*` in production. Otherwise any client that can reach
8094 can spoof forwarded scheme/host/client information.

Do not paste `.env` into tickets, shell history, CI logs, or chat.

## Deploy or upgrade

`deploy.sh` pulls images, runs the separate migration job, starts the stack, and
waits for internal readiness:

```sh
cd /data/projects/codito
./scripts/deploy.sh
```

For the first install, create the administrator interactively without placing the
password on the command line:

```sh
docker compose --env-file .env -f compose.yaml exec relay \
  python /app/relay/manage.py bootstrap_admin
```

Migration is never an implicit web startup side effect. For a backward-incompatible
migration use expand/migrate/deploy/contract steps across releases.

## Internal verification

Run in order and stop if any step fails:

1. `docker compose --env-file .env -f compose.yaml ps` shows a healthy relay;
   `docker ps` independently shows the shared `postgres` and `redis` containers
   healthy.
2. `curl -fsS http://192.168.200.39:8094/health/live` succeeds.
3. `curl -fsS http://192.168.200.39:8094/health/ready` proves dependencies and
   migrations are ready.
4. Fetch authorization-server/OIDC metadata and verify every issuer/endpoint uses
   the public HTTPS base URL, never the internal IP.
5. Create a test device/link and verify its path-specific protected-resource
   metadata has an exact `resource` equal to its MCP URL.
6. Through the internal proxy test, verify WebSocket upgrade, ticket/key proof, and
   epoch increment/replacement.
7. Run cleanup dry-run/metrics authentication and inspect redacted structured logs.

## External edge verification

After the owner configures DNS/TLS/proxy:

- Certificate/hostname/chain and TLS policy pass; HTTP redirects to HTTPS.
- Proxy preserves WebSocket upgrade, long request streaming, original client IP in
  the trusted header scheme, and suitable idle timeout.
- OAuth metadata, authorization/consent/token flow, JWKS, revocation, introspection,
  and per-device resource metadata work through the public host.
- MCP Inspector can initialize and see exactly four tools with matching schemes.
- A real private ChatGPT developer-mode connection completes acceptance tests.

## Routine operations

- Run `scripts/cleanup.sh` daily after the database backup.
- Alert on readiness failure, device/offline churn, stale epochs, queue saturation,
  auth failure rate, refresh replay, operation latency/errors, backup age, disk
  capacity, and certificate/key expiry.
- Keep OAuth client identity stable across image updates. Configure token
  lifetimes through `OAUTH_ACCESS_TOKEN_EXPIRE_SECONDS` (default 3600) and
  `OAUTH_REFRESH_TOKEN_EXPIRE_SECONDS` (default 7776000); see the
  [refresh policy](../protocol/oauth.md#long-lived-connections-without-permanent-bearer-tokens).
  Do not rewrite existing token/grant rows to extend login or recover revocation.
- Rotate OIDC signing keys with overlapping JWKS publication and retain the old
  verification keys through the last issued ID token's advertised expiry.
  OAuth access tokens are opaque database-backed credentials, not signed JWTs.
- Review admin/audit activity and dependency/image advisories at least weekly.
