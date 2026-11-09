"""Local fake servers for adapter tests: a Home Assistant REST fake and a TeslaMateApi fake.

They serve the sanitized live fixtures and record every request so tests can assert exactly what
the adapters sent (e.g. that TeslaMate is only ever read with GET).
"""

from __future__ import annotations

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "live-sanitized"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


class _Server:
    def __init__(self, handler_factory):
        self.requests: List[Tuple[str, str, Optional[Dict[str, Any]], Dict[str, str]]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a, **k):  # silence
                pass

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                if not n:
                    return None
                return json.loads(self.rfile.read(n).decode())

            def _reply(self, code: int, payload: Any):
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                outer.requests.append(("GET", self.path, None, dict(self.headers)))
                handler_factory.handle(self, "GET", self.path, None)

            def do_POST(self):
                body = self._body()
                outer.requests.append(("POST", self.path, body, dict(self.headers)))
                handler_factory.handle(self, "POST", self.path, body)

            def do_PUT(self):
                outer.requests.append(("PUT", self.path, self._body(), dict(self.headers)))
                handler_factory.handle(self, "PUT", self.path, None)

            def do_DELETE(self):
                outer.requests.append(("DELETE", self.path, None, dict(self.headers)))
                handler_factory.handle(self, "DELETE", self.path, None)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeHA:
    """Home Assistant REST fake serving the tesla_fleet fixture, applying simple service effects."""

    TOKEN = "test-token-not-real"

    def __init__(self, states: Optional[List[Dict[str, Any]]] = None, apply_effects: bool = True):
        self.states = {s["entity_id"]: copy.deepcopy(s) for s in (states or load_fixture("ha_states_tesla_fleet.json"))}
        self.apply_effects = apply_effects
        self.fail_services = False
        self.service_calls: List[Tuple[str, Dict[str, Any]]] = []
        self._server = _Server(self)
        self.url = self._server.url

    @property
    def requests(self):
        return self._server.requests

    def stop(self) -> None:
        self._server.stop()

    def set_state(self, entity_id: str, state: str, **attrs: Any) -> None:
        s = self.states[entity_id]
        s["state"] = state
        s["attributes"].update(attrs)

    # -- handler --------------------------------------------------------------------------
    def handle(self, h, method: str, path: str, body):
        if h.headers.get("Authorization") != f"Bearer {self.TOKEN}":
            return h._reply(401, {"message": "401: Unauthorized"})
        if method == "GET" and path == "/api/states":
            return h._reply(200, list(self.states.values()))
        if method == "POST" and path.startswith("/api/services/"):
            if self.fail_services:
                return h._reply(500, {"message": "boom"})
            service = path[len("/api/services/"):]
            self.service_calls.append((service, body or {}))
            if self.apply_effects:
                self._effect(service, body or {})
            return h._reply(200, [])
        return h._reply(404, {"message": "not found"})

    def _effect(self, service: str, data: Dict[str, Any]) -> None:
        ent = data.get("entity_id")
        if ent not in self.states:
            return
        st = self.states[ent]
        domain, name = service.split("/")
        if domain == "lock":
            st["state"] = "locked" if name == "lock" else "unlocked"
        elif domain == "switch":
            st["state"] = "on" if name == "turn_on" else "off"
            if ent.endswith("_charge"):
                self.states["sensor.demo_car_charging"]["state"] = "charging" if name == "turn_on" else "stopped"
        elif domain == "number" and name == "set_value":
            st["state"] = str(int(data["value"]))
        elif domain == "climate":
            if name == "set_hvac_mode":
                st["state"] = data["hvac_mode"]
            if name == "set_temperature":
                st["attributes"]["temperature"] = data["temperature"]
        elif domain == "cover":
            st["state"] = "open" if name == "open_cover" else "closed"
        elif domain == "select":
            st["state"] = data["option"]


class FakeTeslaMate:
    """TeslaMateApi fake serving the fixtures. Records methods; any non-GET is a test failure."""

    TOKEN = "tm-token-not-real"

    def __init__(self, require_token: bool = False):
        self.require_token = require_token
        self._server = _Server(self)
        self.url = self._server.url

    @property
    def requests(self):
        return self._server.requests

    def stop(self) -> None:
        self._server.stop()

    def handle(self, h, method: str, path: str, body):
        if method != "GET":
            return h._reply(405, {"error": "read-only"})
        if self.require_token and h.headers.get("Authorization") != f"Bearer {self.TOKEN}":
            return h._reply(401, {"error": "unauthorized"})
        url = urlsplit(path)
        q = parse_qs(url.query)
        p = url.path
        if p == "/api/v1/cars":
            return h._reply(200, load_fixture("teslamate_cars.json"))
        if p == "/api/v1/cars/1/status":
            return h._reply(200, load_fixture("teslamate_cars_1_status.json"))
        if p == "/api/v1/cars/1/battery-health":
            return h._reply(200, load_fixture("teslamate_cars_1_battery-health.json"))
        if p in ("/api/v1/cars/1/drives", "/api/v1/cars/1/charges"):
            key = "drives" if p.endswith("drives") else "charges"
            data = copy.deepcopy(load_fixture(f"teslamate_cars_1_{key}_show_5.json"))
            page = int(q.get("page", ["1"])[0])
            if page > 1:
                data["data"][key] = []
            return h._reply(200, data)
        return h._reply(404, {"error": "not found"})
