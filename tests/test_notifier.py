import json

import pytest

from weekend_saver import notifier as notifier_module
from weekend_saver.errors import ConfigError
from weekend_saver.models import Action, RunReport, RunStatus
from weekend_saver.notifier import WebhookNotifier

REPORT = RunReport(Action.DISABLE, RunStatus.COMPLETED)


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self) -> bytes:
        return b""


@pytest.mark.parametrize("url", ["http://example.com/hook", "file:///etc/passwd", "example.com"])
def test_rejects_urls_that_are_not_https(url):
    with pytest.raises(ConfigError, match="https"):
        WebhookNotifier(url)


async def test_posts_the_report_text_as_json(monkeypatch):
    sent = []

    def fake_urlopen(request, timeout):
        sent.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr(notifier_module.urllib.request, "urlopen", fake_urlopen)

    await WebhookNotifier("https://example.com/hook", timeout_seconds=3).notify(REPORT)

    ((request, timeout),) = sent
    assert request.full_url == "https://example.com/hook"
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert "disable" in json.loads(request.data)["text"]
    assert timeout == 3
