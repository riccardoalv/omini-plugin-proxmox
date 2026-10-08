import json
import threading
from functools import partial
from pathlib import Path

import httpx
import pytest

import omini_proxmox.collect as collect_module
from omini_proxmox.client import Client

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "PVEAPIToken=omini@pve!omini=6f6a4f3c-1b2d-4e5f-8a9b-0c1d2e3f4a5b"


class FakeProxmox:
    """Answers like a Proxmox VE 8.2 node, from the JSON files in fixtures/
    (`nodes__pve1__status.json` is GET /api2/json/nodes/pve1/status)."""

    def __init__(self):
        self.routes = {}
        self.errors = {}  # path prefix → HTTP status
        self.calls = []
        self.methods = set()
        self.token = TOKEN
        self.lock = threading.Lock()
        for f in FIXTURES.glob("*.json"):
            self.routes["/" + f.stem.replace("__", "/")] = json.loads(f.read_text())
        # The token has PVEAuditor: no Sys.Modify, so the update list is refused.
        self.errors["/nodes/pve1/apt/"] = 403

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api2/json")
        with self.lock:
            self.calls.append(path)
            self.methods.add(request.method)
        if request.headers.get("authorization") != self.token:
            return httpx.Response(401, text="authentication failure")
        for prefix, status in self.errors.items():
            if path.startswith(prefix):
                return httpx.Response(status, json={"data": None})
        if path not in self.routes:
            return httpx.Response(501, json={"data": None})
        return httpx.Response(200, json=self.routes[path])


@pytest.fixture
def pve(monkeypatch):
    fake = FakeProxmox()
    monkeypatch.setattr(
        collect_module, "Client", partial(Client, transport=httpx.MockTransport(fake.handler))
    )
    return fake


@pytest.fixture
def cfg():
    from omini_sdk import Config

    return Config(
        {
            "url": "https://192.168.1.10:8006",
            "token_id": "omini@pve!omini",
            "token_secret": "6f6a4f3c-1b2d-4e5f-8a9b-0c1d2e3f4a5b",
        }
    )
