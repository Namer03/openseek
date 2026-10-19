# python:3.12-slim pinned by digest (multi-arch index); bump deliberately to take OS security updates
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    OPENSEEK_DATA_DIR=/data

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt backend/requirements-lock.txt /app/
RUN pip install -r /app/requirements.txt -c /app/requirements-lock.txt

COPY backend/openseek /app/openseek

RUN useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin openseek \
 && mkdir -p /data && chown openseek:openseek /data
USER openseek
VOLUME ["/data"]

# 8787: HTTPS owner API. 8789: optional guest listener (only if OPENSEEK_PUBLIC_PORT is set).
# The admin listener (8788) is bound to the container's loopback by design; use `docker exec`.
EXPOSE 8787

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import ssl,urllib.request as u; c=ssl.create_default_context(); c.check_hostname=False; c.verify_mode=ssl.CERT_NONE; u.urlopen('https://127.0.0.1:8787/v1/health', context=c, timeout=5)"]

CMD ["python", "-m", "openseek", "serve"]
