# Headless lbmd image — ships the sources that run without a GUI:
#   Tier 1 Qobuz · Tier 2 Deezer + Soulseek(via external slskd) · Tier 3 yt-dlp
# Monochrome (Amazon) needs a headed browser for Cloudflare Turnstile and is NOT
# included here; it only works in a local/desktop install.

FROM python:3.12-slim AS builder
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv build --wheel -o /dist

FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/saikhurana98/load-balanced-music-downloader"
LABEL org.opencontainers.image.description="Load-balanced tiered FLAC music downloader (headless CLI)"

# ffmpeg provides ffprobe (verification) + ffmpeg (remux); yt-dlp is the Tier-3 source.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl yt-dlp && rm -rf /tmp/*.whl

# non-root
RUN useradd -m -u 1000 lbmd
USER lbmd
WORKDIR /downloads

ENTRYPOINT ["lbmd"]
CMD ["--help"]
