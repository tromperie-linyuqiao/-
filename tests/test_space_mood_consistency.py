"""存在层一致性回归：真实移动/会话/观测时效，不使用线上人设或聊天。"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import threading

import pytest

from astrbot_plugin_virtual_body import CONFIG
from astrbot_plugin_virtual_body.core import bridge, mood_engine, mood_signals, mood_store
from astrbot_plugin_virtual_body.core import proactive_intent, space, store, traverse


NOW = 1_700_000_000.0
MANOR = {
    "rooms": [{"id": "a", "floor": "L1", "mood_bias": [0.4, -0.2]},
              {"id": "b", "floor": "L1"}],
    "floors": [{"id": "L1"}],
    "edges": [{"a": "a", "b": "b", "seconds": 90}],
}
PROFILE = {"parts": [{"id": "hand", "label": "手", "sens": 5}]}


def write_json(path, data, timestamp=NOW):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.utime(path, (timestamp, timestamp))
    return path


def place_pair(tmp_path, she=None, he=None, key="demo_owner"):
    data = space.new_state(key)
    data["she"].update(she or {"room": "a", "since": space.iso(NOW), "source": "rp"})
    data["he"].update(he or {"room": "a", "confidence": 1.0})
    write_json(tmp_path / "vb" / "state" / (key + ".space.json"), data)
    return data


def test_restart_does_not_turn_old_arrival_into_new_presence():
    st = space.new_state("demo_owner")
    space.set_room(st, "she", "a", MANOR, NOW - 8000, "rp")
    space.set_room(st, "she", "b", MANOR, NOW - 7200, "rp")
    st["he"].update(room="b", confidence=1.0)
    result = space.advance(st, MANOR, NOW)
    assert st["she"]["room"] == "b"
    assert space.parse_iso(st["she"]["since"]) == NOW - 7110
    assert result["arrived_at"] == {"she": NOW - 7110}
    assert bridge.on_arrival(st, {}, PROFILE, MANOR, NOW) == []


def test_timely_arrival_still_produces_existing_bridge_event():
    st = space.new_state("demo_owner")
    space.set_room(st, "she", "a", MANOR, NOW - 200, "rp")
    space.set_room(st, "she", "b", MANOR, NOW - 90, "rp")
    st["he"].update(room="b", since=space.iso(NOW - 8000), confidence=1.0)
    space.advance(st, MANOR, NOW)
    evs = bridge.on_arrival(st, {}, PROFILE, MANOR, NOW)
    assert len(evs) == 1 and evs[0]["type"] == bridge.PROXIMITY_SRC


def test_rp_movement_refreshes_override_without_renewing_eta():
    st = space.new_state("demo_owner")
    space.set_room(st, "she", "a", MANOR, NOW - 8000, "fallback")
    first = space.set_room(st, "she", "b", MANOR, NOW, "rp")
    second = space.set_room(st, "she", "b", MANOR, NOW + 30, "rp")
    assert first["seconds"] == 90 and second["seconds"] == 60
    assert st["she"]["moving"]["until_ts"] == NOW + 90
    assert space.parse_iso(st["she"]["since"]) == NOW + 30


def test_long_movement_is_one_departure_instead_of_repeated_falls():
    manor = {**MANOR, "edges": [{"a": "a", "b": "b", "seconds": 3600}]}
    st = space.new_state("demo_owner")
    space.set_room(st, "she", "a", manor, NOW - 100, "rp")
    st["he"].update(room="a", confidence=1.0)
    space.set_room(st, "she", "b", manor, NOW, "rp")
    assert bridge.on_arrival(st, {}, PROFILE, manor, NOW)[0]["type"] == bridge.LEAVE_SRC
    assert bridge.on_arrival(st, {}, PROFILE, manor, NOW + CONFIG.PRESENCE_COOLDOWN_SECONDS + 1) == []


def test_two_people_moving_do_not_count_as_leaving_stationary_partner():
    st = space.new_state("demo_owner")
    space.set_room(st, "she", "a", MANOR, NOW - 100, "rp")
    space.set_room(st, "he", "a", MANOR, NOW - 100, "rp")
    space.set_room(st, "he", "b", MANOR, NOW, "rp")
    space.set_room(st, "she", "b", MANOR, NOW, "rp")
    assert bridge.on_arrival(st, {}, PROFILE, MANOR, NOW) == []


def test_old_completed_player_departure_does_not_replay_on_restart():
    st = space.new_state("demo_owner")
    st["she"].update(room="a", since=space.iso(NOW - 9000))
    st["he"].update(room="b", since=space.iso(NOW - 7200), confidence=1.0)
    assert bridge.on_departure(st, {}, PROFILE, MANOR, NOW) == []


@pytest.mark.parametrize("he", [
    {"room": "a", "moving": {"to": "b", "from": "a", "until_ts": NOW + 90}, "confidence": 1},
    {"room": "a", "confidence": 0.1},
    {"room": "a", "confidence": float("nan")},
    {"room": "removed", "confidence": 1},
])
def test_uncertain_player_location_does_not_trigger_same_room_rule(tmp_path, he):
    place_pair(tmp_path, he=he)
    sig = mood_signals.read_presence(str(tmp_path / "vb"), MANOR, "demo_owner")
    assert sig["same_room"] is False
    rule = {"id": "warm", "signal": "presence",
            "conditions": [{"field": "presence.same_room", "op": "eq", "value": True}]}
    assert mood_engine.evaluate([rule], {"presence": sig}, {}, {}, NOW)[0] == []


def test_missing_player_is_not_inferred_beside_moving_character(tmp_path):
    place_pair(tmp_path, she={"room": "a", "moving": {"from": "a", "to": "b", "until_ts": NOW + 90}},
               he={"room": None})
    out = mood_signals.read_presence(str(tmp_path / "vb"), MANOR)
    assert out["he_inferred"] is False and out["same_room"] is False


def test_scoped_presence_never_uses_other_sessions_latest_state(tmp_path):
    place_pair(tmp_path)
    place_pair(tmp_path, she={"room": "b"}, he={"room": "b", "confidence": 1}, key="other_guest")
    write_json(tmp_path / "vb" / "state" / "other_guest.json", {"arousal": 0.9, "phase": "building"}, NOW + 100)
    write_json(tmp_path / "vb" / "state" / "demo_owner.json", {"arousal": 0.1, "phase": "cooldown"})
    out = mood_signals.read_presence(str(tmp_path / "vb"), MANOR, "demo_owner")
    assert out["she_room"] == "a" and out["arousal"] == 0.1 and out["body_phase"] == "cooldown"
    missing = mood_signals.read_presence(str(tmp_path / "vb"), MANOR, "missing_owner")
    assert missing["she_room"] is None and missing["arousal"] == 0
    assert mood_signals.read_presence(str(tmp_path / "vb"), MANOR, "../../other_guest") is None


def test_bad_selected_space_does_not_adopt_other_character(tmp_path):
    place_pair(tmp_path, key="other_guest")
    write_json(tmp_path / "vb" / "state" / "demo_owner.space.json", {"she": "broken", "he": []})
    out = mood_signals.read_presence(str(tmp_path / "vb"), MANOR, "demo_owner")
    assert out["she_room"] is None and out["arousal"] == 0


def test_removed_schedule_room_is_ignored_without_creating_fake_location():
    manor = {**MANOR, "activity_map": [{"match": ["读书"], "room": "removed"}],
             "default_schedule": [{"from_h": 0, "to_h": 24, "room": "removed"},
                                  {"from_h": 0, "to_h": 24, "room": "a"}]}
    st = space.new_state("demo_owner")
    space.advance(st, manor, NOW, plan_text="读书")
    assert st["she"]["room"] == "a"


def test_early_morning_does_not_replay_future_or_yesterday_plan():
    midnight = NOW - ((NOW + 8 * 3600) % 86400)
    today = space.iso(midnight)[:10]
    plan = {"date": today, "items": [{"time": "08:20", "activity": "到书房读书"}]}
    assert space.current_plan_text(plan, midnight + 5 * 3600, 8) is None
    assert space.current_plan_text(plan, midnight + 10 * 3600, 8) == "到书房读书"
    assert space.current_plan_text(plan, midnight + 86400 + 10 * 3600, 8) is None
    # 无日期的旧显式区间保留；只有活动文本的坏项不会被当作全天已开始。
    assert space.current_plan_text([{"from_h": 0, "to_h": 6, "text": "睡觉"}],
                                   midnight + 5 * 3600, 8) == "睡觉"


@pytest.mark.parametrize("item", [
    {"time": "24:00", "activity": "x"}, {"time": "09:99", "activity": "x"},
    {"from_h": -1, "to_h": 24, "text": "x"}, {"from_h": 9, "to_h": 25, "text": "x"},
    {"activity": "x"},
])
def test_bad_time_does_not_fake_started_activity(item):
    midday = NOW - ((NOW + 8 * 3600) % 86400) + 12 * 3600
    assert space.current_plan_text([item], midday, 8) is None


def test_reading_auxiliary_duplicate_and_corrupt_files_do_not_mark_book_finished(tmp_path):
    folder = tmp_path / "unrelated_reading_source"
    folder.mkdir()
    for name in ("0.json", "00.json", "999.json", "index.json"):
        write_json(folder/name, {})
    (folder/"1.json").write_text("{broken", encoding="utf-8")
    api = provider("reading", {"book_title": "示例读物", "chapter_index": 1, "chapter_count": 3})
    out = signal(api, "reading")
    assert out["available"] is True and out["chapter_index"] == 1 and out["book_title"] == "示例读物"
    assert mood_signals.read_reading(str(tmp_path / "vb"))["available"] is False


@pytest.mark.parametrize("edges", [
    [None, {"a": "a", "b": "ghost", "seconds": 0}, {"a": "ghost", "b": "b", "seconds": 0}],
    [{"a": "a", "b": "b", "seconds": -10}],
    [{"a": "a", "b": "b", "seconds": float("inf")}],
    [{"a": "a", "b": "b", "seconds": True}],
])
def test_invalid_edges_do_not_create_free_or_unknown_shortcuts(edges):
    assert traverse.travel_seconds({**MANOR, "edges": edges}, "a", "b") is None


def test_explicit_zero_cost_and_floor_hub_edges_remain_supported():
    manor = {**MANOR, "edges": [{"a": "a", "b": "L1", "seconds": 0},
                               {"a": "L1", "b": "b", "seconds": 4}]}
    assert traverse.travel_seconds(manor, "a", "b") == 4


@pytest.mark.parametrize("observed", [NOW - CONFIG.MO_WEATHER_TTL_SECONDS - 1, NOW + 3600])
def test_copied_old_or_future_weather_is_unavailable_despite_fresh_mtime(tmp_path, observed):
    write_json(tmp_path / "vb" / "weather" / CONFIG.WEATHER_SNAPSHOT_FILE,
               {"weather": "小雨", "updated_at": observed})
    out = mood_signals.read_weather(str(tmp_path / "vb"), now=NOW)
    assert out["available"] is False and out["stale"] is True and out["is_rain"] is None


def test_current_observation_wins_over_helper_json_and_future_forecast(tmp_path):
    write_json(tmp_path / "vb" / "weather" / CONFIG.WEATHER_SNAPSHOT_FILE,
               {"weather": "晴", "condition": "晴", "detail": "明天下大雨",
                "forecast": [{"text": "雨"}], "updated_at": NOW - 60})
    write_json(tmp_path / "vb" / "weather" / "cities.json", {"text": "全国降雨城市"}, NOW + 10)
    out = mood_signals.read_weather(str(tmp_path / "vb"), now=NOW)
    assert out["available"] is True and out["is_rain"] is False and "明天" not in out["condition"]


def test_empty_weather_json_is_unknown_instead_of_clear_weather(tmp_path):
    write_json(tmp_path / "vb" / "weather" / CONFIG.WEATHER_SNAPSHOT_FILE, {"city": "测试"})
    out = mood_signals.read_weather(str(tmp_path / "vb"), now=NOW)
    assert out["available"] is False and out["is_rain"] is None


@pytest.mark.parametrize("user_time", [NOW - 3600, NOW + 3600])
def test_old_or_future_user_emotion_does_not_gain_from_unrelated_pc_write(tmp_path, user_time):
    api = provider("user_activity", {"emotion": "calm"}, now=NOW, observed_at=NOW)
    if user_time > NOW:
        assert not api.update("synthetic", SCOPE, "user_activity", {"emotion": "new"}, observed_at=user_time)
        assert signal(api, "user_activity", now=NOW)["emotion"] == "calm"
    else:
        assert not api.update("synthetic", SCOPE, "user_activity", {"emotion": "old"}, observed_at=user_time)
        assert signal(api, "user_activity", now=NOW)["emotion"] == "calm"
    assert mood_signals.read_verbal(str(tmp_path), ["owner"], now=NOW)["is_intimate"] is False


def test_cached_emotion_expires_as_time_passes_without_file_change(tmp_path, monkeypatch):
    api = provider("user_activity", {"emotion": "calm"}, now=NOW, ttl=60)
    first = signal(api, "user_activity", now=NOW)
    repeated = signal(api, "user_activity", now=NOW+30)
    later = signal(api, "user_activity", now=NOW+61)
    assert first["available"] is True and later["available"] is False
    assert repeated["emotion"] == "calm" and later["stale"] is True
    assert api.diagnostics()["metrics"]["accepted_updates"] == 1


def test_old_daily_calibration_is_not_renewed_by_file_copy(tmp_path):
    api = provider("emotion_hint", {"score": -.8}, now=NOW, observed_at=NOW-1000, ttl=60)
    write_json(tmp_path / "unrelated_source" / "calibration.json", {"score": .9})
    out = signal(api, "emotion_hint", now=NOW)
    assert out["available"] is False and out["stale"] is True and out.get("score") is None


def test_stale_calibration_does_not_override_fresh_body_hint(tmp_path):
    write_json(tmp_path / "unrelated_mood_source" / "unrelated_mood_source" / "mood_calibration.json",
               {"2023-01-01": {"final": -0.8}})
    write_json(tmp_path / "vb" / CONFIG.MO_DATA_SUBDIR / CONFIG.MO_BODY_HINT_FILE,
               {"ts": NOW - 10, "hint": "warm"})
    out = mood_signals.read_mood(str(tmp_path / "vb"), now=NOW)
    assert out["available"] is True and 0 < out["score"] < CONFIG.MO_BODY_HINT_BOOST
    assert out["source"] == "body_hint" and out["confidence"] < 1


@pytest.mark.parametrize("op,value", [("ne", True), ("eq", None), ("changed", None)])
def test_missing_fields_never_fire_custom_rules(op, value):
    rule = {"id": "missing", "signal": "weather",
            "conditions": [{"field": "weather.is_rain", "op": op, "value": value}]}
    assert mood_engine.evaluate([rule], {}, {"weather_is_rain": True}, {}, NOW)[0] == []
    assert mood_engine.evaluate([rule], {"weather": {"available": True}}, {}, {}, NOW)[0] == []


def test_bad_rule_cooldown_and_expiry_do_not_abort_other_rules():
    rule = {"id": "valid", "signal": "weather",
            "conditions": [{"field": "weather.is_rain", "op": "eq", "value": True}]}
    fired, _ = mood_engine.evaluate([None, "bad", rule, rule], {"weather": {"is_rain": True}},
                                    [], {"valid": "corrupt"}, NOW)
    assert len(fired) == 1
    assert mood_engine.prune_expired([None, "bad", {"expires_at": "bad"},
                                     {"expires_at": float("inf")}, fired[0]], NOW) == fired


def test_infinite_custom_rule_parameters_use_existing_safe_defaults():
    rule = {"id": "valid", "signal": "weather",
            "cooldown_minutes": float("inf"), "priority": float("inf"),
            "conditions": [{"field": "weather.is_rain", "op": "eq", "value": True}]}
    fired, _ = mood_engine.evaluate([rule], {"weather": {"is_rain": True}}, {}, {}, NOW)
    assert len(fired) == 1 and fired[0]["priority"] == 3


def test_zero_scene_gain_is_respected_and_nonfinite_bias_is_neutral():
    manor = {"rooms": [{"id": "a", "scene_words": [{"word": "阅读", "gain": 0}],
                       "mood_bias": [float("nan"), 0.4]}]}
    words = space.scene_words(manor, "a")
    assert words[0]["gain"] == 0
    assert space.apply_scene_gain([{"part": "hand", "s": 0.2}], "阅读", words)[0]["s"] == 0.2
    assert space.room_mood_bias(manor, "a") == (0.0, 0.4)


def test_unavailable_boolean_hints_do_not_claim_clear_or_separated():
    hint = mood_engine.render_hint("雨={is_rain} 同室={same_room}", {})
    assert hint == "雨=不明 同室=不明"


def test_rule_cache_cannot_be_modified_through_returned_document(tmp_path):
    mood_store.save_rules(str(tmp_path), {"schema": 1, "rules": [{"id": "original"}]})
    first = mood_store.load_rules(str(tmp_path), str(tmp_path))
    first["rules"][0]["id"] = "mutated"
    assert mood_store.load_rules(str(tmp_path), str(tmp_path))["rules"][0]["id"] == "original"


def test_concurrent_history_appends_are_all_preserved(tmp_path):
    barrier = threading.Barrier(12)
    def append(i):
        barrier.wait(timeout=5)
        mood_store.append_history(str(tmp_path), [{"id": str(i), "rule_id": "r"}])
    with ThreadPoolExecutor(max_workers=12) as executor:
        list(executor.map(append, range(12)))
    data = mood_store.load_json(str(tmp_path), CONFIG.MO_HISTORY_FILE, {})
    assert {item["id"] for item in data["items"]} == {str(i) for i in range(12)}


def test_multiple_store_instances_write_complete_json_without_shared_temp(tmp_path):
    writers = [store.ManorStore(str(tmp_path), suffix=".space.json") for _ in range(4)]
    barrier = threading.Barrier(4)
    def save(i):
        barrier.wait(timeout=5)
        for j in range(10):
            st = space.new_state("demo_owner")
            st["writer"] = (i, j)
            writers[i].save("demo_owner", st)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(save, range(4)))
    assert isinstance(writers[0].load("demo_owner"), dict)
    assert not list((tmp_path / "state").glob("*.tmp"))


def test_corrupt_local_fields_preserve_other_state_and_are_safe_even_when_cached(tmp_path):
    writer = store.ManorStore(str(tmp_path), suffix=".space.json")
    st = space.new_state("demo_owner")
    st.update(she=["broken"], he={"room": "a", "moving": {"until_ts": float("nan"), "to": "b"}},
              events_today={"moves": "bad"}, custom_note="preserved")
    writer.save("demo_owner", st)
    restored = writer.get_or_create("demo_owner", MANOR)
    space.advance(restored, MANOR, NOW)
    assert restored["he"]["room"] == "a" and restored["he"]["moving"] is None
    assert isinstance(restored["she"], dict) and restored["custom_note"] == "preserved"


@pytest.mark.parametrize("key", ["arousal", "verbal_arousal"])
def test_nonfinite_state_cannot_fabricate_touch_intent(key):
    st = {"phase": "building", "arousal": 0.7, "verbal_arousal": 0.4}
    st[key] = float("nan")
    assert proactive_intent.should_intent_touch(st, PROFILE, NOW, templates=["想靠近"]) is None


def test_bad_intent_parameters_and_templates_have_safe_fallback():
    st = {"phase": "building", "arousal": 0.7, "verbal_arousal": 0.4}
    assert proactive_intent.should_intent_touch(st, PROFILE, NOW,
        cfg={"arousal_min": "bad", "verbal_min": float("nan"), "cooldown": -1},
        templates=[None, 123, "想靠近"]) == "想靠近"
    st["proactive_intent"] = {"ts": NOW + 30, "hint": "想靠近"}
    assert proactive_intent.should_intent_touch(st, PROFILE, NOW, templates=["想靠近"]) is None

from tests.provider_fixture import provider, signal, SCOPE
