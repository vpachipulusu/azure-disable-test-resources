"""Send the run report somewhere a human will see it."""

import asyncio
import json
import urllib.request
from typing import Protocol
from urllib.parse import urlsplit

from weekend_saver.errors import ConfigError
from weekend_saver.models import RunReport
from weekend_saver.report import format_report

DEFAULT_TIMEOUT_SECONDS = 10.0


class Notifier(Protocol):
    async def notify(self, report: RunReport) -> None: ...


class WebhookNotifier:
    """POSTs {"text": "<report>"} to an incoming-webhook URL (Slack- and Teams-compatible)."""

    def __init__(self, url: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        if urlsplit(url).scheme != "https":
            raise ConfigError("Webhook URL must use https.")
        self._url = url
        self._timeout_seconds = timeout_seconds

    async def notify(self, report: RunReport) -> None:
        await asyncio.to_thread(self._post, format_report(report))

    def _post(self, text: str) -> None:
        request = urllib.request.Request(
            self._url,
            data=json.dumps({"text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # Scheme is restricted to https in __init__; urlopen raises on 4xx/5xx
        with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
            response.read()
