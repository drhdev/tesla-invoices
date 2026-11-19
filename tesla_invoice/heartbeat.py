import json
import logging
import os
import re
from contextlib import closing
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from threading import Event, Thread
from time import monotonic
from urllib.parse import parse_qsl, urlencode, urlsplit

LOG = logging.getLogger(__name__)
UNHEALTHY_FILE = "worker-unhealthy"


class Heartbeat:
    def __init__(self, root):
        self.root = root
        self.stop = Event()
        self.thread = None
        self.last_tick = monotonic()
        url = os.getenv("KUMA_PUSH_URL", "").strip()
        if not url:
            return
        self.endpoint = urlsplit(url)
        if (
            self.endpoint.scheme not in ("http", "https")
            or not self.endpoint.hostname
            or self.endpoint.username is not None
            or self.endpoint.fragment
            or not re.search(r"/api/push/[A-Za-z0-9_-]+$", self.endpoint.path)
        ):
            raise ValueError("KUMA_PUSH_URL must be an Uptime Kuma Push monitor URL")
        self.port = self.endpoint.port
        self.interval = int(os.getenv("KUMA_PUSH_INTERVAL_SECONDS", "3600"))
        self.stale = int(os.getenv("KUMA_STALE_SECONDS", "3600"))
        if self.interval < 10 or self.stale <= 15:
            raise ValueError("Kuma interval must be at least 10 seconds and the stale threshold must exceed 15 seconds")
        self.thread = Thread(target=self.run, name="kuma-heartbeat", daemon=True)

    def __enter__(self):
        if self.thread:
            self.thread.start()
            LOG.info("Uptime Kuma heartbeat enabled")
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.thread:
            self.stop.set()
            self.thread.join(timeout=2)

    def tick(self):
        self.last_tick = monotonic()

    def status(self):
        if monotonic() - self.last_tick >= self.stale:
            return "down", "Invoice scheduler has stopped progressing"
        if not os.access(self.root, os.W_OK):
            return "down", "Invoice data directory is not writable"
        if (self.root / UNHEALTHY_FILE).exists():
            return "down", "Latest scheduled invoice check is incomplete"
        return "up", "Invoice scheduler is running"

    def push(self):
        status, message = self.status()
        query = [
            (key, value) for key, value in parse_qsl(self.endpoint.query, keep_blank_values=True)
            if key not in ("status", "msg", "ping")
        ]
        query.extend((("status", status), ("msg", message)))
        target = f"{self.endpoint.path}?{urlencode(query)}"
        connect = HTTPSConnection if self.endpoint.scheme == "https" else HTTPConnection
        with closing(connect(self.endpoint.hostname, port=self.port, timeout=5)) as connection:
            connection.request("GET", target)
            with connection.getresponse() as response:
                if response.status != 200:
                    LOG.warning("Uptime Kuma push rejected (HTTP %d)", response.status)
                    return
                reply = json.loads(response.read(4096))
                if not isinstance(reply, dict) or reply.get("ok") is not True:
                    LOG.warning("Uptime Kuma did not acknowledge the push")

    def run(self):
        while not self.stop.is_set():
            started = monotonic()
            try:
                self.push()
            except (OSError, HTTPException, ValueError) as exc:
                # The push token is part of the URL. Log the exception type only.
                LOG.warning("Uptime Kuma push failed (%s)", type(exc).__name__)
            self.stop.wait(max(0, self.interval - (monotonic() - started)))
