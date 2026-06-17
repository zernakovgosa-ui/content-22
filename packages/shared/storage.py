# -*- coding: utf-8 -*-
"""Atomic JSON store for the file-backed data layer.

Tiny and intentionally not a DB — the MVP wants observable, hand-editable files.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any


class JsonStore:
    """Read/write a single JSON file with a process-local lock + atomic write."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def read(self, default: Any = None) -> Any:
        if not self.path.exists():
            return default if default is not None else {}
        with self._lock:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f)

    def write(self, data: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        with self._lock:
            # Write to a temp file in the same directory, then atomic-replace.
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=self.path.name + ".",
                suffix=".tmp",
                dir=str(self.path.parent),
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="\n") as f:
                    f.write(payload)
                os.replace(tmp_path, self.path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise

    def update(self, mutator) -> Any:
        """Read → mutate (callback returns new data) → write."""
        data = self.read()
        new_data = mutator(data)
        self.write(new_data)
        return new_data
