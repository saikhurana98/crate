# crate image. Ships every headless-capable source, plus Monochrome (Amazon):
#   Tier 1 Qobuz · Monochrome · Tier 2 Deezer + Soulseek(via slskd) · Tier 3 yt-dlp
# Monochrome needs a *headed* Chromium (Cloudflare Turnstile flags the headless
# shell), so the runtime image bundles Playwright's Chromium + Xvfb and runs crate
# under a virtual display. Turnstile may still refuse a datacenter IP.

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

# Build Bento4's mp4decrypt from source (CENC decrypt for Monochrome). From
# source so it works on both amd64 and arm64 — bok.net ships amd64 binaries only.
FROM python:3.12-slim AS bento4
RUN apt-get update \
    && apt-get install -y --no-install-recommends git cmake g++ make \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 https://github.com/axiomatic-systems/Bento4 /b4 \
    && cmake -S /b4 -B /b4/build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /b4/build --target mp4decrypt -j"$(nproc)"

FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/saikhurana98/load-balanced-music-downloader"
LABEL org.opencontainers.image.description="Load-balanced tiered FLAC music downloader"

# ffmpeg: ffprobe (verification) + remux. xvfb + xauth: virtual display for the
# headed Monochrome browser (xvfb-run needs xauth, which recommends-off omits).
# tini: real PID 1 — xvfb-run's bash script hangs the server if it runs as PID 1,
# so tini sits above it and reaps/forwards signals. mp4decrypt: CENC decrypt.
# libchromaprint-tools: fpcalc for the review fingerprint guard (ADR-0019) —
# absent -> the guard silently turns itself off, so this is the "full" image only.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg xvfb xauth tini libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

COPY --from=bento4 /b4/build/mp4decrypt /usr/local/bin/mp4decrypt

# App wheel + yt-dlp (Tier 3) + Playwright (Monochrome browser driver).
COPY --from=builder /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl yt-dlp "playwright>=1.61.0" && rm -rf /tmp/*.whl

# Chromium + its system libs, into a world-readable path the non-root user can run.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*

# Built console UI, served by `crate console` (StaticFiles) at CRATE_CONSOLE_STATIC.
COPY --from=ui /ui/dist /app/console
ENV CRATE_CONSOLE_STATIC=/app/console

# Container browser needs --no-sandbox (unprivileged user, no setuid sandbox).
ENV CRATE_BROWSER_NO_SANDBOX=1

# non-root
RUN useradd -m -u 1000 crate
USER crate
WORKDIR /downloads

# tini as PID 1, then Xvfb (headed Chromium needs a display; -a auto-picks one).
# xvfb-run as PID 1 wedges the server, so it must run beneath a real init.
ENTRYPOINT ["tini", "--", "xvfb-run", "-a", "crate"]
CMD ["--help"]
