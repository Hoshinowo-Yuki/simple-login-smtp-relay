import logging
import requests

logger = logging.getLogger("smtp-relay.utils")


class SimpleLoginClient:
    def __init__(self, api_url: str, api_key: str):
        self.api_url = api_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Authentication": api_key})
        self.session.timeout = 10
        self._alias_cache: dict[str, int] = {}

    def _get_alias_id(self, alias_email: str) -> int:
        if alias_email in self._alias_cache:
            return self._alias_cache[alias_email]

        page_id = 0
        while True:
            resp = self.session.get(
                f"{self.api_url}/api/v2/aliases",
                params={"page_id": page_id, "query": alias_email},
            )
            resp.raise_for_status()
            aliases = resp.json().get("aliases", [])

            if not aliases:
                break

            for alias in aliases:
                if alias["email"] == alias_email:
                    alias_id = alias["id"]
                    self._alias_cache[alias_email] = alias_id
                    logger.debug(f"Resolved {alias_email} -> id={alias_id}")
                    return alias_id

            page_id += 1

        raise ValueError(f"Alias not found: {alias_email}")

    def get_reverse_alias(self, sender: str, recipient: str) -> str:
        alias_id = self._get_alias_id(sender)

        resp = self.session.post(
            f"{self.api_url}/api/aliases/{alias_id}/contacts",
            json={"contact": recipient},
        )
        resp.raise_for_status()

        data = resp.json()
        reverse_alias = data.get("reverse_alias")
        if not reverse_alias:
            raise ValueError(f"No reverse_alias in response for {recipient}")

        return reverse_alias