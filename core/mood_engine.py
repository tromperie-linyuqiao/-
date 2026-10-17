"""规则引擎：conditions 求值 + changed 判定 + 冷却 + 优先级。纯函数，无 astrbot 依赖。"""
from __future__ import annotations

import math
import time
from typing import Any

from .. import CONFIG


def _get_field(signals: dict[str, Any], field: str) -> Any:
    """field 形如 "weather.is_rain"——先取信号源再取字段；无信号源返回 None。"""
    parts = field.split(".", 1)
    if len(parts) != 2:
        return None
    src = signals.get(parts[0])
    if not isinstance(src, dict):
        return None
    return src.get(parts[1])


_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: _num(a) is not None and _num(b) is not None and _num(a) > _num(b),
    "lt": lambda a, b: _num(a) is not None and _num(b) is not None and _num(a) < _num(b),
    "gte": lambda a, b: _num(a) is not None and _num(b) is not None and _num(a) >= _num(b),
    "lte": lambda a, b: _num(a) is not None and _num(b) is not None and _num(a) <= _num(b),
    "changed": None,   # 特殊：与 last_state 比对，在 evaluate 内处理
}


def _num(v: Any) -> float | None:
    try:
        value = float(v)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _cond_ok(cond: dict[str, Any], signals: dict[str, Any], last_state: dict[str, Any]) -> bool:
    field = str(cond.get("field") or "")
    op = str(cond.get("op") or "eq")
    # 0.29.1：available=False（信号未激活兜底形态）的源不参与任何判定——
    # eq/gt/lt/gte/lte 对 None 本就不命中，这里把 ne/changed 也一并封死（未激活≠值不等）
    src = signals.get(field.split(".", 1)[0]) if "." in field else None
    if not isinstance(src, dict) or src.get("available") is False:
        return False
    value = _get_field(signals, field)
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return False
    if op == "changed":
        key = field.replace(".", "_")
        return key in last_state and value != last_state.get(key)
    fn = _OPS.get(op)
    if fn is None:
        return False
    return bool(fn(value, cond.get("value")))


def _snapshot(signals: dict[str, Any]) -> dict[str, Any]:
    """信号扁平快照（changed 判定的基线）："weather.is_rain" → "weather_is_rain"。"""
    flat: dict[str, Any] = {}
    for src, data in signals.items():
        if isinstance(data, dict):
            for k, v in data.items():
                flat[f"{src}_{k}"] = v
    return flat


def snapshot(signals: dict[str, Any]) -> dict[str, Any]:
    """公开别名：main tick 落 last_state 用。"""
    return _snapshot(signals)


def evaluate(rules: list[dict[str, Any]], signals: dict[str, Any],
             last_state: dict[str, Any], cooldowns: dict[str, float],
             now: float | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """返回 (触发列表, 新冷却账本)。触发含 id/rule_id/signal/reason/priority/context/created/expires。"""
    now = float(now if now is not None else time.time())
    fired: list[dict[str, Any]] = []
    last_state = last_state if isinstance(last_state, dict) else {}
    cooldowns = cooldowns if isinstance(cooldowns, dict) else {}
    new_cd = dict(cooldowns)
    fired_ids: set[str] = set()
    for rule in rules or []:
        if not isinstance(rule, dict) or not rule.get("id") or rule.get("enabled") is False:
            continue
        rid = str(rule["id"])
        if rid in fired_ids:
            continue
        signal = str(rule.get("signal") or "")
        conds = rule.get("conditions") or []
        if not isinstance(conds, list) or not conds:
            continue
        try:
            cd_min = max(1, int(rule.get("cooldown_minutes") or 60))
        except (TypeError, ValueError, OverflowError):
            cd_min = 60
        _last = _num(cooldowns.get(rid))
        if _last is not None and now - _last < cd_min * 60:
            continue
        if not all(_cond_ok(c if isinstance(c, dict) else {}, signals, last_state) for c in conds):
            continue
        try:
            priority = max(1, min(5, int(rule.get("priority") or 3)))
        except (TypeError, ValueError, OverflowError):
            priority = 3
        source = signals.get(signal)
        ctx = {k: v for k, v in (source.items() if isinstance(source, dict) else ())
               if isinstance(v, (str, int, float, bool))
               and not (isinstance(v, float) and not math.isfinite(v))}
        fired.append({
            "id": f"{rid}_{int(now)}",
            "rule_id": rid,
            "signal": signal,
            "reason": str(rule.get("note") or rule.get("template_hint") or rid)[:80],
            "priority": priority,
            "context": ctx,
            "template_hint": render_hint(str(rule.get("template_hint") or "")[:120], signals),
            "created_at": now,
            "expires_at": now + CONFIG.MO_TRIGGER_TTL_SECONDS,
        })
        new_cd[rid] = now
        fired_ids.add(rid)
    fired.sort(key=lambda t: (t["priority"], -t["created_at"]))
    return fired, new_cd


def prune_expired(triggers: list[dict[str, Any]], now: float | None = None) -> list[dict[str, Any]]:
    now = float(now if now is not None else time.time())
    return [t for t in triggers or []
            if isinstance(t, dict) and _num(t.get("expires_at")) is not None
            and _num(t["expires_at"]) > now]


def render_for_pc(triggers: list[dict[str, Any]]) -> str:
    """拼给 外部调用方 主动消息 prompt 的情境段（≤200 字符，过期过滤由调用方做）。"""
    if not triggers:
        return ""
    parts = []
    for t in [t for t in triggers if isinstance(t, dict)][:3]:
        hint = t.get("template_hint") or t.get("reason") or ""
        parts.append(f"{hint}")
    text = "；".join(p for p in parts if p)
    return f"（情境线索：{text}）"[:200] if text else ""


def render_hint(hint: str, signals: dict[str, Any]) -> str:
    """自定义规则 hint 变量化（0.27.1）：{she_room}/{city}/{is_rain}/{book}/{score}/{phase}
    用四信号上下文替换，未知占位符原样保留。"""
    import re as _re
    if not hint or "{" not in hint:
        return hint
    pres = signals.get("presence") if isinstance(signals.get("presence"), dict) else {}
    wx = signals.get("weather") if isinstance(signals.get("weather"), dict) else {}
    rd = signals.get("reading") if isinstance(signals.get("reading"), dict) else {}
    md = signals.get("mood") if isinstance(signals.get("mood"), dict) else {}
    def _yes_no(value: Any) -> str:
        return "不明" if value is None else ("是" if value else "否")
    mapping = {
        "she_room": pres.get("she_room", ""),
        "she_moving": "是" if pres.get("she_moving") else "否",
        "body_phase": pres.get("body_phase", ""),
        "phase": pres.get("body_phase", ""),   # 终审：{phase} 别名（TERMS 宣称）
        "city": wx.get("city", ""),
        "is_rain": _yes_no(wx.get("is_rain")),
        "book": rd.get("book_title") or "",
        "chapter": rd.get("chapter_index") if rd.get("chapter_index") is not None else "",
        "score": md.get("score") if md.get("score") is not None else "",
        "env_valence": pres.get("env_valence", ""),
        "mood_word": pres.get("mood_word", ""),
        "same_room": _yes_no(pres.get("same_room")),
    }
    return _re.sub(r"\{(\w+)\}", lambda m: str(mapping.get(m.group(1), m.group(0))), hint)
