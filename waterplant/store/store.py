"""Thread safe JSON document behind the control service."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager


class Store:
    """A file backed mapping from string keys to string values.

    Writes are atomic: the document is written to a sibling temporary file and
    then moved over the target path so a crash cannot leave a partial document.
    """

    def __init__(self, path: str, data: dict[str, str] | None = None) -> None:
        self._path = path
        self._data: dict[str, str] = dict(data) if data else {}
        self._lock = threading.RLock()

    @property
    def path(self) -> str:
        return self._path

    @classmethod
    def open(cls, path: str) -> "Store":
        """Load a store from disk, creating an empty one when absent."""

        absolute = os.path.abspath(path)
        parent = os.path.dirname(absolute)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            with open(absolute, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except FileNotFoundError:
            return cls(absolute)
        if not raw.strip():
            return cls(absolute)
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise ValueError("store document must be a JSON object")
        return cls(absolute, {str(key): str(value) for key, value in loaded.items()})

    def get(self, key: str) -> tuple[str, bool]:
        with self._lock:
            if key in self._data:
                return self._data[key], True
            return "", False

    def put(self, key: str, value: str) -> None:
        with self._lock:
            self._data[key] = value
            self._save_locked()

    def put_many(self, values: dict[str, str]) -> None:
        """Persist several keys in one atomic document replacement."""

        with self._lock:
            for key, value in values.items():
                self._data[key] = value
            if values:
                self._save_locked()

    @contextmanager
    def transaction(self) -> Iterator["Store"]:
        """Hold the store lock and persist all mutations once on clean exit."""

        with self._lock:
            yield self
            self._save_locked()

    def get_locked(self, key: str) -> tuple[str, bool]:
        return self.get(key)

    def raw_locked(self, key: str, default: str) -> str:
        return self._data.get(key, default)

    def set_locked(self, key: str, value: str) -> None:
        self._data[key] = value

    def has_locked(self, key: str) -> bool:
        return key in self._data

    def delete(self, key: str) -> None:
        with self._lock:
            if key not in self._data:
                return
            del self._data[key]
            self._save_locked()

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self._data)

    def count(self) -> int:
        with self._lock:
            return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data = {}
            self._save_locked()

    def close(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        raw = json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True)
        temporary = f"{self._path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(raw)
        os.replace(temporary, self._path)
