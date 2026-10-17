"""存储层：规则（默认→覆盖层）、触发产出、快照/冷却/历史。原子写。"""
from __future__ import annotations

import json
import copy
import os
import tempfile
import threading
from typing import Any

from .. import CONFIG

_LOCK = threading.RLock()


def _atomic_write(path: str, data: Any) -> None:
    _d = os.path.dirname(path)
    if _d:
        os.makedirs(_d, exist_ok=True)   # 终审修复：全新安装 mood/ 不存在时静默死亡
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".",
                               suffix=".tmp", dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=1))
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _read(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


_RULES_CACHE: dict[str, tuple[int, dict[str, Any]]] = {}   # mtime 缓存（每 tick 免读盘）


def load_rules(data_dir: str, plugin_dir: str) -> dict[str, Any]:
    """覆盖层 plugin_data/rules.json > 插件 data/rules.json；都无 → 空。mtime 缓存。"""
    for path in (os.path.join(str(data_dir), CONFIG.MO_RULES_FILE),
                 os.path.join(str(plugin_dir), "data", CONFIG.MO_RULES_FILE)):
        try:
            _mt = os.stat(path).st_mtime_ns
        except OSError:
            _RULES_CACHE.pop(path, None)
            continue
        _hit = _RULES_CACHE.get(path)
        if _hit and _hit[0] == _mt:
            return copy.deepcopy(_hit[1])
        data = _read(path, None)
        if isinstance(data, dict) and isinstance(data.get("rules"), list):
            if len(_RULES_CACHE) >= 4:
                _RULES_CACHE.pop(next(iter(_RULES_CACHE)), None)
            _RULES_CACHE[path] = (_mt, data)
            return copy.deepcopy(data)
    return {"schema": 1, "rules": []}


def save_rules(data_dir: str, doc: dict[str, Any]) -> None:
    with _LOCK:
        _atomic_write(os.path.join(str(data_dir), CONFIG.MO_RULES_FILE), doc)


def load_triggers(data_dir: str) -> list[dict[str, Any]]:
    data = _read(os.path.join(str(data_dir), CONFIG.MO_TRIGGER_FILE), {})
    return data.get("triggers") if isinstance(data, dict) and isinstance(data.get("triggers"), list) else []


def save_triggers(data_dir: str, triggers: list[dict[str, Any]]) -> None:
    with _LOCK:
        _atomic_write(os.path.join(str(data_dir), CONFIG.MO_TRIGGER_FILE),
                      {"schema": 1, "updated_at": _now_iso(), "triggers": triggers})


def load_json(data_dir: str, name: str, default: Any) -> Any:
    return _read(os.path.join(str(data_dir), name), default)


def save_json(data_dir: str, name: str, data: Any) -> None:
    with _LOCK:
        _atomic_write(os.path.join(str(data_dir), name), data)


def append_history(data_dir: str, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    # 读-改-写一起持锁；仅给最终 save 加锁无法保护并发追加不丢历史。
    with _LOCK:
        hist = load_json(data_dir, CONFIG.MO_HISTORY_FILE, {"items": []})
        entries = hist.get("items") if isinstance(hist, dict) and isinstance(hist.get("items"), list) else []
        entries = entries[-CONFIG.MO_HISTORY_LIMIT:]
        for t in items:
            if not isinstance(t, dict):
                continue
            entries.append({"id": t.get("id"), "rule_id": t.get("rule_id"), "signal": t.get("signal"),
                            "reason": t.get("reason"), "created_at": _now_iso(), "fired": True})
        save_json(data_dir, CONFIG.MO_HISTORY_FILE, {"items": entries[-CONFIG.MO_HISTORY_LIMIT:]})


def _now_iso() -> str:
    import datetime as dt
    return dt.datetime.now().replace(microsecond=0).isoformat()
