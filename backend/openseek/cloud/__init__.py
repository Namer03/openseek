"""OpenSeek Cloud: the same thumbnail engine, run on a cloud host instead of a home PC.

Runs on Vercel (serverless, see `vercel.py`) or as a container on any Docker host (`python -m openseek cloud`).
State that the self-hosted server keeps on disk lives in cloud services instead:

* Upstash Redis (REST): devices, pairing window, preview records, tile index, locks.
* Cloudflare R2 or Cloudinary: the JPEG tiles (local disk on a Docker host when neither is configured).

There are no long-running background workers. Extraction is driven by the client's own requests: a priority
call (scrubbing) and every status poll do a bounded slice of work, one frame in flight per source.
"""
