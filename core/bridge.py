"""L2-B 存在桥（锚点 8）：空间事件 → 身体事件第四来源（src=space）。

她落位到"他所在房间" = 同室接近 → 一条 proximity 身体事件（profile 里最敏感的
非占位部位，强度 PRESENCE_PROXIMITY_GAIN 级；decay 的事件通道只有正向增益，
proximity 交给 decay.advance_state 施加——增益系数/phase 倍率/耐受/流水全走既有管线）。
她离开"他所在房间"（从他的房间起程移动）→ leave 事件（同部位体感直接回落
PRESENCE_LEAVE_FALL；负向回落 decay 通道表达不了，由本模块 apply_events 直接减值
并记流水——不经耐受、不计今日接触次数，离开不是接触）。
B7 对偶语义 on_departure：他离开"她所在房间"（检测时机=advance 前后他 room 变更且旧值
==她房间，由 main._tick_space 掌握）→ leave_he 事件（幅度更轻的 DEPARTURE_FALL，
常量在本模块，不动 CONFIG.py）。

冷却账本 = 身体状态 history 流水里的 space / space_leave 条目（decay._push_history
本来就把事件 type 落进 src，零新增状态键）：proximity 与 leave 各记各的账，
PRESENCE_COOLDOWN_SECONDS 内各自不重复（同室反复进出不重计、反复离开不重回落；
一次真实到访的"来了又走"两侧都能记上，净效果 +gain-fall）；冷却值同时充当
"刚落位"新鲜窗（她 since 距 now 超窗 = 久处同室，不重计）。流水只留 20 条，
被后续接触挤掉后冷却即失效——幅度 0.05 级，可容忍。

纯函数纪律：不 import astrbot、不做 IO；on_arrival 不改传入 dict，返回新事件列表
由调用方经 apply_events 施加（就地修改 body_st，与 decay.advance_state 同构）。
"""
from __future__ import annotations

import re
import math
from typing import Any

from .. import CONFIG
from . import decay, space

# 桥事件在身体流水里的 src 标记（≤12 字符，对齐 decay._push_history 的截断口径）
PROXIMITY_SRC = "space"
LEAVE_SRC = "space_leave"
DEPARTURE_SRC = "leave_he"      # B7：他离开她所在房间（与她起程离开他——LEAVE_SRC——互为对偶，各记各账）

# B7 他离开她所在房间 → 敏感微落幅度（模块常量，不动 CONFIG.py：
# 量级 = PRESENCE_PROXIMITY_GAIN 0.05 的 0.4 倍 ≈ 0.02，比她侧离开回落 PRESENCE_LEAVE_FALL 0.03 更轻）
DEPARTURE_FALL = 0.02

_PLACEHOLDER_LABEL_RE = re.compile(r"^\d+$")   # 占位部位 label 是纯数字编号（如 "01"）


def should_bridge(cfg_enabled: bool) -> bool:
    """桥总开关：面板配置 × CONFIG 常量，任一 False 即整体关闭。"""
    return bool(cfg_enabled) and bool(CONFIG.PRESENCE_BRIDGE_ENABLED)


def _sens(part: dict[str, Any]) -> float:
    try:
        return float(part.get("sens") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _is_placeholder(part: dict[str, Any]) -> bool:
    """占位部位判定：label 为纯数字编号（皮节参数模板，未命名不给它加值）。"""
    return bool(_PLACEHOLDER_LABEL_RE.fullmatch(str(part.get("label") or "").strip()))


def bridge_part(profile: dict[str, Any]) -> str | None:
    """桥的落点部位：最敏感的非占位部位；全是占位时回退最敏感者；无部位 None。
    sens 并列取 profile 声明序首个（max 取首个最大，确定性）。"""
    parts = [p for p in profile.get("parts") or [] if isinstance(p, dict) and p.get("id")]
    if not parts:
        return None
    pool = [p for p in parts if not _is_placeholder(p)] or parts
    return str(max(pool, key=_sens).get("id"))


def _last_bridge_mark(body_st: dict[str, Any], src: str) -> float | None:
    """身体流水里该类桥事件（src）的最近时刻。"""
    hist = body_st.get("history") if isinstance(body_st.get("history"), list) else []
    best: float | None = None
    for h in hist:
        if not isinstance(h, dict) or str(h.get("src") or "") != src:
            continue
        t = decay.parse_ts(h.get("t"))
        if t is not None and (best is None or t > best):
            best = t
    return best


def _in_cooldown(body_st: dict[str, Any], src: str, now: float) -> bool:
    last = _last_bridge_mark(body_st, src)
    return last is not None and (now - last) < float(CONFIG.PRESENCE_COOLDOWN_SECONDS)


def on_arrival(space_st: dict[str, Any], body_st: dict[str, Any], profile: dict[str, Any],
               manor: dict[str, Any], now: float, enabled: bool = True) -> list[dict[str, Any]]:
    """空间层落位/起程后调用（tick 的 space.advance 之后、观察器的 set_room 之后）。
    纯函数：绝不修改 space_st / body_st，返回新事件列表由调用方施加。

    proximity：{"part", "s": PRESENCE_PROXIMITY_GAIN, "type": "space", "t": now}
               —— 身体事件标准格式，decay.advance_state 可直接消费；
    leave：    {"part", "s": -PRESENCE_LEAVE_FALL, "type": "space_leave", "t": now}
               —— 负 s 表示回落，必须经 apply_events 施加（decay 会把负 s 钳成 0）。
    """
    if not enabled:
        return []
    now = float(now)
    she = space_st.get("she") or {}
    he = space_st.get("he") or {}
    he_room = str(he.get("room") or "")
    rooms = space.known_rooms(manor)
    pid = bridge_part(profile)
    if not pid:
        return []
    # 离开判定：她正从"他所在房间"起程去别处（set_room 起程即 moving，room 留在原地）。
    # moving 存在 → 不做落位判定（移动中同室语义模糊，与 space.same_room 同口径）。
    mv = she.get("moving")
    if isinstance(mv, dict):
        started_at = space.parse_iso(mv.get("started_at"))
        if isinstance(mv.get("started_at"), (int, float)):
            started_at = float(mv["started_at"])
        if started_at is not None and (not math.isfinite(started_at)
                or not 0 <= now - started_at <= float(CONFIG.PRESENCE_COOLDOWN_SECONDS)):
            return []
        if he_room and he_room in rooms and str(mv.get("from") or "") == he_room \
                and not he.get("moving") and str(mv.get("to") or "") != he_room \
                and not _in_cooldown(body_st, LEAVE_SRC, now):
            return [{"part": pid, "s": -float(CONFIG.PRESENCE_LEAVE_FALL),
                     "type": LEAVE_SRC, "t": now}]
        return []
    # 接近判定：她刚落位到他所在房间。since 新鲜 = 刚落位（久处同室不重计）；
    # 他在移动中 = 同室语义模糊，不计（对齐 same_room 的 None 口径）。
    she_room = str(she.get("room") or "")
    if not she_room or not he_room or she_room != he_room:
        return []
    if she_room not in rooms or he_room not in rooms or he.get("moving"):
        return []
    since = space.parse_iso(she.get("since"))
    if since is None or not 0.0 <= now - since <= float(CONFIG.PRESENCE_COOLDOWN_SECONDS):
        return []
    if _in_cooldown(body_st, PROXIMITY_SRC, now):
        return []
    return [{"part": pid, "s": float(CONFIG.PRESENCE_PROXIMITY_GAIN),
             "type": PROXIMITY_SRC, "t": now}]


def on_departure(space_st: dict[str, Any], body_st: dict[str, Any], profile: dict[str, Any],
                 manor: dict[str, Any], now: float, enabled: bool = True) -> list[dict[str, Any]]:
    """B7 他离开她所在房间（对偶于 on_arrival 的她侧语义）：检测时机由调用方掌握——
    main._tick_space 在 space.advance 前记旧 he.room，advance 后他 room 变了且旧值==她房间才调用
    （本函数收到的 space_st 已是"他刚走"后的状态）。
    纯函数：不改 space_st / body_st，返回事件列表由 apply_events 施加（负 s 走直接回落通道）。
    leave_he：{"part", "s": -DEPARTURE_FALL, "type": "leave_he", "t": now}——幅度 0.02 级，
    冷却账本按 DEPARTURE_SRC 各记各的（与她侧 space_leave 互不挤占）。"""
    if not enabled:
        return []
    now = float(now)
    she = space_st.get("she") or {}
    he = space_st.get("he") or {}
    she_room = str(she.get("room") or "")
    he_room = str(he.get("room") or "")
    rooms = space.known_rooms(manor)
    pid = bridge_part(profile)
    if not pid:
        return []
    # 刚离开的形态：双方都有位置、已不同室、他在移动中=语义模糊不计（对齐 same_room 口径）
    if not she_room or not he_room or she_room == he_room:
        return []
    if she_room not in rooms or he_room not in rooms or he.get("moving"):
        return []
    arrived_at = space.parse_iso(he.get("since"))
    if arrived_at is not None and not 0 <= now - arrived_at <= float(CONFIG.PRESENCE_COOLDOWN_SECONDS):
        return []   # 重启补落位不制造几小时前的离开事件。
    if _in_cooldown(body_st, DEPARTURE_SRC, now):
        return []
    return [{"part": pid, "s": -float(DEPARTURE_FALL),
             "type": DEPARTURE_SRC, "t": now}]


def _push_fall_history(body_st: dict[str, Any], et: float, pid: str, old: float, new: float,
                       src: str = LEAVE_SRC) -> None:
    """回落流水（与 decay._push_history 同构）——冷却账本就靠它；src 随事件类走（B7 leave_he 各记各账）。
    上限随 decay 对齐 ADAPT_HISTORY_LIMIT（0.30.0 扩容，长期记忆的数据面）。"""
    try:
        hist = body_st.get("history") if isinstance(body_st.get("history"), list) else []
        hist = hist[-(CONFIG.ADAPT_HISTORY_LIMIT - 1):] + [{"t": decay.iso_ts(et), "part": pid, "old": round(old, 4),
                                                            "new": round(new, 4), "src": src}]
        body_st["history"] = hist
    except Exception:
        pass


def apply_events(body_st: dict[str, Any], profile: dict[str, Any],
                 evs: list[dict[str, Any]], now: float,
                 phase_params: dict[str, float] | None = None) -> dict[str, Any] | None:
    """把 on_arrival 的事件施加到身体状态（就地修改 body_st，调用方负责落盘）。
    正向（proximity）走 decay.advance_state 完整管线；负向（leave 回落）直接减部位值
    并记流水，不动 updated_at（后续衰减从旧锚点补算，回落值作为新 v0 参与指数衰减）。
    phase_params（0.27.9 面板化）：可选 {afterglow_seconds, cooldown_seconds, afterglow_mult,
    cooldown_mult, night_window}，透传给 decay.advance_state；None 回 CONFIG 常量。
    返回 decay 摘要（无正向事件时 None）。"""
    now = float(now)
    up_evs: list[dict[str, Any]] = []
    down_evs: list[dict[str, Any]] = []
    for e in evs or []:
        if not isinstance(e, dict):
            continue
        try:
            s = float(e.get("s") or 0.0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(s):
            continue
        (up_evs if s >= 0.0 else down_evs).append(e)
    summary = None
    if up_evs:
        kw = dict(phase_params) if isinstance(phase_params, dict) else {}
        summary = decay.advance_state(body_st, profile, up_evs, now, **kw)
    parts_meta = {str(p.get("id")): p for p in profile.get("parts") or [] if p.get("id")}
    for ev in down_evs:
        pid = str(ev.get("part") or "")
        if pid not in parts_meta:
            continue
        t = ev.get("t")
        et = float(t) if isinstance(t, (int, float)) else (decay.parse_ts(t) if t is not None else None)
        et = now if et is None else et
        try:
            fall = abs(float(ev.get("s") or 0.0))
        except (TypeError, ValueError):
            continue
        st = body_st.setdefault("parts", {}).setdefault(pid, {})
        old = decay.clamp01(st.get("value") or 0.0)
        new = decay.clamp01(old - fall)
        st["value"] = new
        _push_fall_history(body_st, et, pid, old, new, str(ev.get("type") or LEAVE_SRC))
    return summary
