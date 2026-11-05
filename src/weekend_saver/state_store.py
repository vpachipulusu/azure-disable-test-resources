"""Remember which resources weekend-saver disabled, so enable restores only those.

File format:
    {"version": 1,
     "disabled": {"<lower-cased ARM id>": "<ISO-8601 UTC timestamp>"},
     "last_disable_run": "<ISO-8601 UTC timestamp>" | null}
"""

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from weekend_saver.errors import StateStoreError

_FORMAT_VERSION = 1


@dataclass(frozen=True)
class Snapshot:
    """Resources currently switched off by weekend-saver, keyed by lower-cased ARM ID."""

    disabled: Mapping[str, str] = field(default_factory=dict)
    # Stamped by every disable run, even one that found nothing to stop, so enable can
    # tell a snapshot from this weekend apart from one left over from an earlier week.
    last_disable_run: str | None = None


class StateStore(Protocol):
    def load(self) -> Snapshot | None:
        """Return the snapshot, or None if none has ever been written."""
        ...

    def save(self, snapshot: Snapshot) -> None: ...


class JsonFileStateStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> Snapshot | None:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StateStoreError(f"Could not read state file {self._path}: {exc}") from exc

        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StateStoreError(f"State file {self._path} is not valid JSON: {exc}") from exc
        return self._validated(document)

    def save(self, snapshot: Snapshot) -> None:
        payload = json.dumps(
            {
                "version": _FORMAT_VERSION,
                "disabled": dict(snapshot.disabled),
                "last_disable_run": snapshot.last_disable_run,
            },
            indent=2,
        )
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._replace_atomically(payload)
        except OSError as exc:
            raise StateStoreError(f"Could not write state file {self._path}: {exc}") from exc

    def _replace_atomically(self, payload: str) -> None:
        # A crash mid-write must not leave a truncated snapshot behind. mkstemp gives an
        # exclusive, owner-only file, so a pre-planted temp path cannot redirect the write.
        descriptor, temporary = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(temporary, self._path)
        except OSError:
            Path(temporary).unlink(missing_ok=True)
            raise

    def _validated(self, document: object) -> Snapshot:
        if not isinstance(document, dict) or document.get("version") != _FORMAT_VERSION:
            raise StateStoreError(f"State file {self._path} has an unsupported format.")
        disabled = document.get("disabled")
        if not isinstance(disabled, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in disabled.items()
        ):
            raise StateStoreError(f"State file {self._path} has a malformed 'disabled' section.")
        last_disable_run = document.get("last_disable_run")
        if last_disable_run is not None and not isinstance(last_disable_run, str):
            raise StateStoreError(f"State file {self._path} has a malformed 'last_disable_run'.")
        return Snapshot(disabled, last_disable_run)
