"""她的主动触碰意图（0.30.0）。

条件三重奏：phase=building（平常期才有"想要"的张力）+ arousal 达到主动阈值
（身体已经被语言撩起来）+ verbal_arousal 在场（对话氛围在，不是纯数值堆积）。
命中则从模板池随机出一句意图短语（含最活跃部位 label 占位），main 写入
state["proactive_intent"] = {ts, hint}——渲染出口（inject）在时效内加"她<短语>"尾巴；
外部调用方 的 presence_snapshot 若按字段读 vb 状态文件也能直接看到该键（vb 侧自洽，不依赖 外部调用方 改动）。
冷却 PROACTIVE_TOUCH_COOLDOWN（4h）内不重复出意图（state 记 ts）。
纯函数模块：不 import astrbot、不做 IO；随机只落在模板选择一处（templates 可注入便于确定性测试）。
"""
from __future__ import annotations

import random
import math
import time
from typing import Any

from .. import CONFIG
from . import decay


def active_part_label(state: dict[str, Any], profile: dict[str, Any]) -> str | None:
    """最活跃部位 label：value 最高且 ≥ ACTIVE_AROUSAL_MIN（并列取 profile 声明序首个，确定性）。"""
    parts_meta = {str(p.get("id")): p for p in profile.get("parts") or []
                  if isinstance(p, dict) and p.get("id")}
    parts_state = state.get("parts") if isinstance(state.get("parts"), dict) else {}
    best_pid: str | None = None
    best_v = CONFIG.ACTIVE_AROUSAL_MIN
    for pid, meta in parts_meta.items():
        st = parts_state.get(pid) if isinstance(parts_state.get(pid), dict) else {}
        try:
            raw = float(st.get("value") or 0.0)
        except (TypeError, ValueError):
            continue
        v = decay.clamp01(raw) if math.isfinite(raw) else 0.0
        if v > best_v or (best_pid is None and v == best_v):
            best_pid, best_v = pid, v
    if best_pid is None:
        return None
    return str(parts_meta[best_pid].get("label") or best_pid)


def should_intent_touch(state: dict[str, Any], profile: dict[str, Any],
                        now: float | None = None, cfg: dict[str, Any] | None = None,
                        templates: list[str] | tuple[str, ...] | None = None) -> str | None:
    """判定并返回意图短语（不满足条件返回 None，纯函数不改 state——ts 落账由调用方负责）。
    cfg 可注入 {arousal_min, verbal_min, cooldown}（配置链），缺省回 CONFIG。"""
    now = float(now if now is not None else time.time())
    c = cfg if isinstance(cfg, dict) else {}
    def _parameter(key: str, default: float, limit: float | None = None) -> float:
        try:
            value = float(c.get(key, default))
        except (TypeError, ValueError):
            return float(default)
        if not math.isfinite(value) or value < 0 or (limit is not None and value > limit):
            return float(default)
        return value
    arousal_min = _parameter("arousal_min", CONFIG.PROACTIVE_TOUCH_AROUSAL, 1.0)
    verbal_min = _parameter("verbal_min", CONFIG.PROACTIVE_TOUCH_VERBAL_MIN, 1.0)
    cooldown = _parameter("cooldown", CONFIG.PROACTIVE_TOUCH_COOLDOWN)
    if str(state.get("phase") or "building") != "building":
        return None
    try:
        arousal = float(state.get("arousal") or 0.0)
    except (TypeError, ValueError):
        arousal = 0.0
    if not math.isfinite(arousal) or arousal < arousal_min:
        return None
    try:
        raw_verbal = float(state.get("verbal_arousal") or 0.0)
        verbal = decay.clamp01(raw_verbal) if math.isfinite(raw_verbal) else 0.0
    except (TypeError, ValueError):
        verbal = 0.0
    if verbal <= verbal_min:
        return None
    prev = state.get("proactive_intent") if isinstance(state.get("proactive_intent"), dict) else {}
    last = decay.parse_ts(prev.get("ts"))
    if last is not None and math.isfinite(last) and now - last < cooldown:
        return None
    supplied = (templates if templates is not None
                else (c.get("templates") or CONFIG.PROACTIVE_TOUCH_TEMPLATES))
    if not isinstance(supplied, (list, tuple)):
        supplied = CONFIG.PROACTIVE_TOUCH_TEMPLATES
    pool = [t for t in supplied if isinstance(t, str) and t.strip()]
    if not pool:
        return None
    label = active_part_label(state, profile)
    usable = [t for t in pool if "{part}" not in t or label]
    if not usable:
        usable = [t.replace("{part}", "") for t in pool]
    hint = str(random.choice(usable)).replace("{part}", label or "")
    return hint[:40].strip() or None


def intent_hint(state: dict[str, Any], now: float | None = None,
                ttl: float | None = None) -> str | None:
    """时效内的主动触碰意图短语（渲染尾巴/快照出口用）：超 TTL 视为过期不渲染。
    state["proactive_intent"] = {ts(ISO), hint}；坏值/缺失返回 None。"""
    now = float(now if now is not None else time.time())
    try:
        ttl = CONFIG.PROACTIVE_TOUCH_TTL if ttl is None else float(ttl)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ttl) or ttl < 0:
        return None
    pi = state.get("proactive_intent") if isinstance(state.get("proactive_intent"), dict) else {}
    ts = decay.parse_ts(pi.get("ts"))
    hint = str(pi.get("hint") or "").strip()
    if ts is None or not math.isfinite(ts) or not hint:
        return None
    age = now - ts
    if age < 0.0 or age > ttl:
        return None
    return hint[:14]   # 短句纪律：渲染尾巴 ≤14 字
