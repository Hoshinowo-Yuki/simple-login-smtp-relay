#!/bin/sh
# Manage sender logins in the running Postfix container (run on the VPS host).
#   ./postfix/manage-logins.sh add  <login>   prompts for the password (generate it in your password manager)
#   ./postfix/manage-logins.sh del  <login>
#   ./postfix/manage-logins.sh list
# Remember: each login also needs its allowed address(es) in postfix/config/sender_login.
set -eu

CONTAINER=smtp-postfix
DB=/var/lib/sasl/sasldb2
REALM=$(docker exec "$CONTAINER" postconf -h myhostname)

fix_perms() {
  docker exec "$CONTAINER" sh -c "chown root:postfix $DB && chmod 640 $DB"
}

case "${1:-}" in
  add)  [ -n "${2:-}" ] || { echo "usage: $0 add <login>" >&2; exit 2; }
        docker exec -it "$CONTAINER" saslpasswd2 -c -f "$DB" -u "$REALM" "$2"
        fix_perms ;;
  del)  [ -n "${2:-}" ] || { echo "usage: $0 del <login>" >&2; exit 2; }
        docker exec "$CONTAINER" saslpasswd2 -d -f "$DB" -u "$REALM" "$2"
        fix_perms ;;
  list) docker exec "$CONTAINER" sasldblistusers2 -f "$DB" ;;
  *)    echo "usage: $0 add <login> | del <login> | list" >&2; exit 2 ;;
esac
