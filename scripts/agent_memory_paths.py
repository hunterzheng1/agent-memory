from __future__ import annotations

import os
import re
from pathlib import Path


def resolve_path(raw: str) -> Path:
    """Resolve config paths consistently across POSIX and Windows shells."""
    home = str(Path.home())
    expanded = re.sub(r"^\$\{HOME\}(?=[/\\]|$)", lambda _match: home, raw)
    expanded = re.sub(r"^\$HOME(?=[/\\]|$)", lambda _match: home, expanded)
    return Path(os.path.expandvars(expanded)).expanduser().resolve()
