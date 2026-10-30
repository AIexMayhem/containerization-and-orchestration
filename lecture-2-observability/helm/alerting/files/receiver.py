"""Internal Alertmanager webhook sink with bounded, volatile event history."""

import json
import os
import threading
from collections import deque
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


def reject_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def encode(value):
    return json.dumps(value, separators=(",", ":"), allow_nan=False).encode()


class ReceiverServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, history_limit, max_body_bytes):
        if history_limit < 1 or max_body_bytes < 1:
            raise ValueError("HISTORY_LIMIT and MAX_BODY_BYTES must be positive")
        self.history_limit = history_limit
        self.max_body_bytes = max_body_bytes
        self.events = deque(maxlen=history_limit)
        self.received_total = 0
        self.lock = threading.Lock()
        super().__init__(address, ReceiverHandler)

    def record(self, payload):
        with self.lock:
            event = {
                "id": self.received_total + 1,
                "received_at": datetime.now(UTC).isoformat(),
                "payload": payload,
            }
            serialized = encode(event)
            # Keep serialized bytes, so history memory scales with bounded bodies
            # rather than Python's per-object overhead for every payload field.
            print(encode({"event": "alert_received", **event}).decode(), flush=True)
            self.events.append(serialized)
            self.received_total += 1
            return event["id"]

    def history(self):
        with self.lock:
            prefix = encode(
                {"limit": self.history_limit, "received_total": self.received_total}
            )[:-1]
            return prefix + b',"events":[' + b",".join(self.events) + b"]}"


class ReceiverHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, format, *args):
        # Accepted notifications are the useful log records; omit probe noise.
        pass

    def reply(self, status, value):
        body = value if isinstance(value, bytes) else encode(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        self.reply(code, {"error": message or self.responses[code][0]})

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            self.reply(200, {"status": "ok"})
        elif path == "/events":
            self.reply(200, self.server.history())
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self):
        if urlsplit(self.path).path != "/alerts":
            self.reply(404, {"error": "not found"})
            return
        if self.headers.get("Transfer-Encoding"):
            self.reply(400, {"error": "transfer encoding is not supported"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            self.reply(411, {"error": "Content-Length is required"})
            return
        try:
            if len(lengths) != 1:
                raise ValueError("multiple Content-Length headers")
            length = int(lengths[0])
            if length < 0:
                raise ValueError("negative Content-Length")
        except ValueError:
            self.reply(400, {"error": "invalid Content-Length"})
            return
        if length > self.server.max_body_bytes:
            self.reply(413, {"error": "request body exceeds MAX_BODY_BYTES"})
            return
        try:
            raw = self.rfile.read(length)
        except TimeoutError:
            self.reply(408, {"error": "request body timeout"})
            return
        try:
            if len(raw) != length:
                raise ValueError("incomplete body")
            payload = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
            if not isinstance(payload, dict):
                raise ValueError("expected a JSON object")
            event_id = self.server.record(payload)
        except (ValueError, UnicodeError, RecursionError):
            self.reply(400, {"error": "body must be a valid UTF-8 JSON object"})
            return
        self.reply(200, {"accepted": True, "id": event_id})


def main():
    address = (os.getenv("HOST", "0.0.0.0"), int(os.getenv("PORT", "8080")))
    history_limit = int(os.getenv("HISTORY_LIMIT", "100"))
    max_body_bytes = int(os.getenv("MAX_BODY_BYTES", "65536"))
    with ReceiverServer(address, history_limit, max_body_bytes) as server:
        print(
            encode(
                {
                    "event": "receiver_started",
                    "port": address[1],
                    "history_limit": history_limit,
                    "max_body_bytes": max_body_bytes,
                }
            ).decode(),
            flush=True,
        )
        server.serve_forever()


if __name__ == "__main__":
    main()
