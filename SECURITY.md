# Security Policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Do not
open a public issue containing credentials, exploit details, or user data.
Include affected versions, reproduction steps, and the expected impact. You can
expect an initial response within seven days.

## Secrets and local data

Never commit `.crate.env`, `.env`, `creds/`, `data/`, provider sessions, or a
`config/sources.json` file. The management console stores provider credentials
encrypted in `data/crate.db`; `data/config.key` is required to decrypt them.
Back up those two files together and keep both private.

The supplied Docker Compose file binds the console and shim to `127.0.0.1`.
Use an authenticated HTTPS reverse proxy before making either service reachable
from another machine.
