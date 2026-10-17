"""长期身体记忆（0.30.0 日级适应层）。

身体会"记住"近一周的触碰模式：高频被直接触碰的部位长效敏感化（神经可塑性——
反复刺激的通路更易激活），长期零触碰且原本敏感的部位迟钝化（去传入敏度回落）。
统计面 = state["history"] 流水（decay._push_history 扩容到 ADAPT_HISTORY_LIMIT 条）：
  · 直接命中 = src 非传导（"传导←X" 是间接刺激）且 new≥old（回落类如 space_leave 不算接触）；
  · 窗口 = 近 ADAPT_WINDOW_DAYS 天；
  · 产出 = state["longterm_mult"]: {pid: 0.8~1.3}，decay.event_gain 的部位系数乘它
    （无键=1.0，行为不变——normalize 兼容旧状态文件）。
触发纪律：main 每日一次（日键防重入，走 decay.tz_local_time 统一时区入口）。
纯函数模块：不 import astrbot、不做 IO。
"""
from __future__ import annotations

import time
import math
from typing import Any

from .. import CONFIG
from . import decay


def direct_hit_counts(state: dict[str, Any], now: float | None = None,
                      window_days: float | None = None) -> dict[str, int]:
    """近 N 天各部位直接命中次数（纯函数）。传导（src 以"传导"开头）与回落（new<old）不计。"""
    now = float(now if now is not None else time.time())
    days = CONFIG.ADAPT_WINDOW_DAYS if window_days is None else float(window_days)
    cutoff = now - days * 86400.0
    counts: dict[str, int] = {}
    hist = state.get("history") if isinstance(state.get("history"), list) else []
    for h in hist:
        if not isinstance(h, dict):
            continue
        if str(h.get("src") or "").startswith("传导"):
            continue
        t = decay.parse_ts(h.get("t"))
        if t is None or t < cutoff or t > now:
            continue
        try:
            new = float(h.get("new") or 0.0)
            old = float(h.get("old") or 0.0)
            if not math.isfinite(new) or not math.isfinite(old) or new < old:
                continue   # 回落（如她离开的 space_leave）不是触碰
        except (TypeError, ValueError, OverflowError):
            continue
        pid = str(h.get("part") or "")
        if pid:
            counts[pid] = counts.get(pid, 0) + 1
    return counts


def daily_adaptation(state: dict[str, Any], profile: dict[str, Any],
                     now: float | None = None) -> tuple[dict[str, float], int]:
    """日级适应一步（纯函数·就地写 state["longterm_mult"]，幂等）：
    高频（窗口内直接命中 > ADAPT_FREQ_THRESHOLD）→ ×ADAPT_SENSITIZE_PER_DAY（钳上限 +30%）；
    零触碰且该部位原 sens≥ADAPT_DULL_MIN_SENS → ×ADAPT_DULL_PER_DAY（钳下限 -20%）；
    两者皆非 → 保持既有系数（首次无系数=1.0 中性）。
    返回 (longterm_mult dict, 发生变化的部位数)——main 拿去落盘与日志。"""
    now = float(now if now is not None else time.time())
    counts = direct_hit_counts(state, now)
    prev = state.get("longterm_mult") if isinstance(state.get("longterm_mult"), dict) else {}
    lo, hi = CONFIG.ADAPT_MULT_RANGE
    out: dict[str, float] = {}
    changed = 0
    for p in profile.get("parts") or []:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        pid = str(p["id"])
        try:
            m = float(prev.get(pid, 1.0))
        except (TypeError, ValueError):
            m = 1.0
        if not math.isfinite(m):
            m = 1.0
        m = max(lo, min(hi, m))
        try:
            sens = float(p.get("sens") or 0.0)
        except (TypeError, ValueError):
            sens = 0.0
        hits = counts.get(pid, 0)
        if hits > CONFIG.ADAPT_FREQ_THRESHOLD:
            m2 = min(hi, m * CONFIG.ADAPT_SENSITIZE_PER_DAY)
        elif hits == 0 and sens >= CONFIG.ADAPT_DULL_MIN_SENS:
            m2 = max(lo, m * CONFIG.ADAPT_DULL_PER_DAY)
        else:
            m2 = m
        m2 = round(m2, 4)
        if abs(m2 - m) > 1e-9:
            changed += 1
        out[pid] = m2
    state["longterm_mult"] = out
    return (out, changed)
