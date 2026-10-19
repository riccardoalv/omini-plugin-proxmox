"""Proxmox VE REST API client, authenticated with an API token, on the SDK's
HTTP client: timeouts, retries, and errors that say what went wrong.

https://pve.proxmox.com/wiki/Proxmox_VE_API
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx
from omini_sdk import PluginError
from omini_sdk.http import Client as HttpClient
from omini_sdk.http import Forbidden, HttpError, NotFound, Unavailable, normalize_url

__all__ = ["DEFAULT_PORT", "Client", "Forbidden", "NotFound", "Unavailable", "base_url", "segment"]

DEFAULT_PORT = 8006
API = "/api2/json"


def base_url(url: str) -> str:
    """'192.168.1.10' → 'https://192.168.1.10:8006'. An explicit port is kept
    (e.g. :443 behind a reverse proxy)."""
    return normalize_url(url, port=DEFAULT_PORT, strip=(API,))


def segment(value: Any) -> str:
    """A node name or VM id as one URL path segment."""
    return quote(str(value), safe="")


class Client(HttpClient):
    def __init__(
        self,
        url: str,
        token_id: str,
        token_secret: str,
        verify_tls: bool = False,
        timeout: float = 30,
        transport: httpx.BaseTransport | None = None,
    ):
        super().__init__(
            url,
            name="Proxmox VE",
            api=API,
            port=DEFAULT_PORT,
            strip=(API,),
            verify_tls=verify_tls,
            timeout=timeout,
            user_agent="omini-plugin-proxmox",
            headers={"Authorization": f"PVEAPIToken={token_id}={token_secret}"},
            unauthorized=(
                "Proxmox VE rejected the API token: check the token ID "
                "(user@realm!name) and its secret"
            ),
            transport=transport,
        )

    def check(self, r: httpx.Response, method: str, path: str) -> None:
        """501 is a path this version lacks; another error on a read means
        the API cannot answer it now (a guest agent that is not running, an
        offline cluster node: 595); a refused change gives Proxmox VE's
        reasons."""
        status = r.status_code
        if status < 400 or status in (401, 403, 404, 410):
            super().check(r, method, path)
            return
        where = {"status": status, "method": method, "resource": path}
        if status == 501:
            raise NotFound(f"Proxmox VE has no {path}", **where)
        if method == "GET":
            raise Unavailable(f"{path}: HTTP {status} {r.reason_phrase}".strip(), **where)
        raise HttpError(f"Proxmox VE refused the change: {self.detail(r)}", **where)

    def detail(self, r: httpx.Response) -> str:
        """Proxmox VE's reasons for a refused change (its `errors` per
        parameter), else its status."""
        try:
            errors = r.json().get("errors") or {}
        except (json.JSONDecodeError, AttributeError):
            errors = {}
        if isinstance(errors, dict) and errors:
            return "; ".join(f"{k}: {v}" for k, v in errors.items())
        return r.reason_phrase or f"HTTP {r.status_code}"

    def put(self, path: str, data: dict[str, Any]) -> Any:  # type: ignore[override]
        """PUTs a change (only apply calls it: a VM's network device). Whether
        it took effect is checked by reading again, never assumed."""
        body = self.request("PUT", path, data=data)
        return body.get("data") if isinstance(body, dict) else None

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:  # type: ignore[override]
        """GETs an API path and returns its `data`."""
        body = self.request("GET", path, params=params)
        if not isinstance(body, dict) or "data" not in body:
            raise PluginError(f"{self.base} does not look like Proxmox VE (no API data)")
        return body["data"]
