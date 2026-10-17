"""演算层测试。覆盖验收用例：静置衰减、连击、afterglow、重启补算、冷却、耐受、phase 机器。"""
import json
import math
import pathlib
import time

from astrbot_plugin_virtual_body import CONFIG
from astrbot_plugin_virtual_body.core import decay
from astrbot_plugin_virtual_body.core.state import new_state, normalize

PROF_DIR = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "profiles"
DEMO_A = json.loads((PROF_DIR / "role_a.json").read_text(encoding="utf-8"))

T0 = 1_000_000.0
EAR_TAU = decay.part_tau(next(p for p in DEMO_A["parts"] if p["id"] == "ear_tip"))

import pytest


@pytest.fixture
def no_sens():
    """基础公式断言关掉 wind-up 干扰（敏感化有独立测试覆盖）。"""
    old = CONFIG.SENS_WINDOW
    CONFIG.SENS_WINDOW = 0.0
    yield
    CONFIG.SENS_WINDOW = old


def mk_state(ear=0.0, nape=0.0, tail=0.0, tol=0.17, updated_at=T0):
    st = normalize(None, DEMO_A)
    st["parts"]["ear_tip"]["value"] = ear
    st["parts"]["nape"]["value"] = nape
    st["parts"]["tail_root"]["value"] = tail
    st["tolerance"] = tol
    st["updated_at"] = decay.iso_ts(updated_at)
    return st


# ---- 验收用例 1：静置 5 分钟，ear_tip 期望值 ----

def test_rest_5min_decay():
    st = mk_state(ear=0.62)
    decay.advance_state(st, DEMO_A, None, T0 + 300)
    ear_tau = decay.part_tau(next(p for p in DEMO_A["parts"] if p["id"] == "ear_tip"))   # 期望与 profile τ 解耦
    expect = 0.62 * math.exp(-300 / ear_tau)
    assert abs(st["parts"]["ear_tip"]["value"] - expect) < 1e-9


# ---- 验收用例 2：连击三次从 0 起 ----

def test_three_hits_from_zero():
    st = mk_state(tol=0.17)
    evs = [{"part": "ear_tip", "s": 0.6, "type": "touch", "t": T0} for _ in range(3)]
    decay.advance_state(st, DEMO_A, evs, T0)
    # 同刻事件共用同一 tolerance（0.17）；ear sens=9 → 部位系数 0.96
    expect_v = 3 * 0.25 * 0.96 * 0.6 * (1 - 0.17)
    assert abs(st["parts"]["ear_tip"]["value"] - expect_v) < 1e-9
    # 耐受增量在事件后统一入账：0.17 + 3*0.03*0.6
    assert abs(st["tolerance"] - (0.17 + 3 * 0.03 * 0.6)) < 1e-9
    # ear 档位 react；传导使 tail_root 获得 3×0.25×0.84×(0.6×0.3)×0.83（tail sens=6 → 0.84）
    assert st["parts"]["ear_tip"]["last_react_tier"] == "react"
    v_tail = 3 * 0.25 * 0.84 * (0.6 * 0.3) * (1 - 0.17)
    assert abs(st["parts"]["tail_root"]["value"] - v_tail) < 1e-9
    # arousal 活跃部位归一：ear(0.9) 与 tail(0.6) 活跃，其余静息不稀释
    assert abs(st["arousal"] - (0.9 * expect_v + 0.6 * v_tail) / (0.9 + 0.6)) < 1e-6


# ---- 验收用例 3：跨 0.85 触发 overload，进入 afterglow，敏感度 ×1.4 ----

def test_overload_fires_afterglow_and_mult():
    st = mk_state(ear=0.9, nape=0.9, tail=0.9)
    evs = [{"part": "ear_tip", "s": 0.1, "type": "touch", "t": T0}]
    summary = decay.advance_state(st, DEMO_A, evs, T0)
    assert summary["overload"] is True
    assert st["phase"] == "afterglow"
    assert st["phase_until"] is not None
    for pid in st["parts"]:  # overload 清零并清冷却标记
        assert st["parts"][pid]["value"] == 0.0
        assert st["parts"][pid]["cool_until"] is None
    assert decay.effective_s(0.5, "afterglow") == 0.5 * 1.4
    assert decay.effective_s(0.5, "cooldown") == 0.5 * 0.6
    assert decay.effective_s(0.5, "building") == 0.5


def test_afterglow_then_cooldown_then_baseline():
    st = mk_state(ear=0.9, nape=0.9, tail=0.9)
    decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 0.1, "t": T0}], T0)
    until = decay.parse_ts(st["phase_until"])
    # 余韵未到点：保持
    decay.advance_state(st, DEMO_A, None, until - 1)
    assert st["phase"] == "afterglow"
    # 到点 → cooldown(+20min)；结束 → building（回 baseline）
    decay.advance_state(st, DEMO_A, None, until)
    assert st["phase"] == "cooldown"
    cd_until = decay.parse_ts(st["phase_until"])
    assert abs(cd_until - (until + CONFIG.COOLDOWN_SECONDS)) < 1e-6
    decay.advance_state(st, DEMO_A, None, cd_until)
    assert st["phase"] == "building"
    assert st["phase_until"] is None


# ---- 验收用例 4：重启补算，分步推进与一步推进一致（误差 <1%）----

def test_restart_recomputation_consistency():
    one = mk_state(ear=0.62, nape=0.5, tol=0.2)
    decay.advance_state(one, DEMO_A, None, T0 + 300)

    two = mk_state(ear=0.62, nape=0.5, tol=0.2)
    decay.advance_state(two, DEMO_A, None, T0 + 120)   # 进程存活段
    saved = json.loads(json.dumps(two))               # 落盘-重启-载入
    two2 = normalize(saved, DEMO_A)
    decay.advance_state(two2, DEMO_A, None, T0 + 300)  # 重启补算

    assert abs(one["parts"]["ear_tip"]["value"] - two2["parts"]["ear_tip"]["value"]) < 0.01 * one["parts"]["ear_tip"]["value"] + 1e-12
    assert abs(one["tolerance"] - two2["tolerance"]) < 1e-9


# ---- lose 冷却：触发后 20 秒冷却，冷却内 gain 减半 ----

def test_lose_cool_halves_gain(no_sens):
    # 0.31.0 亲密语境增益：入场 arousal=0.55 > 0.35 → 本轮增益 ×INTIMACY_BOOST_L2
    boost = CONFIG.INTIMACY_BOOST_L2
    v1 = 0.55 + 0.25 * 0.96 * 0.6 * (1 - 0.17) * boost   # 第一次事件后（ear 系数 0.96）
    st = mk_state(ear=0.55, tol=0.17)
    decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 0.6, "t": T0}], T0)
    assert st["parts"]["ear_tip"]["last_react_tier"] == "lose"     # 过 lose
    cool_until = decay.parse_ts(st["parts"]["ear_tip"]["cool_until"])
    assert cool_until is not None and abs(cool_until - (T0 + 20)) < 1e-6

    st2 = json.loads(json.dumps(st))
    decay.advance_state(st2, DEMO_A, [{"part": "ear_tip", "s": 0.6, "t": T0 + 10}], T0 + 10)
    tol2 = 0.17 + 0.03 * 0.6 - 0.01 * (10 / 60.0)
    expect = v1 * math.exp(-10 / EAR_TAU) + 0.25 * 0.96 * 0.6 * 0.5 * (1 - tol2) * boost
    assert abs(st2["parts"]["ear_tip"]["value"] - expect) < 1e-6

    # 冷却过期后恢复全额 gain：衰减到 T0+30 再来一次事件，gain 不再减半
    st3 = json.loads(json.dumps(st))
    decay.advance_state(st3, DEMO_A, [{"part": "ear_tip", "s": 0.6, "t": T0 + 30}], T0 + 30)
    tol3 = 0.17 + 0.03 * 0.6 - 0.01 * (30 / 60.0)
    expect3 = v1 * math.exp(-30 / EAR_TAU) + 0.25 * 0.96 * 0.6 * 1.0 * (1 - tol3) * boost
    assert abs(st3["parts"]["ear_tip"]["value"] - expect3) < 1e-6


# ---- 耐受：上限 0.6、每分钟回落 0.01 ----

def test_tolerance_cap_and_fall():
    assert decay.tolerance_step(0.59, 0, [1.0]) == 0.6
    assert abs(decay.tolerance_step(0.6, 600, []) - 0.5) < 1e-9
    assert decay.tolerance_step(0.2, 0, []) == 0.2


# ---- 空消息 / 无事件不加分 ----

def test_no_event_means_no_gain(no_sens):
    st = mk_state(ear=0.62)
    decay.advance_state(st, DEMO_A, [], T0 + 60)
    expect = 0.62 * math.exp(-60 / EAR_TAU)   # 纯衰减 1 分钟（τ=60）：×e^-1≈0.368
    assert abs(st["parts"]["ear_tip"]["value"] - expect) < 1e-9
    assert st["parts"]["ear_tip"]["value"] < 0.62  # 没有任何加分


# ---- phase 机器单元 ----

def test_phase_advance_pure():
    assert decay.phase_advance("building", None, T0) == ("building", None, False)
    assert decay.phase_advance("afterglow", T0, T0 - 1) == ("afterglow", T0, False)
    p, u, ch = decay.phase_advance("afterglow", T0, T0)
    assert (p, ch) == ("cooldown", True)
    p, u, ch = decay.phase_advance("cooldown", T0, T0 + CONFIG.COOLDOWN_SECONDS)
    assert (p, u, ch) == ("building", None, True)


# ---- 档位判定 ----

def test_tier_of_thresholds():
    tiers = DEMO_A["tiers"]
    assert decay.tier_of(0.0, tiers) is None
    assert decay.tier_of(0.14, tiers) is None
    assert decay.tier_of(0.15, tiers) == "notice"
    assert decay.tier_of(0.36, tiers) == "react"
    assert decay.tier_of(0.60, tiers) == "lose"
    assert decay.tier_of(1.0, tiers) == "overload"


# ---- 自由度轮：profile.params 覆盖 gain / cool_seconds / tolerance_gain ----

def test_profile_params_override():
    prof = dict(DEMO_A)
    prof["params"] = {"gain": 0.5, "cool_seconds": 40, "tolerance_gain": 0.05}
    st = mk_state(ear=0.0, tol=0.0)
    decay.advance_state(st, prof, [{"part": "ear_tip", "s": 1.0, "t": T0}], T0)
    assert abs(st["parts"]["ear_tip"]["value"] - 0.5 * 0.96) < 1e-9   # gain 0.25→0.5，再乘部位系数
    assert abs(st["tolerance"] - 0.05) < 1e-9                          # tolerance_gain 0.03→0.05
    # 坏值与越界回默认
    prof2 = dict(DEMO_A)
    prof2["params"] = {"gain": "快", "cool_seconds": 99999}
    st2 = mk_state(ear=0.55, tol=0.17)
    decay.advance_state(st2, prof2, [{"part": "ear_tip", "s": 0.6, "t": T0}], T0)
    # 0.31.0 亲密增益：入场 arousal 0.55 > 0.35 → 增益 ×L2
    assert abs(st2["parts"]["ear_tip"]["value"]
               - (0.55 + 0.25 * 0.96 * 0.6 * (1 - 0.17) * CONFIG.INTIMACY_BOOST_L2)) < 1e-9
    assert abs(decay.parse_ts(st2["parts"]["ear_tip"]["cool_until"]) - (T0 + CONFIG.COOL_SECONDS_AFTER_LOSE)) < 1e-6
    # 无 params 的 profile 照旧
    st3 = mk_state(ear=0.0, tol=0.0)
    decay.advance_state(st3, DEMO_A, [{"part": "ear_tip", "s": 1.0, "t": T0}], T0)
    assert abs(st3["parts"]["ear_tip"]["value"] - 0.25 * 0.96) < 1e-9


# ---- 找bug 轮回归：补算窗口跨 overload，历史事件按所处 phase 区间取倍率 ----

def _mk_afterglow_state(updated_at):
    st = mk_state(ear=0.0, nape=0.0, tail=0.0, tol=0.17, updated_at=updated_at)
    st["phase"] = "afterglow"
    st["phase_until"] = decay.iso_ts(T0 + 1800.0)
    return st


def test_backfill_event_in_afterglow_interval(no_sens):
    # 事件落在余韵区间（< au）：×1.4；now 时 phase 已推进为 cooldown
    st = _mk_afterglow_state(T0 + 1750)
    decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 0.5, "t": T0 + 1780}], T0 + 1900)
    tol_at = 0.17 - 0.01 * (30 / 60.0)                    # 同刻事件用旧耐受，增量下刻入账
    expect = 0.25 * 0.96 * 0.7 * (1 - tol_at) * math.exp(-120 / EAR_TAU)
    assert abs(st["parts"]["ear_tip"]["value"] - expect) < 1e-9


def test_backfill_event_in_cooldown_interval(no_sens):
    # 事件落在不应期区间（au ≤ t < au+1200）：×0.6
    st = _mk_afterglow_state(T0 + 1750)
    decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 0.5, "t": T0 + 1850}], T0 + 1900)
    tol_at = 0.17 - 0.01 * (100 / 60.0)
    expect = 0.25 * 0.96 * 0.3 * (1 - tol_at) * math.exp(-50 / EAR_TAU)
    assert abs(st["parts"]["ear_tip"]["value"] - expect) < 1e-9


def test_backfill_event_after_all_phases(no_sens):
    # 事件在余韵+不应期全部走完后：×1.0
    st = _mk_afterglow_state(T0 + 1700)
    decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 0.5, "t": T0 + 3200}], T0 + 3300)
    expect = 0.25 * 0.96 * 0.5 * (1 - 0.0) * math.exp(-100 / EAR_TAU)
    assert abs(st["parts"]["ear_tip"]["value"] - expect) < 1e-9


# ---- ST 借鉴轮：昼夜节律（深夜敏感倍数，跨午夜窗口）----

def test_circadian_night_mult():
    assert decay.circadian_mult(0.0, hour=23) == 1.1
    # 自由度轮：profile.params.night_mult 覆盖角色夜间倍率
    assert decay.circadian_mult(0.0, hour=23, night_mult=1.3) == 1.3
    assert decay.circadian_mult(0.0, hour=14, night_mult=1.3) == 1.0   # 白天不受影响
    assert decay.circadian_mult(0.0, hour=2) == 1.1
    assert decay.circadian_mult(0.0, hour=4) == 1.1
    assert decay.circadian_mult(0.0, hour=5) == 1.0
    assert decay.circadian_mult(0.0, hour=14) == 1.0
    assert decay.circadian_mult(0.0, hour=22) == 1.0
    from astrbot_plugin_virtual_body import CONFIG as C
    old = C.CIRCADIAN_ENABLED
    C.CIRCADIAN_ENABLED = False
    assert decay.circadian_mult(0.0, hour=23) == 1.0
    C.CIRCADIAN_ENABLED = old
    # 找bug 轮：统一时区口 TZ_UTC_OFFSET——et=54000 是 UTC 15:00，+8 = 北京 23:00 → 夜间
    old_off = C.TZ_UTC_OFFSET
    C.TZ_UTC_OFFSET = 8
    assert decay.circadian_mult(54000.0) == 1.1
    C.TZ_UTC_OFFSET = 0
    assert decay.circadian_mult(54000.0) == 1.0   # UTC 15:00 白天
    C.TZ_UTC_OFFSET = old_off
    # 日切共用时区口：UTC 17:00 + 8 → 北京次日 01:00，日期必然不同
    old_off2 = C.TZ_UTC_OFFSET
    C.TZ_UTC_OFFSET = 8
    beijing_day = time.strftime("%Y-%m-%d", decay.tz_local_time(61200.0))
    utc_day = time.strftime("%Y-%m-%d", time.gmtime(61200.0))
    C.TZ_UTC_OFFSET = old_off2
    assert beijing_day != utc_day


# ---- ST 借鉴轮：今日接触累计（周期键日切重置）----

def test_events_today_count_and_daily_reset():
    st = mk_state(ear=0.0, tol=0.0)
    decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 0.5, "t": T0}], T0)
    assert st["events_today"]["count"] == 1
    decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 0.5, "t": T0 + 60}], T0 + 60)
    assert st["events_today"]["count"] == 2
    # 日切：day 键变旧 → 清零
    st["events_today"]["day"] = "2000-01-01"
    decay.advance_state(st, DEMO_A, None, T0 + 120)
    assert st["events_today"]["count"] == 0
    assert st["events_today"]["day"] != "2000-01-01"


def test_backfill_tolerance_uses_s_eff():
    # 找bug 轮：余韵期耐受增量与增益同步放大（×1.4），不再是原始 s
    st = _mk_afterglow_state(T0 + 1000)
    decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 0.5, "t": T0 + 1050}], T0 + 1100)
    expect_tol = 0.17 + 0.03 * 0.7 - 0.01 * (100 / 60.0)   # 0.5×1.4=0.7；回落=事件时刻50s+收尾50s
    assert abs(st["tolerance"] - expect_tol) < 1e-6   # 存储时 round 6 位


# ---- 逻辑增强轮：刺激习惯化（同部位窗口内连续刺激收益递减）----

def test_habituation_diminishing_gain(no_sens):
    st = mk_state(ear=0.0, tol=0.0)
    evs = [{"part": "ear_tip", "s": 0.5, "t": T0 + 10 * i} for i in range(4)]
    decay.advance_state(st, DEMO_A, evs, T0 + 31)
    # 精确重演：streak 1,2,3 → 第 3 次（i=2）起 ×0.7；耐受逐刻入账；ear 系数 0.96
    v = 0.0
    tol = 0.0
    for i in range(4):
        s_eff = 0.5
        streak = i + 1
        habit = CONFIG.HABITUATION_FACTOR if streak >= CONFIG.HABITUATION_STREAK else 1.0
        v = v * math.exp(-10 / EAR_TAU) + 0.25 * 0.96 * s_eff * (1 - tol) * habit
        tol = tol + CONFIG.TOLERANCE_GAIN * s_eff - 0.01 * (10 / 60.0)
    v = v * math.exp(-1 / EAR_TAU)             # 最后 1s 推进到 now
    assert abs(st["parts"]["ear_tip"]["value"] - v) < 1e-9
    assert st["parts"]["ear_tip"]["streak_count"] == 4


def test_habituation_window_reset():
    # 超窗口重新计数：不触发习惯化
    st = mk_state(ear=0.0, tol=0.0)
    evs = [{"part": "ear_tip", "s": 0.5, "t": T0 + 10 * i * 7} for i in range(3)]  # 间隔 70s > 60
    decay.advance_state(st, DEMO_A, evs, T0 + 141)
    assert st["parts"]["ear_tip"]["streak_count"] == 1


# ---- 补充能力轮：生理传导（radiate）——主部位刺激带动关联部位 ----

def test_radiation_transfers_gain():
    # 耳尖受刺激（radiate tail_root:0.3）→ 尾根获得 0.3 倍有效刺激（tail 系数 0.84）
    st = mk_state(ear=0.0, tail=0.0, tol=0.0)
    st["events_today"] = {"day": time.strftime("%Y-%m-%d"), "count": 0}
    decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 0.5, "t": T0}], T0)
    tol_at = 0.0  # 同刻事件用旧耐受（0）
    direct = 0.25 * 0.96 * 0.5 * (1 - tol_at)
    assert abs(st["parts"]["ear_tip"]["value"] - direct) < 1e-9
    radiated = 0.25 * 0.84 * 0.15 * (1 - tol_at)   # s_eff×ratio = 0.5×0.3 = 0.15
    assert abs(st["parts"]["tail_root"]["value"] - radiated) < 1e-9
    # 间接刺激不计入今日次数与耐受
    assert st["events_today"]["count"] == 1
    assert abs(st["tolerance"] - (0 + 0.03 * 0.5)) < 1e-9
    assert st["parts"]["tail_root"]["last_react_tier"] is None   # 传导不写直接反应档位


def test_radiation_bad_ratio_ignored():
    # 坏倍率/自环/未知部位安全跳过
    prof = dict(DEMO_A)
    prof["parts"] = [
        {"id": "ear_tip", "label": "耳尖", "sens": 9, "half_life": 60, "aliases": ["耳朵"],
         "radiate": {"ear_tip": 0.9, "ghost": 0.5, "nape": "重"}},
        {"id": "nape", "label": "后颈", "sens": 7, "half_life": 90, "aliases": ["后颈"]},
    ]
    st = mk_state(ear=0.0, nape=0.0, tol=0.0)
    st["parts"]["nape"] = dict(st["parts"]["nape"])
    decay.advance_state(st, prof, [{"part": "ear_tip", "s": 0.5, "t": T0}], T0)
    assert st["parts"]["nape"]["value"] == 0.0    # 坏倍率不传导
    assert st["parts"]["ear_tip"]["value"] > 0    # 主部位正常


def test_habituation_profile_params(no_sens):
    # 自由度轮：profile.params 覆盖习惯化（streak=2 更容易触发、factor 更轻）
    prof = dict(DEMO_A)
    prof["params"] = {"habit_streak": 2, "habit_factor": 0.9}
    st = mk_state(ear=0.0, tol=0.0)
    evs = [{"part": "ear_tip", "s": 0.5, "t": T0 + 10 * i} for i in range(2)]
    decay.advance_state(st, prof, evs, T0 + 20)
    # 第 2 次即习惯化 ×0.9（DEMO_A 默认 streak=3 时无衰减）；收尾再衰减 10s 到 now
    tol2 = 0.03 * 0.5 - 0.01 * (10 / 60.0)
    v2 = (0.25 * 0.96 * 0.5 * (1 - 0.0) * math.exp(-10 / EAR_TAU) + 0.25 * 0.96 * 0.5 * (1 - tol2) * 0.9) * math.exp(-10 / EAR_TAU)
    assert abs(st["parts"]["ear_tip"]["value"] - v2) < 1e-9


# ---- 部位扩充轮：arousal 活跃归一（抗稀释）----

def test_arousal_active_normalization():
    # 单部位激活：arousal = 该部位自身值，与静息部位数量无关
    st = mk_state(ear=0.0, nape=0.0, tail=0.0, tol=0.0)
    st["parts"]["ear_tip"]["value"] = 0.3
    st["parts"]["nape"]["value"] = 0.0   # 静息
    meta = {str(p["id"]): p for p in DEMO_A["parts"]}
    assert abs(decay.arousal_of(st["parts"], meta) - 0.3) < 1e-9
    # 全静息 = 0
    st2 = mk_state(ear=0.0, nape=0.0, tail=0.0)
    assert decay.arousal_of(st2["parts"], meta) == 0.0
    # 双部位活跃：按 sens 加权（权重动态取自 profile，防校准后再破）
    st3 = mk_state(ear=0.0, nape=0.0, tail=0.0)
    st3["parts"]["ear_tip"]["value"] = 0.3
    st3["parts"]["nape"]["value"] = 0.5
    w_ear = next(p["sens"] for p in DEMO_A["parts"] if p["id"] == "ear_tip") / 10
    w_nape = next(p["sens"] for p in DEMO_A["parts"] if p["id"] == "nape") / 10
    assert abs(decay.arousal_of(st3["parts"], meta) - (w_ear * 0.3 + w_nape * 0.5) / (w_ear + w_nape)) < 1e-9


# ---- 借鉴轮：数值变更流水（display_data 思路）----

def test_history_ring_buffer():
    st = mk_state(ear=0.0, tol=0.0)
    evs = [{"part": "ear_tip", "s": 0.5, "type": "摸", "t": T0 + 10 * i} for i in range(210)]
    decay.advance_state(st, DEMO_A, evs, T0 + 2100)
    assert len(st["history"]) == 200                     # 0.30.0 扩容封顶 200 条（长期身体记忆数据面）
    parts_logged = {h["part"] for h in st["history"]}
    assert parts_logged == {"ear_tip", "tail_root", "waist"}   # 直接刺激 + 一跳/二跳传导都有流水
    last = st["history"][-1]
    assert last["part"] in ("ear_tip", "tail_root", "waist")
    assert 0 <= last["old"] <= 1 and 0 <= last["new"] <= 1
    # normalize 保留且封顶
    st2 = normalize(st, DEMO_A)
    assert len(st2["history"]) == 200


# ---- 审查轮：overload 不回滚事件时刻已发生的耐受增量 ----

def test_overload_keeps_tolerance_gain():
    st = mk_state(ear=0.9, nape=0.9, tail=0.9, tol=0.0)
    decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 0.9, "t": T0}], T0)
    assert st["tolerance"] > 0.0   # 公式 +0.03×0.9，不再被 overload 清掉


# ---- 神经链轮：多跳传导图（BFS ≤2 跳，最强路径，环安全）----

def test_radiate_map_bfs():
    meta = {
        "ear": {"id": "ear", "sens": 9, "radiate": {"tail": 0.3, "nape": 0.5}},
        "tail": {"id": "tail", "sens": 6, "radiate": {"waist": 0.2}},
        "nape": {"id": "nape", "sens": 7, "radiate": {"waist": 0.4}},
        "waist": {"id": "waist", "sens": 6, "radiate": {}},
    }
    m = decay.radiate_map(meta, "ear")
    assert m["tail"] == 0.3 and m["nape"] == 0.5
    assert abs(m["waist"] - 0.2) < 1e-9          # 最强路径 nape 0.5×0.4=0.2（胜过 tail 0.3×0.2=0.06）
    assert "ear" not in m                          # 不回环
    # 3 跳截断：waist→knee 边不应从 ear 到达（超 RADIATE_MAX_HOPS）
    meta["waist"]["radiate"] = {"knee": 0.9}
    meta["knee"] = {"id": "knee", "sens": 4, "radiate": {}}
    assert "knee" not in decay.radiate_map(meta, "ear")
    # 环：a↔b 互相辐射不炸
    ring = {"a": {"sens": 5, "radiate": {"b": 0.5}}, "b": {"sens": 5, "radiate": {"a": 0.5}}}
    m2 = decay.radiate_map(ring, "a")
    assert m2 == {"b": 0.5}


def test_radiation_chain_two_hops_in_advance():
    # 耳尖受刺激 → 尾根 0.3、腰侧 0.3×0.2=0.06（role_a 神经链）
    st = mk_state(ear=0.0, tail=0.0, tol=0.0)
    st["events_today"] = {"day": time.strftime("%Y-%m-%d"), "count": 0}
    decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 1.0, "t": T0}], T0)
    v_tail = st["parts"]["tail_root"]["value"]
    v_waist = st["parts"]["waist"]["value"]
    assert v_tail > 0
    assert 0 < v_waist < v_tail                   # 二跳感知显著弱于一跳
    # 精确：waist = 0.25×coeff(waist)×(1.0×0.3×0.2)×(1-0)——系数动态取自 profile
    waist_meta = next(p for p in DEMO_A["parts"] if p["id"] == "waist")
    assert abs(v_waist - 0.25 * decay.part_gain_coeff(waist_meta) * 0.06) < 1e-9


# ---- 0.28.1 联动自定义轮：radiate_hops / radiate_mode 参数化（profile_param 链，默认行为不变） ----

def _radiate_meta():
    return {
        "ear": {"id": "ear", "sens": 9, "radiate": {"tail": 0.3, "nape": 0.5}},
        "tail": {"id": "tail", "sens": 6, "radiate": {"waist": 0.2}},
        "nape": {"id": "nape", "sens": 7, "radiate": {"waist": 0.4}},
        "waist": {"id": "waist", "sens": 6, "radiate": {"knee": 0.9}},
        "knee": {"id": "knee", "sens": 4, "radiate": {}},
    }


def test_radiate_map_params_default_unchanged():
    # 默认行为逐位不变：不传参 == 显式默认（hops=2/mul）== None 回退
    meta = _radiate_meta()
    base = decay.radiate_map(meta, "ear")
    assert base == decay.radiate_map(meta, "ear", max_hops=2, mode="mul")
    assert base == decay.radiate_map(meta, "ear", max_hops=None, mode=None)
    assert base == decay.radiate_map(meta, "ear", max_hops=CONFIG.RADIATE_MAX_HOPS, mode="mul")
    assert base["waist"] == pytest.approx(0.2)   # 最强路径 nape 0.5×0.4（旧版语义）


def test_radiate_map_hops_param():
    meta = _radiate_meta()
    m1 = decay.radiate_map(meta, "ear", max_hops=1)
    assert set(m1) == {"tail", "nape"}             # 1 跳：二跳目标不可达
    m3 = decay.radiate_map(meta, "ear", max_hops=3)
    assert m3["knee"] == pytest.approx(0.5 * 0.4 * 0.9)   # 3 跳：ear→nape→waist→knee


def test_radiate_map_mode_sum():
    meta = _radiate_meta()
    m = decay.radiate_map(meta, "ear", mode="sum")
    # 求和：ear→nape→waist (0.5×0.4) + ear→tail→waist (0.3×0.2) = 0.26
    assert m["waist"] == pytest.approx(0.26)
    assert decay.radiate_map(meta, "ear", mode="mul")["waist"] == pytest.approx(0.2)


def test_advance_radiate_hops_profile_chain():
    # profile.params.radiate_hops=1 → 二跳目标（腰）不再感知；越界坏值回默认（2 跳恢复感知）
    prof = json.loads(json.dumps(DEMO_A))
    prof.setdefault("params", {})["radiate_hops"] = 1
    st = mk_state(ear=0.0, tail=0.0, tol=0.0)
    st["events_today"] = {"day": time.strftime("%Y-%m-%d"), "count": 0}
    decay.advance_state(st, prof, [{"part": "ear_tip", "s": 1.0, "t": T0}], T0)
    assert st["parts"]["tail_root"]["value"] > 0
    assert st["parts"]["waist"]["value"] == 0.0
    prof["params"]["radiate_hops"] = 99   # 越界 → profile_param 回默认 2
    st2 = mk_state(ear=0.0, tail=0.0, tol=0.0)
    st2["events_today"] = {"day": time.strftime("%Y-%m-%d"), "count": 0}
    decay.advance_state(st2, prof, [{"part": "ear_tip", "s": 1.0, "t": T0}], T0)
    assert st2["parts"]["waist"]["value"] > 0


def test_advance_radiate_mode_sum_and_fallback():
    # 双路径到 waist：sum 叠加 > mul 最强路径；坏 mode 字符串回 mul（默认行为）
    prof = json.loads(json.dumps(DEMO_A))
    parts = {p["id"]: p for p in prof["parts"]}
    parts["ear_tip"]["radiate"]["nape"] = 0.5
    parts["nape"]["radiate"] = {"waist": 0.4}
    prof.setdefault("params", {})["radiate_mode"] = "sum"
    st_sum = mk_state(ear=0.0, tail=0.0, tol=0.0)
    st_sum["events_today"] = {"day": time.strftime("%Y-%m-%d"), "count": 0}
    decay.advance_state(st_sum, prof, [{"part": "ear_tip", "s": 1.0, "t": T0}], T0)
    prof["params"]["radiate_mode"] = "mul"
    st_mul = mk_state(ear=0.0, tail=0.0, tol=0.0)
    st_mul["events_today"] = {"day": time.strftime("%Y-%m-%d"), "count": 0}
    decay.advance_state(st_mul, prof, [{"part": "ear_tip", "s": 1.0, "t": T0}], T0)
    assert st_sum["parts"]["waist"]["value"] > st_mul["parts"]["waist"]["value"] > 0
    prof["params"]["radiate_mode"] = "banana"   # 坏值 → 回 mul，与默认逐位一致
    st_bad = mk_state(ear=0.0, tail=0.0, tol=0.0)
    st_bad["events_today"] = {"day": time.strftime("%Y-%m-%d"), "count": 0}
    decay.advance_state(st_bad, prof, [{"part": "ear_tip", "s": 1.0, "t": T0}], T0)
    assert abs(st_bad["parts"]["waist"]["value"] - st_mul["parts"]["waist"]["value"]) < 1e-12


# ---- 文献重平衡轮：敏感化 wind-up + 过载可达性门 ----

def test_sensitization_accumulates_and_decays():
    # 窗口内连击累积 sens_boost，增益放大；静置按 SENS_TAU 衰减
    st = mk_state(ear=0.0, tol=0.0)
    evs = [{"part": "ear_tip", "s": 0.8, "t": T0 + 20 * (i + 1)} for i in range(5)]
    for k in range(1, 6):
        used = [e for e in evs if e["t"] <= T0 + 20 * k and not e.get("_u")]
        for e in used:
            e["_u"] = True
        decay.advance_state(st, DEMO_A, used, T0 + 20 * k)
    assert st["parts"]["ear_tip"]["sens_boost"] > 0.3            # 累积显著
    st2 = json.loads(json.dumps(st))
    decay.advance_state(st2, DEMO_A, None, T0 + 20 * 5 + 300)     # 静置 5 分钟
    assert st2["parts"]["ear_tip"]["sens_boost"] < st["parts"]["ear_tip"]["sens_boost"] * 0.1   # 衰减殆尽


def test_overload_reachable_multi_part():
    # 可达性门（基线修复）：三部位 20s 间隔重度连击应能触发 overload
    st = mk_state(ear=0.0, nape=0.0, tail=0.0, tol=0.0)
    evs = []
    for i in range(14):
        for p in ("ear_tip", "nape", "tail_root"):
            evs.append({"part": p, "s": 0.9, "t": T0 + 20 * (i + 1)})
    fired = False
    for k in range(1, 15):
        t = T0 + 20 * k
        used = [e for e in evs if e["t"] <= t and not e.get("_u")]
        for e in used:
            e["_u"] = True
        summary = decay.advance_state(st, DEMO_A, used, t)
        if summary["overload"]:
            fired = True
            break
    assert fired, "重度持续多部位刺激应能触发过载（wind-up 修复可达性）"


def test_overload_not_reachable_gentle():
    # 反向门：轻柔零散刺激不应误触过载
    st = mk_state(ear=0.0, tol=0.0)
    fired = False
    for i in range(6):
        summary = decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 0.3, "t": T0 + 300 * (i + 1)}], T0 + 300 * (i + 1))
        if summary["overload"]:
            fired = True
    assert not fired


# ---- 筋膜链轮：链张力计算 + 绷紧链减缓衰减（Myers Anatomy Trains）----

def test_chain_tensions_weighted():
    st = mk_state(ear=0.0, nape=0.0, tail=0.0, tol=0.0)
    st["parts"]["nape"]["value"] = 0.8
    st["parts"]["tail_root"]["value"] = 0.4
    meta = {p["id"]: p for p in DEMO_A["parts"]}
    t = decay.chain_tensions(st["parts"], meta)
    assert 0 < t["后侧链"] < 0.8   # sens 加权均值，介于两值之间
    assert t["前侧链"] == 0.0       # 链上部位值全 0 → 张力 0
    st2 = mk_state(tol=0.0)
    assert decay.chain_tensions(st2["parts"], meta)["后侧链"] == 0.0


def test_tense_chain_slows_decay():
    # 后侧链绷紧（nape 0.9）时 sole 衰减比平时慢（τ×1.2）
    meta = {p["id"]: p for p in DEMO_A["parts"]}
    st = mk_state(tol=0.0)
    st["parts"]["nape"]["value"] = 0.9      # 抬高链张力
    st["parts"]["sole"]["value"] = 0.6
    decay.advance_state(st, DEMO_A, None, T0 + 60)
    v_tense = st["parts"]["sole"]["value"]
    st2 = mk_state(tol=0.0)
    st2["parts"]["nape"]["value"] = 0.0
    st2["parts"]["sole"]["value"] = 0.6
    decay.advance_state(st2, DEMO_A, None, T0 + 60)
    v_loose = st2["parts"]["sole"]["value"]
    assert v_tense > v_loose                # 绷紧链上部位消退更慢
    sole_tau = decay.part_tau(meta["sole"])
    assert abs(v_loose - 0.6 * math.exp(-60 / sole_tau)) < 1e-9   # 松弛态=基础公式


# ---- 数值钳制：事件增益不超过 1 ----

def test_gain_clamped_at_one():
    st = mk_state(ear=0.98, tol=0.0)
    decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 1.0, "t": T0}], T0)
    assert st["parts"]["ear_tip"]["value"] == 1.0


# ---- 找bug 轮回归：overload 余韵终点锚定事件时刻，不被感知时刻拉长 ----

def test_overload_until_anchored_to_event_time():
    st = mk_state(ear=0.9, nape=0.9, tail=0.9, updated_at=T0)
    # 事件在 T0 发生，感知（now）在 T0+5：余韵终点应是 T0+1800 而不是 T0+1805
    decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 0.1, "t": T0}], T0 + 5)
    assert st["phase"] == "afterglow"
    assert abs(decay.parse_ts(st["phase_until"]) - (T0 + CONFIG.AFTERGLOW_SECONDS)) < 1e-6


# ---- 找bug 轮回归：停机跨过余韵+不应期，重启时 phase 应直接回 baseline ----

def test_phase_catchup_after_long_shutdown():
    st = mk_state(ear=0.9, nape=0.9, tail=0.9)
    decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 0.1, "t": T0}], T0)
    until = decay.parse_ts(st["phase_until"])
    st2 = json.loads(json.dumps(st))  # 落盘-重启-载入
    decay.advance_state(st2, DEMO_A, None, until + CONFIG.COOLDOWN_SECONDS + 3600)
    assert st2["phase"] == "building"
    assert st2["phase_until"] is None


# ---- 找bug 轮回归：afterglow→cooldown 终点 = 余韵终点 + 20min，不随感知时刻漂移 ----

def test_phase_cooldown_ends_relative_to_afterglow_end():
    st = mk_state(ear=0.9, nape=0.9, tail=0.9)
    decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 0.1, "t": T0}], T0)
    until = decay.parse_ts(st["phase_until"])
    st2 = json.loads(json.dumps(st))
    decay.advance_state(st2, DEMO_A, None, until + 600)  # 余韵结束后 10 分钟才被感知
    assert st2["phase"] == "cooldown"
    cd = decay.parse_ts(st2["phase_until"])
    assert abs(cd - (until + CONFIG.COOLDOWN_SECONDS)) < 1e-6


# ---- 0.27.8 点位失效修复轮：内置 τ 交付基线（600-1500）节奏回归 ----
# 事故背景：覆盖层旧 τ(60-150) 优先级高于内置 → τ×10 从未对 role_a 生效。本组锁内置基线
# 不回落——若 half_life 回到旧量级（强触 1 分钟跌破注意档），下列断言立即翻红。

DEMO_B = json.loads((PROF_DIR / "role_b.json").read_text(encoding="utf-8"))


@pytest.fixture
def fixed_tz():
    """昼夜倍率按 UTC 白天算死（T0=1970-01-12T13:46Z），免宿主时区踩进夜间窗口。"""
    old = CONFIG.TZ_UTC_OFFSET
    CONFIG.TZ_UTC_OFFSET = 0
    yield
    CONFIG.TZ_UTC_OFFSET = old


def _tier_min(profile, key):
    return next(float(t["min"]) for t in profile["tiers"] if t["key"] == key)


def _ear(profile):
    return next(p for p in profile["parts"] if p["id"] == "ear_tip")


@pytest.mark.parametrize("profile", [DEMO_A, DEMO_B], ids=["role_a", "role_b"])
def test_builtin_half_life_baseline_600_1500(profile):
    # 内置交付基线：全部部位 half_life 落在 600-1500（0.23.3 τ×10 后的确认量级）
    hls = [float(p["half_life"]) for p in profile["parts"]]
    assert hls and all(600.0 <= hl <= 1500.0 for hl in hls)


@pytest.mark.parametrize("profile", [DEMO_A, DEMO_B], ids=["role_a", "role_b"])
def test_ear_pulse_pace_after_calibration(profile):
    # 单次强触耳朵（s=1，tol=0）：注意档稳过 5 分钟、余温 ~20 分钟、45 分钟散尽
    # （CHANGELOG 0.23.3 口径，0.27.8 复核内置基线完好）
    ear = _ear(profile)
    tau = decay.part_tau(ear)
    v0 = CONFIG.GAIN_DEFAULT * decay.part_gain_coeff(ear)   # ear 首触值（耐受 0 起步）
    notice = _tier_min(profile, "notice")

    st = normalize(None, profile)
    st["parts"]["ear_tip"]["value"] = v0
    st["updated_at"] = decay.iso_ts(T0)
    decay.advance_state(st, profile, None, T0 + 300)
    assert abs(st["parts"]["ear_tip"]["value"] - v0 * math.exp(-300 / tau)) < 1e-9   # 纯衰减精确重演
    assert st["parts"]["ear_tip"]["value"] >= notice          # 5 分钟仍在注意档

    st2 = normalize(None, profile)
    st2["parts"]["ear_tip"]["value"] = v0
    st2["updated_at"] = decay.iso_ts(T0)
    decay.advance_state(st2, profile, None, T0 + 1200)
    assert st2["parts"]["ear_tip"]["value"] > CONFIG.RENDER_FOLD_BELOW   # 20 分钟余温未散

    st3 = normalize(None, profile)
    st3["parts"]["ear_tip"]["value"] = v0
    st3["updated_at"] = decay.iso_ts(T0)
    decay.advance_state(st3, profile, None, T0 + 2700)
    assert st3["parts"]["ear_tip"]["value"] < CONFIG.RENDER_FOLD_BELOW   # 45 分钟散尽（低于折叠线）


@pytest.mark.parametrize("profile", [DEMO_A, DEMO_B], ids=["role_a", "role_b"])
def test_two_touches_5min_apart_stack_to_react(profile, fixed_tz):
    # 5 分钟间隔两触即叠进反应档（消息间隔内可累积）；τ 若回落旧量级则余温散尽叠不动
    ear = _ear(profile)
    tau = decay.part_tau(ear)
    v0 = CONFIG.GAIN_DEFAULT * decay.part_gain_coeff(ear)
    st = normalize(None, profile)
    st["updated_at"] = decay.iso_ts(T0)
    decay.advance_state(st, profile, [
        {"part": "ear_tip", "s": 1.0, "type": "摸", "t": T0},
        {"part": "ear_tip", "s": 1.0, "type": "摸", "t": T0 + 300},
    ], T0 + 300)
    react = _tier_min(profile, "react")
    assert st["parts"]["ear_tip"]["value"] >= react
    # 精确重演：第二触时耐受已回落到 0（首触增量 0.03 < 5 分钟回落 0.05）
    expect = v0 * math.exp(-300 / tau) + v0
    assert abs(st["parts"]["ear_tip"]["value"] - expect) < 1e-9


# ---- 0.27.9 面板化：phase 时长/倍率/夜间窗口参数化（vb_afterglow_* / vb_cooldown_* / vb_night_window） ----

@pytest.mark.parametrize("ag_secs,cd_secs", [(600, 300), (7200, 7200), (300, 301)])
def test_phase_durations_parameterized(ag_secs, cd_secs):
    # advance_state 注入 afterglow/cooldown 时长：overload 锚点与 cooldown 终点严格按注入值
    st = mk_state(ear=0.9, nape=0.9, tail=0.9)
    decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 0.1, "t": T0}], T0,
                        afterglow_seconds=ag_secs)
    au = decay.parse_ts(st["phase_until"])
    assert st["phase"] == "afterglow"
    assert abs(au - (T0 + ag_secs)) < 1e-6
    st2 = json.loads(json.dumps(st))
    decay.advance_state(st2, DEMO_A, None, au, cooldown_seconds=cd_secs)   # 到点 → cooldown
    assert st2["phase"] == "cooldown"
    assert abs(decay.parse_ts(st2["phase_until"]) - (au + cd_secs)) < 1e-6
    # phase_advance 纯函数同口径
    _, u, _ = decay.phase_advance("afterglow", T0, T0, cooldown_seconds=cd_secs)
    assert abs(u - (T0 + cd_secs)) < 1e-6


def test_afterglow_mult_parameterized(no_sens):
    # 余韵期事件倍率按注入 afterglow_mult 放大（默认 1.4；注入 1.2 → 0.5×1.2 精确重演）
    st = _mk_afterglow_state(T0 + 1000)
    decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 0.5, "t": T0 + 1050}], T0 + 1100,
                        afterglow_mult=1.2)
    tol_at = 0.17 - 0.01 * (50 / 60.0)     # 锚点→事件 50s 的耐受回落
    expect = 0.25 * 0.96 * (0.5 * 1.2) * (1 - tol_at) * math.exp(-50 / EAR_TAU)
    assert abs(st["parts"]["ear_tip"]["value"] - expect) < 1e-9


def test_cooldown_mult_parameterized(no_sens):
    # 不应期事件倍率按注入 cooldown_mult 缩小（默认 0.6；注入 0.8 → 0.5×0.8）
    st = _mk_afterglow_state(T0 + 1750)
    decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 0.5, "t": T0 + 1850}], T0 + 1900,
                        cooldown_seconds=1200, cooldown_mult=0.8)
    tol_at = 0.17 - 0.01 * (100 / 60.0)
    expect = 0.25 * 0.96 * (0.5 * 0.8) * (1 - tol_at) * math.exp(-50 / EAR_TAU)
    assert abs(st["parts"]["ear_tip"]["value"] - expect) < 1e-9


def test_effective_s_phase_mults_injected():
    # effective_s 支持注入倍率表；None 回 CONFIG.PHASE_SENS_MULT
    table = {"afterglow": 1.2, "cooldown": 0.8}
    assert decay.effective_s(0.5, "afterglow", table) == 0.5 * 1.2
    assert decay.effective_s(0.5, "cooldown", table) == 0.4
    assert decay.effective_s(0.5, "building", table) == 0.5
    assert decay.effective_s(0.5, "afterglow") == 0.5 * CONFIG.AFTERGLOW_SENS_MULT
    assert decay.effective_s(0.5, "cooldown") == 0.5 * CONFIG.COOLDOWN_SENS_MULT


def test_parse_night_window():
    assert decay.parse_night_window("23-5") == (23, 5)
    assert decay.parse_night_window(" 0 - 23 ") == (0, 23)
    assert decay.parse_night_window("5-23") == (5, 23)
    for bad in ("", None, "23", "23-5-7", "a-b", "24-5", "-1-5", "5-5", "23,5"):
        assert decay.parse_night_window(bad) is None, bad


def test_night_window_parameterized(no_sens, fixed_tz):
    # T0 为 UTC 13:46（默认窗口白天）；注入 "0-23" 后全天夜间 → 增益 ×night_mult。
    # 注意 s 用 0.5：s=1.0 已饱和，任何倍率都推不过 clamp01 上限。
    st = mk_state(ear=0.0, tol=0.0)
    decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 0.5, "t": T0}], T0,
                        night_window="0-23")
    v_night = st["parts"]["ear_tip"]["value"]
    st2 = mk_state(ear=0.0, tol=0.0)
    decay.advance_state(st2, DEMO_A, [{"part": "ear_tip", "s": 0.5, "t": T0}], T0)
    v_day = st2["parts"]["ear_tip"]["value"]
    assert abs(v_night - v_day * CONFIG.CIRCADIAN_NIGHT_MULT) < 1e-9
    # 元组形态等价；坏字符串回默认窗口（白天 → 无倍率）
    st3 = mk_state(ear=0.0, tol=0.0)
    decay.advance_state(st3, DEMO_A, [{"part": "ear_tip", "s": 0.5, "t": T0}], T0,
                        night_window=(0, 23))
    assert abs(st3["parts"]["ear_tip"]["value"] - v_night) < 1e-9
    st4 = mk_state(ear=0.0, tol=0.0)
    decay.advance_state(st4, DEMO_A, [{"part": "ear_tip", "s": 0.5, "t": T0}], T0,
                        night_window="garbage")
    assert abs(st4["parts"]["ear_tip"]["value"] - v_day) < 1e-9
