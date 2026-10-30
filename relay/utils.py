import logging
import threading
import time

import requests

logger = logging.getLogger("smtp-relay.utils")

# A sender that isn't one of your aliases is remembered for this long, so repeated
# mail from it doesn't hit the SimpleLogin API every time.
NEGATIVE_CACHE_SECONDS = 300
# Upper bound on alias-search pages, so a bad lookup can't loop for long.
MAX_ALIAS_PAGES = 20


class TemporaryError(Exception):
    """Might succeed later (timeouts, SimpleLogin or network down). -> SMTP 451"""


class PermanentError(Exception):
    """Will never succeed as-is (unknown alias, invalid contact). -> SMTP 550"""


class AliasNotFound(PermanentError):
    pass


class SimpleLoginClient:
    def __init__(self, api_url: str, api_key: str, timeout: float = 10):
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout  # passed on every request; requests ignores session.timeout
        self.session = requests.Session()
        self.session.headers.update({"Authentication": api_key})
        self._alias_cache: dict[str, int] = {}
        self._missing_cache: dict[str, float] = {}  # alias -> time it was found missing
        self._lock = threading.Lock()  # lookups now run in worker threads

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """One API call with a real timeout; errors classified as temporary or permanent."""
        url = f"{self.api_url}{path}"
        try:
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except (requests.Timeout, requests.ConnectionError) as e:
            raise TemporaryError(f"SimpleLogin API unreachable: {e}") from e
        except requests.RequestException as e:
            raise TemporaryError(f"SimpleLogin API request failed: {e}") from e

        status = resp.status_code
        if status >= 500 or status == 429:
            raise TemporaryError(f"SimpleLogin API {method} {path} returned {status}")
        if status in (401, 403):
            # bad or revoked API key: a config problem, keep the mail and retry
            raise TemporaryError(f"SimpleLogin API rejected the API key ({status})")
        if status >= 400:
            raise PermanentError(f"SimpleLogin API {method} {path} returned {status}: {resp.text[:200]}")
        return resp

    def _get_alias_id(self, alias_email: str) -> int:
        with self._lock:
            if alias_email in self._alias_cache:
                return self._alias_cache[alias_email]
            missing_since = self._missing_cache.get(alias_email)
            if missing_since and time.monotonic() - missing_since < NEGATIVE_CACHE_SECONDS:
                raise AliasNotFound(f"Alias not found: {alias_email}")

        for page_id in range(MAX_ALIAS_PAGES):
            resp = self._request(
                "GET", "/api/v2/aliases",
                params={"page_id": page_id, "query": alias_email},
            )
            aliases = resp.json().get("aliases", [])
            if not aliases:
                break

            for alias in aliases:
                if alias["email"] == alias_email:
                    alias_id = alias["id"]
                    with self._lock:
                        self._alias_cache[alias_email] = alias_id
                        self._missing_cache.pop(alias_email, None)
                    logger.debug(f"Resolved {alias_email} -> id={alias_id}")
                    return alias_id
        else:
            logger.warning(f"Alias search for {alias_email} stopped after {MAX_ALIAS_PAGES} pages")

        with self._lock:
            self._missing_cache[alias_email] = time.monotonic()
        raise AliasNotFound(f"Alias not found: {alias_email}")

    def get_reverse_alias(self, sender: str, recipient: str) -> str:
        alias_id = self._get_alias_id(sender)

        resp = self._request(
            "POST", f"/api/aliases/{alias_id}/contacts",
            json={"contact": recipient},
        )

        data = resp.json()
        reverse_alias = data.get("reverse_alias")
        if not reverse_alias:
            raise PermanentError(f"No reverse_alias in response for {recipient}")

        return reverse_alias
