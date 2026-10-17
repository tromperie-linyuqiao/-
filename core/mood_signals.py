"""Local simulated signals and owned snapshots only.
No implicit access to another plugin's files. External observations use Presence providers;
legacy mood/reading/verbal readers degrade to unavailable instead of inventing facts.
"""
from __future__ import annotations

import json
import math
import os
import re
import time as _time
from typing import Any

from .. import CONFIG


def _read_json(path: str) -> Any | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _plugin_data_dir(data_dir: str) -> str:
    """存在层合并 0.27.0：心绪与身体/空间同插件——data_dir 即 vb 的 plugin_data 根。"""
    return str(data_dir)




def _collect_text(obj: Any, depth: int = 0) -> str:
    """递归收集 WEATHER_TEXT_KEYS 命中字段的文本（雨词识别用）。"""
    if depth > 4 or not isinstance(obj, dict):
        return ""
    parts = []
    for k, v in obj.items():
        if isinstance(v, str) and k in CONFIG.MO_WEATHER_TEXT_KEYS:
            parts.append(v)
        elif isinstance(v, dict):
            parts.append(_collect_text(v, depth + 1))
        elif isinstance(v, list):
            # 终审修复：list 分支此前不可达（被 dict|list 合并分支吞掉）——嵌套数组里的雨词全漏
            parts.append(" ".join(_collect_text(x, depth + 1) if isinstance(x, dict)
                                  else (x if isinstance(x, str) else "")
                                  for x in v))
    return " ".join(parts)


def read_weather(data_dir: str, now: float | None = None) -> dict[str, Any] | None:
    """天气：本插件 weather/ 下最新 *.json（排除 .names.json / .tmp）。
    0.29.1 兜底：无快照（用户还没问过天气）不返回 None——返回 available=False 的未激活形态，
    规则面板能区分"信号未激活"与"信号不存在"；is_rain=None 让 eq/ne 均不误命中。
    0.32.0 TTL：快照超过 MO_WEATHER_TTL_SECONDS（6h）未更新 → 过期不当真——available=False、
    is_rain=None、stale=True（雨是几小时前的旧闻，规则不再据此触发）。"""
    inner = os.path.join(_plugin_data_dir(data_dir), "weather")
    folder = inner
    unavailable = {"available": False, "city": "", "condition": "",
                   "is_rain": None, "updated_at": "", "stale": False}
    try:
        names = [n for n in os.listdir(folder)
                 if n.endswith(".json") and not n.endswith(".names.json") and not n.endswith(".tmp")]
    except OSError:
        return unavailable
    best: tuple[float, str] | None = None
    for n in names:
        try:
            mtime = os.stat(os.path.join(folder, n)).st_mtime
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, n)
    if best is None:
        return unavailable
    # 自带工具的权威观测固定写 weather_last.json；辅助城市表/其他 JSON 不应抢占天气。
    canonical = os.path.join(folder, CONFIG.WEATHER_SNAPSHOT_FILE)
    if os.path.isfile(canonical):
        try:
            best = (os.stat(canonical).st_mtime, CONFIG.WEATHER_SNAPSHOT_FILE)
        except OSError:
            return unavailable
    data = _read_json(os.path.join(folder, best[1]))
    if not isinstance(data, dict):
        return unavailable
    now = _time.time() if now is None else float(now)
    observed_at = _weather_observed_at(data.get("updated_at"))
    future = observed_at is not None and observed_at - now > 300
    # mtime 与可解析的观测时间取较早值，复制/恢复旧文件无法续期旧天气。
    observed_at = min(best[0], observed_at) if observed_at is not None else best[0]
    age = now - observed_at
    stale = future or age >= CONFIG.MO_WEATHER_TTL_SECONDS or age < -300
    if stale:
        # 过期：available=False 封死 eq/ne/changed（规则引擎 0.29.1 机制），is_rain 不再当真
        return {"available": False, "city": str(data.get("city") or ""),
                "condition": "", "is_rain": None,
                "observed_at": observed_at,
                "updated_at": data.get("updated_at") or "", "stale": True}
    current = [data.get(k) for k in ("weather", "condition") if isinstance(data.get(k), str)]
    text = " ".join(current).lower() if current else _collect_text(data).lower()
    if not text.strip():
        return unavailable
    return {
        "available": True,
        "city": str(data.get("city") or ""),
        "condition": text[:120],
        "is_rain": any(w in text for w in CONFIG.MO_RAIN_WORDS),
        "updated_at": data.get("updated_at") or "",
        "observed_at": observed_at,
        "stale": False,
    }


def _weather_observed_at(value: Any) -> float | None:
    """自带快照的 naive ISO 按生成它的容器本地时区解释；显式 offset 保留。"""
    if not value or isinstance(value, bool):
        return None
    try:
        import datetime as dt
        stamp = (float(value) if isinstance(value, (int, float))
                 else dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
        return stamp if math.isfinite(stamp) else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _body_hint_boost(data_dir: str, now: float | None = None) -> float:
    """身体→情绪（0.30.0）：mood/body_mood_hint.json 的 recent warm hint 加成——
    overload 进余韵时 main 写入 {ts, hint:"warm"}，TTL 内线性衰减（刚结束时 +0.15，2h 归零）。
    坏文件/过期/未来时间戳一律 0.0。"""
    from .decay import parse_ts   # 局部导入避免加载序耦合（decay 不依赖本模块，无环）
    data = _read_json(os.path.join(_plugin_data_dir(data_dir), CONFIG.MO_DATA_SUBDIR,
                                   CONFIG.MO_BODY_HINT_FILE))
    if not isinstance(data, dict) or str(data.get("hint") or "") != "warm":
        return 0.0
    ts = parse_ts(data.get("ts"))
    if ts is None or not math.isfinite(ts):
        return 0.0
    now = _time.time() if now is None else float(now)
    age = now - ts
    if age < 0.0 or age >= CONFIG.MO_BODY_HINT_TTL:
        return 0.0
    return CONFIG.MO_BODY_HINT_BOOST * (1.0 - age / CONFIG.MO_BODY_HINT_TTL)


def read_mood(data_dir: str, now: float | None = None,
              max_age_seconds: float = 2 * 86400) -> dict[str, Any]:
    """Only this plugin's simulated body hint is read; no external user mood is guessed."""
    now = _time.time() if now is None else float(now)
    boost = _body_hint_boost(data_dir, now)
    return {"available": boost > 0, "score": boost if boost > 0 else None,
            "confidence": 0.3 if boost > 0 else 0.0, "day": "", "stale": False,
            "source": "body_hint" if boost > 0 else ""}


def read_reading(data_dir: str) -> dict[str, Any]:
    """No implicit bookshelf access. Reading providers use the Presence contract."""
    return {"available": False, "book_id": "", "book_title": "",
            "chapter_index": None, "chapter_count": 0}


def _latest_body_stem(folder: str, all_names: list[str]) -> str | None:
    """state 目录里最新 mtime 的身体状态文件 stem（<character>_<session>.json，排除
    .space.json / bindings.json / .names.json / .tmp）——presence 兜底的真值来源。"""
    best: tuple[float, str] | None = None
    for n in all_names:
        if not n.endswith(".json") or n.endswith(".tmp"):
            continue
        if n.endswith(".space.json") or n == "bindings.json" or n.endswith(".names.json"):
            continue
        try:
            mtime = os.stat(os.path.join(folder, n)).st_mtime
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, n)
    return None if best is None else best[1][: -len(".json")]


def _has_profile_override(data_dir: str) -> bool:
    """plugin_data/profiles/ 下有 profile 覆盖文件（排除 .names.json/.bak/.tmp）——插件被编辑使用过的迹象。"""
    try:
        names = os.listdir(os.path.join(_plugin_data_dir(data_dir), "profiles"))
    except OSError:
        return False
    return any(n.endswith(".json") and not n.endswith(".names.json") and not n.endswith(".tmp")
               for n in names)


def read_presence(data_dir: str, manor: dict | None = None,
                  session_key: str | None = None) -> dict[str, Any] | None:
    """存在层：最新 .space.json（她房/移动）+ 同 key 身体文件（phase/arousal）。
    0.29.1 兜底（线上根因修复）：白名单会话未触发过空间事件时 .space.json 不存在，
    原实现返回 None → presence_room_changed/longing_invite/mood_warm 等规则全部短路。
    现按三层迹象兜底（插件在服役即给中性默认）：身体状态文件（phase/arousal 取真值）>
    profile 覆盖层 > 已加载庄园 manor；she_room=None 是"changed"的可比基线——
    她首次落位（space 文件生成）即从 None 变为具体房间，规则可触发。
    三层迹象全无（插件未使用）仍返回 None（信号不存在）。"""
    folder = os.path.join(_plugin_data_dir(data_dir), CONFIG.SP_STATE_DIR_NAME)
    try:
        all_names = [n for n in os.listdir(folder) if n.endswith(".json") and not n.endswith(".tmp")]
    except OSError:
        all_names = []
    scoped = session_key is not None
    if scoped:
        session_key = str(session_key)
        if not re.fullmatch(CONFIG.SP_SESSION_RE, session_key):
            return None
        all_names = [n for n in all_names
                     if n in (session_key + ".json", session_key + ".space.json")]
    best: tuple[float, str] | None = None
    for n in all_names:
        if not n.endswith(".space.json"):
            continue
        try:
            mtime = os.stat(os.path.join(folder, n)).st_mtime
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, n)
    sp: dict[str, Any] = {}
    stem: str | None = None
    from_space = False
    if best is not None:
        sp = _read_json(os.path.join(folder, best[1]))
        sp = sp if isinstance(sp, dict) else {}   # 坏 space 文件也走兜底（不再 null）
        stem = best[1][: -len(".space.json")]
        from_space = isinstance(sp.get("she"), dict)
    else:
        stem = _latest_body_stem(folder, all_names)
        if stem is None and not _has_profile_override(data_dir) and not manor:
            return None
    she = sp.get("she") if isinstance(sp.get("she"), dict) else {}
    he = sp.get("he") if isinstance(sp.get("he"), dict) else {}
    she_room = str(she.get("room") or "")
    if manor and she_room not in _presence_rooms(manor):
        she_room = ""
    out: dict[str, Any] = {
        "available": True,
        # 无 space 文件时 she_room=None（未知）：作为 changed 基线可比较；有 space 文件保持 str 语义
        "she_room": (she_room or None),
        "she_moving": bool(she.get("moving")),
        "he_moving": bool(he.get("moving")),
        "body_phase": "",
        "arousal": 0.0,
    }
    if stem is not None:
        body = _read_json(os.path.join(folder, stem + ".json"))
        if isinstance(body, dict):
            out["body_phase"] = str(body.get("phase") or "")
            try:
                value = float(body.get("arousal") or 0.0)
                out["arousal"] = round(max(0.0, min(1.0, value)), 2) if math.isfinite(value) else 0.0
            except (TypeError, ValueError):
                pass
    # 环境底色（0.27.2）：房间 mood_bias → env_valence/env_arousal/mood_word + 同室判定
    from . import space as _space
    if manor:
        v, a = _space.room_mood_bias(manor, out["she_room"])
        out["env_valence"] = v
        out["env_arousal"] = a
        out["mood_word"] = _space.mood_word(v, a)
        declared_he_room = str(he.get("room") or "")
        he_room = declared_he_room
        if he_room not in _presence_rooms(manor):
            he_room = ""
        he_inferred = False
        if not declared_he_room and out["she_room"] and not out["she_moving"] and not out["he_moving"]:
            # 他位置兜底（0.32.0）：he.room 未知而她已落位 → 推断他在她身边——
            # same_room 从恒 False 变为可达 True；confidence=0.5（低于显式陈述），
            # he_inferred=True 标注推断来源（渲染层措辞与正常同室一致，不暴露推断细节）
            he_room = str(out["she_room"])
            he_inferred = True
        out["he_room"] = he_room
        out["he_inferred"] = he_inferred
        if he_inferred:
            out["he_confidence"] = CONFIG.MO_HE_INFER_CONFIDENCE
        try:
            confidence = float(he.get("confidence", 1.0))
            he_known = math.isfinite(confidence) and confidence >= 0.5
        except (TypeError, ValueError):
            he_known = False
        out["same_room"] = bool(he_room) and he_room == (out["she_room"] or "") \
            and not out["she_moving"] and not out["he_moving"] and (he_inferred or he_known)
    return out


def _presence_rooms(manor: dict[str, Any]) -> set[str]:
    from .space import known_rooms
    return known_rooms(manor)






def read_verbal(data_dir: str, whitelist: list[str] | tuple[str, ...] | None = None,
                emotions: Any = None, now: float | None = None,
                ttl: float | None = None) -> dict[str, Any]:
    """No private conversation file access or invented emotional activity."""
    return {"available": False, "emotion": "", "is_intimate": False}


def read_all(data_dir: str, manor: dict | None = None,
             session_key: str | None = None, now: float | None = None) -> dict[str, dict[str, Any] | None]:
    return {"weather": read_weather(data_dir, now), "mood": read_mood(data_dir, now),
            "reading": read_reading(data_dir), "presence": read_presence(data_dir, manor, session_key)}
