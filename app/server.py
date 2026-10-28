"""Glance loopback-only standard-library server. Run: python server.py."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
from urllib.parse import unquote, urlsplit

from workroom import DomainError, Workroom

MAX_BODY = 1024 * 1024
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/world3d.js": ("world3d.js", "text/javascript; charset=utf-8"),
          "/glance-logo.svg": ("glance-logo.svg", "image/svg+xml"),
          "/style.css": ("style.css", "text/css; charset=utf-8"),
          "/assets/codex-reference.png": ("assets/codex-reference.png", "image/png"),
          "/assets/claude-reference.png": ("assets/claude-reference.png", "image/png"),
          "/assets/gemini-reference.jpg": ("assets/gemini-reference.jpg", "image/jpeg"),
          "/styles.css": ("styles.css", "text/css; charset=utf-8")}


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    def bad_constant(value):
        raise ValueError("Invalid JSON constant")
    return json.loads(data, object_pairs_hook=pairs, parse_constant=bad_constant)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        # Windows SO_REUSEADDR can silently share an already occupied listener.
        # Exclusive binding makes startup fail instead of serving another app.
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address, workroom, static_dir=None):
        if address[0] not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("Only loopback binding is allowed")
        self.workroom = workroom
        self.static_dir = Path(static_dir or Path(__file__).with_name("static"))
        self.session_token = secrets.token_urlsafe(32)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalWorkroom/1"
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        # Avoid logging user-controlled request paths or uploaded contents.
        pass

    def _host(self):
        host = self.headers.get("Host", "")
        try:
            parts = urlsplit("http://" + host)
            return (parts.hostname in ("127.0.0.1", "localhost", "::1") and
                    parts.port == self.server.server_port and not parts.username and not parts.password and
                    parts.path == "" and parts.query == "" and parts.fragment == "")
        except ValueError:
            return False

    def _guard(self, write=False):
        if not self._host():
            raise DomainError("INVALID_HOST", "Loopback Host with the current port is required", 403)
        origin = self.headers.get("Origin")
        if origin:
            if origin not in ("http://127.0.0.1:" + str(self.server.server_port), "http://localhost:" + str(self.server.server_port), "http://[::1]:" + str(self.server.server_port)):
                raise DomainError("FOREIGN_ORIGIN", "Cross-origin requests are disallowed", 403)
            if origin != "http://" + self.headers.get("Host"):
                raise DomainError("FOREIGN_ORIGIN", "Origin must match request Host", 403)
        if write and not secrets.compare_digest(self.headers.get("X-Workroom-Token", ""), self.server.session_token):
            raise DomainError("SESSION_TOKEN_REQUIRED", "Refresh state and provide X-Workroom-Token", 403)

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _state(self):
        return {**self.server.workroom.state(), "session_token": self.server.session_token}

    def do_GET(self):
        try:
            self._guard()
            path = unquote(urlsplit(self.path).path)
            if path == "/api/state":
                self._send(200, self._state())
            elif path == "/api/session":
                self._send(200, {"session_token": self.server.session_token})
            elif path == "/api/health":
                self._send(200, {"ok": self.server.workroom.storage_error is None,
                                 "storage_writable": self.server.workroom.storage_error is None, "external_execution_enabled": False})
            elif path in STATIC:
                name, mime = STATIC[path]
                file = self.server.static_dir / name
                if not file.is_file():
                    raise DomainError("NOT_FOUND", "Static asset unavailable", 404)
                self._send(200, file.read_bytes(), mime)
            else:
                raise DomainError("NOT_FOUND", "Route not found", 404)
        except DomainError as exc:
            self._send(exc.status, exc.body())
        except (OSError, ValueError, UnicodeError):
            self._send(500, {"error": {"code": "INTERNAL_ERROR", "message": "Local response could not be read", "details": {}}})

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        try:
            self._guard(write=True)
            if urlsplit(self.path).path != "/api/commands":
                raise DomainError("NOT_FOUND", "Route not found", 404)
            if self.headers.get_content_type() != "application/json":
                raise DomainError("CONTENT_TYPE_REQUIRED", "Use application/json", 400)
            if self.headers.get("Transfer-Encoding"):
                raise DomainError("INVALID_BODY", "Transfer encoding is unsupported", 400)
            try:
                size = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                size = -1
            if not 0 <= size <= MAX_BODY:
                raise DomainError("BODY_LIMIT", "Body must be at most 1 MiB with Content-Length", 400)
            self.connection.settimeout(10)
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise DomainError("INVALID_BODY", "Incomplete request body", 400)
            try:
                request = strict_json(raw.decode("utf-8"))
            except (ValueError, UnicodeError, RecursionError):
                raise DomainError("INVALID_JSON", "Body must contain valid unique-key UTF-8 JSON", 400)
            if not isinstance(request, dict) or set(request) != {"command", "expected_version", "payload"} or not isinstance(request["command"], str):
                raise DomainError("INVALID_REQUEST", "Expected exactly command, expected_version and payload", 400)
            result = self.server.workroom.command(request["command"], request["expected_version"], request["payload"])
            result["state"]["session_token"] = self.server.session_token
            self._send(200, result)
        except DomainError as exc:
            self._send(exc.status, exc.body())
        except (OSError, TimeoutError):
            self._send(400, {"error": {"code": "BODY_READ_FAILED", "message": "Request body failed or timed out", "details": {}}})
        except (TypeError, ValueError, UnicodeError):
            self._send(400, {"error": {"code": "INVALID_FIELD", "message": "Invalid field value", "details": {}}})

    def do_OPTIONS(self):
        self._send(403, {"error": {"code": "CROSS_ORIGIN_DISABLED", "message": "Cross-origin access disabled", "details": {}}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8874)
    parser.add_argument("--data-dir", default=str(Path(__file__).with_name(".state")))
    parser.add_argument("--host", choices=["127.0.0.1", "localhost"], default="127.0.0.1")
    args = parser.parse_args()
    workroom = Workroom(args.data_dir)
    server = LocalServer((args.host, args.port), workroom)
    print(f"Glance: http://{args.host}:{server.server_port} (external execution disabled)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        workroom.close()


if __name__ == "__main__":
    main()
