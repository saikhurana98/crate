# ADR-0022: Imported playlists are mirrored into each user's own Navidrome account

- Status: accepted
- Date: 2026-09-14
- Related: ADR-0018 (management console), ADR-0020 (background jobs)

## Context

Importing a Spotify playlist queues its tracks for download, but the playlist
itself never reached Navidrome, where people actually listen. Crate is multi-user
(Login with Spotify) and so is Navidrome, so a playlist should land in the account
of the person who imported it, not in one shared admin account.

## Decision

- **Per-user link.** Each user links their own Navidrome login on the Sync page.
  The console checks it with a Subsonic `ping` before storing it; a failed check
  stores nothing. Subsonic token auth (`t = md5(password + salt)`) needs the
  password itself, so it is stored encrypted with the existing `SecretBox`
  (AES-256-GCM, `CRATE_CONFIG_KEY` / `config.key`), exactly like source
  credentials. The API never returns it. Unlinking deletes it.
- **Import mirrors.** When a user with a linked account imports a playlist (or
  Liked Songs), the playlist is marked for mirroring and a `navidrome_push` job
  is queued. An "ND" toggle per playlist on the Sync page mirrors, or stops
  mirroring, any other playlist.
- **The push job runs in serve** (sync lane): on import, on link, and every
  `CRATE_NAVIDROME_PUSH_INTERVAL` (default 30 min) while
  `CRATE_NAVIDROME_URL` is set. For each mirrored playlist it:
  1. takes the tracks in Spotify order, from the items Library Sync keeps;
  2. keeps the ones in the library index, since the rest can't be in Navidrome yet;
  3. resolves each to a Navidrome song id, first from a shared
     merge key → song id cache (re-checked after 3 days), else via one `search3`.
     Matching is pure (`domain/navidrome.py`): an ISRC match wins; otherwise the
     artist and title must normalise to the same merge key, and the durations
     must be within 5 s. Among matches it prefers lossless, then bitrate. No
     match means the song is left out, never a guess;
  4. creates the playlist as the user (`createPlaylist`, sent as a POST form so a
     long `songId` list fits), or replaces its songs in place. If it was deleted
     in Navidrome, it's created again.

  Tracks still downloading join on a later run. A rejected login stops that
  user's pushes and asks them to relink; any other failure skips just that
  playlist. Each run is capped at 2,000 searches.
- `CRATE_NAVIDROME_URL` is the server-side API address (here
  `http://127.0.0.1:4533`; both containers use host networking).
  `CRATE_NAVIDROME_PUBLIC_URL` is only for the "open" link in the browser.

## Consequences

- Playlists appear in each user's own Navidrome clients and fill in as downloads
  finish, with no manual step.
- Crate now holds users' Navidrome passwords, encrypted at rest. A user who
  prefers not to share theirs simply doesn't link.
- A mirrored playlist is overwritten on every push, so edits made in Navidrome
  are lost. Spotify is the source of truth, as for Library Sync.

## Update 2026-09-15: Navidrome behind SSO

Some Navidrome instances sit behind the same SSO as crate, using Navidrome's
reverse-proxy authentication. Their users may have no Navidrome password at all.
Navidrome has no OAuth endpoint, so crate can't redirect to it and wait to be sent
back. Instead, "Sign in with Navidrome" uses the identity crate has already
verified:

- It's offered only when `CRATE_NAVIDROME_SSO_HEADER` is set **and** crate itself
  signs people in with SSO (ADR-0023). The username then comes from the identity
  provider (`CRATE_OIDC_USERNAME_CLAIM`, default `preferred_username`), never from
  the user, so nobody can link someone else's Navidrome.
- Crate fetches Navidrome's `/app/` page with that header, from an address in
  `ND_REVERSEPROXYWHITELIST`. Navidrome signs the user in (creating the account on
  a first visit, as it would anyway) and embeds the Subsonic token + salt in the
  page's app config. Crate checks that pair with a `ping`, then stores it encrypted
  (`auth_kind = 'token'`) and uses it exactly like a password-derived token.
- Whitelisting crate's address means crate can act as any Navidrome user, which is
  already true of the reverse proxy. It's the operator's explicit opt-in, and the
  route only ever uses the signed-in user's own SSO username.
- If the Navidrome password behind the token changes, pushes fail with "reconnect",
  and one click links again. The password form stays available as a fallback.
