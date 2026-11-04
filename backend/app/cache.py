"""Content-hash cache for model calls.

Key = sha256(image) + stage + prompt version (+ any extra inputs such as a crop
box or a re-read question). Re-runs during development cost nothing and the demo
replays instantly. Changing a prompt bumps its version, which invalidates only
that stage's entries.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def cache_key(image_sha256: str, stage: str, prompt_version: str, *extra: Any) -> str:
    parts = [image_sha256, stage, prompt_version, *(json.dumps(e, sort_keys=True, default=str) for e in extra)]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


class Cache:
    def __init__(self, root: Path, enabled: bool = True):
        self.root = Path(root)
        self.enabled = enabled

    def _path(self, stage: str, key: str) -> Path:
        return self.root / stage / key[:2] / f"{key}.json"

    def get(self, stage: str, key: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        path = self._path(stage, key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("cache: discarding unreadable entry %s (%s)", path, exc)
            path.unlink(missing_ok=True)
            return None

    def set(self, stage: str, key: str, value: dict[str, Any]) -> None:
        if not self.enabled:
            return
        path = self._path(stage, key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write so a crash never leaves a half-written entry
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(value, f, ensure_ascii=False)
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as exc:
            # The model call already succeeded (and was paid for): don't fail the sheet over it
            log.warning("cache: could not write %s (%s)", path, exc)
