"""刺激解析层。三级事件来源的解析全部在这里：显式指令 / 文本抽取 / LLM 兜底 JSON。

纪律：解析失败一律返回空/None 当作无事件，不猜测、不默认加值（防幻觉编造用户行为）。
"""
from __future__ import annotations

import json
import re
from typing import Any

from .. import CONFIG
from . import material
from .extract import _clause as _observation_clause
from .extract import _not_observation as _not_spatial_observation
from .extract import _negated as _negated_observation
from .extract import _quote_spans as _observation_quotes


def _normalize_word_entry(val: Any) -> dict[str, Any] | None:
    """动作词条目归一化（0.29.0 参数化，向后兼容）：
    旧数字形态 0.6 → {"s": 0.6}；dict 形态校验合并（键可省，坏键丢弃）；返回 None = 该词不可用/禁用。
    dict 内 s 缺省回 0.5；s==0 视为显式禁用（与旧形态 0 同语义，由调用方 pop）。"""
    if isinstance(val, bool):   # bool 是 int 子类——防 True/False 误当 1/0
        return None
    if isinstance(val, (int, float)):
        try:
            v = float(val)
        except (TypeError, ValueError, OverflowError):
            return None
        if 0.0 < v <= 1.0:
            return {"s": v}
        return None
    if not isinstance(val, dict):
        return None
    out: dict[str, Any] = {"s": 0.5}
    try:
        s = float(val.get("s", 0.5))
    except (TypeError, ValueError, OverflowError):
        return None
    if not 0.0 < s <= 1.0:
        return None
    out["s"] = s
    method = str(val.get("method") or "").strip()
    if method and method in ("stroke", "press", "rub", "tap", "blow"):
        out["method"] = method
    for key, (lo, hi) in CONFIG.ACTION_WORD_PARAM_LIMITS.items():
        if key not in val:
            continue
        try:
            x = float(val[key])
        except (TypeError, ValueError, OverflowError):
            continue
        if x == x and lo <= x <= hi:   # NaN 不走值域链
            out[key] = x
    wtype = str(val.get("type") or "").strip()
    if wtype in CONFIG.ACTION_WORD_TYPES:
        out["type"] = wtype
    return out


def _word_disabled(val: Any) -> bool:
    """显式禁用判定：数字 0 / dict {"s": 0}（与旧形态 0 同语义=禁用该词）。"""
    if isinstance(val, bool):
        return False
    if isinstance(val, (int, float)):
        try:
            return float(val) == 0.0
        except (TypeError, ValueError, OverflowError):
            return False
    if isinstance(val, dict):
        try:
            return float(val.get("s", 0.5)) == 0.0
        except (TypeError, ValueError, OverflowError):
            return False
    return False


def _action_words(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """动作词→参数表（0.29.0 归一化形态）：CONFIG 全局表打底，profile 的 action_words 同名覆盖/追加。
    值两种形态：旧数字（等价 {"s": v}）与新 dict {s,method,freq_hz,speed_cm_s,depth,temp_c,cooldown_s,type}。
    显式配 s=0 = 禁用该词；坏值丢弃（保留全局值）。返回 {词: 参数dict}（至少含 s）。"""
    words: dict[str, dict[str, Any]] = {w: {"s": float(v)} for w, v in CONFIG.ACTION_WORDS.items()}
    override = profile.get("action_words")
    if isinstance(override, dict):
        for word, val in override.items():
            w = str(word)
            if not w:
                continue
            entry = _normalize_word_entry(val)
            if entry is None:
                if _word_disabled(val):
                    words.pop(w, None)      # 显式禁用
                continue                     # 其余坏值丢弃（保留全局值）
            words[w] = entry
    return words


def _part_by_word(word: str, profile: dict[str, Any]) -> dict[str, Any] | None:
    """label/alias 全等精确匹配优先；否则子串匹配、最长命中词优先（防"腰"误吃"腰侧"）。"""
    word = str(word or "").strip()
    if not word:
        return None
    best: dict[str, Any] | None = None
    best_len = 0
    for part in profile.get("parts") or []:
        words = [str(part.get("label") or "")] + [str(a) for a in part.get("aliases") or []]
        for w in words:
            if not w:
                continue
            if w == word:
                return part
            if w in word and len(w) > best_len:
                best = part
                best_len = len(w)
    return best


def _intensity_from_text(token: str) -> float | None:
    """强度词表优先，其次 0-1 数字。解析失败 None。"""
    token = str(token or "").strip()
    if not token:
        return None
    if token in CONFIG.TOUCH_INTENSITY_WORDS:
        return float(CONFIG.TOUCH_INTENSITY_WORDS[token])
    m = re.fullmatch(r"\d*\.?\d+", token)
    if m:
        try:
            v = float(token)
        except ValueError:
            return None
        return v if 0.0 <= v <= 1.0 else None
    return None


def _quote_spans(text: str) -> list[tuple[int, int]]:
    """与空间层共用引用边界；未闭合引文也不能变成真实接触。"""
    spans = list(_observation_quotes(text))
    for left, right in (("‘", "’"), ("'", "'"), ("`", "`")):
        start = 0
        while (begin := text.find(left, start)) >= 0:
            # English contractions are not quoted snippets.
            if left == "'" and begin > 0 and begin + 1 < len(text) \
                    and re.fullmatch(r"[A-Za-z]", text[begin - 1]) \
                    and re.fullmatch(r"[A-Za-z]", text[begin + 1]):
                start = begin + 1
                continue
            end = text.find(right, begin + 1)
            spans.append((begin, end if end >= 0 else len(text)))
            if end < 0:
                break
            start = end + 1
    return spans


def _not_contact_observation(text: str, pos: int) -> bool:
    left, right = _observation_clause(text, pos)
    clause = text[left:right]
    head = text[left:pos]
    # Existing RP shorthand deliberately treats a bare "想要摸…" as an action.
    # Preserve it, while questions and attributed wishes remain observations
    # that have not happened (e.g. "我想要摸…" or "想要摸…吗？").
    if head.strip() == "想要" and not (right < len(text) and text[right] in "?？") \
            and not any(word in clause for word in ("吗", "是否", "如果", "假如", "会不会", "怎么办", "怎样")):
        return False
    if _not_spatial_observation(text, pos):
        return True
    return any(word in clause for word in ("能不能", "可不可以", "能否", "要不要", "该不该", "假设", "试想")) \
        or bool(re.search(r"(?:想|可以|想要|希望|准备|打算).{0,6}$", head))


def _without_quotes(text: str, quoted: list[tuple[int, int]]) -> str:
    if not quoted:
        return text
    chars = list(text)
    for left, right in quoted:
        end = min(len(chars), right + 1)
        chars[left:end] = [" "] * (end - left)
    return "".join(chars)


def _not_stop_request(text: str, pos: int) -> bool:
    """条件/讨论不是停止；明确和礼貌的停止请求仍立即生效。"""
    left, right = _observation_clause(text, pos)
    clause = text[left:right]
    if any(word in clause for word in ("如果", "假如", "万一", "要是", "假设", "是否", "是不是",
                                       "会不会", "为什么", "为何", "如何")):
        return True
    question = (right < len(text) and text[right] in "?？") or "吗" in clause
    if question:
        return not bool(re.search(r"(?:能不能|可不可以|能否|可以).*(?:别|不要|不许|停手|松开手|结束接触)", clause))
    return False


def _negated(text: str, pos: int) -> bool:
    """动作词命中位前 3 字内出现否定前缀 → 视为拒绝（"别摸""不要咬"），不触发事件。"""
    head = text[max(0, pos - 3):pos]
    return any(neg in head for neg in CONFIG.NEGATION_PREFIXES) or _negated_observation(text, pos)


_PUNCT = "，。！？；、,.;?!…—"   # 动作词窗口遇标点截断，防止跨句误配


def _clip_seg(seg: str, keep_head: bool) -> tuple[str, int]:
    """标点截断：前置窗口保留靠近部位的一段（末段），后置窗口保留首段。
    返回 (截断文本, 在原窗口内的起始偏移)——供否定检测换算绝对位置。"""
    if keep_head:
        for i, ch in enumerate(seg):
            if ch in _PUNCT:
                return (seg[:i], 0)
        return (seg, 0)
    last = -1
    for i, ch in enumerate(seg):
        if ch in _PUNCT:
            last = i
    return (seg[last + 1:], last + 1)


def _action_near(text: str, pos: int, wlen: int, words_map: dict[str, dict[str, Any]],
                 quoted: list[tuple[int, int]]) -> dict[str, Any] | None:
    """部位词前 6 字与后 4 字窗口内未被否定的动作词最高强度条目；无则 None（不猜测）。
    前置（"摸耳朵"）与后置（"对着尾巴吹气"）语序都支持；否定前缀（"别摸"）视为拒绝；
    窗口遇标点截断（保留靠近部位的一段），动作词不跨句误配；引号内动作词不算。
    0.29.0：返回归一化参数条目（s 已乘修饰词并钳制，method/freq_hz/… 随词携带）。"""
    best: dict[str, Any] | None = None
    segs = [
        (max(0, pos - 6), *_clip_seg(text[max(0, pos - 6):pos], keep_head=False)),
        (pos + wlen, *_clip_seg(text[pos + wlen:pos + wlen + 4], keep_head=True)),
    ]
    for seg_start0, seg, off in segs:
        seg_start = seg_start0 + off
        for word, params in words_map.items():
            wpos = seg.find(word)
            while wpos >= 0:
                apos = seg_start + wpos
                if any(l <= apos < r for l, r in quoted):
                    wpos = seg.find(word, wpos + 1)
                    continue   # 引号内的动作词（歌词/转述）不算
                if _not_contact_observation(text, apos):
                    wpos = seg.find(word, wpos + 1)
                    continue
                if not _negated(text, apos):
                    # 强度修饰词：动作词前 3 字内出现则乘算（"狠狠揉"）
                    head = text[max(0, apos - 3):apos]
                    mod = 1.0
                    for m_word, m_val in CONFIG.INTENSITY_MODIFIERS.items():
                        if m_word in head:
                            mod = m_val
                            break
                    eff = params["s"] * mod
                    eff = 0.0 if eff < 0 else (1.0 if eff > 1 else eff)   # 内联钳制（不引 decay 依赖）
                    if eff <= 0.0:
                        break
                    if best is None or eff > best["s"]:
                        best = dict(params)
                        best["s"] = eff
                        best["word"] = word
                    break
                wpos = seg.find(word, wpos + 1)
    return best


def _material_spans(text: str, quoted: list[tuple[int, int]]) -> list[tuple[int, int, str]]:
    """材质词命中区间 [(位置, 词长, 材质id)]：label+别名子串匹配（"羊毛毯"命中"羊毛"），
    引号内不算（歌词/转述约定同动作词）；长词占优去重（"天鹅绒"不再重复算"绒布"类短词）。"""
    spans: list[tuple[int, int, str]] = []
    for term, mid in material.material_terms():
        start = 0
        while True:
            pos = text.find(term, start)
            if pos < 0:
                break
            if not any(l <= pos < r for l, r in quoted):
                spans.append((pos, len(term), mid))
            start = pos + 1
    kept = [t for t in spans if not any(
        o[0] < t[0] + t[1] and t[0] < o[0] + o[1] and o[1] > t[1] for o in spans
    )]
    kept.sort(key=lambda x: x[0])
    return kept


def _material_near(spans: list[tuple[int, int, str]], pos: int, wlen: int,
                   window: int) -> str | None:
    """离部位词最近的材质（字符距离 ≤ window 且非引号内已过滤）；无则 None（不猜测）。
    跨句/远处的材质不算——"羊毛毯叠在柜底，他拍拍她的头顶"不给头顶挂羊毛。"""
    best: str | None = None
    best_gap = window + 1
    for p, l, mid in spans:
        gap = max(p - (pos + wlen), pos - (p + l), 0)   # 区间间隔（重叠=0，"尾巴上的绒毛"）
        if gap <= window and gap < best_gap:
            best = mid
            best_gap = gap
    return best


def extract_events(text: str, profile: dict[str, Any]) -> list[dict[str, Any]]:
    """文本抽取：部位命中 + 部位邻近的未否定动作词 → 事件。

    只命中部位没有邻近动作词 → 无事件（不猜测）；动作词被否定（"别摸"）→ 不算刺激。
    引号（“”/"）内命中一律忽略（歌词/转述不是实际发生的接触）。
    多部位按消息出现顺序各生成一条；alias 包含关系（腰/腰侧）长词占优。
    """
    text = str(text or "")
    if not text.strip():
        return []
    quoted = _quote_spans(text)
    observed_text = _without_quotes(text, quoted)

    def in_quote(pos: int) -> bool:
        return any(l <= pos < r for l, r in quoted)

    words_map = _action_words(profile)
    hit_parts: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = []  # (位置, 词长, 动作词参数条目, 部位)
    for part in profile.get("parts") or []:
        words = [str(part.get("label") or "")] + [str(a) for a in part.get("aliases") or []]
        hits: list[tuple[int, int]] = []
        for w in words:
            if not w:
                continue
            start = 0
            while True:
                pos = text.find(w, start)
                if pos < 0:
                    break
                if not in_quote(pos):   # 引号内出现不算（歌词/转述）
                    hits.append((pos, len(w)))
                start = pos + 1
        if not hits:
            continue
        # 审查修复：该部位的每一次非引号出现都可能是动作落点——逐位置找邻近动作词，
        # 取首个能配上的（"耳朵很可爱，不过他更喜欢揉耳朵"的第二次出现才是真接触）。
        cand = None
        for pos, wlen in sorted(hits, key=lambda x: (x[0], -x[1])):
            near = _action_near(observed_text, pos, wlen, words_map, quoted)
            if near is not None:
                cand = (pos, wlen, near)
                break
        if cand is None:
            continue
        hit_parts.append((*cand, part))
    if not hit_parts:
        return []
    # 长词占优去重：命中区间被更长命中词覆盖的部位让位（"腰侧"里不再同时算"腰"）
    kept = [t for t in hit_parts if not any(
        op < t[0] + t[1] and t[0] < op + ol and ol > t[1] for op, ol, _, _ in hit_parts
    )]
    kept.sort(key=lambda x: x[0])
    # 材质识别：材质词（materials.json 的 label+别名）落在部位词就近窗口内 → 事件挂 material 字段
    mat_spans = _material_spans(text, quoted) if CONFIG.MATERIAL_ENABLED else []
    out: list[dict[str, Any]] = []
    for pos, wlen, word_params, p in kept:
        ev = {"part": str(p["id"]), "s": round(float(word_params["s"]), 4),
              "type": str(word_params.get("type") or CONFIG.EVENT_TYPE_DEFAULT)}
        # 0.29.0 词级参数随事件下发（缺省不落键——旧词表事件形态逐位不变）：
        # method 方式直选（否则 decay 按事件 type 词映射）/ freq·speed·depth·temp 进材质增益 /
        # cooldown_s 词级冷却（word 键供 decay 记账）
        for key in ("method", "freq_hz", "speed_cm_s", "depth", "temp_c", "cooldown_s"):
            if key in word_params:
                ev[key] = word_params[key]
        if word_params.get("cooldown_s"):
            ev["word"] = str(word_params.get("word") or "")[:32]
        if mat_spans:
            mid = _material_near(mat_spans, pos, wlen, CONFIG.MATERIAL_MATCH_WINDOW)
            if mid:
                ev["material"] = mid
        out.append(ev)
    return out


_STOP_WINDOW = 8   # 否定词与部位词的邻近窗口（字符）——中间隔着动词（"别/不要…摸耳朵"）仍可命中
_STOP_ALL_RE = re.compile(r"停手|松开手|结束接触")


def _explicit_stop_all(text: str, quoted: list[tuple[int, int]]) -> bool:
    """只识别当前明确的结束动作；否定、问题、条件句与引文均不清理接触。"""
    quote_ranges = quoted
    observed_text = _without_quotes(text, quote_ranges)
    for match in _STOP_ALL_RE.finditer(text):
        pos = match.start()
        if any(left <= pos < right for left, right in quote_ranges):
            continue
        if _not_stop_request(observed_text, pos):
            continue
        head = _clip_seg(observed_text[max(0, pos - 12):pos], keep_head=False)[0]
        tail = _clip_seg(observed_text[match.end():match.end() + 12], keep_head=True)[0]
        if any(word in head for word in (*CONFIG.NEGATION_PREFIXES, "不", "没", "未", "如果", "假如", "要是",
                                        "为什么", "怎么", "如何", "是否", "为何")):
            continue
        if any(word in tail for word in ("吗", "么", "呢", "的话", "才", "之前")):
            continue
        if match.end() < len(text) and text[match.end()] in "?？":
            continue
        return True
    return False


def extract_stops(text: str, profile: dict[str, Any]) -> list[str]:
    """停止接触语义抽取（0.31.0 持续接触的否定面）：部位词前窗口内（标点截断，取靠
    近部位的一段）出现否定前缀 → 返回该部位 pid（"别摸耳朵" → ear_tip）。
    引号内不算（歌词/转述约定同 extract_events）；长词占优去重（"腰侧"命中时"腰"
    不再算）；无命中返回 []（不猜测）。extract_events 的否定前缀本就不产事件——
    这里是接触面的对偶：把已建立的持续接触停掉。"""
    text = str(text or "")
    if not text.strip():
        return []
    quoted = _quote_spans(text)
    observed_text = _without_quotes(text, quoted)
    if _explicit_stop_all(text, quoted):
        return [str(part["id"]) for part in profile.get("parts") or []
                if isinstance(part, dict) and part.get("id")]
    hits: list[tuple[int, int, str]] = []   # (位置, 词长, pid)
    for part in profile.get("parts") or []:
        pid = str(part.get("id") or "")
        if not pid:
            continue
        found = False
        for w in [str(part.get("label") or "")] + [str(a) for a in part.get("aliases") or []]:
            if not w:
                continue
            start = 0
            while not found:
                pos = text.find(w, start)
                if pos < 0:
                    break
                start = pos + 1
                if any(l <= pos < r for l, r in quoted):
                    continue   # 引号内的部位词（歌词/转述）不算
                if _not_stop_request(observed_text, pos):
                    continue
                seg = _clip_seg(observed_text[max(0, pos - _STOP_WINDOW):pos], keep_head=False)[0]
                if any(neg in seg for neg in CONFIG.NEGATION_PREFIXES):
                    hits.append((pos, len(w), pid))
                    found = True
            if found:
                break
    kept = [t for t in hits if not any(
        op < t[0] + t[1] and t[0] < op + ol and ol > t[1] for op, ol, _ in hits)]
    out: list[str] = []
    for _, _, pid in sorted(kept, key=lambda x: x[0]):
        if pid not in out:
            out.append(pid)
    return out


def parse_touch_multi(arg_text: str, profile: dict[str, Any]) -> list[dict[str, Any]]:
    """/touch 指令解析：部位用 + 连接可多部位（"耳尖+尾巴 重 舔"），共享强度与类型；
    单部位即普通形式（"后颈 0.5"）。任一部位非法则整条拒绝（返回 []，不部分生效）。"""
    tokens = str(arg_text or "").split()
    if len(tokens) < 2:
        return []
    part_names = [w for w in re.split(r"[+＋]", tokens[0]) if w.strip()]
    if not part_names:
        return []
    resolved: list[dict[str, Any]] = []
    for name in part_names:
        part = _part_by_word(name, profile)
        if part is None:
            return []
        resolved.append(part)
    s = _intensity_from_text(tokens[1])
    if s is None:
        return []
    etype = tokens[2] if len(tokens) >= 3 else CONFIG.EVENT_TYPE_DEFAULT
    # 可选第 4 参：材质词（materials.json label/别名精确或子串最长命中，"羊毛毯"→wool）；
    # 认不出的第 4 参静默忽略（不拒整条——材质是增强信息不是合法性约束）
    mat_id: str | None = None
    if CONFIG.MATERIAL_ENABLED and len(tokens) >= 4:
        tok = tokens[3]
        best_len = 0
        for term, mid in material.material_terms():
            if term == tok:                        # 精确命中即定
                mat_id = mid
                best_len = len(term)
                break
            if term in tok and len(term) > best_len:   # 子串最长命中（"羊毛毯"→wool）
                mat_id = mid
                best_len = len(term)
    out = [{"part": str(p["id"]), "s": float(s), "type": str(etype)[:12]} for p in resolved]
    if mat_id:
        for ev in out:
            ev["material"] = mat_id
    return out


def parse_llm_json(text: str, profile: dict[str, Any]) -> dict[str, Any] | None:
    """LLM 兜底抽取结果解析：{"part","intensity","type"} 或 {"none": true}。

    JSON 坏、part 不在 profile、intensity 越界 → None（当无事件）。
    """
    text = str(text or "").strip()
    if not text:
        return None
    # 容忍模型在 JSON 外带的话：截取第一个 { 到最后一个 }
    l, r = text.find("{"), text.rfind("}")
    if l < 0 or r <= l:
        return None
    try:
        data = json.loads(text[l : r + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("none"):
        return None
    raw_part = str(data.get("part") or "")
    part = next((item for item in profile.get("parts") or []
                 if isinstance(item, dict) and str(item.get("id") or "") == raw_part), None)
    if part is None:
        part = _part_by_word(raw_part, profile)
    if part is None:
        return None
    try:
        s = float(data.get("intensity"))
    except (TypeError, ValueError, OverflowError):
        return None
    if not 0.0 <= s <= 1.0:
        return None
    return {"part": str(part["id"]), "s": s, "type": str(data.get("type") or CONFIG.EVENT_TYPE_DEFAULT)[:12]}
