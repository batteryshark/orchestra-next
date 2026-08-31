"""Stdlib HTTP transport for /api/v3."""
from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from orchestra import api, auth, config, db


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "OrchestraNext/3"

    def log_message(self, format, *args):
        return

    def _send(self):
        parsed = urllib.parse.urlsplit(self.path)
        query = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()}
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            body = json.loads(self.rfile.read(length)) if length else None
        except json.JSONDecodeError:
            return self._json(400, {"error": {"message": "invalid JSON"}})
        raw = self.headers.get("Authorization", "")
        token = raw[7:] if raw.startswith("Bearer ") else None
        con = db.connect()
        try:
            identity = auth.identify(con, token)
            result = api.API(con).handle(self.command, parsed.path, query, body, identity)
            if isinstance(result, api.FileResponse):
                data = result.path.read_bytes()
                self.send_response(result.status)
                self.send_header("Content-Type", result.media_type)
                self.send_header("Content-Length", str(len(data)))
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
