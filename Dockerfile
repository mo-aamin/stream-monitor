# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm

ARG DENO_VERSION=2.9.8
ARG YTDLP_VERSION=
ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    RECORDINGS_DIR=/recordings \
    DENO_NO_UPDATE_CHECK=1 \
    APP_PORT=8080

# ffmpeg/ffprobe for muxing, curl for health checks and Deno download,
# tzdata for the TZ environment variable, gosu for PUID/PGID privilege drop.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        gosu \
        tzdata \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Deno is required by modern yt-dlp for full YouTube support.
RUN set -eux; \
    BUILD_ARCH="${TARGETARCH:-}"; \
    if [ -z "$BUILD_ARCH" ]; then \
        case "$(uname -m)" in \
            x86_64|amd64) BUILD_ARCH="amd64" ;; \
            aarch64|arm64) BUILD_ARCH="arm64" ;; \
            *) BUILD_ARCH="unsupported" ;; \
        esac; \
    fi; \
    case "$BUILD_ARCH" in \
        amd64) DENO_ARCH="x86_64" ;; \
        arm64) DENO_ARCH="aarch64" ;; \
        *) echo "Unsupported architecture: $BUILD_ARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 3 \
        "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-${DENO_ARCH}-unknown-linux-gnu.zip" \
        -o /tmp/deno.zip; \
    unzip -q /tmp/deno.zip -d /usr/local/bin; \
    rm -f /tmp/deno.zip; \
    chmod 0755 /usr/local/bin/deno; \
    deno --version

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ -n "${YTDLP_VERSION}" ]; then \
        pip install --no-cache-dir "yt-dlp[default,curl-cffi]==${YTDLP_VERSION}"; \
    else \
        pip install --no-cache-dir "yt-dlp[default,curl-cffi]"; \
    fi

COPY app ./app
COPY entrypoint.sh /entrypoint.sh
RUN chmod -R a+rX /app \
    && chmod 0755 /entrypoint.sh \
    && groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home appuser \
    && mkdir -p /data /recordings \
    && chown -R appuser:appuser /data /recordings

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${APP_PORT:-8080}/healthz" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${APP_PORT:-8080}\" --workers 1 --proxy-headers --timeout-keep-alive 30"]
