"""Minimal Proxmox VE REST API client, authenticated with an API token.

https://pve.proxmox.com/wiki/Proxmox_VE_API
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from omini_sdk import PluginError

DEFAULT_PORT = 8006


class Forbidden(Exception):
    """The token lacks the privilege for a path (HTTP 403)."""


class NotFound(Exception):
    """The path does not exist on this version (HTTP 404 / 501)."""


class Unavailable(Exception):
    """The API answered with an error for this path: a guest agent that is not
    running, a cluster node that is offline (595)..."""


def base_url(url: str) -> str:
    """'192.168.1.10' → 'https://192.168.1.10:8006'. An explicit port is kept
    (e.g. :443 behind a reverse proxy)."""
    url = url.strip().rstrip("/")
    if not url.startswith(("https://", "http://")):
        url = "https://" + url
    parts = urlsplit(url)
    netloc = parts.netloc
    if parts.port is None:
        netloc = f"{netloc}:{DEFAULT_PORT}"
    path = parts.path
    if path.endswith("/api2/json"):
        path = path[: -len("/api2/json")]
    return urlunsplit((parts.scheme, netloc, path, "", ""))


def segment(value: Any) -> str:
    """A node name or VM id as one URL path segment."""
    return quote(str(value), safe="")


class Client:
    def __init__(
        self,
        url: str,
        token_id: str,
        token_secret: str,
        verify_tls: bool = False,
        timeout: float = 10,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base = base_url(url)
        self.http = httpx.Client(
            base_url=self.base + "/api2/json",
            verify=verify_tls,
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": "omini-plugin-proxmox",
                "Authorization": f"PVEAPIToken={token_id}={token_secret}",
            },
            transport=transport,
        )

    def close(self) -> None:
        self.http.close()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GETs an API path and returns its `data`. Only GETs are ever sent."""
        try:
            r = self.http.get(path, params=params)
        except httpx.ConnectError as e:
            raise PluginError(f"cannot connect to {self.base}") from e
        except httpx.TimeoutException as e:
            raise PluginError(f"{self.base} did not answer in time") from e
        except httpx.HTTPError as e:
            raise PluginError(f"request to {self.base} failed ({type(e).__name__})") from e
        if r.status_code == 401:
            raise PluginError(
                "Proxmox VE rejected the API token: check the token ID "
                "(user@realm!name) and its secret"
            )
        if r.status_code == 403:
            raise Forbidden(path)
        if r.status_code in (404, 501):
            raise NotFound(path)
        if r.status_code >= 400:
            raise Unavailable(f"{path}: HTTP {r.status_code} {r.reason_phrase}".strip())
        try:
            body = r.json()
        except json.JSONDecodeError as e:
            raise PluginError(
                f"{self.base} did not answer with JSON: is it the Proxmox VE address?"
            ) from e
        if not isinstance(body, dict) or "data" not in body:
            raise PluginError(f"{self.base} does not look like Proxmox VE (no API data)")
        return body["data"]
