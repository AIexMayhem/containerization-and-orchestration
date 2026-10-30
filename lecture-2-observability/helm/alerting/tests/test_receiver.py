"""Exercise the receiver over HTTP without Kubernetes or extra dependencies."""

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

RECEIVER = Path(__file__).resolve().parents[1] / "files" / "receiver.py"


class ReceiverHTTPTests(unittest.TestCase):
    def setUp(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.output = tempfile.TemporaryFile(mode="w+b")
        self.addCleanup(self.output.close)
        env = os.environ | {
            "HOST": "127.0.0.1",
            "PORT": str(self.port),
            "HISTORY_LIMIT": "3",
            "MAX_BODY_BYTES": "1024",
        }
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(RECEIVER)],
            env=env,
            stdout=self.output,
            stderr=subprocess.STDOUT,
        )
        self.addCleanup(self.stop_receiver)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.output.seek(0)
                self.fail(f"Receiver did not start: {self.output.read().decode()}")
            try:
                if self.request("GET", "/health")[0] == 200:
                    return
            except OSError:
                time.sleep(0.02)
        self.fail("Receiver did not become healthy")

    def stop_receiver(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def post(self, payload):
        return self.request(
            "POST",
            "/alerts",
            json.dumps(payload).encode(),
            {"Content-Type": "application/json"},
        )

    def raw_post(self, headers):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.putrequest("POST", "/alerts")
            for name, value in headers.items():
                connection.putheader(name, value)
            connection.endheaders()
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_valid_notification_is_preserved_and_logged(self):
        payload = {
            "version": "4",
            "receiver": "lab2-webhook",
            "status": "firing",
            "groupLabels": {"alertname": "Lab2ApiUnavailable"},
            "alerts": [{"status": "firing", "labels": {"job": "lab2-api"}}],
        }
        status, accepted = self.post(payload)
        self.assertEqual(status, 200)
        self.assertEqual(accepted, {"accepted": True, "id": 1})
        status, history = self.request("GET", "/events")
        self.assertEqual(status, 200)
        self.assertEqual(history["received_total"], 1)
        self.assertEqual(history["limit"], 3)
        self.assertEqual(history["events"][0]["payload"], payload)
        self.assertEqual(history["events"][0]["id"], 1)
        self.assertTrue(history["events"][0]["received_at"].endswith("+00:00"))
        self.output.seek(0)
        records = [json.loads(line) for line in self.output.read().splitlines()]
        received = [record for record in records if record["event"] == "alert_received"]
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["payload"], payload)

    def test_invalid_json_never_enters_history(self):
        invalid_bodies = (b"{", b"\xff", b"[]", b"null", b'{"value": NaN}')
        for body in invalid_bodies:
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/alerts", body)[0], 400)
        _, history = self.request("GET", "/events")
        self.assertEqual(history["events"], [])
        self.assertEqual(history["received_total"], 0)

    def test_oversized_body_is_rejected_before_storage(self):
        status, _ = self.post({"padding": "x" * 1024})
        self.assertEqual(status, 413)
        self.assertEqual(self.request("GET", "/events")[1]["received_total"], 0)

    def test_history_evicts_oldest_notifications(self):
        for index in range(5):
            self.assertEqual(self.post({"sequence": index})[0], 200)
        _, history = self.request("GET", "/events")
        self.assertEqual(history["received_total"], 5)
        self.assertEqual([event["id"] for event in history["events"]], [3, 4, 5])
        self.assertEqual(
            [event["payload"]["sequence"] for event in history["events"]], [2, 3, 4]
        )

    def test_health_and_unknown_routes_do_not_create_events(self):
        self.assertEqual(self.request("GET", "/health"), (200, {"status": "ok"}))
        self.assertEqual(self.request("GET", "/missing")[0], 404)
        self.assertEqual(self.request("POST", "/missing", b"{}")[0], 404)
        self.assertEqual(self.request("GET", "/events")[1]["received_total"], 0)

    def test_missing_length_is_rejected(self):
        self.assertEqual(self.raw_post({})[0], 411)

    def test_invalid_content_length_is_rejected(self):
        for length in ("-1", "invalid"):
            with self.subTest(length=length):
                self.assertEqual(self.raw_post({"Content-Length": length})[0], 400)

    def test_chunked_body_is_rejected(self):
        self.assertEqual(self.raw_post({"Transfer-Encoding": "chunked"})[0], 400)


if __name__ == "__main__":
    unittest.main()
