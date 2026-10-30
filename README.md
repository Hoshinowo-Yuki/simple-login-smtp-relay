# SimpleLogin SMTP relay with a Postfix front end

A public SMTP submission relay (ports 587 and 465) for apps and servers that need to send mail
**as your SimpleLogin aliases**. Postfix is the public edge; the SimpleLogin relay script
does the reverse-alias work behind it.

```
senders ──587 STARTTLS / 465 TLS + login──► Postfix ──127.0.0.1:8025──► relay script ──► upstream mailbox ──► SimpleLogin ──► recipient
```

| Part | Job |
| --- | --- |
| **Postfix** (`postfix/`) | TLS, one login per sender, "this login may only send as these addresses", rate limits, queue + retries, logs for fail2ban |
| **Relay script** (`relay/`) | Looks up each recipient's SimpleLogin reverse alias, rewrites `To`/`Cc`, sends via the upstream mailbox |

Postfix never sends to reverse aliases itself — it hands each message, with the original
sender and recipients, to the script, and only removes it from its queue once the script
answers `250`. On `4xx` it retries (up to 7 days).

## Relay script behaviour

| Situation | Reply to Postfix | Postfix then |
| --- | --- | --- |
| Relayed | `250` | removes it from the queue |
| Some recipients refused upstream | `250` + warning in the log | done (the others already received it; retrying would duplicate) |
| Sender isn't one of your aliases, empty sender (bounce), SimpleLogin `4xx`, upstream `5xx` | `550` | gives up, no retries |
| SimpleLogin / upstream timeout, unreachable, `5xx`/`429` from SimpleLogin, bad API key or upstream login, lookups past `DATA_TIMEOUT` | `451` | keeps it and retries (up to 7 days) |

SimpleLogin and upstream calls run in a worker thread with real timeouts, so a slow call
never blocks other connections. Unknown senders are remembered for 5 minutes.

## Files

| Path | In git? | Purpose |
| --- | --- | --- |
| `docker-compose.yml` | yes | Both services |
| `relay/server.py`, `relay/utils.py` | yes | The SimpleLogin relay script (reads all settings from `relay.env`) |
| `relay/Dockerfile`, `relay/requirements.txt` | yes | Builds the script into a small image, run as an unprivileged user |
| `postfix/Dockerfile`, `postfix/entrypoint.sh` | yes | Postfix image; config is assembled at start |
| `postfix/config/main.cf` | yes | Postfix settings (no secrets) |
| `postfix/manage-logins.sh` | yes | Add / remove / list sender logins |
| `fail2ban/`, `logrotate/` | yes | Host-side config templates (`@STACK_DIR@` = where the repo lives) |
| `install-host-config.sh` | yes | Fills in the path and installs those into `/etc`, checks them, restarts fail2ban |
| `.env` ← `.env.example` | **no** | `MYHOSTNAME` |
| `relay.env` ← `relay.env.example` | **no** | Relay script settings: SimpleLogin API key, upstream login, internal login |
| `secrets/relay_login` ← `.example` | **no** | Postfix's login to the script (matches `relay.env`) |
| `postfix/config/sender_login` ← `.example` | **no** | Which login may send as which address |
| Docker volume `sasl-db` | — | Sender logins (Cyrus sasldb; root-only, passwords stored in plain form inside the volume) |
| Docker volume `postfix-spool` | — | Mail queue |

There are no published images: both are built from this repo on your own host
(`docker compose up -d --build`), so what runs is always exactly what you've read.

## Requirements

- Docker with the compose plugin
- A DNS name (e.g. `smtp.example.com`) pointing at the host — **not proxied** if you use Cloudflare
- A certificate for that name in `/etc/letsencrypt/live/<name>/` (e.g. certbot with the Cloudflare DNS plugin)
- Firewall: allow **587/tcp** and **465/tcp** (IPv4 + IPv6); keep 25 and 8025 closed

## Setup

```bash
cd smtp-relay                                          # wherever you cloned it; any path works
cp .env.example .env                                   # set MYHOSTNAME
cp relay.env.example relay.env                         # SimpleLogin + upstream + internal login
cp secrets/relay_login.example secrets/relay_login     # same internal login as relay.env
cp postfix/config/sender_login.example postfix/config/sender_login
chmod 600 relay.env secrets/relay_login
mkdir -p logs

docker compose up -d --build
docker compose logs -f postfix          # should end with Postfix starting, no errors

./postfix/manage-logins.sh add relay-pve
./postfix/manage-logins.sh add relay-nextcloud
./postfix/manage-logins.sh list
```

After editing `sender_login` or `main.cf`: `docker compose restart postfix`.
After changing `relay/server.py` or `utils.py`: `docker compose up -d --build smtp-relay`.

## Certificate renewal

Postfix reads the certificate at start and on reload. Certbot deploy hook
(`/etc/letsencrypt/renewal-hooks/deploy/postfix-reload.sh`):

```sh
#!/bin/sh
docker exec smtp-postfix postfix reload
```

## fail2ban and log rotation

```bash
sudo ./install-host-config.sh
```

The templates contain no fixed path; the script fills in the repo's own location, so the stack can
live anywhere. Re-run it after any `git pull` that changes `fail2ban/` or `logrotate/`. Don't copy
the templates into `/etc` by hand — they'd keep the `@STACK_DIR@` placeholder.

## Client settings

| Setting | Value |
| --- | --- |
| Server | `smtp.example.com` (always the name, never the IP) |
| Port | `587` with STARTTLS, **or** `465` with implicit TLS (same logins and rules) |
| Login | that sender's own login (e.g. `relay-nextcloud`) |
| From | an address listed for that login in `sender_login` |

Many apps call these "STARTTLS" vs "SSL/TLS". In Nodemailer-based apps (e.g. Twikoo),
`secure: false` + port 587 = STARTTLS, `secure: true` + port 465 = implicit TLS; mixing them fails.

Clients should verify the certificate, so a stale DNS entry or a reassigned IP can never collect
their password. For a Postfix client:

```
relayhost = [smtp.example.com]:587
smtp_sasl_auth_enable = yes
smtp_sasl_password_maps = hash:/etc/postfix/sasl_passwd    # [smtp.example.com]:587 relay-x:<password>
smtp_tls_security_level = secure
smtp_tls_CAfile = /etc/ssl/certs/ca-certificates.crt       # some distros (e.g. Proxmox) don't set a CA list
```

## Tests (from outside)

With [swaks](https://jetmore.org/john/code/swaks/) — leave out the password so it prompts, and don't
share terminal output while the prompt is visible (swaks echoes what you paste):

```bash
H="--server smtp.example.com --port 587"
swaks $H --tls --auth LOGIN --auth-user relay-x --quit-after AUTH                                          # wrong password
swaks $H --tls --auth LOGIN --auth-user relay-x --from other@example.com --to t@example.org --quit-after RCPT
swaks $H --tls --from x@example.com --to t@example.org --quit-after RCPT                                   # no login
swaks $H --auth LOGIN --auth-user relay-x --quit-after AUTH                                                # no TLS
swaks --server smtp.example.com --port 465 --tlsc --auth LOGIN --auth-user relay-x --from x@example.com --to t@example.org --quit-after RCPT   # implicit TLS
```

| Test | Expected |
| --- | --- |
| Correct login, allowed sender | `250` — mail arrives |
| Wrong password | `535 5.7.8 … authentication failed` |
| Correct login, another address as sender | `553 5.7.1 … not owned by user` |
| No login | `554 5.7.1 … Client host rejected: Access denied` |
| `AUTH` before `STARTTLS` | not offered |
| Port 465 with `--tlsc` | TLS handshake first, then the same results as 587 |
| Port 25 / 8025 from outside | closed / filtered |

Check fail2ban recognises failures in this log format:

```bash
fail2ban-regex logs/postfix.log 'postfix[mode=auth]' | grep -E "^Lines|Failregex:"
```

## Firewall note

Ports published by Docker (`ports:`) bypass `ufw`, because Docker inserts its own rules ahead of it.
The relay script is published on `127.0.0.1` only, so it is never reachable from outside, and Postfix
uses the host network (so `ufw` does apply to it). For anything else on the host, prefer a firewall
outside the VM (e.g. your provider's cloud firewall). Allow `587/tcp` and `465/tcp` for IPv4 and IPv6.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `smtp-relay` restarts, `Permission denied` on `server.py` | Files not readable by the container user | The Dockerfile uses `COPY --chmod=644`; rebuild with `docker compose up -d --build smtp-relay` |
| `454 4.7.0 Temporary authentication failure`; log: `unable to open Berkeley db /etc/sasldb2` | Postfix not reading `/etc/postfix/sasl/smtpd.conf` | `cyrus_sasl_config_path = /etc/postfix/sasl` in `main.cf` (already set) |
| `saslpasswd2`: `BDB0004 … unexpected file type or format` | An empty `sasldb2` file exists | Delete it; only `saslpasswd2` should create it (the entrypoint removes empty ones) |
| `535 … authentication failed` | Client and relay hold different passwords | Re-enter on the client, or reset with `manage-logins.sh add <login>` |
| `553 … not owned by user relay-x@smtp.example.com` | The From address isn't listed for that login | Fix the client's From, or add it to `sender_login` (with both login forms) |
| Client: `Server certificate not verified` | Client has no CA list configured | e.g. Postfix `smtp_tls_CAfile = /etc/ssl/certs/ca-certificates.crt` |
| Script replies `550` | Permanent: sender isn't a SimpleLogin alias, empty sender, SimpleLogin 4xx, upstream 5xx | See `docker compose logs smtp-relay` |
| Script replies `451` | Temporary: SimpleLogin/upstream down or slow, bad API key or upstream login | Postfix retries automatically (up to 7 days) |
