# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `UPSTREAM_FROM` setting: when set, the relay sends upstream as this address (envelope
  sender and `From` header, keeping the display name; `Sender` and `Return-Path` removed).
  Needed for upstreams that only accept their own address, such as Proton Mail SMTP
  tokens. Unset, behaviour is unchanged. Recipients still see the alias, because
  SimpleLogin rewrites the sender when it forwards through the reverse alias.
  Recommended for every upstream (set it to the `UPSTREAM_USERNAME` address); required for
  Proton Mail.
  SimpleLogin must accept the address for your aliases: add it as an authorized address
  on their mailbox (recommended) or attach it as one of their mailboxes.

## [2.0.0] - 2026-10-30

This release puts Postfix in front of the relay script. The script no longer faces the
internet. It runs on loopback behind Postfix, which handles TLS, logins and the retry queue.
Deployment is now a `docker compose` stack built from source.

### Added

- Postfix front end (`postfix/`, Debian 13, Postfix 3.10) as the only public listener:
  - Port **587** (`submission`), with STARTTLS required.
  - Port **465** (`submissions`), with implicit TLS (RFC 8314).
  - Both ports get identical rules from one loop in `entrypoint.sh`.
  - `AUTH PLAIN LOGIN` is offered only after TLS.
  - Port 25 is disabled.
- One login per sender (Cyrus SASL `sasldb` in a Docker volume), managed with
  `postfix/manage-logins.sh add | del | list`.
- `postfix/config/sender_login`: each login may send only as the addresses listed for it.
  Postfix enforces this with `reject_sender_login_mismatch`.
- Per-client-IP rate limits:
  - Per hour: 60 connections, 100 messages, 200 recipients, 20 auth attempts.
  - Per message: 20 recipients, 25 MB.
- A retry queue: messages are kept and retried for up to 7 days whenever the script answers `4xx`.
- A dedicated Postfix log file (`logs/postfix.log`).
- A fail2ban jail (`smtp-relay`), covering both ports: 5 failed logins within 10 minutes
  bans the IP for 1 hour.
- Weekly log rotation.
- `install-host-config.sh`, which installs the fail2ban and logrotate configuration into
  `/etc`. It fills in the repository's own path, checks both configs and restarts fail2ban.
- Startup checks: Postfix refuses to start if the certificate for `MYHOSTNAME`,
  `secrets/relay_login` or `postfix/config/sender_login` is missing.
- `SL_API_TIMEOUT` setting: a timeout in seconds for every SimpleLogin API call (default 10).
- Example configs: `.env.example`, `relay.env.example`, `secrets/relay_login.example` and
  `postfix/config/sender_login.example`.
- `README.md` covering:
  - setup, client settings, and STARTTLS vs implicit TLS;
  - certificate renewal;
  - `swaks` tests (with `-hs`, so the password is never printed);
  - the firewall note on Docker bypassing `ufw`;
  - troubleshooting.

### Changed

- **Breaking:** the relay script is no longer meant to be exposed. It listens on
  `127.0.0.1:8025`, behind Postfix, and only Postfix logs in to it with an internal login
  (`relay.env` plus `secrets/relay_login`). Senders now connect to Postfix on 587 or 465.
- **Breaking:** the script's Dockerfile moved from the repository root to `relay/`.
- The script image runs as an unprivileged user (`relay`, uid 10001), and its files are
  copied with mode 644.
- Errors are now split into permanent and temporary, so Postfix only retries what can
  succeed later:
  - **`550` (permanent):** the sender isn't one of your aliases, an empty sender, a
    SimpleLogin `4xx`, or an upstream `5xx`.
  - **`451` (temporary):** timeouts, SimpleLogin `5xx`/`429`, network errors, bad
    credentials, or lookups that run past `DATA_TIMEOUT`.
- SimpleLogin lookups and the upstream send run in a worker thread with a shared deadline,
  so one slow message no longer blocks the other connections.
- Unknown senders are remembered for 5 minutes, and alias searches are capped at 20 pages.
- `DATA_TIMEOUT` is now a deadline for the lookup phase. When it passes before anything has
  been sent upstream, the message is deferred and nothing is lost or sent twice.

### Removed

- **Breaking:** published container images. The GitHub Actions workflow that built the
  script image and pushed it to GHCR is gone. Build both images from source with
  `docker compose up -d --build`. The script image was only half of the design: run on its
  own, it has no per-sender rules, rate limits or queue.

### Fixed

- A hung SimpleLogin API call no longer hangs the connection. The previous 10-second
  timeout was set on the `requests` session, which ignores it, so calls had no timeout at
  all. It is now passed on every request.
- A slow lookup or upstream send no longer stalls every other connection. These blocking
  calls used to run on the event loop.
- When some, but not all, recipients are refused upstream, this is now logged and answered
  with `250`. Retrying would send duplicate mail to the recipients who already received it.

### Security

- The relay login check uses a constant-time comparison (`hmac.compare_digest`).
- Empty senders (bounces) are rejected instead of being relayed through a reverse alias.
- Every message passes through the approved upstream mailbox, so SimpleLogin's
  approved-sender check passes for all of them. Per-login sender restrictions in Postfix are
  what stop a leaked login from sending as your other aliases.
- TLS is required on every public connection, and clients are expected to verify the
  certificate, so a stale DNS record can't collect passwords.

## [1.0.0] - 2026-04-14

### Added

- SMTP relay for sending as SimpleLogin aliases: looks up each recipient's reverse alias
  through the SimpleLogin API, rewrites `To`/`Cc`, and sends through the approved upstream
  mailbox.
- Container image built by GitHub Actions and published to GHCR.
- License.

[Unreleased]: https://github.com/OWNER/simple-login-smtp-relay/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/OWNER/simple-login-smtp-relay/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/OWNER/simple-login-smtp-relay/releases/tag/v1.0.0
