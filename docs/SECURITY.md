# Security notes

This repository is an alpha technical preview, not a managed security product.

## Safe defaults

- `codex-monitor local` binds to `127.0.0.1` unless explicitly changed.
- The Codex App Server transport stays local and uses `stdio`.
- Web passwords are hashed with Argon2.
- Browser sessions use HTTP-only, same-site cookies and CSRF tokens.
- Agent authentication uses a separate high-entropy token.
- Web approvals are one-time decisions and do not create permanent allow rules.

## Never commit

- `.env` files
- Codex authentication files or the contents of `CODEX_HOME`
- exported production databases
- browser cookies
- real device tokens

## Internet exposure

Use TLS through a reverse proxy and a strong, unique admin password. Do not expose the Codex App Server listener directly to the internet. Review the source and threat model before using the project for sensitive repositories.

