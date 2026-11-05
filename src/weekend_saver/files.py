"""Small filesystem helpers shared by everything that persists local state."""

import os
import tempfile
from pathlib import Path

APP_FOLDER = "weekend-saver"


def default_data_dir() -> Path:
    """The per-user folder for state, schedule, history and the sign-in record.

    Not the project folder: the schedule and resource list drive runs nobody approves,
    and a shared project folder is often writable by other accounts on the PC.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    else:
        base = os.environ.get("XDG_STATE_HOME")
        root = Path(base) if base else Path.home() / ".local" / "state"
    return root / APP_FOLDER


def ensure_private_dir(path: Path) -> Path:
    """Create `path` for this user only. On Windows the profile folder's ACL does that job."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def write_text_atomically(path: Path, text: str) -> None:
    """Replace `path` with `text` so a crash mid-write never leaves a truncated file.

    The temporary file comes from mkstemp, which creates it exclusively, so a pre-planted
    temp path cannot redirect the write. Raises OSError on failure.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except OSError:
        Path(temporary).unlink(missing_ok=True)
        raise
