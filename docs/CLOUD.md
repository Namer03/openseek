# OpenSeek Cloud

OpenSeek Cloud runs the same thumbnail engine on a cloud host, so no PC has to stay on. It is a second way to run
OpenSeek; the self-hosted server, the Windows installer and the desktop app are unchanged.

| | Self-hosted | Cloud |
|---|---|---|
| Runs on | your PC (Windows service, Docker) | Vercel (serverless), or any Docker host with HTTPS |
| Thumbnails stored in | `OPENSEEK_DATA_DIR` | Cloudflare R2 or Cloudinary (local disk on a Docker host) |
| Devices, pairing, index | SQLite | Upstash Redis |
| TLS | self-signed, clients pin the key | the platform's normal certificate |
| Owner tools | desktop app, loopback dashboard, CLI | `/configure` page, protected by a PIN |
| Who reads the video | your PC | the cloud host (see [Things to know](#things-to-know)) |

## Deploy on Vercel (free plan)

You need three free accounts: Vercel, Upstash (through Vercel) and one storage provider (Cloudflare R2 or Cloudinary).

1. **Vercel project.** On [vercel.com/new](https://vercel.com/new), import this repository. Set **Root Directory** to
   `backend`. Vercel finds the app through `[tool.vercel]` in `backend/pyproject.toml`; no framework preset or build
   command is needed. To deploy a branch other than the default one, set it as the production branch under
   Settings > Git, or use the preview URL of that branch.
2. **Upstash Redis.** In the project, open **Storage**, add **Upstash for Redis** (free) and connect it. This sets
   `KV_REST_API_URL` and `KV_REST_API_TOKEN` (or `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN`; both work).
3. **Tile storage**, one of:
   * **Cloudflare R2** (10 GB free, no egress fees): create a bucket, then an R2 API token with *Object Read & Write*
     on that bucket. Set `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`. The bucket stays
     private. Optional: an R2 lifecycle rule on the prefix `openseek/` that deletes objects after 30 days.
   * **Cloudinary** (free plan): set `CLOUDINARY_URL` (`cloudinary://<api_key>:<api_secret>@<cloud_name>`, from the
     Cloudinary dashboard) or the three `CLOUDINARY_CLOUD_NAME` / `_API_KEY` / `_API_SECRET` variables. Tiles go to the
     folder `openseek/` (`CLOUDINARY_FOLDER` to change it) under unguessable 40-character names.
   * Both set: R2 is used. Set `OPENSEEK_STORAGE=cloudinary` to use Cloudinary instead.
4. **Two secrets** under Settings > Environment Variables:
   * `OPENSEEK_CLOUD_SECRET`: at least 32 random characters, for example the output of `openssl rand -base64 48`.
     It signs device tokens and thumbnail links and encrypts stored stream links. Changing it signs every device out.
   * `OPENSEEK_ADMIN_PIN`: at least 8 characters. It protects the owner page.
5. **Deploy** (or redeploy after adding variables), then open `https://<your-project>.vercel.app/configure`.
   If a setting is missing, the page says which one.

`backend/vercel.json` puts the functions in Frankfurt (`fra1`). Change `regions` to the region nearest to your
debrid or media host.

## Connect a TV

On `/configure`, sign in with the PIN and press **Show a pairing code**. In the TV app enter the server address shown
there (`https://<your-project>.vercel.app`) and the one-time code. The code is valid for 10 minutes, works once, and
is burned after 5 wrong tries.

For clients without pairing, **Add a device with a token** creates a bearer token (shown once). Remove devices on the
same page.

**Client support.** The API is contract v1 (`docs/api/CONTRACT-v1.md`) with two differences a client must handle:
* `POST /v1/pair/start` answers `"spki_pin": "ca"` and `"tls": "ca"`. The client verifies the server certificate the
  normal way (system CA store) instead of pinning, and uses the string `ca` as `spki_pin` in the OSP1 key derivation.
* The VTT always uses the `tiles` layout, whatever layout was asked for.

## Deploy with Docker instead

`Dockerfile.cloud` builds the same app as a container that serves plain HTTP on `$PORT` (default 8080); the host
provides HTTPS. It suits Hugging Face Spaces (Docker SDK, set `PORT=7860`), Render, Google Cloud Run and Fly.io.

```sh
docker build -f Dockerfile.cloud -t openseek-cloud .
docker run -p 8080:8080 -e OPENSEEK_CLOUD_SECRET=... -e OPENSEEK_ADMIN_PIN=... openseek-cloud
```

Without Upstash the container keeps devices in memory (they must pair again after a restart), and without R2 or
Cloudinary it keeps tiles in `/data`, which most free hosts wipe on restart. Set the same variables as on Vercel for a
setup that survives restarts. Without a container, `python -m openseek cloud --port 8080` runs it directly.

## How it works

There are no background workers on a serverless host, so the client's own requests drive the work:

* `POST /v1/previews` checks the link (the same SSRF-hardened fetcher as self-hosted: every address must be public,
  redirects are re-vetted, only bounded range reads), fingerprints the file and stores the link **encrypted** in Redis
  for 6 hours.
* `POST /v1/previews/{id}/priority` (the client sends it while scrubbing) makes the tiles around the scrub target for up
  to `OPENSEEK_PRIORITY_WORK_S` (6 s).
* Every status poll (`GET /v1/previews/{id}`) makes more tiles for up to `OPENSEEK_POLL_WORK_S` (1.5 s): first around the
  playhead, then the overview chosen by `OPENSEEK_PREGEN`.
* A lock in Redis keeps one frame in flight per source across all instances, so the media host sees one reader.
* Tiles go to R2/Cloudinary and are indexed in Redis per content fingerprint, so another link to the same file reuses them.

Inside one instance the decoder runs in a child process with no environment secrets and reads only through a loopback
range proxy, exactly like the self-hosted worker. If the platform cannot start a child process, the app falls back to
decoding in-process (the owner page shows `inprocess`; `OPENSEEK_CLOUD_ENGINE=inprocess` forces it).

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `OPENSEEK_CLOUD_SECRET` | required | Key material, at least 32 characters |
| `OPENSEEK_ADMIN_PIN` | required | Owner page PIN, at least 8 characters |
| `KV_REST_API_URL` / `KV_REST_API_TOKEN` | memory | Upstash Redis REST (required on Vercel) |
| `R2_*`, `CLOUDINARY_*` | local disk | Tile storage (one required on Vercel) |
| `OPENSEEK_STORAGE` | `auto` | `auto`, `r2`, `cloudinary` or `local` |
| `OPENSEEK_PREGEN` | `overview` | Tiles made without scrubbing: `off`, `overview` (every 60 s) or `full` (every 10 s) |
| `OPENSEEK_POLL_WORK_S` | `1.5` | Extraction time per status poll (0 to 10) |
| `OPENSEEK_PRIORITY_WORK_S` | `6` | Extraction time per scrub request (0.5 to 60) |
| `OPENSEEK_RETENTION_DAYS` | `30` | How long the tile index is kept after last use |
| `OPENSEEK_PUBLIC_URL` | from the request | `https://name.example.org` if a proxy changes the Host header |
| `OPENSEEK_JOB_BYTES`, `OPENSEEK_ALLOWED_PORTS`, `OPENSEEK_ALLOW_HTTP_SOURCES`, `OPENSEEK_TOLERANCE_S` | as self-hosted | Same meaning as in the self-hosted server |
| `OPENSEEK_DEVICES_MAX` | `32` | Paired devices allowed |

## Things to know

* **The cloud host reads the video.** The server, not the TV, makes the range requests. Some debrid services lock a
  link to one IP address or do not allow server use. Check your provider's rules before using links from it.
* **Free-plan budget.** Vercel's free plan includes 4 hours of active CPU per month and stops functions for the rest of the
  30 days when a limit is reached. One tile costs very roughly 0.1 to 0.5 s of CPU depending on resolution and codec, so
  the default `overview` (about 120 tiles for a 2-hour film, plus what you scrub to) is the sensible setting there; `full`
  makes 720. The Vercel free plan is for personal, non-commercial use.
* **Privacy.** Stream links and forwarded headers are stored only encrypted (AES-256-GCM, key from
  `OPENSEEK_CLOUD_SECRET`) and expire after 6 hours; logs carry no URLs. Thumbnails sit in your own storage account under
  unguessable names; Cloudinary delivery URLs are public by design if someone learns a name, R2 objects are private.
* **Security model.** The owner API is reachable from the internet, so it relies on device tokens, capability URLs,
  rate limits and the PIN rather than on the LAN-only checks of the self-hosted server. There is no certificate pin.
* **Responsible use** is the same as self-hosted: see [RESPONSIBLE-USE.md](RESPONSIBLE-USE.md). Each owner deploys their
  own instance; nothing is shared between deployments.
