import os
import ssl
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

from utils import SimpleLoginClient

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

# Upstream SMTP config
UPSTREAM_HOST = os.getenv("UPSTREAM_HOST", "smtp.gmail.com")
UPSTREAM_PORT = int(os.getenv("UPSTREAM_PORT", "587"))
UPSTREAM_USERNAME = os.getenv("UPSTREAM_USERNAME")
UPSTREAM_PASSWORD = os.getenv("UPSTREAM_PASSWORD")
UPSTREAM_STARTTLS = os.getenv("UPSTREAM_STARTTLS", "true").lower() == "true"

# Timeout config
DATA_TIMEOUT = int(os.getenv("DATA_TIMEOUT", "30"))
UPSTREAM_TIMEOUT = int(os.getenv("UPSTREAM_TIMEOUT", "15"))

sl_client = None


def validate_config():
    required = {
        "RELAY_USERNAME": RELAY_USERNAME,
        "RELAY_PASSWORD": RELAY_PASSWORD,
        "SL_API_KEY": SL_API_KEY,
        "UPSTREAM_USERNAME": UPSTREAM_USERNAME,
        "UPSTREAM_PASSWORD": UPSTREAM_PASSWORD,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        logger.error(f"Missing required env vars: {', '.join(missing)}")
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

        username = auth_data.login.decode("utf-8")
        password = auth_data.password.decode("utf-8")

        if username == RELAY_USERNAME and password == RELAY_PASSWORD:
            logger.debug(f"Auth success for {username}")
            return AuthResult(success=True)

        logger.warning(f"Auth failed for {username}")
        return AuthResult(success=False, handled=False)


class RelayHandler:
    async def handle_DATA(self, server, session, envelope):
        try:
            return await asyncio.wait_for(
                self._process(envelope),
                timeout=DATA_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error(f"handle_DATA timed out after {DATA_TIMEOUT}s")
            return "451 Timeout processing mail"
        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)
            return "451 Internal error"

    async def _process(self, envelope):
        mail_from = envelope.mail_from
        rcpt_tos = list(envelope.rcpt_tos)
        data = envelope.content

        logger.info(f"Received mail from={mail_from} to={rcpt_tos}")

        msg = email.message_from_bytes(data)

        # Build reverse alias mapping
        alias_map = {}
        for rcpt in rcpt_tos:
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

        with smtplib.SMTP(UPSTREAM_HOST, UPSTREAM_PORT, timeout=UPSTREAM_TIMEOUT) as client:
            client.ehlo()
            if UPSTREAM_STARTTLS:
                client.starttls()
                client.ehlo()
            client.login(UPSTREAM_USERNAME, UPSTREAM_PASSWORD)
            client.sendmail(mail_from, new_rcpts, msg.as_bytes())

        logger.info("Relayed successfully")
        return "250 OK"

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
    sl_client = SimpleLoginClient(SL_API_URL, SL_API_KEY)

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
    logger.info(f"Upstream: {UPSTREAM_HOST}:{UPSTREAM_PORT} (STARTTLS={'on' if UPSTREAM_STARTTLS else 'off'})")
    logger.info(f"TLS: {'enabled' if TLS_ENABLED else 'disabled'}")
    logger.info(f"Timeouts: DATA={DATA_TIMEOUT}s UPSTREAM={UPSTREAM_TIMEOUT}s")

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
