#!/bin/sh
# Builds Postfix's configuration from the mounted files, then runs Postfix in the foreground.
set -eu

: "${MYHOSTNAME:?MYHOSTNAME is not set}"
CERT_DIR="/etc/letsencrypt/live/$MYHOSTNAME"

# --- main.cf --------------------------------------------------------------------
cp /config/main.cf /etc/postfix/main.cf
postconf -e "myhostname = $MYHOSTNAME" \
            "smtpd_tls_cert_file = $CERT_DIR/fullchain.pem" \
            "smtpd_tls_key_file = $CERT_DIR/privkey.pem"

# --- master.cf ------------------------------------------------------------------
# No chroot (simpler in a container); no port 25 listener; submission on 587 only.
postconf -F '*/*/chroot = n'
postconf -MX smtp/inet 2>/dev/null || true
postconf -M  submission/inet="submission inet n - n - - smtpd"
postconf -P  "submission/inet/syslog_name=postfix/submission" \
             "submission/inet/smtpd_tls_security_level=encrypt" \
             "submission/inet/smtpd_sasl_auth_enable=yes" \
             "submission/inet/smtpd_client_restrictions=permit_sasl_authenticated,reject" \
             "submission/inet/smtpd_sender_restrictions=reject_sender_login_mismatch,permit_sasl_authenticated,reject" \
             "submission/inet/smtpd_relay_restrictions=permit_sasl_authenticated,reject" \
             "submission/inet/smtpd_recipient_restrictions=permit_sasl_authenticated,reject"

# --- Logins: Cyrus SASL with a sasldb in the sasl-db volume -----------------------
mkdir -p /etc/postfix/sasl /var/lib/sasl
cat > /etc/postfix/sasl/smtpd.conf <<EOF
pwcheck_method: auxprop
auxprop_plugin: sasldb
mech_list: PLAIN LOGIN
sasldb_path: /var/lib/sasl/sasldb2
EOF
# sasldb2 is created by saslpasswd2 (manage-logins.sh). An empty placeholder file
# is not a valid database, so only fix permissions on a real one.
if [ -s /var/lib/sasl/sasldb2 ]; then
  chown root:postfix /var/lib/sasl/sasldb2
  chmod 640 /var/lib/sasl/sasldb2
else
  rm -f /var/lib/sasl/sasldb2
fi

# --- Secrets ----------------------------------------------------------------------
[ -f /secrets/relay_login ] || { echo "missing secrets/relay_login" >&2; exit 1; }
[ -f /config/sender_login ] || { echo "missing postfix/config/sender_login" >&2; exit 1; }
install -m 600 -o root -g root /secrets/relay_login /etc/postfix/relay_login

# --- Certificate ------------------------------------------------------------------
[ -f "$CERT_DIR/fullchain.pem" ] || { echo "missing certificate in $CERT_DIR" >&2; exit 1; }

# --- Start ------------------------------------------------------------------------
mkdir -p /var/log/postfix
postfix set-permissions >/dev/null 2>&1 || true
postfix check
exec postfix start-fg
