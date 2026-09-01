"""Stdlib HTTP transport and static console serving."""
from __future__ import annotations

import http.cookies
import ipaddress
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from orchestra import api, auth, config, db

UI_DIR = Path(__file__).with_name("ui")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
CSP = ("default-src 'self'; img-src 'self' data:; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'")
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
TAILNET_NETWORKS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
LOOPBACK_NETWORKS = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, settings=None):
        settings = settings or config.read()
        self.trusted_networks = tuple(ipaddress.ip_network(item) for item in settings["trusted_cidrs"])
        if settings["trust_tailnet"]:
            self.trusted_networks += TAILNET_NETWORKS
        if settings["trust_loopback"]:
            self.trusted_networks += LOOPBACK_NETWORKS
        self.extra_hosts = frozenset(item.strip().lower() for item in settings["allowed_hosts"])
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "OrchestraNext"

    def log_message(self, format, *args):
        return

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        super().end_headers()

    def _host_allowed(self) -> bool:
        raw = self.headers.get("Host", "")
        try:
            host = urllib.parse.urlsplit(f"//{raw}").hostname or ""
        except ValueError:
            return False
        if host in ALLOWED_HOSTS or host == self.server.server_address[0]:
            return True
        if host in self.server.extra_hosts:
            return True
        if self.server.trusted_networks:
            if host.endswith(".ts.net"):
                return True
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                return False
            return any(address in network for network in self.server.trusted_networks)
        return False

    def _peer_trusted(self) -> bool:
        if not self.server.trusted_networks:
            return False
        try:
            address = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        if address.version == 6 and address.ipv4_mapped:
            address = address.ipv4_mapped
        return any(address in network for network in self.server.trusted_networks)

    def _static(self, path: str):
        name, media = STATIC[path]
        try:
            data = (UI_DIR / name).read_bytes()
        except OSError:
            return self._json(404, {"error": {"message": "not found"}})
        self.send_response(200)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _credentials(self) -> tuple[str | None, bool]:
        raw = self.headers.get("Authorization", "")
        if raw.startswith("Bearer "):
            return raw[7:], False
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return None, False
        morsel = jar.get(auth.COOKIE_NAME)
        return (morsel.value, True) if morsel else (None, False)

    def _send(self):
        if not self._host_allowed():
            return self._json(400, {"error": {"message": "invalid Host header"}})
        parsed = urllib.parse.urlsplit(self.path)
        if self.command == "GET" and parsed.path in STATIC:
            return self._static(parsed.path)
        query = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()}
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            body = json.loads(self.rfile.read(length)) if length else None
        except json.JSONDecodeError:
            return self._json(400, {"error": {"message": "invalid JSON"}})
        token, from_cookie = self._credentials()
        peer_trusted = self._peer_trusted()
        if self.command != "GET":
            origin = self.headers.get("Origin", "")
            same_origin = origin == "http://" + self.headers.get("Host", "")
            if from_cookie and not same_origin:
                return self._json(403, {"error": {"message": "cookie-authenticated mutations require a same-origin browser request"}})
            if peer_trusted and origin and not same_origin:
                return self._json(403, {"error": {"message": "cross-origin browser mutations are refused for trusted-network callers"}})
        con = db.connect()
        try:
            identity = auth.identify(con, token)
            if identity is None and peer_trusted:
                identity = auth.network_identity(self.client_address[0])
            result = api.API(con).handle(self.command, parsed.path, query, body, identity)
            if isinstance(result, api.FileResponse):
                data = result.path.read_bytes()
                self.send_response(result.status)
                self.send_header("Content-Type", result.media_type)
                self.send_header("Content-Length", str(len(data)))
                for key, item in (result.headers or {}).items():
                    self.send_header(key, item)
                self.end_headers(); self.wfile.write(data); return
            self._json(result.status, result.data, result.headers)
        except api.Problem as exc:
            self._json(exc.status, {"error": {"message": str(exc)}})
        except (ValueError, LookupError, auth.AuthError) as exc:
            self._json(400, {"error": {"message": str(exc)}})
        except Exception as exc:
            self._json(500, {"error": {"message": str(exc)}})
        finally:
            con.close()

    def _json(self, status, value, headers=None):
        raw = json.dumps(value, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, item in (headers or {}).items():
            self.send_header(key, item)
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_PATCH = do_DELETE = _send


def make_server(*, addr=None, port=None):
    value = config.read()
    return Server((addr or value["bind"], value["port"] if port is None else port))


def serve(stop: threading.Event | None = None, *, addr=None, port=None):
    server = make_server(addr=addr, port=port)
    if stop is None:
        server.serve_forever()
        return
    server.timeout = 0.5
    while not stop.is_set():
        server.handle_request()
    server.server_close()
