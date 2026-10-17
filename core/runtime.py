"""Bounded runtime bookkeeping; no event text, user identity or model calls."""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import json
import os
import re
import tempfile
import threading
import time

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_LOCK = threading.Lock()


class DailyFallbackBudget:
    """Reserve before calling. Corrupt/unwritable storage stops paid fallback."""

    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "runtime" / "fallback_budget.json"
        with _LOCKS_LOCK:
            self._lock = _LOCKS.setdefault(str(self.path.resolve()), threading.RLock())
        self.error_type: str | None = None
        self.days: dict[str, int] = {}
        with self._lock:
            self._load()

    def _load(self) -> bool:
        try:
            if not self.path.exists():
                self.days = {}
                self.error_type = None
                return True
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data.get("schema_version") != 1:
                raise ValueError("Unsupported fallback budget")
            days = data.get("days")
            if not isinstance(days, dict) or len(days) > 64:
                raise ValueError("Invalid fallback budget")
            if any(not isinstance(k, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", k)
                   or type(v) is not int or not 0 <= v <= 1_000_000 for k, v in days.items()):
                raise ValueError("Invalid fallback count")
            self.days = dict(days)
            self.error_type = None
            return True
        except (OSError, ValueError, TypeError) as exc:
            self.error_type = type(exc).__name__
            return False

    def used(self, day: str) -> int:
        return self.days.get(day, 0)

    def refresh(self) -> bool:
        with self._lock:
            return self._load()

    @property
    def persisted(self) -> bool:
        return self.error_type is None and self.path.is_file()

    def reserve(self, day: str, limit: int) -> bool:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            return False
        with self._lock:
            if not self._load() or self.used(day) >= max(0, limit):
                return False
            proposed = dict(self.days)
            proposed[day] = self.used(day) + 1
            # Retain a short ledger across date/zone changes; never reset on reload.
            retained = sorted(key for key in proposed if key != day)[-31:] + [day]
            proposed = {key: proposed[key] for key in retained}
            temporary: Path | None = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                                 prefix="fallback-budget-", suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump({"schema_version": 1, "days": proposed}, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                self.days = proposed
                self.error_type = None
                return True
            except OSError as exc:
                self.error_type = type(exc).__name__
                return False
            finally:
                if temporary is not None and temporary.exists():
                    try:
                        temporary.unlink()
                    except OSError:
                        pass


class RecentMessageIds:
    """Only IDs are tracked; repeated text in different messages is still real."""

    def __init__(self, maximum: int = 1024, ttl: float = 600.0):
        self.maximum = max(1, int(maximum))
        self.ttl = max(1.0, float(ttl))
        self._seen: OrderedDict[tuple[str, ...], float] = OrderedDict()
        self.duplicates = 0

    def accept(self, key: tuple[str, ...] | None, now: float | None = None) -> bool:
        if key is None:
            return True
        current = time.monotonic() if now is None else float(now)
        while self._seen and current - next(iter(self._seen.values())) >= self.ttl:
            self._seen.popitem(last=False)
        previous = self._seen.get(key)
        if previous is not None and 0 <= current - previous < self.ttl:
            self.duplicates += 1
            return False
        self._seen.pop(key, None)
        self._seen[key] = current
        while len(self._seen) > self.maximum:
            self._seen.popitem(last=False)
        return True
