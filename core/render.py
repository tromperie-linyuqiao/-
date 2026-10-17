"""注入渲染层：空间底色块 + 门控 + 出站清洗。纯函数。"""
from __future__ import annotations

import re
import math
from html import escape
from typing import Any

from .. import CONFIG
from . import traverse


def room_label(manor: dict[str, Any], room_id: str | None) -> str | None:
    if not room_id:
        return None
    for r in manor.get("rooms") or []:
        if str(r.get("id")) == room_id:
            return str(r.get("label") or room_id)
    return room_id


# ---- 距离/ETA 分档（文献：LLM 对数轴编码，精确秒与坐标不可分辨——分档才可读） ----

def _floor_of(manor: dict[str, Any], room_id: str) -> str | None:
    for r in manor.get("rooms") or []:
        if str(r.get("id")) == room_id:
            return str(r.get("floor"))
    return None


def distance_band(manor: dict[str, Any], a: str | None, b: str | None) -> str:
    """距离分档：同室/隔壁/同层远处/异层。"""
    if not a or not b:
        return "不明"
    if a == b:
        return "同室"
    secs = traverse.travel_seconds(manor, a, b)
    if secs is None:
        return "不明"
    graph_edge = any(
        (e.get("a") == a and e.get("b") == b) or (e.get("a") == b and e.get("b") == a)
        for e in manor.get("edges") or []
    )
    if graph_edge or (secs is not None and secs <= 8):
        return "隔壁"
    if secs is not None and _floor_of(manor, a) == _floor_of(manor, b):
        return "同层远处"
    return "异层"


def parse_eta_thresholds(value: Any) -> tuple[int, int] | None:
    """ETA 分档阈值解析（0.27.9 面板化）："15,60" → (15, 60)。
    要求两段正整数且严格升序、≤3600；坏值返回 None（调用方回内置默认 15/60）。"""
    text = str(value or "").strip()
    if not text:
        return None
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 2:
        return None
    try:
        a, b = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return None
    if 0 < a < b <= 3600:
        return (a, b)
    return None


def eta_band(seconds: int | None, thresholds: tuple[int, int] | None = None) -> str:
    th = (15, 60) if thresholds is None else (int(thresholds[0]), int(thresholds[1]))
    if seconds is None:
        return "得走一阵"
    if seconds <= th[0]:
        return "马上就到"
    if seconds <= th[1]:
        return "还要一会儿"
    return "得走一阵"


def freshness_prefix(who: dict[str, Any], now: float) -> str:
    """时间衰减表达：刚变更（<60s）→"刚走进"，否则中性"在"。"""
    since = parse_iso_local(who.get("since"))
    if since is not None and 0 <= now - since <= 60 and not who.get("moving"):
        return "刚到"
    return ""


def _props_hint(manor: dict[str, Any], room_id: str | None) -> str:
    """她所在房间的家具摘要（前 4 件取"（"前名称）：给模型可互动的道具，控长防预算溢出。"""
    if not room_id:
        return ""
    for r in manor.get("rooms") or []:
        if str(r.get("id")) == room_id:
            names = [str(p).split("（")[0] for p in (r.get("props") or [])][:4]
            text = "·".join(n for n in names if n)
            return f"（{text}）" if text else ""
    return ""


AMBIANCE_HINT_CHARS = 18   # B5：刚到一室的氛围摘要长度（截断加省略号）


def _ambiance_hint(manor: dict[str, Any], room_id: str | None,
                   limit: int = AMBIANCE_HINT_CHARS) -> str:
    """B5 氛围摘要：该房 ambiance 的前 limit 字符，超长截断加省略号；无 ambiance 返回空。
    只在"刚到"时由 render_injection 追加（稳定在房不带——控制字符预算）。"""
    if not room_id:
        return ""
    for r in manor.get("rooms") or []:
        if str(r.get("id")) == room_id:
            amb = str(r.get("ambiance") or "").strip()
            if not amb:
                return ""
            return amb[:limit] + "…" if len(amb) > limit else amb
    return ""


def parse_iso_local(v: Any) -> float | None:
    if not v:
        return None
    try:
        import datetime as dt
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _suffix(source: str | None) -> str:
    return {"rp": "", "schedule": "", "fallback": "", "manual": "", "default": ""}.get(str(source or ""), "")


def render_injection(state: dict[str, Any], manor: dict[str, Any], now: float,
                     user_name: str = "", eta_thresholds: tuple[int, int] | None = None,
                     hearing_same_floor: str | None = None,
                     hearing_adjacent: str | None = None,
                     max_chars: int | None = None) -> str:
    """渲染空间底色块（Generative Agents 裁剪式：当前房细节多、远处条目砍成两三字，≤max_chars）。

    0.27.9 面板化：eta_thresholds 覆盖 ETA 分档阈值（None=内置 15/60）；
    hearing_same_floor / hearing_adjacent 覆盖听觉规则文案（None=内置措辞，保持历史输出逐位一致）。"""
    limits = [CONFIG.SP_RENDER_MAX_CHARS if max_chars is None else int(max_chars)]
    if (manor.get("render") or {}).get("max_chars") is not None:
        limits.append(int(manor["render"]["max_chars"]))
    max_chars = max(0, min(limits))
    she, he = state.get("she") or {}, state.get("he") or {}
    role = str((manor.get("characters") or {}).get("she_name") or "她")
    she_label = room_label(manor, she.get("room")) or "?"
    he_label = room_label(manor, he.get("room"))
    parts: list[str] = []
    if she.get("moving"):
        mv = she["moving"]
        try:
            secs = max(0, math.ceil(float(mv.get("until_ts")) - now))
        except (TypeError, ValueError, OverflowError):
            secs = traverse.travel_seconds(manor, str(mv.get("from")), str(mv.get("to")))
        parts.append(f"{role}正在从{room_label(manor, mv.get('from'))}去{room_label(manor, mv.get('to'))}（{eta_band(secs, thresholds=eta_thresholds)}）")
    elif she.get("room"):
        fresh = freshness_prefix(she, now)
        hint = _props_hint(manor, she.get("room"))
        # B5：刚到一室且该房有 ambiance → 家具摘要后追加分号+氛围前 18 字符；稳定在房不带
        amb = _ambiance_hint(manor, she.get("room")) if fresh == "刚到" else ""
        if amb:
            amb = "；" + amb
        parts.append(f"{role}{fresh}{'在' if fresh != '刚到' else ''}{she_label}{hint}{amb}")
    if he.get("moving"):
        parts.append("他正在走动")
    elif he_label and he.get("room"):
        band = distance_band(manor, she.get("room"), he.get("room"))
        if band == "同室":
            # 她正移动离开该房（she.room 仍是出发地）时"他在身边"是伪陈述——改为"他还在"
            if she.get("moving"):
                parts.append(f"他还在{he_label}")
            else:
                parts.append("他在身边")
        elif band == "隔壁":
            # 听觉规则（B6）：可闻动静——文案面板化（sp_hearing_adjacent），None 保持历史措辞
            if hearing_adjacent:
                parts.append(f"他在{he_label}（{hearing_adjacent}）")
            else:
                parts.append(f"他在隔壁{he_label}（可闻动静）")
        elif band == "异层":
            # 听觉规则（B6）：隔层难闻声息——文案面板化（sp_hearing_same_floor），None 保持历史措辞
            if hearing_same_floor:
                parts.append(f"他在{he_label}（异层，{hearing_same_floor}）")
            else:
                parts.append(f"他在{he_label}（异层，隔层难闻声息）")
        else:
            parts.append(f"他在{he_label}（{band}）")
    elif she.get("room") and not she.get("moving"):
        # 他位置兜底（0.32.0，与 read_presence 的 he_inferred 同口径）：he.room 未知而她已落位
        # → 推断他在她身边；措辞与正常同室完全一致，不暴露推断细节（白名单私聊的默认假设）
        parts.append("他在身边")
    if float(he.get("confidence") or 0) < 0.5 and he.get("room"):
        parts = [p for p in parts if not p.startswith("他")]
        parts.append("他与她可能不在一处")
    header = f'<空间 角色="{escape(str((manor.get("characters") or {}).get("she_name") or "她"), quote=True)}">'
    guard = "（临时快照，随轮刷新）"
    text = header + "\n" + "｜".join([*parts, guard]) + "\n</空间>"
    if len(text) <= max_chars:
        return text
    # 先删房间装饰，保留移动目标、双方位置与不确定性；不写半个房间名。
    short_parts = [re.sub(r"（[^）]*）", "", p).split("；", 1)[0] for p in parts]
    for count in range(len(short_parts), 0, -1):
        text = header + "\n" + "｜".join([*short_parts[:count], guard]) + "\n</空间>"
        if len(text) <= max_chars:
            return text
    return ""


def gate(state: dict[str, Any], now: float, message_text: str,
         manor: dict[str, Any], last_change_ts: float | None) -> bool:
    """注入门控（规范 4.3 四条件，满足其一）：①近 10 分钟有变化 ②消息含房间词
    ③她正在移动 ④位置类问题词。"""
    if last_change_ts is not None and 0 <= now - last_change_ts <= CONFIG.SP_INJECT_RECENT_CHANGE_SECONDS:
        return True
    she = state.get("she") or {}
    if she.get("moving"):
        return True
    if any(w in str(message_text or "") for w in CONFIG.SP_GATE_ROOM_WORDS):
        return True
    from . import extract as ex
    return ex.text_has_room_word(str(message_text or ""), manor)


# ---- 出站清洗（vb sanitize 模式：标签对优先，孤立标签次之，全角变体兜底） ----
_OUTBOUND_TAG_RE = re.compile(
    r"[ \t]*[<＜]空间(?=[\s>＞])[^>＞]*[>＞][\s\S]*?[<＜]/空间[>＞][ \t]*\n?"
    r"|[ \t]*[<＜]/?空间(?=[\s>＞])[^>＞]*[>＞][ \t]*\n?"
)


def sanitize_outbound_text(text: str) -> str:
    """剥掉出站文本中的空间标签残留。"""
    if not text or ("<" not in text and "＜" not in text):
        return text
    return _OUTBOUND_TAG_RE.sub("", text)
