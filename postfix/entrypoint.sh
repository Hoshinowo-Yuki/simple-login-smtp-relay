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
# No chroot (simpler in a container); no port 25 listener.
# Two identical submission listeners:
#   submission  (587) STARTTLS, required before anything else
#   submissions (465) implicit TLS from the first byte (smtpd_tls_wrappermode)
postconf -F '*/*/chroot = n'
postconf -MX smtp/inet 2>/dev/null || true
for svc in submission submissions; do
  postconf -M  "$svc/inet=$svc inet n - n - - smtpd"
  postconf -P  "$svc/inet/syslog_name=postfix/$svc" \
               "$svc/inet/smtpd_tls_security_level=encrypt" \
               "$svc/inet/smtpd_sasl_auth_enable=yes" \
               "$svc/inet/smtpd_client_restrictions=permit_sasl_authenticated,reject" \
               "$svc/inet/smtpd_sender_restrictions=reject_sender_login_mismatch,permit_sasl_authenticated,reject" \
               "$svc/inet/smtpd_relay_restrictions=permit_sasl_authenticated,reject" \
               "$svc/inet/smtpd_recipient_restrictions=permit_sasl_authenticated,reject" \
               "$svc/inet/content_filter=relayscript:[127.0.0.1]:8025"
done
postconf -P "submissions/inet/smtpd_tls_wrappermode=yes"

# Transport to the relay script (loopback, internal login from secrets/relay_login)
postconf -M  "relayscript/unix=relayscript unix - - n - - smtp"
postconf -P  "relayscript/unix/syslog_name=postfix/relayscript" \
             "relayscript/unix/smtp_sasl_auth_enable=yes" \
             "relayscript/unix/smtp_sasl_password_maps=texthash:/etc/postfix/relay_login" \
             "relayscript/unix/smtp_sasl_security_options=noanonymous" \
             "relayscript/unix/smtp_tls_security_level=none"

# Re-injection from the relay script for direct delivery (loopback only, no login).
# Mail accepted here is delivered by this Postfix to the recipient's MX, DKIM-signed
# when DKIM is enabled. No content_filter here, or mail would loop back to the script.
R=127.0.0.1:10026/inet
postconf -M  "$R=127.0.0.1:10026 inet n - n - - smtpd"
postconf -P  "$R/syslog_name=postfix/reinject" \
             "$R/content_filter=" \
             "$R/mynetworks=127.0.0.0/8" \
             "$R/smtpd_sasl_auth_enable=no" \
             "$R/smtpd_tls_security_level=none" \
             "$R/smtpd_client_restrictions=permit_mynetworks,reject" \
             "$R/smtpd_helo_restrictions=" \
             "$R/smtpd_sender_restrictions=" \
             "$R/smtpd_relay_restrictions=permit_mynetworks,reject" \
             "$R/smtpd_recipient_restrictions=permit_mynetworks,reject" \
             "$R/smtpd_client_connection_rate_limit=0" \
             "$R/smtpd_client_message_rate_limit=0" \
             "$R/smtpd_client_recipient_rate_limit=0" \
             "$R/receive_override_options=no_unknown_recipient_checks"

# Outbound direct delivery over IPv4 only (if the IPv6 address has no reverse DNS)
if [ "${OUTBOUND_IPV4_ONLY:-no}" = "yes" ]; then
  postconf -P "smtp/unix/inet_protocols=ipv4"
fi

# --- DKIM (direct delivery) ------------------------------------------------------
# Enabled when DKIM_SELECTOR is set: mail re-injected on 10026 and locally generated
# mail (bounce notices) is signed by the opendkim service on 127.0.0.1:8891.
# If the signer is unreachable, mail is deferred rather than sent unsigned.
if [ -n "${DKIM_SELECTOR:-}" ]; then
  postconf -e "milter_default_action = tempfail" \
              "milter_protocol = 6" \
              "non_smtpd_milters = inet:127.0.0.1:8891"
  postconf -P "$R/smtpd_milters=inet:127.0.0.1:8891"
fi

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
