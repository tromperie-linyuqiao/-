"""渲染层：状态 → 带标签补充数据文本。只产出数据与约束，不写台词。

纪律：注入块是"素材"，模型可见、用户不可见；超预算裁低位条目，始终保留完整标签与约束。
"""
from __future__ import annotations

import re
from html import escape
from typing import Any

from .. import CONFIG
from . import material, proactive_intent
from .decay import chain_tensions, overload_threshold, parse_ts, tier_of

# 出站清洗：模型见过注入格式后可能模仿输出标签残留（clean_chatter 教训）。
# 备选分支顺序敏感：完整标签对必须在前——孤立标签模式会先吃掉开标签导致块内容泄漏。
_OUTBOUND_TAG_RE = re.compile(
    r"[ \t]*[<＜](?P<body_tag>身体状态|BodyState)(?=[\s>＞])[^>＞]*[>＞]"
    r"[\s\S]*?[<＜]/(?P=body_tag)[>＞][ \t]*\n?"
    r"|[ \t]*[<＜]/?(?:身体状态|BodyState)(?=[\s>＞])[^>＞]*[>＞][ \t]*\n?"
)


def sanitize_outbound_text(text: str) -> str:
    """剥掉出站文本中的身体状态标签残留。"""
    if not text or ("<" not in text and "＜" not in text):
        return text
    cleaned = _OUTBOUND_TAG_RE.sub("", text)
    return cleaned


def should_inject(sender_id: str, profile: dict[str, Any]) -> bool:
    """白名单判定。非白名单一律 False（stranger 禁区，不注入也不加值）。"""
    wl = {str(x) for x in profile.get("whitelist") or []}
    return str(sender_id or "") in wl


def _phase_display(state: dict[str, Any]) -> str:
    return str(state.get("phase") or "building")


def _tier_sentence_hint(profile: dict[str, Any], tier_now: str) -> str:
    """档位句长约束：profile.expressions 覆盖 CONFIG.TIER_EXPRESSION（自由度：角色化句长）。"""
    expr = profile.get("expressions") if isinstance(profile.get("expressions"), dict) else {}
    entry = expr.get(tier_now) if isinstance(expr.get(tier_now), dict) else None
    if isinstance(entry, dict):
        try:
            n = int(entry.get("max_sentence_len"))
            if n > 0:
                return f"；单句不超过{n}字"
        except (TypeError, ValueError):
            pass
    return CONFIG.TIER_SENTENCE_HINT.get(tier_now, "")


def posture_hints(profile: dict[str, Any], user_height_cm: float) -> list[str]:
    """身高差姿态提示（生物力学互动修正）：差 >POSTURE_DIFF_MIN cm 才提示。
    profile.body.height_cm 为角色身高；user_height_cm 为互动对方（用户）身高。"""
    try:
        h = float((profile.get("body") or {}).get("height_cm"))
    except (TypeError, ValueError):
        return []
    if not 100 < h < 250:
        return []
    try:
        u = float(user_height_cm)
    except (TypeError, ValueError):
        return []
    if not 100 < u < 250:
        return []
    diff = round(h - u)
    if abs(diff) < CONFIG.POSTURE_DIFF_MIN:
        return []
    if diff > 0:
        return [f"对方矮{diff}cm：你俯身或托抱才顺手，他仰头才能够到你"]
    return [f"对方高{-diff}cm：他需俯身，你踮脚或借高处"]


def render(state: dict[str, Any], profile: dict[str, Any], max_chars: int | None = None,
           show_numbers: bool | None = None, compact: bool | None = None,
           user_height_cm: float = 0.0, constraint_text: str | None = None,
           feel_word_ttl: int | None = None) -> str:
    """渲染注入块。空状态（building 且无兴奋）返回空串。
    compact=True 注入用紧凑形态（一位小数锚点+合并行，LLM 对数编码下两位小数无增益，省 token）；
    默认取 CONFIG.RENDER_COMPACT。user_height_cm 用于身高差姿态提示。
    0.27.9 面板化：constraint_text 覆盖『约束：』行固定段（None=内置文案，紧凑形态有独立短文案）；
    feel_word_ttl 覆盖材质感受词时效（None=CONFIG.MATERIAL_STALE_SECONDS）。"""
    max_chars = max(0, int(CONFIG.RENDER_MAX_CHARS if max_chars is None else max_chars))
    if show_numbers is None:
        show_numbers = CONFIG.SHOW_NUMBERS_TO_MODEL
    if compact is None:
        compact = CONFIG.RENDER_COMPACT
    ttl = CONFIG.MATERIAL_STALE_SECONDS if feel_word_ttl is None else int(feel_word_ttl)
    fmt = (lambda v: f"{v:.1f}") if compact else (lambda v: f"{v:.2f}")
    # 污染纪律：building 期 arousal 未过第一档不注入（无档位可报）；余韵/不应期阶段本身有信息量，不受此门控
    if str(state.get("phase") or "building") == "building":
        mins: list[float] = []
        for t in profile.get("tiers") or []:
            try:
                if t.get("min") is not None:
                    mins.append(float(t.get("min")))
            except (TypeError, ValueError):
                continue  # 坏配置不炸注入
        first_min = min(mins) if mins else CONFIG.RENDER_MIN_AROUSAL
        if float(state.get("arousal") or 0.0) < first_min:
            # 接触记忆例外：60 秒内有直接事件的部位即使低值也放行——"刚被碰过"本身就是信息
            now_ts = parse_ts(state.get("updated_at"))
            recent = False
            for st in (state.get("parts") or {}).values():
                le = parse_ts(st.get("last_event_at")) if isinstance(st, dict) else None
                if le is not None and now_ts is not None and 0 <= now_ts - le <= 60:
                    recent = True
                    break
            if not recent:
                return ""
    parts_state = state.get("parts") or {}
    parts_meta = {str(p.get("id")): p for p in profile.get("parts") or [] if p.get("id")}
    rows: list[tuple[float, str]] = []
    for pid, meta in parts_meta.items():
        st = parts_state.get(pid) or {}
        v = float(st.get("value") or 0.0)
        tier = tier_of(v, profile.get("tiers") or [])
        if v < CONFIG.RENDER_FOLD_BELOW and not tier:
            continue  # token 纪律：近零且无档位的部位不渲染
        label = str(meta.get("label") or pid)
        tier_tag = CONFIG.TIER_LABELS.get(tier, tier) if tier else ""
        etype = ""
        mat_seg = ""
        if isinstance(st, dict):
            now_ts = parse_ts(state.get("updated_at"))
            le = parse_ts(st.get("last_event_at"))
            if le is not None and now_ts is not None and 0 <= now_ts - le <= CONFIG.TYPE_STALE_SECONDS:
                etype = str(st.get("last_event_type") or "")[:12]   # 时效内才渲染接触类型，防过时信息
            # 材质感受词：近期（feel_word_ttl 内）有材质事件 → 追加"丝绸·细滑"式短标签（控长）
            if CONFIG.MATERIAL_ENABLED:
                ma = parse_ts(st.get("last_material_at"))
                if ma is not None and now_ts is not None and 0 <= now_ts - ma <= ttl:
                    mat = material.get_material(str(st.get("last_material") or ""))
                    if mat is not None:
                        mat_seg = f"{mat.get('label')}·{material.feel_word(meta, mat)}"[:12]
        try:
            delta = float(st.get("trend_delta") or 0.0) if isinstance(st, dict) else 0.0
        except (TypeError, ValueError):
            delta = 0.0
        # token 纪律：只渲染有信息量的 ↑/↓，平稳不显示箭头
        arrow = "↑" if delta > CONFIG.TREND_EPS else ("↓" if delta < -CONFIG.TREND_EPS else "")
        # 冷却中的部位标注"平复中"（刚失控过、正在恢复——模型的决策信息）
        cooling = False
        if isinstance(st, dict):
            cu = parse_ts(st.get("cool_until"))
            now_ts = parse_ts(state.get("updated_at"))
            cooling = cu is not None and now_ts is not None and now_ts < cu
        # 标签段序：档位·接触类型·材质感受·平复中（材质段来自近期材质事件，见 mat_seg）
        segs = [s for s in (tier_tag, etype, mat_seg, "平复中" if cooling else "") if s]
        tag = f"{arrow}（{'·'.join(segs)}）" if segs else arrow
        if show_numbers:
            cell = f"{label} {fmt(v)}{tag}"
        else:
            cell = label + tag
        rows.append((v, cell))
    rows.sort(key=lambda x: -x[0])

    arousal = float(state.get("arousal") or 0.0)
    tol = float(state.get("tolerance") or 0.0)
    tier_now = tier_of(arousal, profile.get("tiers") or []) or "notice"
    hint = _tier_sentence_hint(profile, tier_now)
    # 约束行固定段：constraint_text（面板键 vb_constraint_text）显式传入才覆盖——
    # 紧凑形态默认保留独立短文案（token 纪律），完整形态默认 CONFIG.RENDER_CONSTRAINT_FIXED
    custom = str(constraint_text).strip() if constraint_text else ""
    if compact:
        constraint = (custom or "反应藏进动作；动作先于台词；状态系统跟踪") + hint   # 档位 hint 已含句长梯度语义
    else:
        constraint = (custom or CONFIG.RENDER_CONSTRAINT_FIXED) + "；句子随数值缩短" + hint
    constraint += CONFIG.PHASE_HINTS.get(_phase_display(state), "")
    # 过载临近预警：给模型"快到极限"的决策信息（继续推或收力由模型定）
    if str(state.get("phase") or "building") == "building":
        if arousal >= overload_threshold(profile) - CONFIG.OVERLOAD_WARN_MARGIN:
            constraint += "；快到极限了"

    if compact:
        line1 = f"兴奋 {fmt(arousal)}｜{_phase_display(state)}｜耐受 {fmt(tol)}"
        header = f"<身体状态 角色=\"{escape(str(profile.get('name') or profile.get('id') or ''), quote=True)}\">"
        guard = "（临时快照，随轮刷新）"
    else:
        line1 = f"兴奋度 {arousal:.2f}｜阶段 {_phase_display(state)}｜耐受 {tol:.2f}"
        header = f"<身体状态 快照=\"临时\" 角色=\"{escape(str(profile.get('name') or profile.get('id') or ''), quote=True)}\">"
        guard = "（系统临时状态快照，随轮刷新，仅用于本轮表演参考）"
    ev_today = state.get("events_today") if isinstance(state.get("events_today"), dict) else {}
    try:
        ev_count = int(ev_today.get("count") or 0)
    except (TypeError, ValueError):
        ev_count = 0
    if ev_count > 0:
        line1 += f"｜今日 {ev_count} 次"
    # 语言调情托底（0.30.0）：verbal 高于渲染门槛时头部披露"语言撩拨 X.X"
    # （托底进 arousal 的那条线已可见，这里给模型"兴奋来自对话"的归因锚点；紧凑形态一位小数）
    try:
        verbal = float(state.get("verbal_arousal") or 0.0)
    except (TypeError, ValueError):
        verbal = 0.0
    if verbal > CONFIG.VERBAL_RENDER_MIN:
        line1 += f"｜语言撩拨 {fmt(verbal)}"
    # 主动触碰意图尾巴（0.30.0）：时效内加"她<短语>"（≤14 字）——mark_as_temp 注入通道自然携带，
    # 外部调用方 presence_snapshot 按字段读 state 文件也能看到 proactive_intent 键（vb 侧自洽）
    hint = proactive_intent.intent_hint(state, now=parse_ts(state.get("updated_at")))
    if hint:
        line1 += f"｜她{hint}"
    body = "｜".join(cell for _, cell in rows)

    # 虚拟体感层（派生，不加状态存储）：自主神经分级 + 肌链张力 + 颤抖
    parts_state_chain = state.get("parts") or {}
    parts_meta_all = {str(p.get("id")): p for p in profile.get("parts") or [] if p.get("id")}
    tensions = chain_tensions(parts_state_chain, parts_meta_all)
    tense = [f"{name}绷紧" for name, t in tensions.items() if t > CONFIG.FASCIA_TENSION_THRESHOLD]

    def _autonomic(table, v):
        for threshold, label in table:
            if v >= threshold:
                return label
        return table[-1][1]

    somatic = [_autonomic(CONFIG.AUTONOMIC_BREATH, arousal), _autonomic(CONFIG.AUTONOMIC_HEART, arousal)]
    max_boost = 0.0
    for pst in parts_state_chain.values():
        try:
            max_boost = max(max_boost, float(pst.get("sens_boost") or 0.0))
        except (TypeError, ValueError):
            continue
    if max_boost > CONFIG.TREMOR_THRESHOLD or any(t > CONFIG.TREMOR_THRESHOLD for t in tensions.values()):
        somatic.append("肌肉轻颤")
    somatic += tense[:2]   # 至多 2 条链名，控长度
    somatic_line = "｜".join(s for s in somatic if s)

    # 动作可行性层：自身可达（固定短行）+ 身高差姿态（条件注入）
    posture = posture_hints(profile, user_height_cm)
    if compact:
        reach_line = "可及：" + CONFIG.SELF_REACH_HINT.split("（")[0] + ("；" + posture[0] if posture else "")
    else:
        reach_line = "动作边界：" + CONFIG.SELF_REACH_HINT + ("；" + posture[0] if posture else "")

    lines = [header, line1] + ([body] if body else []) + [f"约束：{constraint}", f"体感：{somatic_line}", reach_line, guard, "</身体状态>"]

    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    # 优先裁低位部位，保留完整护栏和可行性提示；每次只丢完整条目。
    keep = list(rows)
    while keep:
        keep.pop()
        body2 = "｜".join(c for _, c in keep)
        lines2 = [header, line1] + ([body2] if body2 else []) + [f"约束：{constraint}", f"体感：{somatic_line}", reach_line, guard, "</身体状态>"]
        text = "\n".join(lines2)
        if len(text) <= max_chars:
            return text
    # 较小预算先去派生体感与姿态；自定义约束不裁成半句。
    for tail in ([f"约束：{constraint}", guard],
                 [f"约束：{constraint}", "（临时快照）"]):
        text = "\n".join([header, line1, *tail, "</身体状态>"])
        if len(text) <= max_chars:
            return text
    # 极小预算使用完整短摘要，而不是截断 XML 或写出半条状态。
    # 自定义约束不能完整容纳时跳过本块，避免改变管理员的约束语义。
    short_line = f"兴奋 {fmt(arousal)}｜{_phase_display(state)}"
    short_constraint = custom or "动作先于台词；仅本轮参考"
    text = "\n".join([header, short_line, f"约束：{short_constraint}", "</身体状态>"])
    return text if len(text) <= max_chars else ""


def expression_params(arousal: float, profile: dict[str, Any]) -> dict[str, Any]:
    """第 9 节：档位 → 表达/语音参数字典。接 TTS 时把该 dict 交给 TTS 侧。"""
    tier = tier_of(arousal, profile.get("tiers") or []) or "notice"
    params = dict(CONFIG.TIER_EXPRESSION.get(tier) or CONFIG.TIER_EXPRESSION["notice"])
    params["tier"] = tier
    return params


# 终审修复：半角开+全角闭（及镜像）的混合标签对——原配对 pattern 要求两侧同宽形态，混合对只剥标签漏内容
import re as _re
OUTBOUND_MIXED_PATTERNS = [
    _re.compile(r"[ \t]*<身体状态[^>]*>[\s\S]*?＜/身体状态>[ \t]*\n?"),
    _re.compile(r"[ \t]*＜身体状态[^＜>]*＞[\s\S]*?</身体状态>[ \t]*\n?"),
]


def sanitize_outbound_mixed(text: str) -> str:
    if "<" not in text and "＜" not in text:
        return text
    for pat in OUTBOUND_MIXED_PATTERNS:
        text = pat.sub("", text)
    return text
