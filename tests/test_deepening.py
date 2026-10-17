# -*- coding: utf-8 -*-
"""0.30.0 存在深化轮测试：语言调情→身体兴奋 / 长期身体记忆 / 情绪↔身体耦合 / 主动触碰意图。"""
import itertools
import time
import json
import math
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from astrbot_plugin_virtual_body import CONFIG  # noqa: E402
from astrbot_plugin_virtual_body.core import adaptation, decay, inject  # noqa: E402
from astrbot_plugin_virtual_body.core import mood_signals as signals  # noqa: E402
from astrbot_plugin_virtual_body.core import proactive_intent as pi  # noqa: E402
from astrbot_plugin_virtual_body.core.state import normalize, state_signature  # noqa: E402

PROF_DIR = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "profiles"
DEMO_A = json.loads((PROF_DIR / "role_a.json").read_text(encoding="utf-8"))

T0 = 1_000_000.0
PARTS_META = {str(p["id"]): p for p in DEMO_A["parts"]}


@pytest.fixture
def no_sens():
    """增益类断言关掉 wind-up 干扰（与 test_decay 同款隔离）。"""
    old = CONFIG.SENS_WINDOW
    CONFIG.SENS_WINDOW = 0.0
    yield
    CONFIG.SENS_WINDOW = old


def mk_state(**kw):
    st = normalize(None, DEMO_A)
    st["updated_at"] = decay.iso_ts(T0)
    st.update(kw)
    return st


# ---- 任务一：语言调情→身体兴奋 ----

def test_verbal_step_intimate_climb_to_cap():
    st = mk_state()
    cfg = {"rate": 0.006, "cap": 0.35, "tau": 900.0}
    v = 0.0
    for i in range(1, 61):   # 60 次调用（模拟每分钟 tick 持续调情 1 小时）
        v = decay.verbal_arousal_step(st, True, T0 + 60 * i, cfg)
    assert abs(v - 0.35) < 1e-9          # 0.006×60=0.36 → 钳 cap 0.35
    assert st["verbal_arousal"] == pytest.approx(0.35)
    assert st["verbal_at"] is not None


def test_verbal_step_decay_when_stops():
    st = mk_state(verbal_arousal=0.35, verbal_at=decay.iso_ts(T0))
    v = decay.verbal_arousal_step(st, False, T0 + 900.0, {"tau": 900.0})
    assert v == pytest.approx(0.35 * math.exp(-1.0), abs=1e-6)   # 一个 τ 衰减到 1/e
    v2 = decay.verbal_arousal_step(st, False, T0 + 900.0 + 300.0, {"tau": 900.0})
    assert v2 == pytest.approx(v * math.exp(-1.0 / 3.0), abs=1e-6)   # 从新锚点续衰


def test_verbal_step_neutral_no_climb():
    st = mk_state(verbal_arousal=0.2, verbal_at=decay.iso_ts(T0))
    v = decay.verbal_arousal_step(st, False, T0, {})   # dt=0：原样
    assert v == pytest.approx(0.2)
    v2 = decay.verbal_arousal_step(st, True, T0, {})   # intimate +rate（cfg 空 → CONFIG）
    assert v2 == pytest.approx(0.2 + CONFIG.VERBAL_AROUSAL_RATE)


def test_arousal_of_verbal_floor():
    parts = {"ear_tip": {"value": 0.2}}
    meta = {"ear_tip": PARTS_META["ear_tip"]}
    phys = decay.arousal_of(parts, meta)
    assert phys == pytest.approx(0.2)
    # 托底：max(physical, verbal×weight)
    assert decay.arousal_of(parts, meta, verbal_arousal=0.5) == pytest.approx(0.5 * 0.8)
    assert decay.arousal_of(parts, meta, verbal_arousal=0.5, verbal_weight=1.0) == pytest.approx(0.5)
    # verbal 低于 physical 时不压低（max 语义）
    assert decay.arousal_of(parts, meta, verbal_arousal=0.1) == pytest.approx(0.2)
    # 缺省 verbal=0 与旧版逐位一致
    assert decay.arousal_of(parts, meta) == decay.arousal_of(parts, meta, verbal_arousal=0.0)


def test_advance_state_arousal_includes_verbal():
    st = mk_state(verbal_arousal=0.3)
    decay.advance_state(st, DEMO_A, None, T0)
    assert st["arousal"] == pytest.approx(0.3 * CONFIG.VERBAL_AROUSAL_WEIGHT)   # 全静息 → 语言托底


def test_render_verbal_segment():
    st = mk_state(arousal=0.5, verbal_arousal=0.24)
    block = inject.render(st, DEMO_A)
    assert "语言撩拨 0.2" in block            # 紧凑形态一位小数
    st2 = mk_state(arousal=0.5, verbal_arousal=0.05)
    assert "语言撩拨" not in inject.render(st2, DEMO_A)   # 低于门槛不渲染（token 纪律）


_write_pc_seq = itertools.count()
_write_pc_base = time.time_ns()


def _write_pc(data_dir: pathlib.Path, users: dict):
    pc = data_dir.parent / "unrelated_message_source"
    pc.mkdir(parents=True, exist_ok=True)
    path = pc / "unrelated_records.json"
    path.write_text(json.dumps({"users": users}), encoding="utf-8")
    # Windows 下连续两次写入可能落在同一 mtime 粒度内（st_mtime_ns 不变），
    # read_verbal 的 mtime 缓存会读到上一版——主动戳单调递增 mtime 消除竞态
    stamp = _write_pc_base + next(_write_pc_seq) * 10_000_000
    os.utime(path, ns=(stamp, stamp))


def test_read_verbal_whitelist_and_intimate(tmp_path):
    # External user activity is explicit, scoped and provided through the real API.
    api = provider("user_activity", {"emotion": "calm", "is_intimate": False})
    out = signal(api, "user_activity")
    assert out["available"] is True and out["is_intimate"] is False and out["emotion"] == "calm"
    out2 = signal(api, "user_activity", scope=dict(SCOPE, user_id="other"))
    assert out2["available"] is False
    assert api.update("synthetic", SCOPE, "user_activity", {"emotion": "warm", "is_intimate": True}, observed_at=1001)
    out3 = signal(api, "user_activity", now=1001)
    assert out3["available"] is True and out3["emotion"] == "warm" and out3["is_intimate"] is True
    # Private conversation files cannot silently supply activity in this distribution.
    _write_pc(tmp_path, {"111": {"intent_profile": {"emotion": "flirty"}}})
    assert signals.read_verbal(str(tmp_path), whitelist=["111"])["is_intimate"] is False
    assert signals.read_verbal(str(tmp_path), emotions=("flirty",))["available"] is False


def test_read_verbal_missing_pc(tmp_path):
    out = signals.read_verbal(str(tmp_path / "vb"), whitelist=["111"])
    assert out == {"available": False, "emotion": "", "is_intimate": False}


def test_normalize_verbal_fields():
    st = normalize(None, DEMO_A)
    assert st["verbal_arousal"] == 0.0 and st["verbal_at"] is None
    bad = normalize({"verbal_arousal": 9.9, "verbal_at": "bad"}, DEMO_A)
    assert bad["verbal_arousal"] == 1.0 and bad["verbal_at"] is None   # 数值钳制，坏时间锚点清理


# ---- 任务二：长期身体记忆 ----

def _hist(entries):
    return [{"t": decay.iso_ts(t), "part": p, "old": o, "new": n, "src": s}
            for t, p, o, n, s in entries]


def test_direct_hit_counts_filters():
    st = mk_state(history=_hist([
        (T0 - 100, "ear_tip", 0.1, 0.3, "摸"),               # 直接命中 ✓
        (T0 - 200, "tail_root", 0.0, 0.05, "传导←ear_tip"),  # 传导 ✗
        (T0 - 300, "ear_tip", 0.3, 0.28, "space_leave"),     # 回落 ✗
        (T0 - 8 * 86400, "ear_tip", 0.1, 0.3, "摸"),         # 7 天窗口外 ✗
        (T0 - 400, "nape", 0.0, 0.2, "舔"),                  # 直接命中 ✓
    ]))
    counts = adaptation.direct_hit_counts(st, now=T0)
    assert counts == {"ear_tip": 1, "nape": 1}


def test_adaptation_high_freq_sensitize():
    hist = [(T0 - 60 * i, "ear_tip", 0.1, 0.3, "摸") for i in range(CONFIG.ADAPT_FREQ_THRESHOLD + 1)]
    st = mk_state(history=_hist(hist))
    mult, changed = adaptation.daily_adaptation(st, DEMO_A, now=T0)
    assert mult["ear_tip"] == pytest.approx(1.01, abs=1e-4)   # 高频 → +1%
    assert changed >= 1   # 首日其余零触碰的 sens≥5 部位同步迟钝化（设计内：spec 零触碰条款）
    assert st["longterm_mult"] == mult


def test_adaptation_zero_touch_dull_and_low_sens_exempt():
    st = mk_state(history=[])   # 无任何触碰
    mult, _ = adaptation.daily_adaptation(st, DEMO_A, now=T0)
    assert mult["tail_root"] == pytest.approx(0.992, abs=1e-4)   # sens 6 ≥5 → 迟钝化
    assert mult["ear_tip"] == pytest.approx(0.992, abs=1e-4)     # sens 9 ≥5 → 迟钝化
    assert mult["waist"] == 1.0                                  # sens 3 <5 → 豁免（本来就没感觉）


def test_adaptation_clamp_boundaries():
    hist = [(T0 - 60 * i, "ear_tip", 0.1, 0.3, "摸") for i in range(30)]
    st = mk_state(history=_hist(hist),
                  longterm_mult={"ear_tip": 1.295, "tail_root": 0.805})
    mult, _ = adaptation.daily_adaptation(st, DEMO_A, now=T0)
    assert mult["ear_tip"] == pytest.approx(CONFIG.ADAPT_MULT_RANGE[1])   # 钳 +30%
    assert mult["tail_root"] == pytest.approx(CONFIG.ADAPT_MULT_RANGE[0])   # 钳 -20%


def test_longterm_mult_in_advance(no_sens):
    ev = [{"part": "ear_tip", "s": 0.6, "type": "摸", "t": T0}]
    st_a = mk_state(tol=0.0)
    decay.advance_state(st_a, DEMO_A, ev, T0)
    st_b = mk_state(tol=0.0, longterm_mult={"ear_tip": 1.3})
    decay.advance_state(st_b, DEMO_A, ev, T0)
    v_a = st_a["parts"]["ear_tip"]["value"]
    v_b = st_b["parts"]["ear_tip"]["value"]
    assert v_b == pytest.approx(v_a * 1.3, abs=1e-6)   # 长效系数乘在部位增益上
    # accessor：无键/坏值/越界回中性 1.0
    assert decay.longterm_mult(st_a, "ear_tip") == 1.0
    assert decay.longterm_mult({"longterm_mult": {"ear_tip": 9.0}}, "ear_tip") == 1.0
    assert decay.longterm_mult({"longterm_mult": {"ear_tip": "x"}}, "ear_tip") == 1.0


def test_normalize_longterm_and_history_200():
    st = mk_state(longterm_mult={"ear_tip": 5.0, "bad": "x"})
    out = normalize(st, DEMO_A)
    assert out["longterm_mult"]["ear_tip"] == CONFIG.ADAPT_MULT_RANGE[1]   # 越界钳制
    assert "bad" not in out["longterm_mult"]                              # 坏值剔除
    big = _hist([(T0 - i, "ear_tip", 0.1, 0.3, "摸") for i in range(250)])
    out2 = normalize(mk_state(history=big), DEMO_A)
    assert len(out2["history"]) == CONFIG.ADAPT_HISTORY_LIMIT == 200


# ---- 任务三：情绪↔身体耦合 ----

def _event_value(st_extra=None):
    st = mk_state(tol=0.0, **(st_extra or {}))
    decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 0.6, "type": "摸", "t": T0}], T0)
    return st


def test_mood_coupling_low_suppresses_gain(no_sens):
    st_n = _event_value()
    st_low = _event_value()
    decay.apply_mood_coupling(st_low, -0.5)
    assert st_low["mood_gain_mult"] == pytest.approx(CONFIG.MOOD_COUPLE_LOW_GAIN)
    decay.advance_state(st_low, DEMO_A, [], T0)   # 空事件推进：只消费系数不添值
    assert "mood_gain_mult" not in st_low and "mood_threshold_add" not in st_low   # 消费即清
    # 增益抑制经由事件路径验证：低落时同一事件产出更小
    st_ev = mk_state(tol=0.0)
    decay.apply_mood_coupling(st_ev, -0.5)
    decay.advance_state(st_ev, DEMO_A, [{"part": "ear_tip", "s": 0.6, "type": "摸", "t": T0}], T0)
    assert st_ev["parts"]["ear_tip"]["value"] == pytest.approx(
        st_n["parts"]["ear_tip"]["value"] * CONFIG.MOOD_COUPLE_LOW_GAIN, abs=1e-6)


def test_mood_coupling_high_boosts_gain(no_sens):
    st_n = _event_value()
    st_hi = mk_state(tol=0.0)
    assert decay.apply_mood_coupling(st_hi, 0.7) is True
    decay.advance_state(st_hi, DEMO_A, [{"part": "ear_tip", "s": 0.6, "type": "摸", "t": T0}], T0)
    assert st_hi["parts"]["ear_tip"]["value"] == pytest.approx(
        st_n["parts"]["ear_tip"]["value"] * CONFIG.MOOD_COUPLE_HIGH_GAIN, abs=1e-6)
    # 中性段（-0.3 ≤ score ≤ 0.5）不写键：行为与旧版一致
    st_mid = mk_state(tol=0.0)
    assert decay.apply_mood_coupling(st_mid, 0.1) is False
    assert "mood_gain_mult" not in st_mid


def test_mood_coupling_threshold_add_blocks_overload():
    ev = [{"part": "ear_tip", "s": 0.1, "type": "摸", "t": T0}]
    st_n = mk_state()
    for pid in ("ear_tip", "nape", "tail_root"):
        st_n["parts"][pid]["value"] = 0.9
    s_n = decay.advance_state(st_n, DEMO_A, ev, T0)
    assert s_n["overload"] is True   # arousal 0.9 ≥ 0.85：中性正常过载
    st_low = mk_state()
    for pid in ("ear_tip", "nape", "tail_root"):
        st_low["parts"][pid]["value"] = 0.9
    decay.apply_mood_coupling(st_low, -0.5)
    s_low = decay.advance_state(st_low, DEMO_A, ev, T0)
    assert s_low["overload"] is False and st_low["phase"] == "building"   # 阈值+8% → 绷着不过载


def _write_hint(data_dir: pathlib.Path, ts: float):
    m = data_dir / "mood"
    m.mkdir(parents=True, exist_ok=True)
    (m / CONFIG.MO_BODY_HINT_FILE).write_text(
        json.dumps({"ts": decay.iso_ts(ts), "hint": "warm"}), encoding="utf-8")


def _write_insight(data_dir: pathlib.Path, score: float):
    # 0.30.1：实际目录是 unrelated_mood_source/unrelated_mood_source/，键名 final
    ic = data_dir.parent / "unrelated_mood_source" / "unrelated_mood_source"
    ic.mkdir(parents=True, exist_ok=True)
    day = time.strftime("%Y-%m-%d", time.gmtime(2_000_000.0 + 8 * 3600))
    (ic / "mood_calibration.json").write_text(
        json.dumps({"calibrations": {day: {"final": score}}}), encoding="utf-8")


def test_body_hint_fresh_boosts_mood(tmp_path):
    vb = tmp_path / "vb"
    vb.mkdir()
    _write_insight(vb, 0.1)
    now = 2_000_000.0
    _write_hint(vb, now - 60.0)
    out = signals.read_mood(str(vb), now=now)
    assert out["available"] is True
    assert out["score"] == pytest.approx(CONFIG.MO_BODY_HINT_BOOST * (1 - 60.0 / CONFIG.MO_BODY_HINT_TTL))


def test_body_hint_stale_and_hint_only(tmp_path):
    vb = tmp_path / "vb"
    vb.mkdir()
    _write_insight(vb, 0.1)
    now = 2_000_000.0
    _write_hint(vb, now - CONFIG.MO_BODY_HINT_TTL - 600.0)   # 2h 外过期
    assert signals.read_mood(str(vb), now=now)["score"] is None
    # 无 insight 标定但 hint 新鲜：身体余温单独构成小正情绪（available=True）
    vb2 = tmp_path / "nested" / "vb2"   # 独立父目录：不共享上面的 insight 标定
    vb2.mkdir(parents=True)
    _write_hint(vb2, now - 10.0)
    out2 = signals.read_mood(str(vb2), now=now)
    assert out2["available"] is True and 0.0 < out2["score"] <= CONFIG.MO_BODY_HINT_BOOST


# ---- 任务四：主动触碰意图 ----

def _intent_state(**kw):
    st = mk_state(arousal=0.5, verbal_arousal=0.2)
    st["parts"]["ear_tip"]["value"] = 0.6
    st.update(kw)
    return st


def test_proactive_conditions_hit():
    st = _intent_state()
    hint = pi.should_intent_touch(st, DEMO_A, now=T0, templates=["想被他碰{part}"])
    assert hint == "想被他碰耳尖"   # 最活跃部位（耳尖 0.6）占位替换
    hint2 = pi.should_intent_touch(st, DEMO_A, now=T0,
                                   templates=["想主动蹭他的手", "盼他把手贴上{part}"])
    assert hint2 in ("想主动蹭他的手", "盼他把手贴上耳尖")
    # 默认模板池：{part} 已替换
    hint3 = pi.should_intent_touch(st, DEMO_A, now=T0)
    assert hint3 and "{part}" not in hint3 and hint3 in {
        t.replace("{part}", "耳尖") for t in CONFIG.PROACTIVE_TOUCH_TEMPLATES}


def test_proactive_phase_and_threshold_gates():
    st = _intent_state()
    assert pi.should_intent_touch(mk_state(arousal=0.5, verbal_arousal=0.2, phase="afterglow"),
                                  DEMO_A, now=T0, templates=["想被他碰{part}"]) is None   # 余韵期无意图
    assert pi.should_intent_touch(mk_state(arousal=0.3, verbal_arousal=0.2),
                                  DEMO_A, now=T0, templates=["想被他碰{part}"]) is None   # 兴奋不足
    assert pi.should_intent_touch(mk_state(arousal=0.5, verbal_arousal=0.1),
                                  DEMO_A, now=T0, templates=["想被他碰{part}"]) is None   # 氛围不在（≤门槛）


def test_proactive_cooldown():
    st = _intent_state(proactive_intent={"ts": decay.iso_ts(T0 - 100.0), "hint": "x"})
    assert pi.should_intent_touch(st, DEMO_A, now=T0, templates=["想被他碰{part}"]) is None   # 4h 内冷却
    st2 = _intent_state(proactive_intent={"ts": decay.iso_ts(T0 - CONFIG.PROACTIVE_TOUCH_COOLDOWN - 1.0),
                                          "hint": "x"})
    assert pi.should_intent_touch(st2, DEMO_A, now=T0, templates=["想被他碰{part}"]) == "想被他碰耳尖"


def test_intent_hint_ttl_and_render_tail():
    st = mk_state(arousal=0.5)
    st["proactive_intent"] = {"ts": decay.iso_ts(T0 - 60.0), "hint": "想被他碰耳尖"}
    assert pi.intent_hint(st, now=T0) == "想被他碰耳尖"
    st["proactive_intent"] = {"ts": decay.iso_ts(T0 - CONFIG.PROACTIVE_TOUCH_TTL - 1.0), "hint": "想被他碰耳尖"}
    assert pi.intent_hint(st, now=T0) is None   # 过期不渲染
    st2 = mk_state(arousal=0.5)
    st2["proactive_intent"] = {"ts": decay.iso_ts(T0), "hint": "想被他碰耳尖"}
    st2["updated_at"] = decay.iso_ts(T0)
    assert "她想被他碰耳尖" in inject.render(st2, DEMO_A)   # 注入块"她…"尾巴


def test_normalize_proactive_intent():
    st = normalize({"proactive_intent": {"ts": decay.iso_ts(T0), "hint": "想被他碰耳尖"}}, DEMO_A)
    assert st["proactive_intent"]["hint"] == "想被他碰耳尖"
    bad = normalize({"proactive_intent": {"ts": None, "hint": "x"}}, DEMO_A)
    assert bad["proactive_intent"] is None   # 坏值当无
    # 签名进指纹：意图落账/verbal 爬升触发写盘判定
    a = mk_state(verbal_arousal=0.1)
    b = mk_state(verbal_arousal=0.2)
    assert state_signature(a) != state_signature(b)
    c = mk_state()
    c["proactive_intent"] = {"ts": decay.iso_ts(T0), "hint": "想主动蹭他的手"}
    assert state_signature(c) != state_signature(mk_state())

from tests.provider_fixture import provider, signal, SCOPE
