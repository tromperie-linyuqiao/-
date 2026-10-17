"""持续接触层（0.31.0 体验修复：接触一旦建立就自动延续，不需要每句都重新描述）。

用户实测痛点：说一次"揉耳朵"后必须每句重复描述，否则 5 分钟掉光（单次揉耳尖
0.144 低于注意档阈值，完全无感）。本层把"接触"从一次性事件升格为可延续状态：
  · 建立——直接事件落账 state.active_contact[pid]
    = {action, s, since, last_renew, last_msg, last_tick}（覆盖同 pid 旧接触）；
  · 延续——双通道：消息驱动（persist_events 默认通道，对话在场即续期：每条消息
    给一次 s×cs_persist_rate 的延续增益并刷新 last_renew）+ tick 补充（tick=True，
    每 cs_persist_tick_every 个 tick 一发，不刷新 last_renew——过期窗仍由最近
    一次对话锚定，静默超过 cs_persist_max_gap 即散）；
  · 否定——"别摸耳朵"类语义由 events.extract_stops 识别（否定前缀+部位词邻近），
    main 观察器调 clear_contacts 清除（events 层的否定前缀本就不产事件，这里是
    接触面的对偶）；
  · 过期——now - last_renew > cs_persist_max_gap 清除；overload 进余韵时由
    decay.advance_state 清全部（高潮后接触自然结束）。
延续事件的增益纪律：不积累耐受、不产生 history 流水、不计 events_today、不动
习惯化/敏化/冷却/传导——只增加值与 arousal（合成事件带 src="persist" 标记，
advance_state 特判消费）。同一条消息触发的观察器推进与注入推进间隔小于
CS_PERSIST_MSG_MIN_GAP 时只计一次增益（续期照常），防一轮双算。
纯函数模块：不 import astrbot、不做 IO。
"""
from __future__ import annotations

import time
import math
from typing import Any

from .. import CONFIG
from . import decay


def contact_semantics(event: dict[str, Any]) -> dict[str, Any]:
    """接触的可选原始语义，旧记录无字段时返回空；不预乘任何刺激倍率。

    仅保留演算支持的有限参数，不把词级冷却或合成来源写入接触，避免自续期。
    """
    out: dict[str, Any] = {}
    kind = str(event.get("type") or "")[:12]
    if kind and kind != CONFIG.EVENT_TYPE_DEFAULT:
        out["type"] = kind
    mid = str(event.get("material") or "")[:16]
    if mid:
        out["material"] = mid
    method = str(event.get("method") or "")
    if method in ("stroke", "press", "rub", "tap", "blow"):
        out["method"] = method
    for key, (lo, hi) in {"freq_hz": (0.0, 500.0), "speed_cm_s": (0.0, 200.0),
                           "depth": (0.0, 1.0), "temp_c": (0.0, 60.0)}.items():
        raw = event.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value) and lo <= value <= hi:
            out[key] = value
    return out


def _config_number(config: dict[str, Any], key: str, default: float,
                   minimum: float, maximum: float | None = None) -> float:
    try:
        value = float(config.get(key, default))
    except (TypeError, ValueError, OverflowError):
        return float(default)
    if not math.isfinite(value):
        return float(default)
    return max(minimum, min(value, maximum)) if maximum is not None else max(minimum, value)


def record_contacts(state: dict[str, Any], events: list[dict[str, Any]] | None,
                    now: float | None = None) -> int:
    """直接事件落账（覆盖同 pid 旧接触，刷新全部锚点）。返回写入条数。"""
    now = float(now if now is not None else time.time())
    contacts = state.get("active_contact")
    if not isinstance(contacts, dict):
        contacts = {}
        state["active_contact"] = contacts
    n = 0
    for ev in events or []:
        ev = ev if isinstance(ev, dict) else {}
        if ev.get("src") == "persist":
            continue
        pid = str(ev.get("part") or "")
        s = decay.clamp01(ev.get("s") or 0.0)
        if not pid or s <= 0.0:
            continue
        action = str(ev.get("word") or ev.get("type") or CONFIG.EVENT_TYPE_DEFAULT)[:12]
        contacts[pid] = {
            "action": action,
            "s": round(s, 6),
            "since": decay.iso_ts(now),
            "last_renew": decay.iso_ts(now),
            "last_msg": decay.iso_ts(now),
            "last_tick": None,
        }
        contacts[pid].update(contact_semantics(ev))
        n += 1
    return n


def clear_contacts(state: dict[str, Any], pids: list[str] | tuple[str, ...] | set[str]) -> int:
    """按 pid 清除接触（否定语义"别摸耳朵" → 清 ear_tip）。返回实际清除条数。"""
    contacts = state.get("active_contact")
    if not isinstance(contacts, dict) or not pids:
        return 0
    n = 0
    for pid in {str(p) for p in pids}:
        if pid in contacts:
            del contacts[pid]
            n += 1
    return n


def in_gap(entry: dict[str, Any], now: float, max_gap: float) -> bool:
    """接触仍在消息间隔窗内（last_renew 可解析且距 now 未超 max_gap）。"""
    last = decay.parse_ts(entry.get("last_renew"))
    return last is not None and 0.0 <= (now - last) <= float(max_gap)


def _persist_event(pid: str, s: float, entry: dict[str, Any], now: float) -> dict[str, Any]:
    action = str(entry.get("action") or "")
    etype = action if action in CONFIG.ACTION_WORD_TYPES else CONFIG.EVENT_TYPE_DEFAULT
    out = {"part": str(pid), "s": round(float(s), 4), "type": etype,
           "t": float(now), "src": "persist"}
    semantics = contact_semantics(entry)
    if semantics:
        out.update(semantics)
        out["contact_semantics"] = True
    return out


def persist_events(state: dict[str, Any], events: list[dict[str, Any]] | None,
                   now: float | None = None, cfg: dict[str, Any] | None = None,
                   tick: bool = False) -> list[dict[str, Any]]:
    """计算本轮延续事件（就地做过期清除/续期/通道记账），返回合成事件列表。

    events = 本轮直接事件（其 pid 不叠加延续——全额增益已给，记录面由
    record_contacts 在 advance 后覆盖刷新）；tick=True 走 tick 通道（频率
    tick_every×tick_interval、不刷新续期锚点），否则消息通道（每条消息最多一发、
    刷新 last_renew——对话在场接触不断）。余韵/不应期不叠增益只保续期。
    无 active_contact 字段的旧 state 安全（当无接触，不炸）。"""
    now = float(now if now is not None else time.time())
    c = cfg or {}
    rate = _config_number(c, "rate", CONFIG.CS_PERSIST_RATE, 0.0, 1.0)
    max_gap = _config_number(c, "max_gap", CONFIG.CS_PERSIST_MAX_GAP, 0.0)
    tick_every = int(_config_number(c, "tick_every", CONFIG.CS_PERSIST_TICK_EVERY, 1.0))
    tick_interval = _config_number(c, "tick_interval", CONFIG.TICK_INTERVAL_SECONDS, 1.0)
    contacts = state.get("active_contact")
    if not isinstance(contacts, dict):
        return []
    direct = {str(e.get("part") or "") for e in events or [] if isinstance(e, dict)}
    gains_allowed = str(state.get("phase") or "building") == "building"
    out: list[dict[str, Any]] = []
    for pid in list(contacts):
        entry = contacts.get(pid)
        if not isinstance(entry, dict):
            del contacts[pid]
            continue
        if not in_gap(entry, now, max_gap):
            del contacts[pid]     # 过期：距最近一次对话超消息间隔窗
            continue
        if pid in direct:
            continue              # 本轮有直接事件：不叠加延续（记录面稍后覆盖刷新）
        s = decay.clamp01(entry.get("s") or 0.0) * rate
        if tick:
            last_tick = decay.parse_ts(entry.get("last_tick"))
            if s > 0.0 and gains_allowed and (last_tick is None
                                              or now - last_tick >= tick_every * tick_interval):
                out.append(_persist_event(pid, s, entry, now))
                entry["last_tick"] = decay.iso_ts(now)
            # tick 通道不刷新 last_renew：静默超过 max_gap 仍会过期（接触靠对话延续，不靠挂机）
        else:
            last_msg = decay.parse_ts(entry.get("last_msg"))
            if s > 0.0 and gains_allowed and (last_msg is None
                                              or now - last_msg >= CONFIG.CS_PERSIST_MSG_MIN_GAP):
                out.append(_persist_event(pid, s, entry, now))
                entry["last_msg"] = decay.iso_ts(now)
            entry["last_renew"] = decay.iso_ts(now)   # 消息通道：对话在场即续期
    return out
