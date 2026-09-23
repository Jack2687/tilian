from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def resolve_python() -> str:
    """Return the interpreter used by standalone engine subprocesses."""
    explicit = str(os.environ.get("TILIAN_PYTHON") or "").strip()
    if explicit:
        return explicit

    candidates = [
        ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"),
        ROOT / "backend" / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    current = Path(sys.executable)
    if current.is_file() and not current.name.lower().startswith("python"):
        return str(current)
    return sys.executable
