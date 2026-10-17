# -*- coding: utf-8 -*-
"""心绪编排层测试：信号适配器 + 规则引擎 + 存储。"""
import json
import pathlib
import sys
import tempfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from astrbot_plugin_virtual_body.core import mood_engine as engine, mood_signals as signals  # noqa: E402
from astrbot_plugin_virtual_body.core import mood_store as st  # noqa: E402
from astrbot_plugin_virtual_body import CONFIG  # noqa: E402

T0 = 1_000_000.0


# ---- signals ----

def test_weather_rain_detection(tmp_path):
    wz = tmp_path / "vb" / "weather"
    wz.mkdir(parents=True)
    (wz / "weather_last.json").write_text(
        json.dumps({"updated_at": "x", "city": "示例城市", "weather": "天气:小雨 降水:2mm"}), encoding="utf-8")
    out = signals.read_weather(str(tmp_path / "vb"))
    assert out and out["is_rain"] is True and out["city"] == "示例城市"


def test_weather_clear_and_missing(tmp_path):
    wz = tmp_path / "vb" / "weather"
    wz.mkdir(parents=True)
    (wz / "weather_last.json").write_text(json.dumps({"weather": "晴 降水:0mm"}), encoding="utf-8")
    assert signals.read_weather(str(tmp_path / "vb"))["is_rain"] is False
    # 独立空目录（无任何天气源）→ 0.29.1 未激活兜底形态（不再是 None）：
    # available=False + is_rain=None——面板显示"未激活"，eq/ne 均不误命中
    with tempfile.TemporaryDirectory() as empty:
        out = signals.read_weather(str(pathlib.Path(empty) / "mood"))
    assert out is not None and out["available"] is False and out["is_rain"] is None


def test_mood_latest_day(tmp_path):
    api = provider("emotion_hint", {"score": .1}, observed_at=900)
    assert api.update("synthetic", SCOPE, "emotion_hint", {"score": -.6}, observed_at=1000)
    assert signal(api, "emotion_hint")["score"] == -.6
    assert not api.update("synthetic", SCOPE, "emotion_hint", {"score": .1}, observed_at=900)
    assert signal(api, "emotion_hint")["score"] == -.6


def test_mood_score_fallback(tmp_path):
    api = provider("emotion_hint", {"score": .3})
    assert signal(api, "emotion_hint")["score"] == .3
    assert signals.read_mood(str(tmp_path))["score"] is None


def test_reading_progress(tmp_path):
    api = provider("reading", {"book_title": "示例读物", "chapter_index": 2, "chapter_count": 27})
    out = signal(api, "reading")
    assert out["available"] and out["book_title"] == "示例读物" and out["chapter_index"] == 2 and out["chapter_count"] == 27
    assert signals.read_reading(str(tmp_path))["available"] is False


def test_presence_space_and_body(tmp_path):
    pd = tmp_path / "vb" / "state"
    pd.mkdir(parents=True)
    (pd / "role_a_demo-user.space.json").write_text(json.dumps(
        {"she": {"room": "veranda", "moving": None}}), encoding="utf-8")
    (pd / "role_a_demo-user.json").write_text(json.dumps(
        {"phase": "building", "arousal": 0.12}), encoding="utf-8")
    out = signals.read_presence(str(tmp_path / "vb"))
    assert out and out["she_room"] == "veranda" and out["body_phase"] == "building"


# ---- engine ----

RULES = [
    {"id": "rain_care", "enabled": True, "signal": "weather",
     "conditions": [{"field": "weather.is_rain", "op": "eq", "value": True}],
     "cooldown_minutes": 360, "priority": 2, "template_hint": "外面下雨了"},
    {"id": "mood_low", "enabled": True, "signal": "mood",
     "conditions": [{"field": "mood.score", "op": "lte", "value": -0.4}],
     "cooldown_minutes": 720, "priority": 1, "template_hint": "情绪分低"},
]


def _sig(rain=False, score=0.1):
    return {"weather": {"is_rain": rain}, "mood": {"score": score}}


def test_engine_fires_and_priority():
    fired, cd = engine.evaluate(RULES, _sig(rain=True, score=-0.6), {}, {}, now=T0)
    ids = [f["rule_id"] for f in fired]
    assert ids == ["mood_low", "rain_care"]   # priority 1 排前


def test_engine_cooldown_blocks():
    fired, cd = engine.evaluate(RULES, _sig(rain=True), {}, {}, now=T0)
    assert cd["rain_care"] == T0
    fired2, _ = engine.evaluate(RULES, _sig(rain=True), {}, cd, now=T0 + 60)
    assert all(f["rule_id"] != "rain_care" for f in fired2)
    fired3, _ = engine.evaluate(RULES, _sig(rain=True), {}, cd, now=T0 + 40000)
    assert any(f["rule_id"] == "rain_care" for f in fired3)


def test_engine_changed_op():
    rules = [{"id": "room", "enabled": True, "signal": "presence",
              "conditions": [{"field": "presence.she_room", "op": "changed"}],
              "cooldown_minutes": 1, "priority": 3, "template_hint": "移动"}]
    sig = {"presence": {"she_room": "kitchen"}}
    last = {"presence_she_room": "living"}
    fired, _ = engine.evaluate(rules, sig, last, {}, now=T0)
    assert len(fired) == 1
    fired2, _ = engine.evaluate(rules, sig, engine.snapshot(sig), {}, now=T0 + 120)
    assert fired2 == []


def test_engine_disabled_rule_skipped():
    rules = [dict(RULES[0], enabled=False)]
    fired, _ = engine.evaluate(rules, _sig(rain=True), {}, {}, now=T0)
    assert fired == []


def test_prune_and_render():
    trig = [{"rule_id": "a", "expires_at": T0 - 1}, {"rule_id": "b", "expires_at": T0 + 999,
             "template_hint": "外面下雨了"}]
    alive = engine.prune_expired(trig, now=T0)
    assert [t["rule_id"] for t in alive] == ["b"]
    text = engine.render_for_pc(alive)
    assert "下雨" in text and text.startswith("（情境线索")


# ---- store ----

def test_store_roundtrip(tmp_path):
    doc = {"schema": 1, "rules": RULES}
    st.save_rules(str(tmp_path), doc)
    back = st.load_rules(str(tmp_path), str(tmp_path))
    assert back["rules"] == RULES
    st.save_triggers(str(tmp_path), [{"id": "x", "expires_at": T0 + 1}])
    assert len(st.load_triggers(str(tmp_path))) == 1
    st.append_history(str(tmp_path), [{"id": "x", "rule_id": "rain_care", "signal": "weather", "reason": "r"}])
    hist = st.load_json(str(tmp_path), CONFIG.MO_HISTORY_FILE, {"items": []})
    assert hist["items"][0]["rule_id"] == "rain_care"


# ---- 0.29.1 信号兜底（线上根因修复：无源数据 → 未激活形态而非 None）----

def test_mood_unavailable_fallback(tmp_path):
    # unrelated_mood_source 未装/未标定：available=False + score=None（lte/gte 不命中，面板显示未激活）
    out = signals.read_mood(str(tmp_path / "vb"))
    assert out is not None and out["available"] is False and out["score"] is None


def test_reading_unavailable_fallback(tmp_path):
    out = signals.read_reading(str(tmp_path / "vb"))
    assert out is not None and out["available"] is False and out["chapter_index"] is None


def test_presence_fallback_manor_only(tmp_path):
    # 线上场景复刻：无 .space.json（白名单会话未触发过空间事件）、无身体状态文件、
    # 无 profile 覆盖层——但庄园已加载（插件服役）→ 中性默认（available=True），
    # she_room=None 是 changed 的可比基线，env 字段照常合成
    out = signals.read_presence(str(tmp_path / "vb"), {"rooms": [{"id": "veranda", "mood_bias": [0.5, 0.2]}]})
    assert out is not None and out["available"] is True
    assert out["she_room"] is None and out["arousal"] == 0.0 and out["body_phase"] == ""
    assert out["env_valence"] == 0.0 and out["mood_word"] == "平静" and out["same_room"] is False


def test_presence_fallback_body_file(tmp_path):
    # 无 space 文件但有身体状态文件：phase/arousal 取真值
    sd = tmp_path / "vb" / "state"
    sd.mkdir(parents=True)
    (sd / "role_a_demo-user.json").write_text(json.dumps(
        {"phase": "building", "arousal": 0.42}), encoding="utf-8")
    out = signals.read_presence(str(tmp_path / "vb"))
    assert out is not None and out["available"] is True
    assert out["body_phase"] == "building" and abs(out["arousal"] - 0.42) < 1e-9
    assert out["she_room"] is None


def test_presence_fallback_profile_override(tmp_path):
    # 无状态文件但有 profile 覆盖层 → 默认值；三层迹象全无 → None（插件未使用）
    pd = tmp_path / "vb" / "profiles"
    pd.mkdir(parents=True)
    (pd / "role_a.json").write_text(json.dumps({"id": "role_a"}), encoding="utf-8")
    assert signals.read_presence(str(tmp_path / "vb")) is not None
    with tempfile.TemporaryDirectory() as empty:
        assert signals.read_presence(str(pathlib.Path(empty) / "vb")) is None


def test_engine_unavailable_signal_gates_all_ops():
    # available=False 的源：eq/ne/changed 一律不成立（未激活 ≠ 值不等）
    sig = {"weather": {"available": False, "is_rain": None}}
    rules = [
        {"id": "r_eq", "signal": "weather", "conditions": [{"field": "weather.is_rain", "op": "eq", "value": True}],
         "cooldown_minutes": 1, "priority": 3},
        {"id": "r_ne", "signal": "weather", "conditions": [{"field": "weather.is_rain", "op": "ne", "value": True}],
         "cooldown_minutes": 1, "priority": 3},
    ]
    fired, _ = engine.evaluate(rules, sig, {}, {}, now=T0)
    assert fired == []
    # available=True 或无 available 键的老结构不受影响
    fired2, _ = engine.evaluate(rules[:1], {"weather": {"is_rain": True}}, {}, {}, now=T0)
    assert [f["rule_id"] for f in fired2] == ["r_eq"]


def test_changed_baseline_from_fallback_none():
    # presence 兜底 she_room=None 落首轮基线 → 她首次落位（space 文件出现）即触发 changed
    rules = [{"id": "room", "signal": "presence",
              "conditions": [{"field": "presence.she_room", "op": "changed"}],
              "cooldown_minutes": 120, "priority": 4}]
    fallback = {"presence": {"available": True, "she_room": None, "arousal": 0.0}}
    fired, _ = engine.evaluate(rules, fallback, {}, {}, now=T0)
    assert fired == []   # 首轮：last_state 空，只建基线
    landed = {"presence": {"available": True, "she_room": "veranda", "arousal": 0.0}}
    fired2, _ = engine.evaluate(rules, landed, engine.snapshot(fallback), {}, now=T0 + 60)
    assert [f["rule_id"] for f in fired2] == ["room"]


def test_end_to_end_tick_fires_and_persists(tmp_path):
    api = provider("reading", {"book_title": "示例读物", "chapter_index": 1}, now=T0, ttl=600)
    mood_dir = tmp_path / "mood"
    rules = [{"id": "reading_new_chapter", "enabled": True, "signal": "reading",
              "conditions": [{"field": "reading.chapter_index", "op": "changed"}],
              "cooldown_minutes": 240, "priority": 3, "template_hint": "{book}读到第{chapter}章"}]
    def tick(now):
        sig = {"reading": signal(api, "reading", now=now)}
        last = st.load_json(str(mood_dir), CONFIG.MO_LAST_STATE_FILE, {})
        cds = st.load_json(str(mood_dir), CONFIG.MO_COOLDOWNS_FILE, {})
        fired, new_cd = engine.evaluate(rules, sig, last, cds, now=now)
        old = [t for t in engine.prune_expired(st.load_triggers(str(mood_dir)), now)
               if t.get("rule_id") not in {f.get("rule_id") for f in fired}]
        st.save_triggers(str(mood_dir), old + fired)
        if fired: st.append_history(str(mood_dir), fired)
        st.save_json(str(mood_dir), CONFIG.MO_COOLDOWNS_FILE, new_cd)
        st.save_json(str(mood_dir), CONFIG.MO_LAST_STATE_FILE, engine.snapshot(sig))
        return sig, fired
    sig1, fired1 = tick(T0)
    assert sig1["reading"]["available"] is True and sig1["reading"]["chapter_index"] == 1
    assert fired1 == []
    api._clock = lambda: T0+60
    assert api.update("synthetic", SCOPE, "reading", {"book_title": "示例读物", "chapter_index": 2}, observed_at=T0+60)
    sig2, fired2 = tick(T0+60)
    assert sig2["reading"]["chapter_index"] == 2
    assert [f["rule_id"] for f in fired2] == ["reading_new_chapter"]
    assert "示例读物" in fired2[0]["template_hint"] and "2" in fired2[0]["template_hint"]
    assert [t["rule_id"] for t in st.load_triggers(str(mood_dir))] == ["reading_new_chapter"]
    hist = st.load_json(str(mood_dir), CONFIG.MO_HISTORY_FILE, {"items": []})
    assert len(hist["items"]) == 1 and hist["items"][0]["rule_id"] == "reading_new_chapter"
    assert st.load_json(str(mood_dir), CONFIG.MO_COOLDOWNS_FILE, {}).get("reading_new_chapter") == T0+60
    api._clock = lambda: T0+120
    assert api.update("synthetic", SCOPE, "reading", {"book_title": "示例读物", "chapter_index": 3}, observed_at=T0+120)
    _, fired3 = tick(T0+120)
    assert fired3 == []


# ---- 0.32.0 P0 修复：天气 TTL / 他位置兜底 / 完本保护 / 规则可达性 ----

RULES_PATH = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "rules.json"


def _data_rules():
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))["rules"]


MANOR_MIN = {"rooms": [{"id": "living", "mood_bias": [0.4, -0.2]},
                       {"id": "veranda", "mood_bias": [0.25, -0.25]}]}


def test_weather_ttl_stale_expires(tmp_path):
    """P0-1：快照超过 6h 未更新 → available=False / is_rain=None / stale=True（过期不当真），
    天气类规则被规则引擎的 available=False 机制一并封死。
    （P4 后内置规则已无天气条目，此处内联等价规则验证引擎封死语义。）"""
    import os
    import time
    wz = tmp_path / "vb" / "weather"
    wz.mkdir(parents=True)
    f = wz / "weather_last.json"
    f.write_text(json.dumps({"updated_at": "x", "city": "示例城市", "weather": "中雨"}), encoding="utf-8")
    old = time.time() - (CONFIG.MO_WEATHER_TTL_SECONDS + 3600)
    os.utime(f, (old, old))
    out = signals.read_weather(str(tmp_path / "vb"))
    assert out["available"] is False and out["is_rain"] is None and out["stale"] is True
    inline_rain_rules = [
        {"id": "rain_care_inline", "enabled": True, "signal": "weather",
         "conditions": [{"field": "weather.is_rain", "op": "eq", "value": True}],
         "cooldown_minutes": 360, "priority": 2, "template_hint": "t", "note": "n"},
        {"id": "rain_veranda_inline", "enabled": True, "signal": "weather",
         "conditions": [{"field": "weather.is_rain", "op": "eq", "value": True},
                        {"field": "presence.she_room", "op": "eq", "value": "veranda"}],
         "cooldown_minutes": 300, "priority": 3, "template_hint": "t", "note": "n"},
    ]
    fired, _ = engine.evaluate(inline_rain_rules, {"weather": out, "presence": {"she_room": "veranda"}},
                               {}, {}, now=T0)
    assert fired == []


def test_weather_ttl_fresh_keeps_active(tmp_path):
    """P0-1：6h 内的快照照常激活；无快照的未激活形态 stale=False（是"从未有"不是"过期"）。"""
    import os
    import time
    wz = tmp_path / "vb" / "weather"
    wz.mkdir(parents=True)
    f = wz / "weather_last.json"
    f.write_text(json.dumps({"city": "示例城市", "weather": "小雨"}), encoding="utf-8")
    recent = time.time() - 3600
    os.utime(f, (recent, recent))
    out = signals.read_weather(str(tmp_path / "vb"))
    assert out["available"] is True and out["is_rain"] is True and out["stale"] is False
    with tempfile.TemporaryDirectory() as empty:
        miss = signals.read_weather(str(pathlib.Path(empty) / "vb"))
    assert miss["available"] is False and miss["stale"] is False


def test_presence_he_inferred_when_room_unknown(tmp_path):
    """P0-2：he.room 未知而她已落位 → 推断他在她身边（he_room=she_room、confidence=0.5、
    he_inferred=True）——same_room 从恒 False 变为可达 True。"""
    pd = tmp_path / "vb" / "state"
    pd.mkdir(parents=True)
    (pd / "role_a_1.space.json").write_text(json.dumps(
        {"she": {"room": "veranda", "moving": None}}), encoding="utf-8")   # 无 he 键
    out = signals.read_presence(str(tmp_path / "vb"), MANOR_MIN)
    assert out["he_inferred"] is True and out["he_room"] == "veranda"
    assert out["he_confidence"] == 0.5 and out["same_room"] is True


def test_presence_he_known_no_inference(tmp_path):
    """P0-2：he.room 已知 → 不推断，按真实位置判同室（he_confidence 键不出现）。"""
    pd = tmp_path / "vb" / "state"
    pd.mkdir(parents=True)
    (pd / "role_a_1.space.json").write_text(json.dumps(
        {"she": {"room": "veranda", "moving": None}, "he": {"room": "living"}}), encoding="utf-8")
    out = signals.read_presence(str(tmp_path / "vb"), MANOR_MIN)
    assert out["he_inferred"] is False and out["he_room"] == "living"
    assert out["same_room"] is False and "he_confidence" not in out


def test_mood_warm_reachable_via_he_inference(tmp_path):
    """P0-2 端到端：兜底推断的 same_room=True 让 mood_warm 重新可达（env_valence 0.4 过阈）。"""
    pd = tmp_path / "vb" / "state"
    pd.mkdir(parents=True)
    (pd / "role_a_1.space.json").write_text(json.dumps(
        {"she": {"room": "living", "moving": None}}), encoding="utf-8")
    sig = signals.read_all(str(tmp_path / "vb"), MANOR_MIN)
    assert sig["presence"]["he_inferred"] is True and sig["presence"]["same_room"] is True
    warm = next(r for r in _data_rules() if r["id"] == "mood_warm")
    fired, _ = engine.evaluate([warm], sig, {}, {}, now=T0)
    assert [f["rule_id"] for f in fired] == ["mood_warm"]


def test_reading_finished_book_marks_unavailable(tmp_path):
    api = provider("reading", {"book_title": "示例读物", "chapter_index": 2, "chapter_count": 3})
    assert api.update("synthetic", SCOPE, "reading", None, status="unknown", observed_at=1001)
    out = signal(api, "reading", now=1001)
    assert out["available"] is False and "book_title" not in out
    assert out.get("chapter_index") is None


def test_reading_new_book_supersedes_finished(tmp_path):
    api = provider("reading", {"book_title": "旧示例", "chapter_index": 3})
    assert api.update("synthetic", SCOPE, "reading", None, status="unknown", observed_at=1001)
    assert signal(api, "reading", now=1001)["available"] is False
    assert api.update("synthetic", SCOPE, "reading", {"book_title": "新示例", "chapter_index": 1}, observed_at=1002)
    out = signal(api, "reading", now=1002)
    assert out["available"] is True and out["book_title"] == "新示例" and out["chapter_index"] == 1


def test_mood_low_threshold_lowered():
    """P0-3 历史：mood_low 阈值 -0.4 → -0.3。P4 规则精简后该规则已从内置删除
    （数据源 unrelated_mood_source 停用 + 阈值不可达，双重死因），此处内联等价规则
    保留引擎的 mood 信号 lte 语义覆盖。"""
    low = {"id": "mood_low_inline", "enabled": True, "signal": "mood",
           "conditions": [{"field": "mood.score", "op": "lte", "value": -0.3}],
           "cooldown_minutes": 720, "priority": 1, "template_hint": "t", "note": "n"}
    fired, _ = engine.evaluate([low], {"mood": {"score": -0.32, "available": True}}, {}, {}, now=T0)
    assert [f["rule_id"] for f in fired] == ["mood_low_inline"]
    fired2, _ = engine.evaluate([low], {"mood": {"score": -0.1, "available": True}}, {}, {}, now=T0 + 60)
    assert fired2 == []

from tests.provider_fixture import provider, signal, SCOPE
