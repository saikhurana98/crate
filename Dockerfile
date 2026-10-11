# crate image. Ships the configured headless sources, including direct Monochrome FLAC:
#   Tier 1 Qobuz · Monochrome · Tier 2 Deezer + Soulseek(via slskd) · Tier 3 yt-dlp

FROM python:3.12-slim AS builder
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv build --wheel -o /dist

# Build the management console UI (vanilla TS + Vite) into static assets.
FROM node:22-slim AS ui
WORKDIR /ui
COPY apps/console/package.json apps/console/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/console/ ./
RUN npm run build

FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/saikhurana98/load-balanced-music-downloader"
LABEL org.opencontainers.image.description="Load-balanced tiered FLAC music downloader"

# ffmpeg provides ffprobe verification; tini forwards signals as PID 1.
# libchromaprint-tools: fpcalc for the review fingerprint guard (ADR-0019) —
# absent -> the guard silently turns itself off, so this is the "full" image only.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tini libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

# App wheel + yt-dlp (Tier 3).
COPY --from=builder /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl yt-dlp && rm -rf /tmp/*.whl

# Built console UI, served by `crate console` (StaticFiles) at CRATE_CONSOLE_STATIC.
COPY --from=ui /ui/dist /app/console
ENV CRATE_CONSOLE_STATIC=/app/console

# non-root
RUN useradd -m -u 1000 crate
USER crate
WORKDIR /downloads

# tini as PID 1, forwarding shutdown signals to crate.
ENTRYPOINT ["tini", "--", "crate"]
CMD ["--help"]
