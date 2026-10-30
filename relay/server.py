import os
import ssl
import hmac
import time
import email
import email.utils
import smtplib
import asyncio
import signal
import sys
import threading
import logging
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, LoginPassword

from utils import SimpleLoginClient, TemporaryError, PermanentError, AliasNotFound

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("smtp-relay")

# Relay config
RELAY_HOST = os.getenv("RELAY_HOST", "0.0.0.0")
RELAY_PORT = int(os.getenv("RELAY_PORT", "8025"))
RELAY_USERNAME = os.getenv("RELAY_USERNAME")
RELAY_PASSWORD = os.getenv("RELAY_PASSWORD")

# TLS config
TLS_ENABLED = os.getenv("TLS_ENABLED", "false").lower() == "true"
TLS_CERT = os.getenv("TLS_CERT", "")
TLS_KEY = os.getenv("TLS_KEY", "")

# SimpleLogin config
SL_API_URL = os.getenv("SL_API_URL", "https://app.simplelogin.io")
SL_API_KEY = os.getenv("SL_API_KEY")
SL_API_TIMEOUT = float(os.getenv("SL_API_TIMEOUT", "10"))

# Upstream SMTP config
UPSTREAM_HOST = os.getenv("UPSTREAM_HOST", "smtp.gmail.com")
UPSTREAM_PORT = int(os.getenv("UPSTREAM_PORT", "587"))
UPSTREAM_USERNAME = os.getenv("UPSTREAM_USERNAME")
UPSTREAM_PASSWORD = os.getenv("UPSTREAM_PASSWORD")
UPSTREAM_STARTTLS = os.getenv("UPSTREAM_STARTTLS", "true").lower() == "true"
# Optional: send upstream as this address (envelope sender + From header).
# Needed for providers that only allow the account's own address (e.g. Proton SMTP
# tokens); it must be a SimpleLogin mailbox of the aliases. Gmail rewrites the sender
# itself, so it works without this. Recipients still see the alias: SimpleLogin
# rewrites From when it forwards through the reverse alias.
UPSTREAM_FROM = os.getenv("UPSTREAM_FROM", "").strip()

# Timeout config
# DATA_TIMEOUT is a deadline for the lookup phase: if it passes before anything has
# been sent upstream, the message is deferred (451) so the client retries later.
# Once the upstream send has started it is never interrupted (no duplicates).
DATA_TIMEOUT = int(os.getenv("DATA_TIMEOUT", "30"))
UPSTREAM_TIMEOUT = int(os.getenv("UPSTREAM_TIMEOUT", "15"))

sl_client = None


def validate_config():
    required = {
        "RELAY_USERNAME": RELAY_USERNAME,
        "RELAY_PASSWORD": RELAY_PASSWORD,
        "SL_API_KEY": SL_API_KEY,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        logger.error(f"Missing required env vars: {', '.join(missing)}")
        sys.exit(1)
    # The upstream login is optional (none for direct delivery via the local Postfix),
    # but a username without a password (or the reverse) is a mistake.
    if bool(UPSTREAM_USERNAME) != bool(UPSTREAM_PASSWORD):
        logger.error("Set both UPSTREAM_USERNAME and UPSTREAM_PASSWORD, or neither")
        sys.exit(1)
    if TLS_ENABLED and (not TLS_CERT or not TLS_KEY):
        logger.error("TLS_ENABLED=true but TLS_CERT or TLS_KEY not set")
        sys.exit(1)


class Authenticator:
    def __call__(self, server, session, envelope, mechanism, auth_data):
        if mechanism not in ("LOGIN", "PLAIN"):
            return AuthResult(success=False, handled=False)
        if not isinstance(auth_data, LoginPassword):
            return AuthResult(success=False, handled=False)

        # constant-time comparison of both fields
        user_ok = hmac.compare_digest(auth_data.login, RELAY_USERNAME.encode("utf-8"))
        pass_ok = hmac.compare_digest(auth_data.password, RELAY_PASSWORD.encode("utf-8"))
        username = auth_data.login.decode("utf-8", errors="replace")

        if user_ok and pass_ok:
            logger.debug(f"Auth success for {username}")
            return AuthResult(success=True)

        peer = session.peer[0] if session.peer else "unknown"
        logger.warning(f"Auth failed for {username} from {peer}")
        return AuthResult(success=False, handled=False)


class RelayHandler:
    async def handle_DATA(self, server, session, envelope):
        deadline = time.monotonic() + DATA_TIMEOUT
        try:
            # blocking work (SimpleLogin API, upstream SMTP) runs in a worker thread,
            # so one slow call never freezes the other connections
            return await asyncio.to_thread(self._process, envelope, deadline)
        except PermanentError as e:
            logger.error(f"Rejected (permanent): {e}")
            code = "550 5.7.1" if isinstance(e, AliasNotFound) else "550 5.1.0"
            return f"{code} {e}"
        except TemporaryError as e:
            logger.warning(f"Deferred (temporary): {e}")
            return f"451 4.4.0 {e}"
        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)
            return "451 4.3.0 Internal error"

    def _process(self, envelope, deadline):
        mail_from = envelope.mail_from or ""
        rcpt_tos = list(envelope.rcpt_tos)
        data = envelope.content

        logger.info(f"Received mail from={mail_from or '<>'} to={rcpt_tos}")

        # Bounce notices (empty sender) can't be mapped to an alias
        if mail_from in ("", "<>"):
            raise PermanentError("Empty sender (bounce) not accepted")

        msg = email.message_from_bytes(data)

        # Build reverse alias mapping
        alias_map = {}
        for rcpt in rcpt_tos:
            if time.monotonic() > deadline:
                raise TemporaryError(f"Lookups exceeded {DATA_TIMEOUT}s; nothing sent, retry later")
            reverse_alias = sl_client.get_reverse_alias(mail_from, rcpt)
            alias_map[rcpt] = reverse_alias
            logger.info(f"  {rcpt} -> {reverse_alias}")

        # Replace addresses in To header
        if "To" in msg:
            new_to = self._replace_addresses(msg["To"], alias_map)
            del msg["To"]
            msg["To"] = new_to

        # Replace addresses in Cc header
        if "Cc" in msg:
            new_cc = self._replace_addresses(msg["Cc"], alias_map)
            del msg["Cc"]
            msg["Cc"] = new_cc

        # Strip Bcc header, just in case
        # Recipients are already in envelope rcpt_tos, so generally they should also receive the email.
        if "Bcc" in msg:
            del msg["Bcc"]

        # Forward via upstream SMTP
        # Extract bare email from reverse aliases
        new_rcpts = []
        for reverse in alias_map.values():
            _, addr = email.utils.parseaddr(reverse)
            new_rcpts.append(addr)

        if time.monotonic() > deadline:
            raise TemporaryError(f"Lookups exceeded {DATA_TIMEOUT}s; nothing sent, retry later")

        upstream_from = mail_from
        if UPSTREAM_FROM:
            display_name, _ = email.utils.parseaddr(msg.get("From", ""))
            del msg["From"]
            msg["From"] = email.utils.formataddr((display_name, UPSTREAM_FROM))
            for header in ("Sender", "Return-Path"):
                del msg[header]
            upstream_from = UPSTREAM_FROM

        refused = self._send_upstream(upstream_from, new_rcpts, msg.as_bytes())

        if refused:
            # Some (not all) recipients were refused upstream. The others already got
            # the mail, so retrying would duplicate it: report success, but log loudly.
            logger.warning(f"Relayed, but upstream refused {len(refused)} recipient(s): {refused}")
        else:
            logger.info("Relayed successfully")
        return "250 OK"

    def _send_upstream(self, mail_from, rcpts, data):
        """Send via the upstream mailbox; SMTP errors classified as temporary or permanent."""
        try:
            with smtplib.SMTP(UPSTREAM_HOST, UPSTREAM_PORT, timeout=UPSTREAM_TIMEOUT) as client:
                client.ehlo()
                if UPSTREAM_STARTTLS:
                    client.starttls()
                    client.ehlo()
                if UPSTREAM_USERNAME:
                    client.login(UPSTREAM_USERNAME, UPSTREAM_PASSWORD)
                return client.sendmail(mail_from, rcpts, data)
        except smtplib.SMTPAuthenticationError as e:
            # wrong upstream credentials: a config problem, keep the mail and retry
            raise TemporaryError(f"Upstream login failed ({e.smtp_code})") from e
        except smtplib.SMTPRecipientsRefused as e:
            codes = [code for code, _ in e.recipients.values()]
            if all(500 <= code < 600 for code in codes):
                raise PermanentError(f"Upstream refused all recipients: {e.recipients}") from e
            raise TemporaryError(f"Upstream deferred recipients: {e.recipients}") from e
        except (smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as e:
            if 500 <= e.smtp_code < 600:
                raise PermanentError(f"Upstream rejected the message ({e.smtp_code}): {e.smtp_error!r}") from e
            raise TemporaryError(f"Upstream deferred the message ({e.smtp_code}): {e.smtp_error!r}") from e
        except (smtplib.SMTPException, OSError) as e:
            # disconnects, timeouts, DNS/network errors
            raise TemporaryError(f"Upstream unavailable: {e}") from e

    def _replace_addresses(self, header_value: str, alias_map: dict) -> str:
        """Parse addresses from header and replace with reverse aliases."""
        addresses = email.utils.getaddresses([header_value])
        replaced = []
        for display_name, addr in addresses:
            if addr in alias_map:
                _, reverse_addr = email.utils.parseaddr(alias_map[addr])
                replaced.append(email.utils.formataddr((display_name, reverse_addr)))
            else:
                replaced.append(email.utils.formataddr((display_name, addr)))
        return ", ".join(replaced)


def main():
    global sl_client

    validate_config()
    sl_client = SimpleLoginClient(SL_API_URL, SL_API_KEY, timeout=SL_API_TIMEOUT)

    handler = RelayHandler()
    authenticator = Authenticator()

    tls_context = None
    if TLS_ENABLED:
        tls_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        tls_context.load_cert_chain(TLS_CERT, TLS_KEY)

    controller = Controller(
        handler,
        hostname=RELAY_HOST,
        port=RELAY_PORT,
        authenticator=authenticator,
        auth_required=True,
        auth_require_tls=TLS_ENABLED,
        require_starttls=TLS_ENABLED,
        tls_context=tls_context,
    )

    controller.start()
    logger.info(f"Listening on {RELAY_HOST}:{RELAY_PORT}")
    logger.info(f"Upstream: {UPSTREAM_HOST}:{UPSTREAM_PORT} (STARTTLS={'on' if UPSTREAM_STARTTLS else 'off'})"
                f" login={'yes' if UPSTREAM_USERNAME else 'none'}"
                f" sending as {UPSTREAM_FROM or 'the original sender'}")
    logger.info(f"TLS: {'enabled' if TLS_ENABLED else 'disabled'}")
    logger.info(f"Timeouts: DATA={DATA_TIMEOUT}s UPSTREAM={UPSTREAM_TIMEOUT}s SL_API={SL_API_TIMEOUT}s")

    # Graceful shutdown
    shutdown_event = threading.Event()

    def handle_signal(signum, _):
        logger.info(f"Received signal {signum}, shutting down...")
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    shutdown_event.wait()
    controller.stop()
    logger.info("Shutdown complete")
    sys.exit(0)


if __name__ == "__main__":
    main()
