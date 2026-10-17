"""状态读写层。每角色每会话一份 JSON：<data_dir>/state/<character>_<session>.json。

纪律：小文件毫秒级写，原子写（tmp→replace）防写坏；load 后必须过 normalize() 补缺字段，
防止旧文件缺键被裸索引炸掉。本模块不 import astrbot，路径由调用方传入。
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import tempfile
from typing import Any

from .. import CONFIG
from .decay import iso_ts, parse_ts
from .persist_contact import contact_semantics

_WRITE_LOCK = threading.RLock()


def _atomic_write(path: str, content: bytes) -> None:
    """Unique temporary files keep overlapping instances from sharing a writer."""
    with _WRITE_LOCK:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=os.path.basename(path) + ".",
                                          suffix=".tmp", dir=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _atomic_json(path: str, data: Any, *, compact: bool = False) -> None:
    text = json.dumps(data, ensure_ascii=False,
                      separators=(",", ":") if compact else None,
                      indent=None if compact else 1)
    _atomic_write(path, text.encode("utf-8"))


def _valid_ts(value: Any) -> Any:
    """保留合法旧时间戳的原形（包括 0），坏锚点统一为空。"""
    return value if parse_ts(value) is not None else None


def new_state(character: str, session: str) -> dict[str, Any]:
    return {
        "character": str(character),
        "session": str(session),
        "schema": CONFIG.STATE_SCHEMA,
        "phase": "building",
        "phase_until": None,
        "arousal": 0.0,
        "tolerance": 0.0,
        "parts": {},
        "active_contact": {},
        "updated_at": iso_ts(0.0),
    }


def normalize(state: dict[str, Any] | None, profile: dict[str, Any]) -> dict[str, Any]:
    """补缺字段 + 清掉 profile 已不存在的部位条目。幂等，load 后必调。"""
    if not isinstance(state, dict):
        state = {}
    base = new_state(profile.get("id") or "?", state.get("session") or "?")
    merged = dict(base)
    for key in ("character", "session", "phase", "phase_until", "arousal", "tolerance", "updated_at"):
        if state.get(key) is not None:
            merged[key] = state[key]
    try:
        merged["schema"] = int(state.get("schema") or CONFIG.STATE_SCHEMA)
    except (TypeError, ValueError, OverflowError):
        merged["schema"] = CONFIG.STATE_SCHEMA
    if not isinstance(merged["phase"], str) or merged["phase"] not in CONFIG.PHASE_SENS_MULT:
        merged["phase"] = "building"
    merged["updated_at"] = _valid_ts(merged["updated_at"])
    merged["phase_until"] = _valid_ts(merged["phase_until"])
    if merged["phase"] in ("afterglow", "cooldown") and merged["phase_until"] is None:
        # 无终点的余韵/不应期不能永远锁住身体；旧损坏状态安全回平常。
        merged["phase"] = "building"
    raw_today = state.get("events_today") if isinstance(state.get("events_today"), dict) else {}
    merged["events_today"] = {
        "day": str(raw_today.get("day") or ""),
        "count": int(raw_today["count"]) if isinstance(raw_today.get("count"), int) and raw_today.get("count") >= 0 else 0,
    }
    raw_hist = state.get("history") if isinstance(state.get("history"), list) else []
    clean_hist = []
    for h in raw_hist[-CONFIG.ADAPT_HISTORY_LIMIT:]:
        if isinstance(h, dict) and h.get("part"):
            clean_hist.append({"t": str(h.get("t") or ""), "part": str(h.get("part")),
                               "old": _safe_float(h.get("old"), 0.0, 0.0, 1.0),
                               "new": _safe_float(h.get("new"), 0.0, 0.0, 1.0),
                               "src": str(h.get("src") or "")[:12]})
    merged["history"] = clean_hist   # 0.30.0 扩容 20→200：长期身体记忆的数据面（旧文件兼容裁剪）
    # 语言调情→身体兴奋（0.30.0）：verbal_arousal 0-1 + 锚点时间戳
    merged["verbal_arousal"] = _safe_float(state.get("verbal_arousal"), 0.0, 0.0, 1.0)
    merged["verbal_at"] = _valid_ts(state.get("verbal_at"))
    # 长期身体记忆（0.30.0）：{pid: 0.8-1.3}——可解析的越界钳制、不可解析的剔除（accessor 无键回 1.0）
    raw_ltm = state.get("longterm_mult") if isinstance(state.get("longterm_mult"), dict) else {}
    clean_ltm: dict[str, float] = {}
    for k, v in raw_ltm.items():
        pid = str(k).strip()
        if not pid:
            continue
        try:
            x = float(v)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(x):
            continue
        clean_ltm[pid] = max(CONFIG.ADAPT_MULT_RANGE[0], min(CONFIG.ADAPT_MULT_RANGE[1], x))
    merged["longterm_mult"] = clean_ltm
    # 日级适应的防重入键需要跨进程保留，否则同一天重启会再执行一轮。
    longterm_day = str(state.get("longterm_day") or "")
    merged["longterm_day"] = longterm_day if re.fullmatch(r"\d{4}-\d{2}-\d{2}", longterm_day) else ""
    # 主动触碰意图（0.30.0）：{ts, hint}——坏值当无
    raw_pi = state.get("proactive_intent") if isinstance(state.get("proactive_intent"), dict) else {}
    if raw_pi.get("ts") and str(raw_pi.get("hint") or "").strip():
        merged["proactive_intent"] = {"ts": str(raw_pi.get("ts")), "hint": str(raw_pi["hint"])[:40]}
    else:
        merged["proactive_intent"] = None
    # 词级冷却账本（0.29.0）：{词: ISO 终点}——坏值当空，词 ≤32 字符防膨胀
    raw_wcd = state.get("word_cd") if isinstance(state.get("word_cd"), dict) else {}
    wcd_items = [(str(w)[:32], str(v)) for w, v in raw_wcd.items()
                 if str(w).strip() and v]
    merged["word_cd"] = dict(wcd_items[:200])
    known_ids = {str(p.get("id")) for p in profile.get("parts") or []
                 if isinstance(p, dict) and p.get("id")}
    parts = state.get("parts") if isinstance(state.get("parts"), dict) else {}
    clean: dict[str, dict[str, Any]] = {}
    for pid in known_ids:
        raw = parts.get(pid) if isinstance(parts.get(pid), dict) else {}
        clean[pid] = {
            "value": _safe_float(raw.get("value"), 0.0, 0.0, 1.0),
            "last_event_at": _valid_ts(raw.get("last_event_at")),
            "last_react_tier": raw.get("last_react_tier") or None,
            "cool_until": _valid_ts(raw.get("cool_until")),
            "last_event_type": str(raw.get("last_event_type") or "")[:12],
            "last_material": str(raw["last_material"])[:16] if raw.get("last_material") else None,
            "last_material_at": _valid_ts(raw.get("last_material_at")),
            "trend_delta": _safe_float(raw.get("trend_delta"), 0.0, -1.0, 1.0),
            "streak_count": int(raw["streak_count"]) if isinstance(raw.get("streak_count"), int) and raw.get("streak_count") >= 0 else 0,
            "streak_since": _valid_ts(raw.get("streak_since")),
            "sens_boost": _safe_float(raw.get("sens_boost"), 0.0, 0.0, CONFIG.SENS_CAP),
        }
    merged["parts"] = clean
    # 持续接触（0.31.0）：{pid: {action,s,since,last_renew,last_msg,last_tick}}——
    # 未知部位/坏条目/s≤0/last_renew 不可解析（无法判过期）一律剔除，旧文件缺键补空 dict
    raw_ac = state.get("active_contact") if isinstance(state.get("active_contact"), dict) else {}
    clean_ac: dict[str, dict[str, Any]] = {}
    for k, v in raw_ac.items():
        pid = str(k).strip()
        if not pid or pid not in known_ids or not isinstance(v, dict):
            continue
        s = _safe_float(v.get("s"), 0.0, 0.0, 1.0)
        if s <= 0.0:
            continue
        entry = {"action": str(v.get("action") or "")[:12], "s": s,
                 "since": _valid_ts(v.get("since")), "last_renew": _valid_ts(v.get("last_renew")),
                 "last_msg": _valid_ts(v.get("last_msg")), "last_tick": _valid_ts(v.get("last_tick"))}
        if parse_ts(entry["last_renew"]) is None:
            continue   # 无可解析续期锚点的接触无法判过期——当坏条目丢弃
        entry.update(contact_semantics(v))
        clean_ac[pid] = entry
    merged["active_contact"] = clean_ac
    merged["arousal"] = _safe_float(merged["arousal"], 0.0, 0.0, 1.0)
    merged["tolerance"] = _safe_float(merged["tolerance"], 0.0, 0.0, CONFIG.TOLERANCE_MAX)
    return merged


def _safe_float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if x != x or x in (float("inf"), float("-inf")):   # NaN/Inf 不走钳制链（NaN 比较恒 False 会顶到上界）
        return default
    return max(lo, min(hi, x))


def state_signature(state: dict[str, Any]) -> tuple:
    """变化指纹（不含 updated_at）：tick 空推进跳写盘的判定依据。
    数值 round 4 位——衰减引起的末位漂移不触发写盘（指数衰减从旧锚点补算无损）。"""
    try:
        tol = round(float(state.get("tolerance") or 0.0), 4)
    except (TypeError, ValueError, OverflowError):
        tol = 0.0
    parts = state.get("parts") or {}
    part_sig = tuple(sorted(
        (str(pid), round(float((p.get("value") if isinstance(p, dict) else 0) or 0), 4),
         str(p.get("cool_until") or "") if isinstance(p, dict) else "",
         str(p.get("last_react_tier") or "") if isinstance(p, dict) else "")
        for pid, p in parts.items()
    ))
    ev_today = state.get("events_today") if isinstance(state.get("events_today"), dict) else {}
    try:
        ev_count = int(ev_today.get("count") or 0)
    except (TypeError, ValueError, OverflowError):
        ev_count = 0
    # 0.30.0：verbal_arousal / longterm_mult / proactive_intent 进指纹——
    # 语言爬升、日适应、意图落账都要触发写盘（tick 跳写判定才不失明）
    try:
        verbal = round(float(state.get("verbal_arousal") or 0.0), 4)
    except (TypeError, ValueError, OverflowError):
        verbal = 0.0
    raw_ltm = state.get("longterm_mult") if isinstance(state.get("longterm_mult"), dict) else {}
    ltm_sig = tuple(sorted((str(k), round(float(v), 4)) for k, v in raw_ltm.items()
                           if isinstance(v, (int, float))))
    pi = state.get("proactive_intent") if isinstance(state.get("proactive_intent"), dict) else {}
    pi_sig = (str(pi.get("ts") or ""), str(pi.get("hint") or "")[:14])
    # 0.31.0：active_contact 进指纹——接触建立/续期/过期清除都要触发写盘（tick 跳写判定不失明）
    raw_ac = state.get("active_contact") if isinstance(state.get("active_contact"), dict) else {}
    # 通道锚点和材质语义同样要落盘；小增益被 round 消掉时，重启不能再发一次 tick。
    ac_keys = ("action", "s", "since", "last_renew", "last_msg", "last_tick",
               "type", "material", "method", "freq_hz", "speed_cm_s", "depth", "temp_c")
    ac_sig = tuple(sorted((str(k), tuple(str(v.get(key)) if v.get(key) is not None else ""
                                         for key in ac_keys))
                          for k, v in raw_ac.items() if isinstance(v, dict)))
    return (str(state.get("phase") or ""), str(state.get("phase_until") or ""), tol, part_sig,
            str(ev_today.get("day") or ""), ev_count, verbal, ltm_sig,
            str(state.get("longterm_day") or ""), pi_sig, ac_sig)


_PROFILE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_CHARACTER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def validate_parts(parts: Any) -> tuple[bool, str]:
    """profile.parts 全量校验：id/label/sens/half_life/aliases/radiate +
    0.29.1 部位级细调键（gain_mult/tolerance_mult/windup_mult/type_mults/material_affinity，
    全部可选、缺省中性）。返回 (ok, 错误信息)。"""
    if not isinstance(parts, list) or not parts:
        return (False, "parts 必须是非空列表")
    ids: set[str] = set()
    for p in parts:
        if not isinstance(p, dict):
            return (False, "parts 元素必须是对象")
        pid = str(p.get("id") or "")
        if not _PROFILE_ID_RE.match(pid):
            return (False, f"部位 id 非法: {pid!r}（小写字母开头，仅小写字母/数字/下划线）")
        if pid in ids:
            return (False, f"部位 id 重复: {pid}")
        ids.add(pid)
        if not str(p.get("label") or "").strip():
            return (False, f"{pid}: label 不能为空")
        try:
            sens = float(p.get("sens"))
        except (TypeError, ValueError, OverflowError):
            return (False, f"{pid}: sens 必须是数字")
        if not 0 < sens <= 10:
            return (False, f"{pid}: sens 须在 0-10")
        try:
            hl = float(p.get("half_life"))
        except (TypeError, ValueError, OverflowError):
            return (False, f"{pid}: half_life 必须是数字")
        if not 5 <= hl <= 3600:
            return (False, f"{pid}: half_life 须在 5-3600 秒")
        aliases = p.get("aliases")
        if not isinstance(aliases, list) or not all(str(a).strip() for a in aliases):
            return (False, f"{pid}: aliases 必须是非空字符串列表")
        radiate = p.get("radiate") or {}
        if not isinstance(radiate, dict):
            return (False, f"{pid}: radiate 必须是对象")
        for rid, ratio in radiate.items():
            if str(rid) == pid:
                return (False, f"{pid}: radiate 不能指向自身")
            try:
                rf = float(ratio)
            except (TypeError, ValueError, OverflowError):
                return (False, f"{pid}: radiate.{rid} 倍率必须是数字")
            if not 0 < rf <= 1:
                return (False, f"{pid}: radiate.{rid} 倍率须在 0-1")
        # ---- 0.29.1 部位级细调键（缺省即中性；演算层坏值自回中性，这里是保存口的显式拦截）----
        for mkey, rng in (("gain_mult", CONFIG.PART_GAIN_MULT_RANGE),
                          ("tolerance_mult", CONFIG.PART_TOLERANCE_MULT_RANGE),
                          ("windup_mult", CONFIG.PART_WINDUP_MULT_RANGE)):
            if p.get(mkey) is None:
                continue
            try:
                mv = float(p.get(mkey))
            except (TypeError, ValueError, OverflowError):
                return (False, f"{pid}: {mkey} 必须是数字")
            if not rng[0] <= mv <= rng[1]:
                return (False, f"{pid}: {mkey} 须在 {rng[0]:g}-{rng[1]:g}")
        tm = p.get("type_mults")
        if tm is not None:
            if not isinstance(tm, dict):
                return (False, f"{pid}: type_mults 必须是对象")
            for tk, tv in tm.items():
                if str(tk) not in CONFIG.ACTION_WORD_TYPES:
                    return (False, f"{pid}: type_mults 键须为 {'/'.join(CONFIG.ACTION_WORD_TYPES)}")
                try:
                    tvt = float(tv)
                except (TypeError, ValueError, OverflowError):
                    return (False, f"{pid}: type_mults.{tk} 必须是数字")
                if not CONFIG.PART_TYPE_MULT_RANGE[0] <= tvt <= CONFIG.PART_TYPE_MULT_RANGE[1]:
                    return (False, f"{pid}: type_mults.{tk} 须在 "
                                   f"{CONFIG.PART_TYPE_MULT_RANGE[0]:g}-{CONFIG.PART_TYPE_MULT_RANGE[1]:g}")
        ma = p.get("material_affinity")
        if ma is not None:
            if not isinstance(ma, dict) or len(ma) > 64:
                return (False, f"{pid}: material_affinity 必须是对象（≤64 键）")
            for mk, mv2 in ma.items():
                if not str(mk).strip() or len(str(mk)) > 32:
                    return (False, f"{pid}: material_affinity 键须为非空材质 id（≤32 字符）")
                try:
                    mvt = float(mv2)
                except (TypeError, ValueError, OverflowError):
                    return (False, f"{pid}: material_affinity.{mk} 必须是数字")
                if not CONFIG.PART_MATERIAL_AFFINITY_RANGE[0] <= mvt <= CONFIG.PART_MATERIAL_AFFINITY_RANGE[1]:
                    return (False, f"{pid}: material_affinity.{mk} 须在 "
                                   f"{CONFIG.PART_MATERIAL_AFFINITY_RANGE[0]:g}-{CONFIG.PART_MATERIAL_AFFINITY_RANGE[1]:g}")
    for p in parts:
        for rid in (p.get("radiate") or {}):
            if str(rid) not in ids:
                return (False, f"{p.get('id')}: radiate 目标 {rid} 不在部位列表中")
    # 跨部位 label/alias 查重：共享词会让文本抽取对同一句话双计两个部位
    owner: dict[str, str] = {}
    for p in parts:
        for word in [str(p.get("label") or "").strip()] + [str(a).strip() for a in (p.get("aliases") or [])]:
            if not word:
                continue
            if word in owner and owner[word] != str(p.get("id")):
                return (False, f"别名 {word!r} 同时属于 {owner[word]} 与 {p.get('id')}")
            owner[word] = str(p.get("id"))
    return (True, "")


def validate_action_words(words: Any) -> tuple[bool, str]:
    """action_words 校验（0.29.0 兼容两种形态）：非空词键、值 0-1 数字 或
    {s,method,freq_hz,speed_cm_s,depth,temp_c,cooldown_s,type} 参数 dict（键可省）。"""
    if not isinstance(words, dict):
        return (False, "action_words 必须是对象")
    for word, val in words.items():
        if not str(word).strip():
            return (False, "动作词不能为空")
        if isinstance(val, dict):
            try:
                s = float(val.get("s", 0.5))
            except (TypeError, ValueError, OverflowError):
                return (False, f"动作词 {word!r} 的 s 必须是数字")
            if not 0 <= s <= 1:
                return (False, f"动作词 {word!r} 的 s 须在 0-1（0 表示禁用该词）")
            method = str(val.get("method") or "")
            if method and method not in ("stroke", "press", "rub", "tap", "blow"):
                return (False, f"动作词 {word!r} 的 method 须为 stroke/press/rub/tap/blow")
            wtype = str(val.get("type") or "")
            if wtype and wtype not in CONFIG.ACTION_WORD_TYPES:
                return (False, f"动作词 {word!r} 的 type 须为 {'/'.join(CONFIG.ACTION_WORD_TYPES)}")
            for key, (lo, hi) in CONFIG.ACTION_WORD_PARAM_LIMITS.items():
                if val.get(key) is None:
                    continue
                try:
                    x = float(val.get(key))
                except (TypeError, ValueError, OverflowError):
                    return (False, f"动作词 {word!r} 的 {key} 必须是数字")
                if not lo <= x <= hi:
                    return (False, f"动作词 {word!r} 的 {key} 须在 {lo:g}-{hi:g}")
            continue
        try:
            v = float(val)
        except (TypeError, ValueError, OverflowError):
            return (False, f"动作词 {word!r} 强度必须是数字或参数对象")
        if not 0 <= v <= 1:
            return (False, f"动作词 {word!r} 强度须在 0-1（0 表示禁用该词）")
    return (True, "")


def load_names_overlay(data_dir: str, character: str) -> dict[str, dict[str, Any]]:
    """命名层：label/aliases 独立存 <plugin_data>/profiles/<id>.names.json，与参数层分离——
    参数迭代只碰 profile 主文件，不读写名称。坏文件当空。"""
    path = os.path.join(str(data_dir), "profiles", f"{character}.names.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    clean: dict[str, dict[str, Any]] = {}
    for pid, entry in data.items():
        if not isinstance(entry, dict):
            continue
        aliases = entry.get("aliases")
        clean[str(pid)] = {
            "label": str(entry.get("label") or "")[:24],
            "aliases": [str(a)[:16] for a in aliases][:8] if isinstance(aliases, list) else [],
        }
    return clean


def save_names_overlay(data_dir: str, character: str, names: dict[str, Any]) -> tuple[bool, str]:
    """保存命名层（校验：pid 存在由调用方保证；这里校验结构）。"""
    clean: dict[str, dict[str, Any]] = {}
    for pid, entry in (names or {}).items():
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "").strip()
        if not label:
            continue   # 空名不落
        aliases = entry.get("aliases")
        clean[str(pid)] = {"label": label[:24],
                           "aliases": [str(a).strip()[:16] for a in aliases if str(a).strip()][:8]
                           if isinstance(aliases, list) else []}
    overrides_dir = os.path.join(str(data_dir), "profiles")
    path = os.path.join(overrides_dir, f"{character}.names.json")
    try:
        _atomic_json(path, clean, compact=True)
    except OSError as exc:
        return (False, f"写入失败: {exc}")
    return (True, "")


def apply_names_overlay(profile: dict[str, Any], names: dict[str, Any]) -> None:
    """把命名层覆盖到 profile 的 label/aliases（就地，仅已存在部位）。"""
    for part in profile.get("parts") or []:
        entry = names.get(str(part.get("id")))
        if isinstance(entry, dict):
            if str(entry.get("label") or "").strip():
                part["label"] = str(entry["label"])[:24]
            if isinstance(entry.get("aliases"), list):
                part["aliases"] = [str(a)[:16] for a in entry["aliases"]][:8]


def _auto_part_ids(parts: list[Any]) -> list[Any]:
    """id 兜底：空/占位（new_part_N）自动生成唯一 part_N（拼音 slug 由前端生成，这里只兜底）。"""
    taken = {str(p.get("id") or "") for p in parts if isinstance(p, dict)}
    n = 1
    for p in parts:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "").strip()
        if not pid or pid.startswith("new_part_"):
            while f"part_{n}" in taken:
                n += 1
            pid = f"part_{n}"
            taken.add(pid)
            p["id"] = pid
    return parts


def save_profile_override(data_dir: str, base_profile: dict[str, Any],
                          patch: dict[str, Any]) -> tuple[bool, str]:
    """WebUI 编辑落盘：合并 patch 写 plugin_data/profiles/<id>.json（覆盖层，热生效靠调用方重载）。
    base_profile 为内存中的当前生效档案；旧覆盖文件先备份 .bak。
    0.27.9 覆盖层版本化：写入 base_version=CONFIG.VERSION——加载端据此识别旧代覆盖层并做参数迁移。"""
    pid = str((base_profile or {}).get("id") or "")
    if not pid:
        return (False, "base_profile 无 id")
    merged = dict(base_profile or {})
    merged["id"] = pid
    for key in ("action_words", "params"):
        if key in patch:
            ok, err = validate_action_words(patch[key]) if key == "action_words" else (isinstance(patch[key], dict), f"{key} 必须是对象")
            if not ok:
                return (False, err)
            merged[key] = patch[key]
    if "parts" in patch:
        parts = _auto_part_ids(patch["parts"] if isinstance(patch["parts"], list) else [])
        ok, err = validate_parts(parts)
        if not ok:
            return (False, err)
        merged["parts"] = parts
    if "tiers" in patch:
        tiers = patch["tiers"]
        tier_keys = ("notice", "react", "lose", "overload")
        if not isinstance(tiers, list) or len(tiers) != len(tier_keys):
            return (False, "tiers 必须是 4 项列表（notice/react/lose/overload）")
        clean_tiers = []
        previous = -1.0
        for key, tier in zip(tier_keys, tiers):
            if not isinstance(tier, dict) or tier.get("key") != key:
                return (False, f"tiers 的 key 须依次为 {'/'.join(tier_keys)}")
            try:
                minimum = float(tier.get("min"))
            except (TypeError, ValueError, OverflowError):
                return (False, f"tiers.{key}.min 须在 0-1")
            if not 0 <= minimum <= 1 or minimum <= previous:
                return (False, "tiers 的 min 须在 0-1 且严格升序")
            clean_tiers.append({"key": key, "min": minimum})
            previous = minimum
        merged["tiers"] = clean_tiers
    merged["base_version"] = CONFIG.VERSION
    overrides_dir = os.path.join(str(data_dir), "profiles")
    path = os.path.join(overrides_dir, f"{pid}.json")
    with _WRITE_LOCK:
        if os.path.isfile(path):
            # Keep the original in place until its replacement is complete.
            try:
                with open(path, "rb") as f:
                    old_bytes = f.read()
                _atomic_write(path + ".bak", old_bytes)
            except OSError:
                pass
        try:
            _atomic_json(path, merged)
        except OSError as exc:
            return (False, f"写入失败: {exc}")
    return (True, "")


class StateStore:
    """线程内互斥的单进程存储；AstrBot 单事件循环 + 本地锁已足够。

    消耗优化：mtime 缓存——get_or_create 命中缓存（文件未被外部改）时跳过 json.load+normalize，
    返回深拷贝防调用方污染；save 紧凑 JSON 省字节。"""

    def __init__(self, data_dir: str):
        self._dir = os.path.join(str(data_dir), CONFIG.STATE_DIR_NAME)
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}   # key → (mtime_ns, normalize 后状态)
        self._identity_cache: dict[str, tuple[int, tuple[str, str]]] = {}
        os.makedirs(self._dir, exist_ok=True)

    def _path(self, character: str, session: str) -> str:
        name = CONFIG.STATE_FILE_FMT.format(character=str(character), session=str(session))
        return os.path.join(self._dir, name)

    def _mtime(self, path: str) -> int | None:
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return None

    def load(self, character: str, session: str) -> dict[str, Any] | None:
        path = self._path(character, session)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def save(self, character: str, session: str, state: dict[str, Any]) -> None:
        path = self._path(character, session)
        with self._lock, _WRITE_LOCK:
            _atomic_json(path, state, compact=True)
            mtime = self._mtime(path)
            if mtime is not None:
                self._cache[(str(character), str(session))] = (mtime, json.loads(json.dumps(state)))
                self._identity_cache[os.path.basename(path)] = (mtime, (str(character), str(session)))

    def get_or_create(self, character: str, session: str, profile: dict[str, Any]) -> dict[str, Any]:
        character, session = str(character), str(session)
        path = self._path(character, session)
        mtime = self._mtime(path)
        cached = self._cache.get((character, session)) if mtime is not None else None
        if cached and cached[0] == mtime:
            # 文件未改但 profile 可热改；缓存也需按当前部位归一，防已删除部位/接触残留。
            st = normalize(cached[1], profile)
        else:
            raw = self.load(character, session)
            st = normalize(raw, profile)
            if raw is not None and mtime is not None:
                self._cache[(character, session)] = (mtime, json.loads(json.dumps(st)))
        # 文件内容与会话标识错位时（复制/改名），以调用方锚点为准
        st["character"] = character
        st["session"] = session
        return st

    def delete_state(self, character: str, session: str) -> bool:
        """删除一份会话状态（WebUI 重置用）。文件不存在返回 False。"""
        path = self._path(character, session)
        try:
            os.remove(path)
        except OSError:
            return False
        self._cache.pop((str(character), str(session)), None)   # 终审：删状态同步清缓存防慢泄漏
        self._identity_cache.pop(os.path.basename(path), None)
        return True

    def load_bindings(self) -> dict[str, str]:
        """session→character 绑定表；坏文件当空表，绝不抛出。"""
        path = os.path.join(self._dir, CONFIG.BINDINGS_FILE)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if str(k) and str(v)}

    def save_bindings(self, bindings: dict[str, str]) -> None:
        path = os.path.join(self._dir, CONFIG.BINDINGS_FILE)
        with self._lock, _WRITE_LOCK:
            _atomic_json(path, dict(bindings or {}))

    def list_states(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        try:
            names = os.listdir(self._dir)
        except OSError:
            return out
        for name in names:
            if not name.endswith(".json") or name.endswith(".tmp"):
                continue
            stem = name[: -len(".json")]
            # 旧无身份文件保留首 '_' 的历史拆法；新状态以内置身份和完整文件名校验消歧。
            idx = stem.find("_")
            if idx <= 0:
                continue
            # 终审修复（P0）：键含 "." 一律排除——.space.json 是空间层文件，
            # 误拆为伪身体会话会在日切时被身体形态覆写（数据损坏）
            if "." in stem:
                continue
            identity = (stem[:idx], stem[idx + 1:])
            path = os.path.join(self._dir, name)
            mtime = self._mtime(path)
            cached = self._identity_cache.get(name)
            if mtime is not None and cached is not None and cached[0] == mtime:
                identity = cached[1]
            else:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    character = str(raw.get("character") or "") if isinstance(raw, dict) else ""
                    session = str(raw.get("session") or "") if isinstance(raw, dict) else ""
                    if (_CHARACTER_ID_RE.fullmatch(character) and session
                            and CONFIG.STATE_FILE_FMT.format(character=character, session=session) == name):
                        identity = (character, session)
                except (OSError, ValueError, TypeError):
                    pass
                if mtime is not None:
                    self._identity_cache[name] = (mtime, identity)
            out.append(identity)
        # 外部清理也同步回收身份缓存；正常 tick 不额外读 JSON。
        live_names = set(names)
        for name in list(self._identity_cache):
            if name not in live_names:
                self._identity_cache.pop(name, None)
        return out


