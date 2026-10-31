# SimpleLogin SMTP relay with a Postfix front end

A public SMTP submission relay (ports 587 and 465) for apps and servers that need to send mail
**as your SimpleLogin aliases**. Postfix is the public edge; the SimpleLogin relay script
does the reverse-alias work behind it.

```
senders ──587 STARTTLS / 465 TLS + login──► Postfix ──127.0.0.1:8025──► relay script ──► upstream ──► SimpleLogin ──► recipient
```

The upstream is either an external mailbox (any SMTP account), or **direct delivery**: the script
hands the mail back to Postfix on `127.0.0.1:10026`, which DKIM-signs it and delivers it straight
to SimpleLogin's servers on port 25, with no third-party mailbox involved.

| Part | Job |
| --- | --- |
| **Postfix** (`postfix/`) | TLS, one login per sender, "this login may only send as these addresses", rate limits, queue + retries, logs for fail2ban |
| **Relay script** (`relay/`) | Looks up each recipient's SimpleLogin reverse alias, rewrites `To`/`Cc`, sends via the upstream |
| **OpenDKIM** (`opendkim/`) | Direct delivery only: signs outgoing mail with your DKIM key (idles otherwise) |

Postfix hands each message from a sender, with the original sender and recipients, to the
script (a `content_filter` on 587/465), and only removes it from its queue once the script
answers `250`. On `4xx` it retries (up to 7 days). With direct delivery, the rewritten message
comes back on `127.0.0.1:10026` and Postfix queues and delivers it like any outgoing mail.

## Relay script behaviour

| Situation | Reply to Postfix | Postfix then |
| --- | --- | --- |
| Relayed | `250` | removes it from the queue |
| Some recipients refused upstream | `250` + warning in the log | done (the others already received it; retrying would duplicate) |
| Sender isn't one of your aliases, empty sender (bounce), SimpleLogin `4xx`, upstream `5xx` | `550` | gives up, no retries |
| SimpleLogin / upstream timeout, unreachable, `5xx`/`429` from SimpleLogin, bad API key or upstream login, lookups past `DATA_TIMEOUT` | `451` | keeps it and retries (up to 7 days) |

SimpleLogin and upstream calls run in a worker thread with real timeouts, so a slow call
never blocks other connections. Unknown senders are remembered for 5 minutes.

## Upstream mailbox

The script sends everything through one SMTP account (`UPSTREAM_*` in `relay.env`). SimpleLogin
only accepts mail to a reverse alias from one of that alias's mailboxes, or from an **authorized
address** of one of those mailboxes. So the address the script sends as must be one of these
for every alias you send as:

- **Recommended: an authorized address** on the mailbox your aliases already use (SimpleLogin →
  Mailboxes → that mailbox → Authorized addresses). One setting, covers every alias on that
  mailbox (including future ones), and doesn't change where incoming mail goes.
- Or a mailbox attached to each alias. This works too, but every mailbox on an alias receives a
  copy of its incoming mail, and each new alias needs it attached.

**Set `UPSTREAM_FROM` to the same address as `UPSTREAM_USERNAME`.** This is recommended for
every upstream, and required for some. The script then always sends as the upstream account's
own address, so it behaves the same with every provider, and switching providers is just
swapping the `UPSTREAM_*` lines together.

| Upstream | Settings | `UPSTREAM_FROM` |
| --- | --- | --- |
| Proton Mail | `smtp.protonmail.ch:587`, username = a custom-domain address, password = an SMTP token for it | **Required**: the token's address. Without it Proton refuses with `553 … Sender address rejected: not owned by user` |
| Gmail | `smtp.gmail.com:587`, an app password | Recommended: the Gmail address. Left empty it still works, because Gmail rewrites the sender itself |

Recipients always see the alias: SimpleLogin rewrites the sender when it forwards through the reverse alias.

> [!WARNING]
> Don't use the same account as both upstream and destination. If mail ends up in the same
> mailbox account it was sent from (e.g. Proton as upstream, and the recipient alias forwards to
> that Proton account), the provider sees its own sent message coming back and won't show it in
> the inbox. Use a separate account for sending, such as a dedicated Gmail.

## Direct delivery

The relay can deliver straight to SimpleLogin instead of using an external mailbox. Every
message goes to a reverse alias, so the only server that ever receives mail from the relay is
SimpleLogin's. A big provider's sending reputation isn't needed; correct authentication is.

Requirements (shown for `MYHOSTNAME=smtp.example.com`, the same name everywhere):

| Item | Value |
| --- | --- |
| Outbound port 25 | Open on your host (many clouds block it until you ask) |
| Reverse DNS | Both server IPs (v4 and v6) → `smtp.example.com` (matching A/AAAA) |
| SPF | TXT `smtp.example.com`: `v=spf1 ip4:<v4> ip6:<v6> -all` |
| DKIM | TXT `<selector>._domainkey.smtp.example.com`, printed by `genkey` |
| DMARC | TXT `_dmarc.smtp.example.com`: start with `v=DMARC1; p=none; adkim=s; aspf=s`, then `p=reject` once mail passes |
| SimpleLogin | `relay@smtp.example.com` as an **authorized address** on your aliases' mailbox |

Setup:

```bash
# .env
DKIM_SELECTOR=relay
#OUTBOUND_IPV4_ONLY=yes      # if your IPv6 address has no reverse DNS yet

docker compose build opendkim
docker compose run --rm opendkim genkey     # prints the DKIM DNS record to add
```

`relay.env`:

```
UPSTREAM_HOST=127.0.0.1
UPSTREAM_PORT=10026
UPSTREAM_USERNAME=
UPSTREAM_PASSWORD=
UPSTREAM_STARTTLS=false
UPSTREAM_FROM=relay@smtp.example.com
```

Then `docker compose up -d --build`. Check a delivered message's headers at the recipient:
`spf=pass`, `dkim=pass` (`d=smtp.example.com`) and `dmarc=pass`.

- If OpenDKIM is down, Postfix defers the mail (`milter_default_action = tempfail`) instead of
  sending it unsigned.
- Bounce notices are delivered directly too (to the sender's alias), signed the same way.
- `docker compose run --rm opendkim showkey` prints the DNS record again. To rotate the key, use
  a new selector, publish its record, switch `DKIM_SELECTOR`, then remove the old record.

## Files

| Path | In git? | Purpose |
| --- | --- | --- |
| `docker-compose.yml` | yes | All three services (Postfix, relay script, OpenDKIM), host network |
| `relay/server.py`, `relay/utils.py` | yes | The SimpleLogin relay script (reads all settings from `relay.env`) |
| `relay/Dockerfile`, `relay/requirements.txt` | yes | Builds the script into a small image, run as an unprivileged user |
| `postfix/Dockerfile`, `postfix/entrypoint.sh` | yes | Postfix image; config is assembled at start |
| `postfix/config/main.cf` | yes | Postfix settings (no secrets) |
| `postfix/manage-logins.sh` | yes | Add / remove / list sender logins |
| `opendkim/` | yes | DKIM signer image (`genkey` / `showkey` / `run`) |
| `fail2ban/`, `logrotate/` | yes | Host-side config templates (`@STACK_DIR@` = where the repo lives) |
| `install-host-config.sh` | yes | Fills in the path and installs those into `/etc`, checks them, restarts fail2ban |
| `.env` ← `.env.example` | **no** | `MYHOSTNAME` |
| `relay.env` ← `relay.env.example` | **no** | Relay script settings: SimpleLogin API key, upstream login, internal login |
| `secrets/relay_login` ← `.example` | **no** | Postfix's login to the script (matches `relay.env`) |
| `postfix/config/sender_login` ← `.example` | **no** | Which login may send as which address |
| `dkim/` | **no** | DKIM private key (direct delivery), created by `genkey` |
| Docker volume `sasl-db` | — | Sender logins (Cyrus sasldb; root-only, passwords stored in plain form inside the volume) |
| Docker volume `postfix-spool` | — | Mail queue |

There are no published images: both are built from this repo on your own host
(`docker compose up -d --build`), so what runs is always exactly what you've read.

## Requirements

- Docker with the compose plugin
- A DNS name (e.g. `smtp.example.com`) pointing at the host — **not proxied** if you use Cloudflare
- A certificate for that name in `/etc/letsencrypt/live/<name>/` (e.g. certbot with the Cloudflare DNS plugin)
- Firewall: allow **587/tcp** and **465/tcp** inbound (IPv4 + IPv6); keep 25 and 8025 closed inbound. Direct delivery also needs **outbound** port 25

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

With [swaks](https://jetmore.org/john/code/swaks/) — leave out the password so it prompts, and always
use `-hs` (`--hide-send`): without it swaks prints the AUTH exchange, and the line after
`334 UGFzc3dvcmQ6` is the password, only base64-encoded.

```bash
H="-hs --server smtp.example.com --port 587"
swaks $H --tls --auth LOGIN --auth-user relay-x --quit-after AUTH                                          # wrong password
swaks $H --tls --auth LOGIN --auth-user relay-x --from other@example.com --to t@example.org --quit-after RCPT
swaks $H --tls --from x@example.com --to t@example.org --quit-after RCPT                                   # no login
swaks $H --auth LOGIN --auth-user relay-x --quit-after AUTH                                                # no TLS
swaks -hs --server smtp.example.com --port 465 --tlsc --auth LOGIN --auth-user relay-x --from x@example.com --to t@example.org --quit-after RCPT   # implicit TLS
```

| Test | Expected |
| --- | --- |
| Correct login, allowed sender | `250` — mail arrives |
| Wrong password | `535 5.7.8 … authentication failed` |
| Correct login, another address as sender | `553 5.7.1 … not owned by user` |
| No login | `554 5.7.1 … Client host rejected: Access denied` |
| `AUTH` before `STARTTLS` | not offered |
| Port 465 with `--tlsc` | TLS handshake first, then the same results as 587 |
| Ports 25, 8025, 10026, 8891 from outside | closed / filtered (Postfix has no port-25 listener; the others are loopback only) |

Check fail2ban recognises failures in this log format:

```bash
fail2ban-regex logs/postfix.log 'postfix[mode=auth]' | grep -E "^Lines|Failregex:"
```

## Firewall note

Ports published by Docker (`ports:`) bypass `ufw`, because Docker inserts its own rules ahead of it.
The relay script is published on `127.0.0.1` only, so it is never reachable from outside, and Postfix
uses the host network (so `ufw` does apply to it). The relay script (8025), the re-injection port (10026) and
OpenDKIM (8891) all listen on `127.0.0.1` only. For anything else on the host, prefer a firewall
outside the VM (e.g. your provider's cloud firewall). Allow `587/tcp` and `465/tcp` for IPv4 and IPv6.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `smtp-relay` restarts, `Permission denied` on `server.py` | Files not readable by the container user | The Dockerfile uses `COPY --chmod=644`; rebuild with `docker compose up -d --build smtp-relay` |
| `454 4.7.0 Temporary authentication failure`; log: `unable to open Berkeley db /etc/sasldb2` | Postfix not reading `/etc/postfix/sasl/smtpd.conf` | `cyrus_sasl_config_path = /etc/postfix/sasl` in `main.cf` (already set) |
| `saslpasswd2`: `BDB0004 … unexpected file type or format` | An empty `sasldb2` file exists | Delete it; only `saslpasswd2` should create it (the entrypoint removes empty ones) |
| Script log: `Upstream refused all recipients … 553 … not owned by user` | Upstream only accepts its own address as sender (e.g. Proton) | Set `UPSTREAM_FROM` to the upstream address; rebuild `smtp-relay` |
| `535 … authentication failed` | Client and relay hold different passwords | Re-enter on the client, or reset with `manage-logins.sh add <login>` |
| `553 … not owned by user relay-x@smtp.example.com` | The From address isn't listed for that login | Fix the client's From, or add it to `sender_login` (with both login forms) |
| Client: `Server certificate not verified` | Client has no CA list configured | e.g. Postfix `smtp_tls_CAfile = /etc/ssl/certs/ca-certificates.crt` |
| Script replies `550` | Permanent: sender isn't a SimpleLogin alias, empty sender, SimpleLogin 4xx, upstream 5xx | See `docker compose logs smtp-relay` |
| Script replies `451` | Temporary: SimpleLogin/upstream down or slow, bad API key or upstream login | Postfix retries automatically (up to 7 days) |
