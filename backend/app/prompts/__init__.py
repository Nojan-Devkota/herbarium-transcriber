"""Versioned prompt files.

Prompts live as `v<N>_<name>.md` files, not strings in code. `load_prompt(name)`
returns the highest version; its version tag ("transcribe@v1") is part of the
cache key, so editing a prompt and bumping the file version invalidates exactly
that stage's cached outputs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from string import Template

PROMPT_DIR = Path(__file__).parent
_FILE_RE = re.compile(r"^v(\d+)_(.+)\.md$")


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    template: str

    @property
    def tag(self) -> str:
        return f"{self.name}@v{self.version}"

    def render(self, **values: object) -> str:
        return Template(self.template).substitute({k: str(v) for k, v in values.items()})


@lru_cache
def load_prompt(name: str) -> Prompt:
    best: tuple[int, Path] | None = None
    for path in PROMPT_DIR.glob(f"v*_{name}.md"):
        m = _FILE_RE.match(path.name)
        if m and m.group(2) == name and (best is None or int(m.group(1)) > best[0]):
            best = (int(m.group(1)), path)
    if best is None:
        raise FileNotFoundError(f"no prompt file v*_{name}.md in {PROMPT_DIR}")
    return Prompt(name=name, version=best[0], template=best[1].read_text("utf-8").strip())
