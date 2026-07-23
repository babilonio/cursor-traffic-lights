"""Shared paths and import setup for local tournament tooling."""

from __future__ import annotations

import os
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
"""Absolute path to the repository root."""

ARENA_ROOT = REPOSITORY_ROOT / "traffic-lights-arena"
"""Absolute path to the unmodified challenge package."""

CONTROLLER_PATH = ARENA_ROOT / "controller.py"
"""Absolute path to the challenge controller."""

# Short aliases are convenient for callers and preserve one canonical Path object.
REPO_ROOT = REPOSITORY_ROOT
ARENA_DIR = ARENA_ROOT


def ensure_traffic_arena_importable() -> Path:
    """Put the local arena root first on ``sys.path`` and return it.

    The repository deliberately keeps the challenge under the non-importable
    directory name ``traffic-lights-arena``.  Local tools should call this
    helper before importing ``traffic_arena`` instead of relying on their
    current working directory.
    """

    package_dir = ARENA_ROOT / "traffic_arena"
    if not package_dir.is_dir():
        raise FileNotFoundError(f"traffic_arena package not found at {package_dir}")

    arena_key = os.path.normcase(os.path.abspath(os.fspath(ARENA_ROOT)))
    matching_entries = [
        entry
        for entry in sys.path
        if os.path.normcase(os.path.abspath(entry or os.curdir)) == arena_key
    ]
    if not matching_entries or sys.path[0] not in matching_entries:
        sys.path[:] = [
            entry
            for entry in sys.path
            if os.path.normcase(os.path.abspath(entry or os.curdir)) != arena_key
        ]
        sys.path.insert(0, os.fspath(ARENA_ROOT))

    return ARENA_ROOT
