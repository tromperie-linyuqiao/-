"""空间状态机：她/他的位置、移动过程、重启补算。纯函数，无 IO 无 astrbot 依赖。

规范 4.1/4.2：她的位置优先级 RP 覆盖 > 指令 > 外部调用方 日程映射 > 兜底动线；
移动必须有过程（moving={to,until_ts,from}），重启按 updated_at 补算落位。
"""
from __future__ import annotations

import re
import math
import time
from typing import Any

from .. import CONFIG
from . import traverse


_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})")


def _local_time(now: float, tz_offset: int | None):
    """None 跟随容器本地时区；数字（含负偏移）按 UTC 偏移换算。"""
    return time.localtime(now) if tz_offset is None else time.gmtime(now + tz_offset * 3600)


def current_plan_text(plan: Any, now: float, tz_offset: int | None = 8) -> str | None:
    """外部调用方 daily_plan → 当前时段项文本（纯函数，可测）。

    真实结构（线上实测）：daily_plan 是 dict，取其 items 列表，
    项形如 {"time": "08:20", "activity": "起床收拾", ...}——选"已开始的最近一项"。
    兼容旧结构 from_h/to_h/text（直接命中当前小时）。
    """
    if isinstance(plan, dict):
        day = str(plan.get("date") or "").strip()
        if day and day != time.strftime("%Y-%m-%d", _local_time(now, tz_offset)):
            return None   # 昨日日程不冒充今天；无日期的旧列表结构保持兼容。
        plan = plan.get("items")
    if not isinstance(plan, list) or not plan:
        return None
    lt = _local_time(now, tz_offset)
    now_min = lt.tm_hour * 60 + lt.tm_min
    best = None
    best_key = -1
    for item in plan:
        if not isinstance(item, dict):
            continue
        th = item.get("time")
        if isinstance(th, str):
            m = _HHMM_RE.match(th.strip())
            if not m:
                continue
            hour, minute = int(m.group(1)), int(m.group(2))
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                continue
            key = hour * 60 + minute
            if best_key <= key <= now_min:
                best, best_key = item, key
            continue
        if "from_h" not in item:
            continue
        try:
            fh, te = int(item.get("from_h", -1)), int(item.get("to_h", 24))
        except (TypeError, ValueError):
            continue
        if 0 <= fh < te <= 24 and fh <= lt.tm_hour < te:
            best = item
            best_key = now_min
    if not isinstance(best, dict):
        return None
    return str(best.get("activity") or best.get("text") or best.get("desc") or best.get("title") or "") or None


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.gmtime(ts + 8 * 3600))


def parse_iso(v: Any) -> float | None:
    if not v:
        return None
    try:
        import datetime as dt
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def known_rooms(manor: dict[str, Any]) -> set[str]:
    return {str(r.get("id")) for r in manor.get("rooms") or []
            if isinstance(r, dict) and r.get("id")}


def new_state(session: str) -> dict[str, Any]:
    return {
        "schema": CONFIG.SP_STATE_SCHEMA,
        "session": str(session),
        "she": {"room": None, "since": None, "source": None, "moving": None},
        "he": {"room": None, "since": None, "source": "default", "confidence": 0.0, "moving": None},
        "updated_at": None,
        "events_today": {"day": "", "moves": 0},
    }


def _land_if_arrived(who: dict[str, Any], now: float) -> float | None:
    """moving 到点落位（不瞬移公理的完成侧）。"""
    mv = who.get("moving")
    if not isinstance(mv, dict):
        return
    until = mv.get("until_ts")
    if isinstance(until, (int, float)) and not isinstance(until, bool) \
            and math.isfinite(until) and now >= float(until) and mv.get("to"):
        who["room"] = str(mv.get("to"))
        # 重启/迟到 tick 补算的是原计划到达时刻，不能把旧到达伪装成刚靠近。
        who["since"] = iso(float(until))
        who["moving"] = None
        return float(until)
    return None


def _room_floor(manor: dict[str, Any], room_id: str) -> str | None:
    for r in manor.get("rooms") or []:
        if not isinstance(r, dict):
            continue
        if str(r.get("id")) == room_id:
            return str(r.get("floor"))
    return None


def _rainy_room(manor: dict[str, Any], room_id: str) -> str:
    """雨天替换（B4）：户外房（floor=="OUT"）走 manor["rainy_fallback"]（id→id）；
    室内房不替换；映射目标不是已知房间 id 时忽略映射走原逻辑。"""
    if _room_floor(manor, room_id) != "OUT":
        return room_id
    mapping = manor.get("rainy_fallback")
    if not isinstance(mapping, dict):
        return room_id
    mapped = mapping.get(room_id)
    if mapped and str(mapped) in known_rooms(manor):
        return str(mapped)
    return room_id


def fallback_room(manor: dict[str, Any], now: float, tz_offset: int | None = 8,
                  rainy: bool = False) -> str | None:
    """兜底动线：default_schedule 时段表（本地时区小时）。
    rainy=True 时户外房（floor=="OUT"）的兜底替换为室内近义房（rainy_fallback 映射）。"""
    hour = _local_time(now, tz_offset).tm_hour
    for slot in manor.get("default_schedule") or []:
        if not isinstance(slot, dict):
            continue
        try:
            if int(slot.get("from_h")) <= hour < int(slot.get("to_h")):
                room = str(slot.get("room"))
                if room in known_rooms(manor):
                    return _rainy_room(manor, room) if rainy else room
        except (TypeError, ValueError):
            continue
    return None


def schedule_room(manor: dict[str, Any], plan_text: str | None) -> str | None:
    """外部调用方 日程文本 → activity_map 关键词 → 房间（按序首个命中）。"""
    text = str(plan_text or "").strip()
    if not text:
        return None
    for m in manor.get("activity_map") or []:
        if not isinstance(m, dict) or str(m.get("room")) not in known_rooms(manor):
            continue
        matches = m.get("match") or []
        if not isinstance(matches, (list, tuple)):
            continue
        for kw in matches:
            if str(kw) and str(kw) in text:
                return str(m.get("room"))
    return None


def set_room(state: dict[str, Any], who_key: str, room: str, manor: dict[str, Any],
             now: float, source: str, confidence: float | None = None) -> dict[str, Any] | None:
    """直接落位（同室/同房则无事发生）。返回 {"changed": True, "seconds": N} 或 None。
    位置变化必须有过程：产生 moving={to, until_ts=now+秒数, from}（不瞬移公理）。"""
    who = state.setdefault(who_key, {})
    rooms = known_rooms(manor)
    if room not in rooms:
        return None
    # 指令/观察器可先于下一次 tick 到达；先完成已到点的移动再判断同目标。
    _land_if_arrived(who, now)
    _mv = who.get("moving")
    if isinstance(_mv, dict) and str(_mv.get("to") or "") == str(room):
        who["source"] = source
        if source == "rp":
            who["since"] = iso(now)
        if confidence is not None:
            who["confidence"] = confidence
        try:
            remaining = max(0, math.ceil(float(_mv.get("until_ts")) - now))
        except (TypeError, ValueError, OverflowError):
            remaining = 30
        # 兼容原返回的移动字段，同时满足 /where 读取 seconds 的契约。
        return {**_mv, "changed": False, "seconds": remaining}
    cur = who.get("room")
    if cur == room and not who.get("moving"):
        who["source"] = source
        if source == "rp":
            who["since"] = iso(now)   # RP 重申同房也刷新覆盖窗，否则过期 since 会被日程立刻拽走
        if confidence is not None:
            who["confidence"] = confidence
        return None
    if cur is None:
        # 首次落位：无移动过程（没有"从哪来"）
        who["room"] = room
        who["since"] = iso(now)
        who["source"] = source
        if confidence is not None:
            who["confidence"] = confidence
        return None
    t = traverse.travel_seconds(manor, str(cur), room)
    seconds = t if t is not None else 30   # 图不连通时 30 秒兜底——绝不瞬移落位
    who["moving"] = {"to": room, "from": cur, "until_ts": now + seconds, "started_at": now}
    who["source"] = source
    if source == "rp":
        who["since"] = iso(now)
    if confidence is not None:
        who["confidence"] = confidence
    state["events_today"]["moves"] = int(state["events_today"].get("moves") or 0) + 1
    return {"changed": True, "seconds": seconds}

def advance(state: dict[str, Any], manor: dict[str, Any], now: float,
            tz_offset: int | None = 8, plan_text: str | None = None,
            rp_minutes: int | None = None, rainy: bool = False) -> dict[str, Any]:
    """推进状态到 now：落位到点的移动、日切、她位置的日程推导（RP/指令覆盖优先）。
    就地修改 state，返回摘要（vb 范本同构：返回值不是状态）。
    rp_minutes：RP 覆盖有效期（分钟），由调用方传配置值（面板可调）。
    rainy：雨天动线开关（B4）——只影响兜底动线（fallback_room），不动 外部调用方 日程映射。"""
    rp_minutes = CONFIG.SP_RP_OVERRIDE_MINUTES if rp_minutes is None else int(rp_minutes)
    summary: dict[str, Any] = {"arrived": [], "arrived_at": {}, "she_moved": False, "day_rolled": False,
                               "she_room": None, "he_room": None, "moving": None}
    # 1. 到点落位
    for key in ("she", "he"):
        who = state.setdefault(key, {})
        mv = who.get("moving")
        if isinstance(mv, dict):
            until = mv.get("until_ts")
            landed_at = _land_if_arrived(who, now)
            if landed_at is not None:
                summary["arrived"].append(key)
                summary["arrived_at"][key] = landed_at
    # 2. 日切（周期键纪律）
    import time as _t
    day = _t.strftime("%Y-%m-%d", _local_time(now, tz_offset))
    et = state.setdefault("events_today", {"day": "", "moves": 0})
    if et.get("day") != day:
        state["events_today"] = {"day": day, "moves": 0}
        summary["day_rolled"] = True
    # 3. 她的位置推导（RP 覆盖未过期则不动）
    she = state.setdefault("she", {})
    rp_since = parse_iso(she.get("since"))
    rp_fresh = she.get("source") == "rp" and rp_since is not None and \
        0 <= (now - rp_since) < rp_minutes * 60
    if not she.get("moving") and not rp_fresh:
        plan_room = schedule_room(manor, plan_text) if plan_text else None
        target = plan_room or fallback_room(manor, now, tz_offset, rainy)
        if target and she.get("room") != target:
            if she.get("room") is None:
                she["room"] = target
                she["since"] = iso(now)
                she["source"] = "schedule" if plan_room else "fallback"
            else:
                set_room(state, "she", target, manor, now,
                         "schedule" if plan_room else "fallback")
                summary["she_moved"] = True
    if she.get("moving"):
        summary["moving"] = {"from": she["moving"].get("from"), "to": she["moving"].get("to")}
    summary["she_room"] = she.get("room")
    summary["he_room"] = state.setdefault("he", {}).get("room")
    state["updated_at"] = iso(now)
    return summary


def same_room(state: dict[str, Any]) -> bool | None:
    """同室判定：任一方位置未知或移动中 → None（模糊表述）。"""
    she, he = state.get("she") or {}, state.get("he") or {}
    if not she.get("room") or not he.get("room") or she.get("moving") or he.get("moving"):
        return None
    return she["room"] == he["room"]


def scene_words(manor: dict[str, Any], room_id: str | None) -> list[dict[str, Any]]:
    """房间的自定义场景互动词（0.27.1 联动迭代）：[{word, gain, note}]，坏结构宽容。"""
    if not room_id:
        return []
    for r in manor.get("rooms") or []:
        if not isinstance(r, dict):
            continue
        if str(r.get("id")) == room_id:
            words = r.get("scene_words")
            if not isinstance(words, list):
                return []
            out = []
            for w in words:
                if isinstance(w, dict) and str(w.get("word") or "").strip():
                    try:
                        value = float(w.get("gain", 0.05))
                        gain = max(0.0, min(0.3, value)) if math.isfinite(value) else 0.05
                    except (TypeError, ValueError):
                        gain = 0.05
                    out.append({"word": str(w["word"]).strip(), "gain": gain,
                                "note": str(w.get("note") or "")[:30]})
            return out
    return []


def apply_scene_gain(evs: list[dict[str, Any]], text: str,
                     words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """事件场景增益（0.27.1）：文本命中她所在房间的场景词 → s ×(1+gain)，ev 记 scene 词。
    一词只吃一次增益，多词命中取最高。纯函数不改入参。"""
    if not evs or not words or not text:
        return evs
    hit: dict[str, float] = {}
    for w in words:
        if w["word"] in text:
            hit[w["word"]] = max(hit.get(w["word"], 0.0), w["gain"])
    if not hit:
        return evs
    best_gain = max(hit.values())
    scene = "/".join(hit)[:24]
    out = []
    for ev in evs:
        e2 = dict(ev)
        try:
            s = round(float(e2.get("s") or 0.0) * (1.0 + best_gain), 4)
        except (TypeError, ValueError):
            s = e2.get("s")
        e2["s"] = s
        e2["scene"] = scene
        out.append(e2)
    return out


def room_mood_bias(manor: dict[str, Any], room_id: str | None) -> tuple[float, float]:
    """房间环境底色（0.27.2，Russell 环形/PAD）：[valence 愉悦, arousal 唤醒]，缺省中性。"""
    if not room_id:
        return (0.0, 0.0)
    for r in manor.get("rooms") or []:
        if not isinstance(r, dict):
            continue
        if str(r.get("id")) == room_id:
            b = r.get("mood_bias")
            if isinstance(b, (list, tuple)) and len(b) == 2:
                def _ax(x: Any) -> float:
                    try:
                        value = float(x)
                        return max(-1.0, min(1.0, value)) if math.isfinite(value) else 0.0
                    except (TypeError, ValueError):
                        return 0.0
                return (_ax(b[0]), _ax(b[1]))   # 坏值逐轴归零，不拖垮另一轴
    return (0.0, 0.0)


def mood_word(valence: float, arousal: float) -> str:
    """valence×arousal → 情感词（Russell 八分区简化四象限 + 中性带）。"""
    if abs(valence) < 0.15 and abs(arousal) < 0.15:
        return "平静"
    if valence >= 0:
        return "雀跃" if arousal >= 0.1 else ("满足" if valence >= 0.3 else "安心")
    return "不安" if arousal >= 0 else "低落"


def state_fingerprint(state: dict[str, Any]) -> tuple:
    """空间状态变化指纹（0.27.3 优化）：tick 空推进跳写盘的判定依据。
    含位置/移动/来源/置信/日切计数——她和他的全部语义字段。"""
    she = state.get("she") or {}
    he = state.get("he") or {}
    mv = she.get("moving") or {}
    he_mv = he.get("moving") or {}
    return (
        str(she.get("room") or ""), str(she.get("since") or ""), str(she.get("source") or ""),
        str(mv.get("from") or ""), str(mv.get("to") or ""), str(mv.get("until_ts") or ""),
        str(mv.get("started_at") or ""),
        str(he.get("room") or ""), str(he.get("since") or ""), str(he.get("source") or ""),
        str(he.get("confidence") or ""), str(he_mv.get("from") or ""),
        str(he_mv.get("to") or ""), str(he_mv.get("until_ts") or ""),
        str(he_mv.get("started_at") or ""),
        str((state.get("events_today") or {}).get("moves") or 0),
        str((state.get("events_today") or {}).get("day") or ""),
    )
