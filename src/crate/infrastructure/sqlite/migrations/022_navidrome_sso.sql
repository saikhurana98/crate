-- Navidrome behind SSO (ADR-0022, 2026-09-15 update).
-- users.sso_username: the username crate's SSO provider asserted
-- (CRATE_OIDC_USERNAME_CLAIM), the same name a reverse proxy hands Navidrome.
-- navidrome_accounts.auth_kind: 'password', or 'token' (a Subsonic token + salt
-- Navidrome issued through its trusted-header sign-in, so no password is known).
ALTER TABLE users ADD COLUMN sso_username TEXT;
ALTER TABLE navidrome_accounts ADD COLUMN auth_kind TEXT NOT NULL DEFAULT 'password';
