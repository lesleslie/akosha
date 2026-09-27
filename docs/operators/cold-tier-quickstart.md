# Cold Tier Quickstart

This guide covers three things:

1. Running akosha's cold tier against fake-gcs-server (dev).
2. Pointing the cold tier at Cloudflare R2 (prod).
3. A smoke-test recipe for verifying your R2 wiring before committing.

## 1. fake-gcs-server (dev)

### Install

```bash
brew install fake-gcs-server
```

### Start

The session-buddy repo ships lifecycle scripts at `scripts/fake-gcs-{start,stop,init}.sh`,
but the simplest path is to call the binary directly:

```bash
mkdir -p ~/.cache/akosha/fake-gcs
nohup fake-gcs-server \
  -filesystem-root ~/.cache/akosha/fake-gcs \
  -port 4443 \
  -host 127.0.0.1 \
  -public-host 127.0.0.1:4443 \
  -location US-CENTRAL1 \
  -scheme http \
  > ~/.cache/akosha/fake-gcs/server.log 2>&1 &
```

The `-scheme http` flag is important: fake-gcs-server defaults to HTTPS,
but the SDK is pointed at `http://127.0.0.1:4443`. Mismatched schemes
produce `400 Client sent an HTTP request to an HTTPS server`.

### Verify

```bash
curl -i http://127.0.0.1:4443/storage/v1/b
# Expect: 200 OK (empty list of objects is fine)
```

### Pre-create the bucket

fake-gcs-server (unlike real GCS) does NOT auto-create buckets on first PUT.
Buckets are exposed as filesystem dirs in the data root:

```bash
mkdir -p ~/.cache/akosha/fake-gcs/akosha-cold-data
```

(Real GCS requires the bucket to be created via `gcloud storage buckets create`
or the Cloudflare dashboard — fake-gcs just needs a directory to exist.)

### Configure akosha

In `settings/local.yaml` (gitignored) or `~/.config/akosha/local.yaml`:

```yaml
cold:
  backend: gcs
  bucket: akosha-cold-data
  prefix: conversations/
  format: parquet
  endpoint_url: http://127.0.0.1:4443
  project: local-dev
  region: us-west-2  # unused for gcs; kept for parity
```

Or via env vars (preferred for CI):

```bash
export AKOSHA__STORAGE__COLD__BACKEND=gcs
export AKOSHA__STORAGE__COLD__ENDPOINT_URL=http://127.0.0.1:4443
export AKOSHA__STORAGE__COLD__PROJECT=local-dev
```

### Reset dev data

```bash
rm -rf ~/.cache/akosha/fake-gcs
```

## 2. Cloudflare R2 (prod)

R2 is S3-compatible and gives 10 GB free storage + free egress — best dev-tier
cold-storage option.

### Step 1: Create the bucket

```bash
# Option A: Cloudflare dashboard → R2 → Create bucket
# Option B: wrangler CLI (if installed)
wrangler r2 bucket create akosha-cold-data
```

Bucket names are global (like S3) — pick something unique.

### Step 2: Generate an API token

Cloudflare dashboard → R2 → Manage R2 API Tokens → Create token. Choose:
- Object Read & Write permissions (not Admin)
- Scope to the bucket you just created
- TTL: as short as you're comfortable with (rotate regularly)

Copy the **Access Key ID** and **Secret Access Key**.

### Step 3: Configure akosha

In `settings/local.yaml` (gitignored):

```yaml
cold:
  backend: s3
  bucket: akosha-cold-data  # your bucket name
  prefix: conversations/
  format: parquet
  region: auto  # R2 mandatory
  endpoint_url: https://<YOUR_ACCOUNT_ID>.r2.cloudflarestorage.com
```

Set credentials via env vars (preferred — keep `local.yaml` out of git):

```bash
export AKOSHA__STORAGE__COLD__ACCESS_KEY_ID=<your-access-key-id>
export AKOSHA__STORAGE__COLD__SECRET_ACCESS_KEY=<your-secret-access-key>
```

### Step 4: Credential rotation

R2 tokens don't auto-rotate. Periodically:

1. Generate a new token in the Cloudflare dashboard.
2. Update env vars / `local.yaml`.
3. Revoke the old token in the Cloudflare dashboard.

## 3. Smoke-test recipe

After configuring R2 (or fake-gcs), verify the wiring end-to-end:

```bash
akosha mcp start
akosha cold export --batch sample.json
akosha cold list
```

If `cold export` fails with a 401 / `SignatureDoesNotMatch`, your credentials
or region are wrong. If it fails with `ConnectionError`, your `endpoint_url`
is wrong. Check `akosha logs` for the actual error.

## Health checks

- fake-gcs-server: `curl http://127.0.0.1:4443/storage/v1/b`
- R2: not directly health-checkable without credentials; rely on the
  smoke-test recipe above.

## Logs

Successful cold exports emit:

```
INFO akosha.storage.cold_store Successfully exported N records to conversations/...
INFO oneiric.adapters.storage.gcs gcs-endpoint-override endpoint=http://127.0.0.1:4443
```

If `gcs-endpoint-override` doesn't appear when you expect it, your
`endpoint_url` env var didn't bind — check `ColdStorageConfig` loading.

## Environment quirks worth knowing

- **`GOOGLE_APPLICATION_CREDENTIALS`** — if you have real Google ADC
  credentials in your environment (e.g., `~/.config/gcloud/application_default_credentials.json`),
  the GCS SDK will use them by default. For the fake-gcs-server path,
  unset `GOOGLE_APPLICATION_CREDENTIALS` in your shell so the SDK falls
  back to anonymous credentials via `use_auth_w_custom_endpoint=False`.
- **`CLOUDSDK_HOME`** — set by Google Cloud SDK. Doesn't interfere with
  fake-gcs but worth knowing about when debugging auth issues.
- **gcloud Application Default Credentials** (`~/.config/gcloud/`).
  Exists on this machine; can interfere with emulator setups. Unset
  `GOOGLE_APPLICATION_CREDENTIALS` and use the `anonymous_credentials`
  cold-tier field for fake-gcs-server connections.
