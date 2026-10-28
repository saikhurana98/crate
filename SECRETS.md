# Secrets and Provider Configuration

No secret values belong in this repository. Copy `.crate.env.example` to
`.crate.env` for local environment configuration, or enter credentials in the
console under **Provider Settings**.

Console-managed credentials are encrypted with AES-256-GCM in `data/crate.db`.
The key is generated as `data/config.key` unless `CRATE_CONFIG_KEY` supplies a
base64-encoded 32-byte key. Keep the database and key private and back them up
together.

## Provider inventory

| Provider | Required values | How to obtain them |
| --- | --- | --- |
| Qobuz | `CRATE_QOBUZ_APP_ID`, `CRATE_QOBUZ_APP_SECRET`, `CRATE_QOBUZ_TOKEN` | Your authorized Qobuz application/session |
| Monochrome | None | Public API |
| TIDAL | None | Public catalog and manifest services |
| Octave | `CRATE_OCTAVE_ACCOUNT_KEY` | Link Discord on Octave and restore the current `octv_…` key |
| Antra | `CRATE_ANTRA_USERNAME`, `CRATE_ANTRA_PASSWORD` | Create an account at the configured Antra service |
| Deezer | `CRATE_DEEZER_ARL` | Your Deezer browser session cookie |
| arcod | None | Public proxy |
| Soulseek | `CRATE_SLSKD_URL`, `CRATE_SLSKD_API_KEY`, `CRATE_SLSKD_DOWNLOADS` | A reachable slskd instance and shared download directory |
| Telegram | `CRATE_TG_API_ID`, `CRATE_TG_API_HASH`, `CRATE_TG_SESSION`, `CRATE_TG_BOT` | Telegram developer credentials, session, and bot username |
| yt-dlp | None | Always enabled as the final fallback |

Optional integrations use Spotify client credentials, an AcoustID API key,
Navidrome credentials, and OIDC settings documented in `.crate.env.example` and
the console.

## Files that must stay private

- `.crate.env` and `.env`
- `data/`, including `crate.db` and `config.key`
- `creds/` browser state or cookie files
- Telegram and other provider session files
- any local `config/sources.json` export

These paths are ignored by Git. Before publishing a fork, scan its complete Git
history as well as the current working tree; adding a path to `.gitignore` does
not remove a secret from earlier commits.
