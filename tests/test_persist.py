# -*- coding: utf-8 -*-
"""0.31.0 体验修复轮测试：持续接触（建立/延续/过期/否定/tick 通道/记账纪律）+
亲密语境增益（档位/取最大不叠乘/接入 advance）+ 阈值基线（react 0.30 / lose 0.50）+ 旧状态兼容。"""
import json
import math
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from astrbot_plugin_virtual_body import CONFIG  # noqa: E402
from astrbot_plugin_virtual_body.core import decay, events, persist_contact  # noqa: E402
from astrbot_plugin_virtual_body.core.state import normalize, state_signature  # noqa: E402

PROF_DIR = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "profiles"
DEMO_A = json.loads((PROF_DIR / "role_a.json").read_text(encoding="utf-8"))
DEMO_B = json.loads((PROF_DIR / "role_b.json").read_text(encoding="utf-8"))

T0 = 1_000_000.0
EAR_TAU = decay.part_tau(next(p for p in DEMO_A["parts"] if p["id"] == "ear_tip"))   # 900
PCFG = {"rate": CONFIG.CS_PERSIST_RATE, "max_gap": float(CONFIG.CS_PERSIST_MAX_GAP),
        "tick_every": CONFIG.CS_PERSIST_TICK_EVERY, "tick_interval": 60.0}


@pytest.fixture
def fixed_tz():
    """昼夜倍率按 UTC 白天算死（T0=1970-01-12T13:46Z），免宿主时区踩进夜间窗口。"""
    old = CONFIG.TZ_UTC_OFFSET
    CONFIG.TZ_UTC_OFFSET = 0
    yield
    CONFIG.TZ_UTC_OFFSET = old


def mk_state(**kw):
    st = normalize(None, DEMO_A)
    st["updated_at"] = decay.iso_ts(T0)
    st.update(kw)
    return st


def msg_round(st, evs, now):
    """main._advance_session 的消息驱动缩小版：延续事件合成 → advance → 直接事件落账。"""
    p_evs = persist_contact.persist_events(st, evs, now, PCFG)
    decay.advance_state(st, DEMO_A, (evs or []) + p_evs, now)
    persist_contact.record_contacts(st, evs, now)
    return p_evs


def _tier_min(profile, key):
    return next(float(t["min"]) for t in profile["tiers"] if t["key"] == key)


# ---- 修复一：持续接触——建立 → 消息延续 → 值持续上升 ----

def test_persist_climb_and_react_in_two_rounds(fixed_tz):
    # 一轮重咬(0.9) + 一轮延续（间隔消息）→ 反应档（0.31.0 阈值 react=0.30 的验收场景）
    st = mk_state()
    msg_round(st, [{"part": "ear_tip", "s": 0.9, "type": "touch", "t": T0}], T0)
    v1 = 0.25 * 0.96 * 0.9                      # 入场 arousal 0 → 无加成：0.216（不再"仅 notice"）
    assert abs(st["parts"]["ear_tip"]["value"] - v1) < 1e-9
    assert "ear_tip" in st["active_contact"]
    # 第二轮：不再描述触碰，纯聊天消息 → 延续增益（入场 arousal 0.216>0.15 → ×1.3）
    p_evs = msg_round(st, [], T0 + 60)
    assert len(p_evs) == 1 and p_evs[0]["src"] == "persist"
    assert p_evs[0]["s"] == pytest.approx(0.9 * CONFIG.CS_PERSIST_RATE, abs=1e-4)
    tol2 = 0.03 * 0.9 - 0.01                    # 耐受 60s 回落后 0.017
    expect = v1 * math.exp(-60 / EAR_TAU) + 0.25 * 0.96 * (0.9 * 0.4) * (1 - tol2) \
        * CONFIG.INTIMACY_BOOST_L1
    assert abs(st["parts"]["ear_tip"]["value"] - expect) < 1e-9
    assert decay.tier_of(st["parts"]["ear_tip"]["value"], DEMO_A["tiers"]) == "react"
    assert st["parts"]["ear_tip"]["value"] >= 0.30


def test_persist_expires_after_silence(fixed_tz):
    # 静默超过消息间隔窗（默认 300s）→ 接触过期清除，之后只剩纯衰减
    st = mk_state()
    msg_round(st, [{"part": "ear_tip", "s": 0.6, "type": "touch", "t": T0}], T0)
    msg_round(st, [], T0 + 240)                 # 窗口内消息 → 续期
    assert "ear_tip" in st["active_contact"]
    v_prev = st["parts"]["ear_tip"]["value"]
    st["updated_at"] = decay.iso_ts(T0 + 240)
    out = persist_contact.persist_events(st, [], T0 + 240 + 301, PCFG)   # 静默 301s > 300
    assert out == [] and st["active_contact"] == {}
    decay.advance_state(st, DEMO_A, None, T0 + 240 + 301)
    assert abs(st["parts"]["ear_tip"]["value"] - v_prev * math.exp(-301 / EAR_TAU)) < 1e-9


def test_persist_no_tolerance_no_history_no_count(fixed_tz):
    # 延续事件只增加值与 arousal：不积累耐受 / 不产生 history 流水 / 不计今日次数
    st = mk_state()
    persist_contact.record_contacts(st, [{"part": "ear_tip", "s": 0.9, "type": "touch"}], T0)
    p_evs = persist_contact.persist_events(st, [], T0 + 30, PCFG)
    assert len(p_evs) == 1
    decay.advance_state(st, DEMO_A, p_evs, T0 + 30)
    assert st["parts"]["ear_tip"]["value"] > 0
    assert st["tolerance"] == 0.0
    assert st["history"] == []
    assert st["events_today"]["count"] == 0


def test_persist_tick_channel_frequency(fixed_tz):
    # tick 通道：每 tick_every×tick_interval(3×60s) 一发；不刷新续期锚点 → 静默仍会过期
    st = mk_state()
    persist_contact.record_contacts(st, [{"part": "ear_tip", "s": 0.6, "type": "touch"}], T0)
    assert len(persist_contact.persist_events(st, None, T0 + 60, PCFG, tick=True)) == 1   # 首发无锚
    assert len(persist_contact.persist_events(st, None, T0 + 120, PCFG, tick=True)) == 0  # <180s
    assert len(persist_contact.persist_events(st, None, T0 + 240, PCFG, tick=True)) == 1  # ≥180s
    assert st["active_contact"]["ear_tip"]["last_renew"] == decay.iso_ts(T0)   # tick 不续期
    assert persist_contact.persist_events(st, None, T0 + 301, PCFG, tick=True) == []      # 过期
    assert st["active_contact"] == {}


def test_persist_msg_channel_min_gap_guard(fixed_tz):
    # 消息通道最小间隔：同一条消息的观察器/注入双推进只计一次增益，续期照常
    st = mk_state()
    persist_contact.record_contacts(st, [{"part": "ear_tip", "s": 0.6, "type": "touch"}], T0)
    assert persist_contact.persist_events(st, [], T0 + 5, PCFG) == []        # 5s < 20s：不叠增益
    assert st["active_contact"]["ear_tip"]["last_renew"] == decay.iso_ts(T0 + 5)   # 但已续期
    assert len(persist_contact.persist_events(st, [], T0 + 25, PCFG)) == 1   # 25s ≥ 20s：增益


def test_persist_skips_direct_pid(fixed_tz):
    # 本轮有直接事件的部位不叠加延续（全额增益已给）；其他接触照常延续
    st = mk_state()
    persist_contact.record_contacts(st, [{"part": "ear_tip", "s": 0.9, "type": "touch"}], T0)
    out = persist_contact.persist_events(
        st, [{"part": "ear_tip", "s": 0.9, "type": "touch", "t": T0 + 30}], T0 + 30, PCFG)
    assert out == []
    # 直接事件打别的部位：耳尖接触照常延续
    persist_contact.record_contacts(st, [{"part": "nape", "s": 0.5, "type": "touch"}], T0 + 30)
    out2 = persist_contact.persist_events(st, [{"part": "nape", "s": 0.5, "type": "touch",
                                               "t": T0 + 90}], T0 + 90, PCFG)
    assert [e["part"] for e in out2] == ["ear_tip"]
    # 直接事件落账覆盖刷新（s 与锚点更新）
    assert st["active_contact"]["nape"]["s"] == pytest.approx(0.5)


def test_persist_afterglow_no_gain(fixed_tz):
    # 余韵/不应期不叠延续增益（接触保留、续期照常），回平常后恢复
    st = mk_state(phase="afterglow", phase_until=decay.iso_ts(T0 + 1800))
    persist_contact.record_contacts(st, [{"part": "ear_tip", "s": 0.9, "type": "touch"}], T0)
    assert persist_contact.persist_events(st, [], T0 + 30, PCFG) == []
    assert "ear_tip" in st["active_contact"]


def test_negation_stops_clear_contact(fixed_tz):
    # "别摸耳朵" → extract_stops 命中 ear_tip → 清除已建立的接触（之后不再延续）
    assert events.extract_stops("别摸耳朵", DEMO_A) == ["ear_tip"]
    assert events.extract_stops("请不要继续摸我的耳朵", DEMO_A) == ["ear_tip"]
    assert events.extract_stops("继续揉耳朵", DEMO_A) == []
    assert events.extract_stops("今天天气不错", DEMO_A) == []
    assert events.extract_stops('"别摸耳朵"', DEMO_A) == []        # 引号内不算（歌词/转述）
    st = mk_state()
    persist_contact.record_contacts(st, [{"part": "ear_tip", "s": 0.9, "type": "touch"}], T0)
    assert persist_contact.clear_contacts(st, events.extract_stops("别摸耳朵", DEMO_A)) == 1
    assert persist_contact.persist_events(st, [], T0 + 60, PCFG) == []
    assert st["active_contact"] == {}


def test_overload_clears_contacts(fixed_tz):
    # 过载进余韵：全部接触终止（高潮后接触自然结束）
    st = mk_state()
    for pid in ("ear_tip", "nape", "tail_root"):
        st["parts"][pid]["value"] = 0.9
    persist_contact.record_contacts(st, [{"part": "ear_tip", "s": 0.9, "type": "touch"}], T0)
    summary = decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 0.1, "t": T0}], T0)
    assert summary["overload"] is True
    assert st["active_contact"] == {}


# ---- 修复二：亲密语境增益 ----

def test_intimacy_boost_unit():
    # 两档取最大不叠乘；无命中回 1.0（0.32.1 死代码清理：sleep 档移除，无 sleep 参数/常量）
    assert decay.intimacy_boost(0.2, 0.0) == CONFIG.INTIMACY_BOOST_L1
    assert decay.intimacy_boost(0.0, 0.06) == CONFIG.INTIMACY_BOOST_L1   # 语言氛围通道
    assert decay.intimacy_boost(0.4, 0.0) == CONFIG.INTIMACY_BOOST_L2
    assert decay.intimacy_boost(0.4, 0.0, {"l1": 1.0}) == CONFIG.INTIMACY_BOOST_L2   # L2 不被低 l1 覆盖
    assert decay.intimacy_boost(0.1, 0.04) == 1.0
    assert decay.intimacy_boost(0.0, 0.0) == 1.0                         # 无 sleep 档：零兴奋零氛围不加成
    assert not hasattr(CONFIG, "INTIMACY_BOOST_SLEEP")                    # sleep 档常量已随死代码移除
    assert decay.intimacy_boost(0.2, 0.0, {"l1": 1.6}) == 1.6            # 配置链覆盖


def test_intimacy_boost_30pct_higher_touch(fixed_tz):
    # arousal>0.15 时同一触碰的增益比无加成高 30%（1.3×）
    st_cold = mk_state()
    decay.advance_state(st_cold, DEMO_A, [{"part": "ear_tip", "s": 0.6, "t": T0}], T0)
    v_cold = st_cold["parts"]["ear_tip"]["value"]
    st_warm = mk_state()
    st_warm["parts"]["nape"]["value"] = 0.3      # 入场 arousal 0.3 > 0.15（nape 静息权重不稀释单点）
    decay.advance_state(st_warm, DEMO_A, [{"part": "ear_tip", "s": 0.6, "t": T0}], T0)
    gain_warm = st_warm["parts"]["ear_tip"]["value"]   # ear 自身从 0 起：增益即终值
    assert gain_warm == pytest.approx(v_cold * CONFIG.INTIMACY_BOOST_L1, abs=1e-9)


def test_intimacy_boost_verbal_channel(fixed_tz):
    # verbal_arousal > 0.05（语言氛围在场）同样触发 L1 加成
    st = mk_state(verbal_arousal=0.1)
    decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 0.6, "t": T0}], T0)
    assert st["parts"]["ear_tip"]["value"] == pytest.approx(
        0.25 * 0.96 * 0.6 * CONFIG.INTIMACY_BOOST_L1, abs=1e-9)


# ---- 修复三：内置阈值基线（react 0.30 / lose 0.50；用户覆盖层不动） ----

@pytest.mark.parametrize("profile", [DEMO_A, DEMO_B], ids=["role_a", "role_b"])
def test_builtin_tiers_react_lose(profile):
    assert _tier_min(profile, "react") == pytest.approx(0.30)
    assert _tier_min(profile, "lose") == pytest.approx(0.50)
    assert _tier_min(profile, "notice") == pytest.approx(0.15)   # 其余档不动
    assert _tier_min(profile, "overload") == pytest.approx(0.85)


# ---- 兼容：旧状态文件无 active_contact 字段不炸 ----

def test_normalize_active_contact_compat():
    assert normalize(None, DEMO_A)["active_contact"] == {}
    assert normalize({"active_contact": None}, DEMO_A)["active_contact"] == {}
    good = {"active_contact": {"ear_tip": {"action": "摸", "s": 0.9,
                                           "since": decay.iso_ts(T0), "last_renew": decay.iso_ts(T0)}}}
    out = normalize(good, DEMO_A)
    assert out["active_contact"]["ear_tip"]["s"] == pytest.approx(0.9)
    bad = {"active_contact": {"ghost": {"s": 0.5, "last_renew": decay.iso_ts(T0)},   # 未知部位
                              "ear_tip": {"s": 0.0, "last_renew": decay.iso_ts(T0)},  # s≤0
                              "nape": {"s": 0.5, "last_renew": "garbage"}}}           # 锚点不可解析
    assert normalize(bad, DEMO_A)["active_contact"] == {}
    # 接触变化进指纹（建立/续期/清除都要触发写盘判定）
    a = mk_state()
    b = mk_state()
    persist_contact.record_contacts(b, [{"part": "ear_tip", "s": 0.9, "type": "touch"}], T0)
    assert state_signature(a) != state_signature(b)


def test_old_state_without_contact_field_safe(fixed_tz):
    # 未过 normalize 的裸旧 state（无 active_contact 键）在延续合成与演算里都不炸
    raw = {"character": "role_a", "session": "x", "schema": 1, "phase": "building",
           "phase_until": None, "arousal": 0.0, "tolerance": 0.0, "parts": {},
           "updated_at": decay.iso_ts(T0)}
    assert persist_contact.persist_events(raw, [], T0 + 60, PCFG) == []
    decay.advance_state(raw, DEMO_A, [{"part": "ear_tip", "s": 0.9, "t": T0}], T0)
    assert raw["parts"]["ear_tip"]["value"] > 0
