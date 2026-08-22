# Secrets

Every third-party credential the service touches lives here. Adapter PRs must update this file in the same commit that introduces the credential reference.

**No secret values are ever committed.** This file is a *map* — names, where they're used, how to obtain them, and the env-var convention.

## Convention

- Env-var prefix: `CRATE_`
- Naming: `CRATE_<SERVICE>_<KIND>` — example: `CRATE_SPOTIFY_CLIENT_ID`, `CRATE_ACOUSTID_API_KEY`
- Loaded via `process.env` inside `composition/config.ts`. Adapters never read `process.env` directly.
- Missing required env at startup → fail-fast with a list of what's missing.

## Inventory (placeholder — adapter PRs append)

### MusicBrainz
- **Env**: `CRATE_MUSICBRAINZ_USER_AGENT` (required by their API ToS)
- **How to obtain**: arbitrary string in form `AppName/Version (contact-email)`; no signup
- **Used in**: `adapters/metadata/musicbrainz.ts`

### Spotify Web API
- **Env**: `CRATE_SPOTIFY_CLIENT_ID`, `CRATE_SPOTIFY_CLIENT_SECRET`
- **How to obtain**: https://developer.spotify.com/dashboard → Create app → Client Credentials flow
- **Used in**: `adapters/metadata/spotify.ts`, `adapters/bulk/spotify.ts`, `adapters/cover-art/spotify-art.ts`
- **Note**: Client-credentials flow is sufficient (no user OAuth needed for catalog metadata)

#### Login with Spotify (console multi-user, Phase 2)
- **Env**: `CRATE_SPOTIFY_CLIENT_ID`, `CRATE_SPOTIFY_CLIENT_SECRET`, `CRATE_SPOTIFY_REDIRECT_URI`
  (same app as above). The redirect URI is the console's login callback:
  `https://<host>.<tailnet>.ts.net/api/auth/callback` — register it verbatim in the
  dashboard (Spotify accepts only https or loopback). Front the console with
  `tailscale serve` to get that HTTPS name.
- **Scopes**: `user-read-email user-library-read playlist-read-private
  playlist-read-collaborative` (+ the existing player scopes). Broadening scopes forces
  everyone previously connected to re-consent — expected.
- **Dev-mode cap**: a Development-mode app authorizes at most 25 users; add each friend's
  Spotify email under the app's user list (or request Extended Quota).
- **Behavior**: when these are set the console requires Login-with-Spotify (session cookie);
  when absent it stays single-user (open, or `CRATE_OPS_API_KEY` only) exactly as before.

### Apple Music API
- **Env**: `CRATE_APPLE_DEVELOPER_TOKEN` (JWT, signed with private key)
- **How to obtain**: Apple Developer account → MusicKit → generate developer token
- **Used in**: `adapters/metadata/apple.ts`, `adapters/bulk/apple.ts`, `adapters/cover-art/apple-art.ts`

### YouTube Music
- **Env**: `CRATE_YTMUSIC_AUTH_HEADERS_PATH` (path to JSON file with cookies + headers extracted via `ytmusicapi setup`)
- **How to obtain**: install `ytmusicapi`, run `ytmusicapi browser`, paste headers
- **Used in**: `adapters/metadata/ytmusic.ts`, `adapters/bulk/ytmusic.ts`

### AcoustID
- **Env**: `CRATE_ACOUSTID_API_KEY`
- **How to obtain**: https://acoustid.org/login → register an application → API key
- **Used in**: `adapters/fingerprint/chromaprint-acoustid.ts`

### last.fm (optional, cover-art fallback)
- **Env**: `CRATE_LASTFM_API_KEY`
- **How to obtain**: https://www.last.fm/api/account/create
- **Used in**: `adapters/cover-art/lastfm-art.ts`

### Source-tier creds (added per adapter)

#### OnTheSpot (Spotify ripper)
- **Env**: `CRATE_ONTHESPOT_USERNAME`, `CRATE_ONTHESPOT_PASSWORD` (Spotify account creds)
- **How to obtain**: a Spotify account; **dedicated account recommended** to avoid risk to primary
- **Used in**: `adapters/sources/onthespot.ts`
- **Risk**: account may be flagged for suspicious activity; rotate as needed

#### yt-dlp / YouTube Music
- **Env**: `CRATE_YTDLP_BIN_PATH` (path to yt-dlp binary), `CRATE_YTDLP_COOKIES_PATH` (optional, for age-restricted)
- **How to obtain**: install `yt-dlp` system-wide; cookies optional
- **Used in**: `adapters/sources/ytdlp-ytmusic.ts`

#### Squid/Qobuz mirror (Squid WTF)
- **Env**: typically none — public mirror
- **Note**: rate-limit-only; if mirror requires header tokens in future, add here
- **Used in**: `adapters/sources/squid-qobuz.ts`

#### Lucida
- **Env**: TBD by adapter author — likely cookies + Cloudflare clearance via Playwright session
- **Used in**: `adapters/sources/lucida.ts`

#### Monochrome
- **Env**: TBD by adapter author
- **Used in**: `adapters/sources/monochrome.ts`

#### Soulseek (via slskd)
- **Env**: `CRATE_SLSKD_URL`, `CRATE_SLSKD_API_KEY`, `CRATE_SLSKD_DOWNLOADS`
- **How to obtain**: run an [slskd](https://github.com/slskd/slskd) daemon; the API key is a
  random string you set under `web.authentication.api_keys` in its `slskd.yml`.
- **Note**: the Soulseek *account* (username/password) belongs to slskd's own config, not to
  crate — crate only talks to slskd's REST API and reads its downloads dir (needs write access
  too, since it moves the finished file out).
- **Used in**: `infrastructure/soulseek/slskd_client.py`, wired in `composition/sources.py`

## Local development

Recommended: copy `crate.env.example` (TBD, generated by Phase 0 unit) to `.crate.env`, fill in values, source via `set -a; source .crate.env; set +a` before `pnpm start`. `.crate.env` is in `.gitignore`.

## Production

Whatever process supervisor is in use (systemd, pm2, docker-compose, …) must inject env vars at start. The service does not read any file other than `crate.config.json` for runtime config — secrets only via env.
