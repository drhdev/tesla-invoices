import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from tesla_invoice.heartbeat import UNHEALTHY_FILE, Heartbeat


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {}, clear=True))

    def heartbeat(self):
        with patch.dict(os.environ, {
            "KUMA_PUSH_URL": "https://kuma.example.com/api/push/test-secret?status=up&msg=OK&ping=",
        }):
            return Heartbeat(self.root)

    def test_monitoring_is_optional(self):
        with Heartbeat(self.root) as heartbeat:
            self.assertIsNone(heartbeat.thread)

    def test_default_interval_is_one_hour(self):
        heartbeat = self.heartbeat()
        self.assertEqual(heartbeat.interval, 3600)

    def test_reports_idle_failure_and_stall(self):
        heartbeat = self.heartbeat()
        self.assertEqual(heartbeat.status()[0], "up")
        (self.root / UNHEALTHY_FILE).touch()
        self.assertEqual(heartbeat.status(), ("down", "Latest scheduled invoice check is incomplete"))
        (self.root / UNHEALTHY_FILE).unlink()
        with patch("tesla_invoice.heartbeat.monotonic", return_value=heartbeat.last_tick + heartbeat.stale + 1):
            self.assertEqual(heartbeat.status(), ("down", "Invoice scheduler has stopped progressing"))

    def test_push_replaces_example_status(self):
        heartbeat = self.heartbeat()
        (self.root / UNHEALTHY_FILE).touch()
        with patch("tesla_invoice.heartbeat.HTTPSConnection") as client:
            connection = client.return_value
            response = connection.getresponse.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b'{"ok":true}'
            heartbeat.push()
        client.assert_called_once_with("kuma.example.com", port=None, timeout=5)
        method, target = connection.request.call_args.args
        parts = urlsplit(target)
        self.assertEqual(method, "GET")
        self.assertEqual(parts.path, "/api/push/test-secret")
        self.assertEqual(parse_qs(parts.query), {
            "status": ["down"], "msg": ["Latest scheduled invoice check is incomplete"],
        })
