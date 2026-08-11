from __future__ import annotations

import os
import re
from pathlib import Path


def resolve_path(raw: str, *, lexical: bool = False) -> Path:
    """Expand config paths consistently, optionally preserving lexical components."""
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
    expanded_path = Path(expanded).expanduser()
    if lexical:
        return Path(os.path.abspath(expanded_path))
    return expanded_path.resolve()
