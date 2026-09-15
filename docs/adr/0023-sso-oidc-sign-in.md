# ADR-0023: SSO (OpenID Connect) sign-in, with Spotify as a linked account

- Status: accepted
- Date: 2026-09-15
- Related: ADR-0018 v5 (Login with Spotify), ADR-0022 (per-user Navidrome)

## Context

"Log in with Spotify" made the Spotify account the identity. People who already
run an identity provider for their homelab (Authentik, Authelia, Keycloak,
Pocket ID, …) want crate behind it, like their other services. Crate still
needs each person's Spotify account, though, to read their playlists, Liked Songs
and plays.

## Decision

- **Optional and exclusive.** With `CRATE_OIDC_ISSUER` (plus client id, secret and
  redirect URI) set, SSO is the only sign-in and "Log in with Spotify" is gone.
  Without it, nothing changes.
- **A generic relying party.** Discovery, authorization code with PKCE (S256),
  and a `state` and `nonce` per sign-in. The client authenticates with
  `client_secret_basic`, or `client_secret_post` if that's all the provider
  supports; with no secret it's a public client relying on PKCE. The ID token comes
  straight from the token endpoint over TLS, so, as OIDC Core 3.1.3.7 (6) allows,
  crate checks its claims (`iss`, `aud`/`azp`, `exp`, `iat`, `nonce`, `sub`)
  rather than its signature. That avoids a JOSE dependency. Userinfo claims are
  merged in only when their `sub` matches.
- **Identity** is `(issuer, sub)` (migration 021). Name, email and picture are
  refreshed at every sign-in.
- **Admin** comes from `CRATE_OIDC_ADMIN_GROUPS` (the `groups` claim, configurable)
  when that's set. Otherwise it comes from `CRATE_ADMIN_SPOTIFY_IDS`, checked
  against the linked Spotify account. The configured rule always wins, granting or
  revoking.
- **Spotify is linked**, not signed in with. A signed-in user starts the link; the
  state is bound to their user id and checked against the session when Spotify
  calls back. It reuses the Spotify callback already registered, so the Spotify
  app needs no change. Tokens are stored per user as before (`spotify:<user id>`).
- **Carrying accounts over.** When someone links a Spotify account that belongs
  to an older Spotify-login user with no SSO identity, the new SSO user is folded
  into that older one. Every row it owns moves across (any table with a `user_id`
  column, found at run time), the older account's rows win where a table allows
  only one per user, and the older account takes the SSO identity and the session.
  So history, tiers, downloads and Navidrome links survive the switch. A Spotify
  account already linked to a different SSO user is refused.
- **Old sessions stop working.** Once SSO is on, a session only counts if its user
  has an SSO identity, so nobody bypasses the provider with an old cookie.

## Consequences

- One sign-in for crate and the rest of the homelab; the provider handles MFA and
  offboarding.
- Existing users do one extra step, once: sign in with SSO, then link Spotify.
- Signing out of crate doesn't sign you out of the provider (no RP-initiated
  logout yet).
