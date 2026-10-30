#!/bin/sh
# OpenDKIM signer for direct delivery. Keys live in /keys (./dkim on the host, gitignored).
#   run      (default) sign on 127.0.0.1:8891; idles if DKIM is not configured
#   genkey   create a 2048-bit key for DKIM_SELECTOR and print the DNS record
#   showkey  print the DNS record for the existing key again
set -eu

DKIM_DOMAIN="${DKIM_DOMAIN:-${MYHOSTNAME:-}}"
KEY="/keys/${DKIM_SELECTOR:-none}.private"

need_config() {
  [ -n "${DKIM_SELECTOR:-}" ] && [ -n "$DKIM_DOMAIN" ] || {
    echo "set DKIM_SELECTOR (and MYHOSTNAME or DKIM_DOMAIN) in .env" >&2; exit 1; }
}

print_record() {
  echo
  echo "Add this DNS record (TXT, DNS only):"
  echo "  Name:  ${DKIM_SELECTOR}._domainkey.${DKIM_DOMAIN}"
  # join the quoted chunks opendkim-genkey writes into one value
  printf '  Value: '
  grep -o '"[^"]*"' "/keys/${DKIM_SELECTOR}.txt" | tr -d '"\n'
  echo
}

case "${1:-run}" in
  genkey)
    need_config
    [ -e "$KEY" ] && { echo "$KEY already exists; delete it first to make a new key" >&2; exit 1; }
    opendkim-genkey -b 2048 -h sha256 -d "$DKIM_DOMAIN" -s "$DKIM_SELECTOR" -D /keys
    chown opendkim:opendkim "$KEY"; chmod 600 "$KEY"
    print_record ;;
  showkey)
    need_config; print_record ;;
  run)
    if [ -z "${DKIM_SELECTOR:-}" ]; then
      echo "DKIM_SELECTOR not set: DKIM signing disabled, idling"
      exec sleep infinity
    fi
    need_config
    [ -f "$KEY" ] || { echo "missing $KEY - run: docker compose run --rm opendkim genkey" >&2; exit 1; }
    chown opendkim:opendkim "$KEY"; chmod 600 "$KEY"
    cat > /etc/opendkim.conf <<CONF
Mode                    s
Domain                  $DKIM_DOMAIN
Selector                $DKIM_SELECTOR
KeyFile                 $KEY
Socket                  inet:8891@127.0.0.1
InternalHosts           127.0.0.1,::1
Canonicalization        relaxed/simple
SignatureAlgorithm      rsa-sha256
OversignHeaders         From
UserID                  opendkim:opendkim
Syslog                  no
Background              no
CONF
    echo "DKIM signing for $DKIM_DOMAIN with selector $DKIM_SELECTOR on 127.0.0.1:8891"
    exec opendkim -f -x /etc/opendkim.conf ;;
  *)
    echo "usage: run | genkey | showkey" >&2; exit 2 ;;
esac
