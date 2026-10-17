"""纯函数演算层。纪律：不 import astrbot、不做文件 IO、不改全局状态，全部输入输出走参数。

演算主入口 advance_state()：给定状态 dict、profile、事件列表和当前时间，
就地推进部位值/耐受/arousal/档位/phase，并返回触发摘要（供日志与确认文案）。
"""
from __future__ import annotations

import math
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any
from .. import CONFIG
from . import material


def clamp01(x: float) -> float:
    try:
        x = float(x)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if x != x or x in (float("inf"), float("-inf")):   # NaN/Inf 手工编辑的坏文件回 0
        return 0.0
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def clamp(x: float, lo: float, hi: float) -> float:
    try:
        x = float(x)
    except (TypeError, ValueError, OverflowError):
        return lo
    if not math.isfinite(x):
        return lo
    return max(lo, min(hi, x))


def iso_ts(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        value = float(ts)
        if not math.isfinite(value):
            return None
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def parse_ts(value: Any) -> float | None:
    """兼容 ISO 字符串与数字时间戳；解析失败返回 None，绝不抛出。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            value = float(value)
            # 时间戳既需有限，也需能回写为 ISO；否则坏锚点会让推进时崩溃。
            return value if iso_ts(value) is not None else None
        except (TypeError, ValueError, OverflowError):
            return None
    try:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def profile_param(profile: dict[str, Any], key: str, default: float,
                  lo: float | None = None, hi: float | None = None) -> float:
    """profile.params.<key> 覆盖全局参数；坏值/越界回 default（自由度：同一套代码换 profile 换手感）。"""
    params = profile.get("params")
    if isinstance(params, dict):
        try:
            v = float(params.get(key))
        except (TypeError, ValueError, OverflowError):
            return default
        if not math.isfinite(v):
            return default
        if lo is not None and v < lo:
            return default
        if hi is not None and v > hi:
            return default
        return v
    return default


def profile_param_str(profile: dict[str, Any], key: str, default: str,
                      allowed: tuple[str, ...]) -> str:
    """profile.params.<key> 字符串枚举参数（0.28.1 联动模式用）；非字符串/不在枚举内回 default。"""
    params = profile.get("params")
    if isinstance(params, dict):
        v = params.get(key)
        if isinstance(v, str) and v in allowed:
            return v
    return default


# ---- 部位级细调参数（0.29.1）：parts[] 可选键 → 中性默认 1.0，坏值/越界回中性 ----

def part_mult(meta: dict[str, Any] | None, key: str, lo: float, hi: float) -> float:
    """部位单值倍率（gain_mult/tolerance_mult/windup_mult）：无键/坏值/越界 → 1.0（行为不变）。"""
    try:
        v = float((meta or {}).get(key))
    except (TypeError, ValueError, OverflowError):
        return 1.0
    if v != v or not lo <= v <= hi:   # NaN/越界回中性
        return 1.0
    return v


def part_map_mult(meta: dict[str, Any] | None, key: str, sub: str, lo: float, hi: float) -> float:
    """部位映射倍率（type_mults{type:mult} / material_affinity{材质id:mult}）：
    sub 不在映射内/坏值/越界 → 1.0。"""
    m = (meta or {}).get(key)
    if not isinstance(m, dict):
        return 1.0
    try:
        v = float(m.get(sub))
    except (TypeError, ValueError, OverflowError):
        return 1.0
    if v != v or not lo <= v <= hi:
        return 1.0
    return v


# ---- 基础公式 ----

def decay_value(v0: float, dt: float, tau: float) -> float:
    """v(t) = v0 * e^(-dt/tau)。dt<=0 原样返回；tau<=0 视为瞬间归零。结果钳 0-1。"""
    v0 = clamp01(v0)
    dt = max(0.0, float(dt))
    tau = float(tau)
    if dt == 0.0:
        return v0
    if tau <= 0.0:
        return 0.0
    return clamp01(v0 * math.exp(-dt / tau))


def tolerance_step(tol: float, dt: float, event_ss: list[float] | None = None,
                   tolerance_gain: float | None = None) -> float:
    """耐受推进：事件增量 +gain*S（逐个），时间回落 0.01/分钟。钳 0-TOLERANCE_MAX。"""
    tol = clamp(float(tol), 0.0, CONFIG.TOLERANCE_MAX)
    tg = CONFIG.TOLERANCE_GAIN if tolerance_gain is None else float(tolerance_gain)
    inc = sum(tg * clamp01(s) for s in (event_ss or []))
    fall = CONFIG.TOLERANCE_FALL_PER_MIN * (max(0.0, float(dt)) / 60.0)
    return clamp(tol + inc - fall, 0.0, CONFIG.TOLERANCE_MAX)


def event_gain(gain: float, s: float, tolerance: float, in_cool: bool = False) -> float:
    """单次事件增益 gain*S*(1-tolerance)，冷却内 gain 减半。"""
    g = float(gain) * (CONFIG.COOL_GAIN_FACTOR if in_cool else 1.0)
    return g * clamp01(s) * (1.0 - clamp(tolerance, 0.0, CONFIG.TOLERANCE_MAX))


def part_tau(part: dict[str, Any]) -> float:
    """部位 τ：profile 的 half_life 优先，缺省回 CONFIG.DECAY_TAU_DEFAULT。"""
    try:
        hl = float(part.get("half_life") or 0)
    except (TypeError, ValueError, OverflowError):
        hl = 0.0
    return hl if math.isfinite(hl) and hl > 0 else CONFIG.DECAY_TAU_DEFAULT


def arousal_of(parts_state: dict[str, dict[str, Any]], parts_meta: dict[str, dict[str, Any]],
               verbal_arousal: float = 0.0, verbal_weight: float | None = None) -> float:
    """arousal = Σ(sens/10·v)/Σ(sens/10)，仅在**活跃部位**（v≥ACTIVE_AROUSAL_MIN）内归一——
    全身部位扩充后静息部位不再稀释权重（单点刺激的 arousal 与部位数无关），全静息 = 0。
    0.30.0 语言调情合成：arousal = max(physical, verbal_arousal × verbal_weight)——
    语言把兴奋度"托底"到可感知但不产生部位事件（缺省 verbal=0 与旧版逐位一致）。"""
    num = 0.0
    den = 0.0
    for pid, meta in parts_meta.items():
        w = clamp01(float(meta.get("sens") or 0) / 10.0)
        if w <= 0:
            continue
        st = parts_state.get(pid) or {}
        v = clamp01(st.get("value") or 0.0)
        if v < CONFIG.ACTIVE_AROUSAL_MIN:
            continue
        den += w
        num += w * v
    phys = clamp01(num / den) if den > 0 else 0.0
    if verbal_arousal is None:
        return phys
    vw = CONFIG.VERBAL_AROUSAL_WEIGHT if verbal_weight is None else float(verbal_weight)
    return clamp01(max(phys, clamp01(float(verbal_arousal)) * vw))


def verbal_arousal_step(state: dict[str, Any], is_intimate: bool, now: float,
                        cfg: dict[str, float] | None = None) -> float:
    """语言调情→身体兴奋推进（0.30.0，纯函数·就地写 state）：
    intimate：每次调用 +rate，钳 cap（对话氛围在场时兴奋持续爬升，约一小时聊满）；
    非 intimate：从上次锚点（verbal_at）按 τ 指数衰减（氛围散去回落）。
    cfg = {rate, cap, tau}（配置链注入，None 回 CONFIG）。返回推进后的 verbal_arousal。"""
    c = cfg or {}
    rate = float(c.get("rate", CONFIG.VERBAL_AROUSAL_RATE))
    cap = float(c.get("cap", CONFIG.VERBAL_AROUSAL_CAP))
    tau = float(c.get("tau", CONFIG.VERBAL_AROUSAL_TAU))
    now = float(now)
    v = clamp01(state.get("verbal_arousal") or 0.0)
    if is_intimate:
        v = clamp(v + rate, 0.0, max(cap, 0.0))
    else:
        last = parse_ts(state.get("verbal_at"))
        if last is not None and now > last:
            v = decay_value(v, now - last, tau)
    state["verbal_arousal"] = round(clamp01(v), 6)
    state["verbal_at"] = iso_ts(now)
    return state["verbal_arousal"]


def longterm_mult(state: dict[str, Any], pid: str) -> float:
    """长期身体记忆系数（0.30.0）：state.longterm_mult[pid]，无键/坏值/越界回中性 1.0。
    由 core/adaptation.py 日级适应层产出（高频触碰敏感化 / 零触碰迟钝化）。"""
    m = state.get("longterm_mult")
    if not isinstance(m, dict):
        return 1.0
    try:
        v = float(m.get(pid))
    except (TypeError, ValueError, OverflowError):
        return 1.0
    lo, hi = CONFIG.ADAPT_MULT_RANGE
    if v != v or not lo <= v <= hi:
        return 1.0
    return v


def intimacy_boost(arousal: float, verbal: float,
                   cfg: dict[str, float] | None = None) -> float:
    """亲密语境增益加成（0.31.0）：她已经有感觉时，新刺激更容易被推高——
    arousal > 0.15 或 verbal_arousal > 0.05 → ×L1（默认 1.3，有感觉）；
    arousal > 0.35 → ×L2（默认 1.5，接近 react 敏感度放大）。
    两条件取最大值（不叠乘），无命中回 1.0（行为不变）。
    0.32.1 死代码清理：sleep 档（×1.2）移除——state 白名单结构性封死写入、
    生产恒为 False，行为等价。cfg = {l1, l2}（面板配置链注入，None 回 CONFIG）。"""
    c = cfg or {}
    l1 = float(c.get("l1", CONFIG.INTIMACY_BOOST_L1))
    l2 = float(c.get("l2", CONFIG.INTIMACY_BOOST_L2))
    ar = clamp01(arousal)
    va = clamp01(verbal)
    best = 1.0
    if ar > CONFIG.INTIMACY_BOOST_AROUSAL_L1 or va > CONFIG.INTIMACY_BOOST_VERBAL_MIN:
        best = max(best, l1)
    if ar > CONFIG.INTIMACY_BOOST_AROUSAL_L2:
        best = max(best, l2)
    return best


def apply_mood_coupling(state: dict[str, Any], mood_score: Any) -> bool:
    """情绪→身体耦合（0.30.0）：把 外部情绪 分档写进 state 临时系数——
    低落（<-0.3）：全局增益 ×0.92、overload 阈值 +8%（身体绷着，不易兴奋也不易失控）；
    愉悦（>0.5）：全局增益 ×1.05。中性/坏值不写键（advance_state 走中性默认）。
    系数由 advance_state 消费后即清除（不持久化残留）；返回是否写入了耦合。"""
    try:
        ms = float(mood_score)
    except (TypeError, ValueError, OverflowError):
        return False
    if ms != ms:
        return False
    if ms < CONFIG.MOOD_COUPLE_LOW:
        state["mood_gain_mult"] = CONFIG.MOOD_COUPLE_LOW_GAIN
        state["mood_threshold_add"] = CONFIG.MOOD_COUPLE_LOW_THR
        return True
    if ms > CONFIG.MOOD_COUPLE_HIGH:
        state["mood_gain_mult"] = CONFIG.MOOD_COUPLE_HIGH_GAIN
        state["mood_threshold_add"] = 0.0
        return True
    return False


def tier_of(value: float, tiers: list[dict[str, Any]]) -> str | None:
    """返回最高命中的档位 key；低于所有阈值返回 None。tiers 按 min 升序取最后命中。"""
    v = clamp01(value)
    hit = None
    for t in tiers or []:
        try:
            mn = float(t.get("min") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if v >= mn and t.get("key"):
            hit = str(t["key"])
    return hit


def effective_s(s: float, phase: str, phase_mults: dict[str, float] | None = None) -> float:
    """phase 敏感度乘在刺激 S 上：同等动作在余韵期等效更强刺激（增益与耐受增量同步放大）。
    phase_mults 可注入 {"afterglow": x, "cooldown": y}（配置链值），None 回 CONFIG.PHASE_SENS_MULT。"""
    table = CONFIG.PHASE_SENS_MULT if phase_mults is None else phase_mults
    mult = table.get(str(phase or "building"), 1.0)
    return clamp01(clamp01(s) * float(mult))


def tz_local_time(et: float):
    """按 TZ_UTC_OFFSET 取本地时间 struct_time：None=容器本地时区，数字=UTC+偏移。
    昼夜节律与"今日"日切共用此入口，避免 Docker UTC 容器时区错位。"""
    offset = CONFIG.TZ_UTC_OFFSET
    if isinstance(offset, (int, float)):
        return time.gmtime(float(et) + float(offset) * 3600)
    return time.localtime(float(et))


def parse_night_window(value: Any) -> tuple[int, int] | None:
    """夜间窗口解析（0.27.9 面板化）："23-5" → (23, 5)；止 ≤ 起视为跨午夜。
    坏值（非两段、非整数、越界 0-23、起==止）返回 None，调用方回 CONFIG 默认。"""
    text = str(value or "").strip()
    if not text:
        return None
    parts = text.split("-")
    if len(parts) != 2:
        return None
    try:
        start_h, end_h = int(parts[0].strip()), int(parts[1].strip())
    except (TypeError, ValueError, OverflowError):
        return None
    if not (0 <= start_h <= 23 and 0 <= end_h <= 23) or start_h == end_h:
        return None
    return (start_h, end_h)


def circadian_mult(et: float, hour: int | None = None, night_mult: float | None = None,
                   night_window: tuple[int, int] | str | None = None) -> float:
    """昼夜节律：深夜刺激敏感度 ×night_mult（profile.params.night_mult 可覆盖，角色化节律）。
    hour 可注入以便确定性测试。night_window 支持 "23-5" 字符串或 (起, 止) 元组（跨午夜），None 回 CONFIG。"""
    if not CONFIG.CIRCADIAN_ENABLED:
        return 1.0
    mult = CONFIG.CIRCADIAN_NIGHT_MULT if night_mult is None else float(night_mult)
    if night_window is None:
        start_h, end_h = CONFIG.CIRCADIAN_NIGHT_WINDOW
    elif isinstance(night_window, str):
        parsed = parse_night_window(night_window)
        start_h, end_h = parsed if parsed is not None else CONFIG.CIRCADIAN_NIGHT_WINDOW
    else:
        start_h, end_h = (int(night_window[0]), int(night_window[1]))
    if hour is not None:
        h = int(hour)
    else:
        h = tz_local_time(float(et)).tm_hour
    if start_h > end_h:  # 跨午夜窗口
        return mult if h >= start_h or h < end_h else 1.0
    return mult if start_h <= h < end_h else 1.0


# ---- phase 机器 ----

def phase_advance(phase: str, phase_until: float | None, now: float,
                  cooldown_seconds: float | None = None) -> tuple[str, float | None, bool]:
    """返回 (新phase, 新phase_until, 是否发生变化)。
    afterglow 到点 → cooldown（终点 = 余韵终点 + cooldown_seconds，不依赖感知时刻）；
    cooldown 到点 → building（回 baseline）。cooldown_seconds 由配置链注入（None 回 CONFIG）。"""
    cd_secs = CONFIG.COOLDOWN_SECONDS if cooldown_seconds is None else float(cooldown_seconds)
    phase = str(phase or "building")
    if phase == "afterglow":
        if phase_until is not None and now >= float(phase_until):
            return ("cooldown", float(phase_until) + cd_secs, True)
        return (phase, phase_until, False)
    if phase == "cooldown":
        if phase_until is not None and now >= float(phase_until):
            return ("building", None, True)
        return (phase, phase_until, False)
    return ("building", None, False)


def overload_threshold(profile: dict[str, Any]) -> float:
    """overload 阈值：profile.tiers 里 key=overload 的 min 优先，缺省回 CONFIG。"""
    for t in profile.get("tiers") or []:
        if str(t.get("key") or "") == "overload":
            try:
                return float(t.get("min"))
            except (TypeError, ValueError, OverflowError):
                pass
    return CONFIG.OVERLOAD_AROUSAL


def check_overload(arousal: float, profile: dict[str, Any]) -> bool:
    """arousal 达到 overload 阈值。"""
    return arousal >= overload_threshold(profile)


def radiate_map(parts_meta: dict[str, dict[str, Any]], pid: str,
                max_hops: int | None = None, mode: str | None = None) -> dict[str, float]:
    """神经链传导图：从 pid 出发按 radiate 边 BFS（≤max_hops 跳，默认 CONFIG.RADIATE_MAX_HOPS），
    返回 {目标: 累计传导率}——环自动截断（简单路径，不走回头路）。

    0.28.1 自定义联动算法（profile_param 链，纯函数加参数）：
    - mode="mul"（默认）：每目标取最强路径（各跳倍率连乘）——与旧版行为逐位一致；
    - mode="sum"：所有简单路径的连乘贡献求和（钳 0-1）——多路神经汇聚叠加。
    两参数均由 advance_state 从 profile.params.radiate_hops/radiate_mode 注入；坏值回默认。
    mul 按跳数保留最强前缀；边倍率 ≤1，含环的路径去环后不弱且跳数更少，
    所以至多 N 跳的最强值与简单路径穷举相同。sum 保留原简单路径贡献语义。"""
    hops_cap = CONFIG.RADIATE_MAX_HOPS if max_hops is None else max(1, int(max_hops))
    sum_mode = (str(mode or "mul") == "sum")
    best: dict[str, float] = {}
    if not sum_mode:
        # A global best alone is insufficient: a weaker, shorter prefix can
        # still reach a target within the remaining hop budget.
        frontier_by_hop = {pid: 1.0}
        for _ in range(hops_cap):
            next_hop: dict[str, float] = {}
            for cur, cum in frontier_by_hop.items():
                edges = parts_meta.get(cur, {}).get("radiate") or {}
                if not isinstance(edges, dict):
                    continue
                for rid, ratio in edges.items():
                    rid = str(rid)
                    if rid == pid or rid not in parts_meta:
                        continue
                    try:
                        r = float(ratio)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if not 0 < r <= 1:
                        continue
                    value = clamp01(cum * r)
                    if value > next_hop.get(rid, 0.0):
                        next_hop[rid] = value
                    if value > best.get(rid, 0.0):
                        best[rid] = value
            frontier_by_hop = next_hop
            if not frontier_by_hop:
                break
        return best
    frontier = deque([(pid, 1.0, 0, frozenset({pid}))])
    while frontier:
        cur, cum, hops, seen = frontier.popleft()
        if hops >= hops_cap:
            continue
        edges = parts_meta.get(cur, {}).get("radiate") or {}
        if not isinstance(edges, dict):
            continue
        for rid, ratio in edges.items():
            rid = str(rid)
            if rid == pid or rid not in parts_meta or rid in seen:
                continue
            try:
                r = float(ratio)
            except (TypeError, ValueError, OverflowError):
                continue
            if not 0 < r <= 1:
                continue
            cum_next = clamp01(cum * r)
            if cum_next <= 0:
                continue
            best[rid] = clamp01(best.get(rid, 0.0) + cum_next)
            if hops + 1 < hops_cap:
                frontier.append((rid, cum_next, hops + 1, seen | {rid}))
    return best


def chain_tensions(parts_state: dict[str, dict[str, Any]], parts_meta: dict[str, dict[str, Any]]) -> dict[str, float]:
    """筋膜链张力（Myers Anatomy Trains）：链上**活跃部位**（v≥0.05）的 sens 加权均值——
    语义为"激活段的紧张度"，静息部位不稀释；单点激活不算链绷紧（≥2 个活跃段才计），链上无活跃段返回 0。"""
    out: dict[str, float] = {}
    for name, ids in CONFIG.FASCIA_CHAINS.items():
        num = 0.0
        den = 0.0
        active = 0
        for pid in ids:
            meta = parts_meta.get(pid)
            if meta is None:
                continue
            v = clamp01((parts_state.get(pid) or {}).get("value") or 0.0)
            if v < CONFIG.ACTIVE_AROUSAL_MIN:
                continue   # 静息部位不参与（不稀释链张力）
            active += 1
            w = clamp01(float(meta.get("sens") or 0) / 10.0)
            if w <= 0:
                continue
            den += w
            num += w * v
        out[name] = clamp01(num / den) if den > 0 and active >= 2 else 0.0
    return out


def part_gain_coeff(meta: dict[str, Any]) -> float:
    """部位增益系数：sens 归一后映射 [SENS_GAIN_MIN, MIN+SPAN]——敏感部位升得更快（数值细化）。"""
    try:
        sens_norm = clamp01(float(meta.get("sens") or 0) / 10.0)
    except (TypeError, ValueError, OverflowError):
        sens_norm = 0.0
    return CONFIG.SENS_GAIN_MIN + sens_norm * CONFIG.SENS_GAIN_SPAN


def type_gain_mult(ev_type: str, part_meta: dict[str, Any] | None,
                   material: dict[str, Any] | None = None) -> float:
    """type 语义类别增益微调（0.29.0；倍率全部 CONFIG 可调，touch 恒 1.0——旧词表行为不变）：
    · pain：sens≤TYPE_PAIN_LOW_SENS_MAX 的低敏部位 ×TYPE_PAIN_LOW_SENS_MULT——
      痛觉广谱受体（nociceptor）全身分布均匀，钝区对抚摸迟钝但对痛不迟钝
    · temp：受材质 thermal 调制 ×(1+TYPE_TEMP_THERMAL_COEFF×thermal)（金属/冰放大温差感）、
      受材质 warm_hold 放大 ×(1+TYPE_TEMP_WARM_COEFF×warm_hold)（保温层让温感停留更久）
    · air：flags 含 exposed 的暴露部位 ×TYPE_AIR_EXPOSED_MULT——裸露皮肤气流感受更直接
    · constrain：恒 1.0（束缚语义靠 RP 层表达，数值不另调）
    纯函数；材质未知（None/无 material 字段的事件）时 temp 只走 warm_hold 通道。"""
    t = str(ev_type or CONFIG.EVENT_TYPE_DEFAULT)
    mult = 1.0
    if t == "pain":
        try:
            sens = float((part_meta or {}).get("sens") or 0)
        except (TypeError, ValueError, OverflowError):
            sens = 0.0
        if sens <= CONFIG.TYPE_PAIN_LOW_SENS_MAX:
            mult *= CONFIG.TYPE_PAIN_LOW_SENS_MULT
    elif t == "temp":
        if isinstance(material, dict):
            try:
                th = clamp01(float(material.get("thermal") or 0.0))
            except (TypeError, ValueError, OverflowError):
                th = 0.0
            mult *= 1.0 + CONFIG.TYPE_TEMP_THERMAL_COEFF * th
            try:
                wh = clamp01(float(material.get("warm_hold") or 0.0))
            except (TypeError, ValueError, OverflowError):
                wh = 0.0
            mult *= 1.0 + CONFIG.TYPE_TEMP_WARM_COEFF * wh
    elif t == "air":
        flags = (part_meta or {}).get("flags")
        if isinstance(flags, (list, tuple)) and "exposed" in [str(f) for f in flags]:
            mult *= CONFIG.TYPE_AIR_EXPOSED_MULT
    return mult


def _push_history(state: dict[str, Any], et: float, pid: str, old: float, new: float, src: str) -> None:
    """数值变更流水（display_data 思路）：最近 ADAPT_HISTORY_LIMIT 条（0.30.0 扩容 20→200，
    长期身体记忆的数据面），WebUI 调参回放 + 日级适应层统计共用。"""
    try:
        hist = state.get("history") if isinstance(state.get("history"), list) else []
        hist = hist[-(CONFIG.ADAPT_HISTORY_LIMIT - 1):] + [{"t": iso_ts(et), "part": pid, "old": round(old, 4),
                                                            "new": round(new, 4), "src": str(src)[:12]}]
        state["history"] = hist
    except Exception:
        pass


# ---- 主入口 ----

def advance_state(
    state: dict[str, Any],
    profile: dict[str, Any],
    events: list[dict[str, Any]] | None,
    now: float,
    *,
    afterglow_seconds: float | None = None,
    cooldown_seconds: float | None = None,
    afterglow_mult: float | None = None,
    cooldown_mult: float | None = None,
    night_window: tuple[int, int] | str | None = None,
    intimacy_cfg: dict[str, float] | None = None,
    verbal_weight: float | None = None,
) -> dict[str, Any]:
    """把状态从 state.updated_at 推进到 now，中途应用 events（按时间升序）。

    events 元素：{"part": part_id, "s": 0-1, "type": str, "t": 绝对秒}；
    0.29.0 可选键：method（直选方式）/freq_hz/speed_cm_s/depth/temp_c（材质增益参数）、
    word+cooldown_s（词级冷却——同词冷却内命中增益 ×ACTION_WORD_CD_FACTOR）、
    type 可为语义类别 pain/temp/air/constrain（增益微调见 type_gain_mult）。
    0.29.1 部位级细调（parts[] 可选键，缺省中性 1.0=行为不变，见 part_mult/part_map_mult）：
    gain_mult 总增益 / tolerance_mult 耐受积累 / windup_mult 敏感化累积 /
    type_mults 部位×类别 / material_affinity 部位×材质亲和。
    就地修改 state；返回触发摘要 {"new_tiers": {pid: key}, "overload": bool, "phase_changed": bool}。

    0.27.9 面板化：四个 phase 参数走完整配置链（profile.params > 调用方传入的面板值 > CONFIG 常量），
    night_window 为昼夜节律窗口（None 回 CONFIG.CIRCADIAN_NIGHT_WINDOW）。全部可选，缺省行为与旧版逐位一致。

    0.31.0 两项接线：①亲密语境增益——本轮所有事件（含持续接触延续事件）的 event_gain
    结果统一乘 intimacy_boost（入场兴奋快照定档，本轮事件自己推高的兴奋不反向放大本轮）；
    ②持续接触——事件带 src="persist"（core/persist_contact 合成）时走特判分支：
    只增加值与 arousal，不计次/不积耐受/不进流水/不动习惯化·敏化·冷却·传导。
    """
    now = float(now)
    parts_meta: dict[str, dict[str, Any]] = {str(p.get("id")): p for p in profile.get("parts") or [] if p.get("id")}
    tiers = profile.get("tiers") or []
    # 情绪→身体耦合（0.30.0）：临时系数消费即清（main 每轮注入前 apply_mood_coupling 写入，
    # 这里读完就 pop——不持久化残留，坏值/缺键走中性 1.0/0.0 行为不变）
    try:
        mood_gain_mult = clamp(float(state.pop("mood_gain_mult", 1.0) or 1.0), 0.5, 2.0)
    except (TypeError, ValueError, OverflowError):
        mood_gain_mult = 1.0
    try:
        mood_thr_add = clamp(float(state.pop("mood_threshold_add", 0.0) or 0.0), -0.2, 0.2)
    except (TypeError, ValueError, OverflowError):
        mood_thr_add = 0.0
    # 角色参数：profile.params 可覆盖，坏值回 CONFIG 默认
    gain = profile_param(profile, "gain", CONFIG.GAIN_DEFAULT, 0.0, 2.0) * mood_gain_mult
    verbal_weight = profile_param(profile, "verbal_arousal_weight",
                                  CONFIG.VERBAL_AROUSAL_WEIGHT if verbal_weight is None else verbal_weight,
                                  CONFIG.VERBAL_AROUSAL_WEIGHT_RANGE[0],
                                  CONFIG.VERBAL_AROUSAL_WEIGHT_RANGE[1])
    cool_secs = profile_param(profile, "cool_seconds", CONFIG.COOL_SECONDS_AFTER_LOSE, 0.0, 600.0)
    tol_gain = profile_param(profile, "tolerance_gain", CONFIG.TOLERANCE_GAIN, 0.0, 0.2)
    night_mult = profile_param(profile, "night_mult", CONFIG.CIRCADIAN_NIGHT_MULT, 1.0, 2.0)
    habit_streak = int(profile_param(profile, "habit_streak", CONFIG.HABITUATION_STREAK, 2, 10))
    habit_window = profile_param(profile, "habit_window", CONFIG.HABITUATION_WINDOW, 10.0, 600.0)
    habit_factor = profile_param(profile, "habit_factor", CONFIG.HABITUATION_FACTOR, 0.1, 1.0)
    # 联动算法自定义（0.28.1）：跳数 1-3 默认 CONFIG.RADIATE_MAX_HOPS；模式 mul 连乘默认/sum 求和；坏值回默认
    radiate_hops = int(profile_param(profile, "radiate_hops", CONFIG.RADIATE_MAX_HOPS,
                                     float(CONFIG.RADIATE_HOPS_RANGE[0]), float(CONFIG.RADIATE_HOPS_RANGE[1])))
    radiate_mode = profile_param_str(profile, "radiate_mode", "mul", CONFIG.RADIATE_MODES)
    # phase 参数链（0.27.9）：profile.params > 面板值（调用方传入）> CONFIG 常量
    ag_secs = profile_param(profile, "afterglow_seconds",
                            CONFIG.AFTERGLOW_SECONDS if afterglow_seconds is None else float(afterglow_seconds),
                            300.0, 7200.0)
    cd_secs = profile_param(profile, "cooldown_seconds",
                            CONFIG.COOLDOWN_SECONDS if cooldown_seconds is None else float(cooldown_seconds),
                            300.0, 7200.0)
    ag_mult = profile_param(profile, "afterglow_mult",
                            CONFIG.AFTERGLOW_SENS_MULT if afterglow_mult is None else float(afterglow_mult),
                            1.0, 2.0)
    cd_mult = profile_param(profile, "cooldown_mult",
                            CONFIG.COOLDOWN_SENS_MULT if cooldown_mult is None else float(cooldown_mult),
                            0.2, 1.0)
    start = parse_ts(state.get("updated_at"))
    if start is None:
        start = now
    start = min(start, now)

    # 今日接触累计（周期键日切重置纪律：day 变化即清零；日切走统一时区入口）
    today = time.strftime("%Y-%m-%d", tz_local_time(now))
    ev_today = state.get("events_today") if isinstance(state.get("events_today"), dict) else {}
    if ev_today.get("day") != today or not isinstance(ev_today.get("count"), int):
        ev_today = {"day": today, "count": 0}
    state["events_today"] = ev_today

    # phase 先推到 now（overload 后的两阶段计时与事件无关，纯时间驱动）。
    # 停机跨过余韵+不应期时一次推进到位（building 必然收敛，循环有界）。
    old_phase = str(state.get("phase") or "building")
    old_pu = parse_ts(state.get("phase_until"))
    phase = old_phase
    phase_until = old_pu
    phase_changed = False
    for _ in range(4):
        new_phase, new_until, ch = phase_advance(phase, phase_until, now, cooldown_seconds=cd_secs)
        if not ch:
            break
        phase, phase_until = new_phase, new_until
        phase_changed = True
    state["phase"] = phase
    state["phase_until"] = iso_ts(phase_until) if phase_until is not None else None

    def phase_mult_at(et: float) -> float:
        """事件时刻所处的 phase 敏感倍率：按时间区间查询（含已推进过的旧链），
        补算窗口跨 overload 的历史事件不误放大/不漏放大。倍率/时长全部走配置链值。"""
        et = float(et)
        pu = parse_ts(state.get("phase_until"))
        ph = str(state.get("phase") or "building")
        if ph == "afterglow" and pu is not None:
            if et < pu:
                return ag_mult
            if et < pu + cd_secs:
                return cd_mult
            return 1.0
        if ph == "cooldown" and pu is not None:
            # cooldown 终点 pu 反推余韵上界 = pu - cd_secs
            if et < pu - cd_secs:
                return ag_mult
            return cd_mult if et < pu else 1.0
        if ph == "building":
            # 当前已回 building：查推进前的旧链区间
            if old_phase == "afterglow" and old_pu is not None:
                if et < old_pu:
                    return ag_mult
                if et < old_pu + cd_secs:
                    return cd_mult
                return 1.0
            if old_phase == "cooldown" and old_pu is not None:
                if et < old_pu - cd_secs:
                    return ag_mult
                return cd_mult if et < old_pu else 1.0
        return 1.0

    # 肌肉联动：链张力>阈值的链上部位衰减 τ 放缓（紧张让敏感消退变慢）
    tensions = chain_tensions(state.get("parts") or {}, parts_meta)
    tense_chains = {name for name, t in tensions.items() if t > CONFIG.FASCIA_TENSION_THRESHOLD}
    tense_parts: set[str] = set()
    for cname in tense_chains:
        tense_parts.update(CONFIG.FASCIA_CHAINS.get(cname) or [])

    tol = clamp(state.get("tolerance") or 0.0, 0.0, CONFIG.TOLERANCE_MAX)
    # 0.31.0 亲密语境增益：入场兴奋快照 → 本轮所有事件（含延续）增益统一乘 boost。
    # 快照在衰减/事件施加之前取——"她已经有感觉"指本轮之前的状态，本轮事件自己
    # 推高的兴奋不反向放大本轮（语义锚：intimacy_boost 两条件取最大不叠乘）。
    entry_arousal = arousal_of(state.get("parts") or {}, parts_meta,
                               verbal_arousal=state.get("verbal_arousal") or 0.0,
                               verbal_weight=verbal_weight)
    boost_mult = intimacy_boost(entry_arousal, clamp01(state.get("verbal_arousal") or 0.0),
                                intimacy_cfg)
    norm_events: list[dict[str, Any]] = []
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        pid = str(ev.get("part") or "")
        if pid not in parts_meta:
            continue
        t = ev.get("t")
        t = parse_ts(t)
        if t is None:
            t = now
        t = max(start, min(t, now))
        nev = {"part": pid, "s": clamp01(ev.get("s") or 0.0),
               "type": str(ev.get("type") or CONFIG.EVENT_TYPE_DEFAULT), "t": t,
               "material": str(ev.get("material") or "")[:16] or None}
        # 0.31.0 持续接触延续事件标记（persist_contact 合成）：特判分支只加值不加账
        if str(ev.get("src") or "") == "persist":
            nev["src"] = "persist"
            if ev.get("contact_semantics") is True:
                nev["contact_semantics"] = True
        # 0.29.0 事件级可选键（动作词参数化下发；旧事件不带即行为不变）：
        # method 直选方式（缺省按 type 词映射）/ 频率·速度·压深·温度进 material_gain /
        # word+cooldown_s 词级冷却账本
        method = str(ev.get("method") or "").strip()
        if method in ("stroke", "press", "rub", "tap", "blow"):
            nev["method"] = method
        for key, lo, hi in (("freq_hz", 0.0, 500.0), ("speed_cm_s", 0.0, 200.0),
                            ("depth", 0.0, 1.0), ("temp_c", 0.0, 60.0), ("cooldown_s", 0.0, 3600.0)):
            if ev.get(key) is None:
                continue
            try:
                x = float(ev.get(key))
            except (TypeError, ValueError, OverflowError):
                continue
            if x == x and lo <= x <= hi:
                nev[key] = x
        word = str(ev.get("word") or "").strip()
        if word and nev.get("cooldown_s"):
            nev["word"] = word[:32]
        norm_events.append(nev)
    norm_events.sort(key=lambda e: e["t"])

    # 趋势快照：本窗口各部位增量（借鉴 ST 状态系统——模型读"↑/↓/→"比只读绝对值信息量大）
    start_values = {pid: clamp01((state.get("parts") or {}).get(pid, {}).get("value") or 0.0)
                    for pid in parts_meta}

    new_tiers: dict[str, str] = {}
    overload_fired = False
    cursor = start
    pending_tol_events: list[float] = []  # 当前时刻发生的全部事件 S（tolerance 按时刻聚合）

    def flush_tol(dt: float) -> None:
        nonlocal tol
        tol = tolerance_step(tol, dt, pending_tol_events, tolerance_gain=tol_gain)
        pending_tol_events.clear()

    ev_idx = 0
    n_ev = len(norm_events)
    while True:
        # 下一事件时刻
        if ev_idx < n_ev:
            et = norm_events[ev_idx]["t"]
        else:
            et = now
        # 推进耐受与所有部位值到 et（纯衰减，无事件）
        flush_tol(et - cursor)
        for pid, meta in parts_meta.items():
            st = state.setdefault("parts", {}).setdefault(pid, {})
            # 敏感化 wind-up：中枢敏化让响应更持久（文献：继发痛敏稳定 ≥2h，Ziegler 1999）——
            # 敏感化期衰减 τ 放缓；boost 自身按 SENS_TAU 指数衰减
            b_now = clamp01(st.get("sens_boost") or 0.0) if isinstance(st.get("sens_boost"), (int, float)) else 0.0
            tau_eff = part_tau(meta) * (1.0 + 0.6 * b_now)
            if pid in tense_parts:
                tau_eff *= CONFIG.FASCIA_TENSION_TAU_MULT   # 绷紧的肌链消退更慢
            st["value"] = decay_value(st.get("value") or 0.0, et - cursor, tau_eff)
            st["sens_boost"] = round(b_now * math.exp(-max(0.0, et - cursor) / CONFIG.SENS_TAU), 6)
        cursor = et
        if ev_idx >= n_ev:
            break
        # 同刻事件聚合施加
        same_t: list[dict[str, Any]] = []
        while ev_idx < n_ev and norm_events[ev_idx]["t"] == et:
            same_t.append(norm_events[ev_idx])
            ev_idx += 1
        for ev in same_t:
            pid = ev["part"]
            meta = parts_meta[pid]
            st = state["parts"][pid]
            if ev.get("src") == "persist":
                # 0.31.0 持续接触延续（persist_contact 合成事件）：只增加值与 arousal——
                # 不计 events_today/不积耐受/不进 history 流水/不动习惯化·敏化·冷却·传导
                # （防刷屏与死循环；记录面由 record_contacts 单独管理）
                p_cool = parse_ts(st.get("cool_until"))
                p_in_cool = p_cool is not None and et < p_cool
                p_gain = gain * part_mult(meta, "gain_mult",
                                          CONFIG.PART_GAIN_MULT_RANGE[0],
                                          CONFIG.PART_GAIN_MULT_RANGE[1]) \
                    * longterm_mult(state, pid) * part_gain_coeff(meta)
                p_old = clamp01(st.get("value") or 0.0)
                p_s = clamp01(ev["s"])
                # 新接触可携带原始材质/方式/类别；旧接触无标记，算法逐位保持。
                # s 始终是未经材质缩放的原始强度 × 延续率，语义倍率只在此乘一次。
                if ev.get("contact_semantics") is True:
                    p_mat = material.get_material(str(ev.get("material") or ""))
                    if p_mat is not None:
                        p_method = ev.get("method") or material.method_from_word(str(ev.get("type") or ""))
                        p_params = {k: ev[k] for k in ("speed_cm_s", "depth", "freq_hz", "temp_c") if k in ev}
                        p_s = clamp01(p_s * material.material_gain(meta, p_mat, p_method, p_params or None)
                                      * part_map_mult(meta, "material_affinity", str(ev["material"]),
                                                      *CONFIG.PART_MATERIAL_AFFINITY_RANGE))
                    p_type = str(ev.get("type") or CONFIG.EVENT_TYPE_DEFAULT)
                    p_s = clamp01(p_s * type_gain_mult(p_type, meta, p_mat)
                                  * part_map_mult(meta, "type_mults", p_type, *CONFIG.PART_TYPE_MULT_RANGE))
                    if p_mat is not None:
                        st["last_material"] = str(ev["material"])[:16]
                        st["last_material_at"] = iso_ts(et)
                st["value"] = clamp01(p_old + event_gain(p_gain, p_s,
                                                         clamp(tol, 0.0, CONFIG.TOLERANCE_MAX),
                                                         p_in_cool) * boost_mult)
                continue
            state["events_today"]["count"] += 1
            in_cool = False
            cool_until = parse_ts(st.get("cool_until"))
            if cool_until is not None and et < cool_until:
                in_cool = True
            s_eff = clamp01(ev["s"] * phase_mult_at(et) * circadian_mult(et, night_mult=night_mult,
                                                                        night_window=night_window))
            # 材质增益：事件带 material 字段时 s 乘 material_gain（方式优先取事件级 method，
            # 缺省由事件 type 词映射；0.29.0 事件级 freq/speed/depth/temp 参数直入增益——
            # 同一 s_eff，语义一致）。未知材质 id 静默跳过（回无材质）。
            # 0.29.1 部位×材质亲和（material_affinity）：该部位对此材质的刺激感受倍率（如舌尖 {"silk":1.4}）
            mat_ev = None
            if ev.get("material"):
                mat_ev = material.get_material(str(ev["material"]))
                if mat_ev is not None:
                    method = ev.get("method") or material.method_from_word(str(ev.get("type") or ""))
                    gain_params = {k: ev[k] for k in ("speed_cm_s", "depth", "freq_hz", "temp_c") if k in ev}
                    mg = material.material_gain(meta, mat_ev, method, gain_params or None) * part_map_mult(
                        meta, "material_affinity", str(ev["material"]),
                        CONFIG.PART_MATERIAL_AFFINITY_RANGE[0], CONFIG.PART_MATERIAL_AFFINITY_RANGE[1])
                    s_eff = clamp01(s_eff * mg)
                else:
                    mat_ev = None
            # type 语义类别微调（0.29.0）：pain/temp/air 倍率见 type_gain_mult 文档；
            # 增益同步影响耐受增量（与材质增益同一 s_eff 语义）
            # 0.29.1 部位×类别交互（type_mults，如尾根 {"air":1.5}）：乘在该部位该类别的 s_eff 上
            ev_type = str(ev.get("type") or CONFIG.EVENT_TYPE_DEFAULT)
            s_eff = clamp01(s_eff * type_gain_mult(ev_type, meta, mat_ev) * part_map_mult(
                meta, "type_mults", ev_type,
                CONFIG.PART_TYPE_MULT_RANGE[0], CONFIG.PART_TYPE_MULT_RANGE[1]))
            # 词级冷却（0.29.0）：同词冷却内命中，本事件增益 ×ACTION_WORD_CD_FACTOR（防复读刷值）；
            # 只削增益不削耐受（重复表达不等于耐受飞涨）；无 cooldown_s 的词零开销（旧行为）
            ev_gain_mult = 1.0
            if ev.get("word") and ev.get("cooldown_s"):
                word_cd = state.get("word_cd") if isinstance(state.get("word_cd"), dict) else {}
                w_until = parse_ts(word_cd.get(ev["word"]))
                if w_until is not None and et < w_until:
                    ev_gain_mult = CONFIG.ACTION_WORD_CD_FACTOR
                word_cd[str(ev["word"])] = iso_ts(et + float(ev["cooldown_s"]))
                state["word_cd"] = word_cd
            pending_tol_events.append(s_eff * part_mult(meta, "tolerance_mult",
                                   CONFIG.PART_TOLERANCE_MULT_RANGE[0], CONFIG.PART_TOLERANCE_MULT_RANGE[1])
                                   * (1.0 - 0.4 * (clamp01(st.get("sens_boost") or 0.0)
                                   if isinstance(st.get("sens_boost"), (int, float)) else 0.0)))   # 敏感化期耐受积累变慢
            # 习惯化：同部位窗口内连续刺激收益递减；同刻事件视为一组不重复计数
            streak_since = parse_ts(st.get("streak_since"))
            streak = st.get("streak_count") if isinstance(st.get("streak_count"), int) else 0
            if streak_since is not None and et == streak_since:
                pass
            elif streak_since is not None and 0 < et - streak_since <= habit_window:
                streak += 1
            else:
                streak = 1
            st["streak_since"] = iso_ts(et)
            st["streak_count"] = streak
            habit_mult = habit_factor if streak >= habit_streak else 1.0
            # 敏感化 wind-up：窗口内连击累积（与习惯化对偶；重标定的频率依赖时间总和）。
            # 结构：敏感化中和耐受（SOP 模型"短间隔先易化"——wind-up 与 habituation 竞争），
            # 并减缓耐受积累、放大增益——三路合力使重度持续刺激可推至过载。
            b_cur = clamp01(st.get("sens_boost") or 0.0) if isinstance(st.get("sens_boost"), (int, float)) else 0.0
            last_ev = parse_ts(st.get("last_event_at"))
            if last_ev is not None and 0 < et - last_ev <= CONFIG.SENS_WINDOW:
                # 0.29.1 部位敏感化累积倍率（windup_mult）：该部位短窗连击的 wind-up 放大/抑制
                b_cur = min(CONFIG.SENS_CAP, b_cur + CONFIG.SENS_GAIN * clamp01(ev["s"]) * part_mult(
                    meta, "windup_mult", CONFIG.PART_WINDUP_MULT_RANGE[0], CONFIG.PART_WINDUP_MULT_RANGE[1]))
            st["sens_boost"] = round(b_cur, 6)
            eff_tol = clamp(tol * (1.0 - 0.5 * b_cur), 0.0, CONFIG.TOLERANCE_MAX)
            part_coeff = part_gain_coeff(meta)
            # 0.29.1 部位总增益倍率（gain_mult）：直接事件乘源部位倍率；
            # 0.30.0 长期身体记忆：部位系数再乘 longterm_mult（日级适应层，无键=1.0 行为不变）
            ev_gain = gain * ev_gain_mult * part_mult(meta, "gain_mult",
                                                      CONFIG.PART_GAIN_MULT_RANGE[0], CONFIG.PART_GAIN_MULT_RANGE[1]) \
                * longterm_mult(state, pid)
            v_old = clamp01(st.get("value") or 0.0)
            st["value"] = clamp01(v_old + event_gain(ev_gain * part_coeff, s_eff, eff_tol, in_cool)
                                   * habit_mult * (1.0 + 0.5 * b_cur) * boost_mult)
            _push_history(state, et, pid, v_old, st["value"], str(ev.get("type") or CONFIG.EVENT_TYPE_DEFAULT))
            # 生理传导：神经链多跳（耳尖→尾根 0.3、尾根→腰 0.2 ⇒ 刺激耳尖时腰感知 0.06）
            # 间接刺激不积累耐受/不触发冷却/不计数；每目标取最强路径
            for rid, ratio_cum in radiate_map(parts_meta, pid, max_hops=radiate_hops,
                                              mode=radiate_mode).items():
                rst = state["parts"].setdefault(rid, {})
                r_cool = parse_ts(rst.get("cool_until"))
                r_in_cool = r_cool is not None and et < r_cool
                # 传导目标用自己的部位增益系数（目标自身敏感度决定传导落点的强度）；
                # 0.29.1 gain_mult 同理取目标部位自己的（"该部位作为落点"的总增益）；
                # 0.30.0 longterm_mult 同样取目标自己的（该部位作为落点的长效敏感度）
                r_coeff = part_gain_coeff(parts_meta[rid])
                r_old = clamp01(rst.get("value") or 0.0)
                rst["value"] = clamp01(r_old
                                       + event_gain(gain * ev_gain_mult * part_mult(
                                           parts_meta[rid], "gain_mult",
                                           CONFIG.PART_GAIN_MULT_RANGE[0], CONFIG.PART_GAIN_MULT_RANGE[1])
                                           * longterm_mult(state, rid) * r_coeff,
                                           clamp01(s_eff * ratio_cum), tol, r_in_cool) * boost_mult)
                _push_history(state, et, rid, r_old, rst["value"], f"传导←{pid}")
            st["last_event_at"] = iso_ts(et)
            st["last_event_type"] = str(ev.get("type") or CONFIG.EVENT_TYPE_DEFAULT)[:12]
            if mat_ev is not None:
                st["last_material"] = str(ev["material"])[:16]     # 材质感受词渲染锚（inject 按时效读）
                st["last_material_at"] = iso_ts(et)
            tier = tier_of(st["value"], tiers)
            st["last_react_tier"] = tier
            if tier:
                new_tiers[pid] = tier
            if tier == "lose":
                st["cool_until"] = iso_ts(et + cool_secs)
        # overload 检查（事件后即刻）；余韵计时锚定事件时刻 et，补算时不被感知时刻拉长。
        # 0.30.0：阈值加情绪耦合抬升（低落 mood_threshold_add>0 → 更难失控，"绷着"）；
        # arousal 用合成值（语言托底 cap×weight ≤0.28，物理主导时才可能真正过载）
        if state["phase"] == "building":
            ar_all = arousal_of(state.get("parts") or {}, parts_meta,
                                verbal_arousal=state.get("verbal_arousal") or 0.0,
                                verbal_weight=verbal_weight)
            if ar_all >= overload_threshold(profile) + mood_thr_add:
                overload_fired = True
                state["phase"] = "afterglow"
                state["phase_until"] = iso_ts(et + ag_secs)
                phase_changed = True
                for pid in state["parts"]:
                    state["parts"][pid]["value"] = 0.0
                    state["parts"][pid]["last_react_tier"] = None
                    state["parts"][pid]["cool_until"] = None
                    state["parts"][pid]["sens_boost"] = 0.0
                new_tiers.clear()
                # 0.31.0 持续接触：过载进余韵——全部接触终止（高潮后接触自然结束）
                _ac = state.get("active_contact")
                if isinstance(_ac, dict):
                    _ac.clear()
                # 审查修复：耐受增量按公式在事件时刻已发生，overload 不回滚（清部位值/冷却即可）

    # 收尾：tolerance、arousal、趋势、updated_at
    flush_tol(now - cursor)
    state["tolerance"] = round(tol, 6)
    parts_now = state.get("parts") or {}
    for pid, v0 in start_values.items():
        try:
            parts_now[pid]["trend_delta"] = round(clamp01(parts_now[pid].get("value") or 0.0) - v0, 4)
        except (KeyError, TypeError):
            continue
    state["arousal"] = round(arousal_of(parts_now, parts_meta,
                                        verbal_arousal=state.get("verbal_arousal") or 0.0,
                                        verbal_weight=verbal_weight), 6)
    state["updated_at"] = iso_ts(now)
    return {"new_tiers": new_tiers, "overload": overload_fired, "phase_changed": phase_changed}
