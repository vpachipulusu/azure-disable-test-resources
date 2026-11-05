"""Keep a record of finished runs so the dashboard can show what happened unattended.

One JSON object per line in runs.jsonl:
    {"id", "action", "trigger", "dry_run", "started_at", "finished_at",
     "status", "failed", "error", "results": [{resource_id, name, outcome, detail, state}]}
"""

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 20


class RunHistory:
    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, record: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def recent(self, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
        """Return the newest runs first. Unreadable lines are skipped, not fatal."""
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        records = []
        for line in reversed(lines):
            if len(records) == limit:
                break
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Skipping an unreadable line in %s", self._path)
                continue
            if isinstance(record, dict):
                records.append(record)
        return records
