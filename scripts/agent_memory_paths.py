from __future__ import annotations

import os
import re
from pathlib import Path


def resolve_path(raw: str) -> Path:
    """Resolve config paths consistently across POSIX and Windows shells."""
    expanded = raw
    if "$HOME" in raw or "${HOME}" in raw:
        home = str(Path.home())
        expanded = re.sub(r"^\$\{HOME\}(?=[/\\]|$)", lambda _match: home, expanded)
        expanded = re.sub(r"^\$HOME(?=[/\\]|$)", lambda _match: home, expanded)
    expanded = os.path.expandvars(expanded)
    # os.path.expandvars only expands Windows %VAR% syntax on Windows; do it
    # explicitly so configs authored with %VAR% resolve the same on any host.
    expanded = re.sub(
        r"%([^%]+)%",
        lambda match: os.environ.get(match.group(1), match.group(0)),
        expanded,
    )
    return Path(expanded).expanduser().resolve()
