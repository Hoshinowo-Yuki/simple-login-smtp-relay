#!/bin/sh
# Install the host-side config (fail2ban jail, logrotate) for this stack.
# Paths are taken from wherever this repo is cloned — nothing is hardcoded.
# Safe to re-run after every `git pull` that changes fail2ban/ or logrotate/.
set -eu

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
STACK_DIR=$(cd "$(dirname "$0")" && pwd)

render() {   # $1 = template, $2 = destination
  tmp=$(mktemp)
  sed "s|@STACK_DIR@|$STACK_DIR|g" "$1" > "$tmp"
  install -m 644 -o root -g root "$tmp" "$2"
  rm -f "$tmp"
  echo "installed $2"
}

mkdir -p "$STACK_DIR/logs"
render "$STACK_DIR/fail2ban/jail.d/smtp-relay.local" /etc/fail2ban/jail.d/smtp-relay.local
render "$STACK_DIR/logrotate/smtp-relay"             /etc/logrotate.d/smtp-relay

logrotate -d /etc/logrotate.d/smtp-relay >/dev/null 2>&1 || { echo "logrotate config check failed" >&2; exit 1; }
fail2ban-client -t >/dev/null || { echo "fail2ban config check failed" >&2; exit 1; }
systemctl restart fail2ban
sleep 2
fail2ban-client status smtp-relay
