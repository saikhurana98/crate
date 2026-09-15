-- SSO / OIDC sign-in (ADR-0023). With CRATE_OIDC_* set, people sign in with the
-- identity provider and link Spotify separately, so spotify_id becomes just the
-- linked account. (oidc_issuer, oidc_sub) is the stable OIDC identity; SQLite
-- treats NULLs as distinct, so users without one don't collide.
ALTER TABLE users ADD COLUMN oidc_issuer TEXT;
ALTER TABLE users ADD COLUMN oidc_sub TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_oidc ON users(oidc_issuer, oidc_sub);
