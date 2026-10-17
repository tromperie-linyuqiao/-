"""Body continuity, corrupt legacy anchors and persistence regression scenarios."""
from copy import deepcopy
import json
import math
from pathlib import Path

import pytest

from astrbot_plugin_virtual_body import CONFIG
from astrbot_plugin_virtual_body.core import adaptation, decay, material, persist_contact
from astrbot_plugin_virtual_body.core.state import StateStore, normalize, state_signature

PROFILE = json.loads((Path(__file__).parents[1] / "tests/fixtures/profiles/role_a.json").read_text(encoding="utf-8"))
NOW = 1_000_000.0


def state():
    result = normalize(None, PROFILE)
    result["updated_at"] = decay.iso_ts(NOW)
    return result


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), 1e100, True])
def test_unrepresentable_timestamps_do_not_enter_state(bad):
    assert decay.parse_ts(bad) is None
    cleaned = normalize({"phase": "afterglow", "phase_until": bad, "updated_at": bad,
                         "verbal_at": bad, "active_contact": {"ear_tip": {"s": .5, "last_renew": bad}}}, PROFILE)
    assert cleaned["phase"] == "building"
    assert cleaned["phase_until"] is None
    assert cleaned["updated_at"] is None
    assert cleaned["verbal_at"] is None
    assert not cleaned["active_contact"]
    decay.advance_state(cleaned, PROFILE, [{"part": "ear_tip", "s": .2, "t": bad}], NOW)
    assert math.isfinite(cleaned["arousal"])
    json.dumps(cleaned, allow_nan=False)


@pytest.mark.parametrize("value", [0, 0.0, "1970-01-01T00:00:00Z", "1970-01-01T08:00:00+08:00"])
def test_legitimate_epoch_and_utc_anchors_remain_compatible(value):
    assert decay.parse_ts(value) == 0.0
    cleaned = normalize({"phase": "afterglow", "phase_until": value, "updated_at": value,
                         "verbal_at": value, "parts": {"ear_tip": {"last_event_at": value}},
                         "active_contact": {"ear_tip": {"s": .5, "last_renew": value}}}, PROFILE)
    assert cleaned["phase"] == "afterglow"
    assert cleaned["updated_at"] == value
    assert cleaned["parts"]["ear_tip"]["last_event_at"] == value
    assert cleaned["active_contact"]["ear_tip"]["last_renew"] == value
    assert normalize(cleaned, PROFILE) == cleaned


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_profile_parameters_use_neutral_defaults(bad):
    assert decay.profile_param({"params": {"gain": bad}}, "gain", .25, 0, 2) == .25
    assert decay.part_tau({"half_life": bad}) == CONFIG.DECAY_TAU_DEFAULT
    assert decay.clamp(bad, 0, 1) == 0
    assert material.clamp(bad, 0, 1) == 0
    assert material._num(bad, 5) == 5
    assert normalize({"schema": bad}, PROFILE)["schema"] == CONFIG.STATE_SCHEMA


@pytest.mark.parametrize("semantics", [
    {"material": "silk", "method": "stroke", "speed_cm_s": 3},
    {"type": "temp", "material": "ice", "method": "press", "depth": .3, "temp_c": 0},
    {"type": "air", "material": "feather", "method": "blow", "temp_c": 32},
    {"material": "wool", "method": "rub", "freq_hz": 8},
])
def test_contact_survives_disk_with_same_semantics_and_single_gain(semantics, tmp_path, monkeypatch):
    # Freeze circadian and entry-context effects so first/continued tactile semantics are comparable.
    monkeypatch.setattr(CONFIG, "CIRCADIAN_ENABLED", False)
    profile = deepcopy(PROFILE)
    ear = next(p for p in profile["parts"] if p["id"] == "ear_tip")
    ear["radiate"] = {}
    ear["material_affinity"] = {semantics["material"]: 1.1}
    ear["type_mults"] = {semantics.get("type", "touch"): 1.1}
    initial = state()
    event = {"part": "ear_tip", "s": .2, "type": "touch", "t": NOW, **semantics}
    decay.advance_state(initial, profile, [event], NOW, intimacy_cfg={"l1": 1, "l2": 1})
    first_gain = initial["parts"]["ear_tip"]["value"]
    persist_contact.record_contacts(initial, [event], NOW)
    store = StateStore(str(tmp_path))
    store.save("role_a", "test", initial)
    restored = store.get_or_create("role_a", "test", profile)
    later = NOW + 60
    continuation = persist_contact.persist_events(restored, [], later, {"rate": .4})
    assert len(continuation) == 1 and continuation[0]["s"] == .08
    for key, value in semantics.items():
        assert continuation[0][key] == value
    old_count, old_history = restored["events_today"]["count"], deepcopy(restored["history"])
    decay.advance_state(restored, profile, continuation, later, intimacy_cfg={"l1": 1, "l2": 1})
    decayed_first = first_gain * math.exp(-60 / decay.part_tau(ear))
    actual_increment = restored["parts"]["ear_tip"]["value"] - decayed_first
    # Initial event's material, type and per-part affinity are applied once in both paths.
    assert actual_increment / (1 - restored["tolerance"]) == pytest.approx(first_gain * .4, abs=1e-10)
    assert restored["events_today"]["count"] == old_count
    assert restored["history"] == old_history
    assert restored["parts"]["ear_tip"]["last_material"] == semantics["material"]
    assert restored["parts"]["ear_tip"]["last_material_at"] == decay.iso_ts(later)


def test_legacy_contact_retains_original_neutral_continuation(monkeypatch):
    monkeypatch.setattr(CONFIG, "CIRCADIAN_ENABLED", False)
    result = state()
    result["active_contact"] = {"ear_tip": {"action": "pain", "s": .5,
                                            "last_renew": decay.iso_ts(NOW)}}
    profile = deepcopy(PROFILE)
    ear = next(p for p in profile["parts"] if p["id"] == "ear_tip")
    ear["type_mults"] = {"pain": 2}
    continuation = persist_contact.persist_events(result, [], NOW + 60, {"rate": .4})
    assert "contact_semantics" not in continuation[0]
    decay.advance_state(result, profile, continuation, NOW + 60)
    assert result["parts"]["ear_tip"]["value"] == pytest.approx(.25 * decay.part_gain_coeff(ear) * .2)


def test_material_contact_stops_expires_and_cannot_recreate_from_synthetic():
    result = state()
    event = {"part": "ear_tip", "s": .5, "material": "silk", "method": "stroke"}
    persist_contact.record_contacts(result, [event], NOW)
    continuation = persist_contact.persist_events(result, [], NOW + 60)
    persist_contact.clear_contacts(result, ["ear_tip"])
    assert persist_contact.record_contacts(result, continuation, NOW + 60) == 0
    assert not result["active_contact"]
    persist_contact.record_contacts(result, [event], NOW)
    assert persist_contact.persist_events(result, [], NOW + 301) == []
    assert not result["active_contact"]


def test_rich_contact_is_cleared_at_overload(monkeypatch):
    monkeypatch.setattr(CONFIG, "CIRCADIAN_ENABLED", False)
    result = state()
    for pid in ("ear_tip", "nape", "tail_root"):
        result["parts"][pid]["value"] = .9
    persist_contact.record_contacts(result, [{"part": "ear_tip", "s": .8, "material": "silk"}], NOW)
    summary = decay.advance_state(result, PROFILE, [{"part": "ear_tip", "s": .1}], NOW)
    assert summary["overload"] and not result["active_contact"]


@pytest.mark.parametrize("key,value", [("last_tick", decay.iso_ts(NOW + 60)),
                                      ("last_msg", decay.iso_ts(NOW + 60)),
                                      ("method", "rub"), ("material", "silk"), ("s", .7)])
def test_contact_channel_and_semantic_changes_trigger_persistence(key, value):
    before = state()
    persist_contact.record_contacts(before, [{"part": "ear_tip", "s": .5}], NOW)
    after = deepcopy(before)
    after["active_contact"]["ear_tip"][key] = value
    assert state_signature(before) != state_signature(after)


def test_tiny_tick_still_records_guard_across_restart(tmp_path):
    result = state()
    persist_contact.record_contacts(result, [{"part": "ear_tip", "s": .000001}], NOW)
    before = state_signature(result)
    generated = persist_contact.persist_events(result, [], NOW + 60, tick=True)
    decay.advance_state(result, PROFILE, generated, NOW + 60)
    assert state_signature(result) != before
    store = StateStore(str(tmp_path))
    store.save("role_a", "test", result)
    restored = store.get_or_create("role_a", "test", PROFILE)
    assert persist_contact.persist_events(restored, [], NOW + 120, tick=True) == []


@pytest.mark.parametrize("config", [{"rate": float("nan")}, {"tick_every": float("inf")},
                                   {"max_gap": "bad"}, {"tick_interval": None}])
def test_corrupt_optional_continuation_settings_use_existing_defaults(config):
    result = state()
    persist_contact.record_contacts(result, [{"part": "ear_tip", "s": .5}], NOW)
    assert len(persist_contact.persist_events(result, [], NOW + 60, config, tick=True)) == 1


def test_future_contact_is_not_permanently_renewed():
    result = state()
    persist_contact.record_contacts(result, [{"part": "ear_tip", "s": .5}], NOW + 86400)
    assert persist_contact.persist_events(result, [], NOW) == []
    assert not result["active_contact"]


def test_adaptation_ignores_future_nonfinite_and_indirect_history():
    result = state()
    valid = {"part": "ear_tip", "old": .1, "new": .2, "t": decay.iso_ts(NOW), "src": "touch"}
    result["history"] = [valid, {**valid, "t": decay.iso_ts(NOW + 86400)},
                         {**valid, "new": float("nan")}, {**valid, "old": float("inf")},
                         {**valid, "src": "传导←nape"}]
    assert adaptation.direct_hit_counts(result, NOW) == {"ear_tip": 1}


@pytest.mark.parametrize("raw", [[], {"materials": "bad"}, {"materials": [None, 1, "bad"]}])
def test_malformed_material_library_degrades_without_breaking_body(raw, tmp_path):
    path = tmp_path / "materials.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert material.load_materials(path, force=True) == {}


@pytest.mark.parametrize("character", ["role_demo", "role-demo"])
def test_state_listing_preserves_underscore_character_and_session_on_restart(tmp_path, character):
    store = StateStore(str(tmp_path))
    first = state()
    first.update(character=character, session="session_with_parts")
    store.save(character, "session_with_parts", first)
    restarted = StateStore(str(tmp_path))
    assert restarted.list_states() == [(character, "session_with_parts")]
    assert restarted.list_states() == [(character, "session_with_parts")]
    assert restarted.delete_state(character, "session_with_parts")
    assert restarted.list_states() == [] and not restarted._identity_cache


def test_legacy_listing_and_mismatched_identity_do_not_retarget_files(tmp_path):
    store = StateStore(str(tmp_path))
    directory = tmp_path / CONFIG.STATE_DIR_NAME
    (directory / "legacyrole_old_session.json").write_text("{}", encoding="utf-8")
    (directory / "legacyrole_other.json").write_text(json.dumps({"character": "role_demo", "session": "fake"}), encoding="utf-8")
    (directory / "role_a_side.space.json").write_text("{}", encoding="utf-8")
    assert set(store.list_states()) == {("legacyrole", "old_session"), ("legacyrole", "other")}


def test_profile_hot_edit_normalizes_cached_parts_and_contacts(tmp_path):
    store = StateStore(str(tmp_path))
    before = state()
    before["parts"]["ear_tip"]["value"] = .4
    persist_contact.record_contacts(before, [{"part": "ear_tip", "s": .5}], NOW)
    store.save("role_a", "test", before)
    profile = deepcopy(PROFILE)
    profile["parts"] = [part for part in profile["parts"] if part["id"] != "ear_tip"]
    profile["parts"].append({"id": "new_part", "sens": 5, "half_life": 60})
    after = store.get_or_create("role_a", "test", profile)
    assert "ear_tip" not in after["parts"] and "ear_tip" not in after["active_contact"]
    assert after["parts"]["new_part"]["value"] == 0
    # Caller mutation cannot contaminate the saved snapshot or another read.
    after["parts"]["new_part"]["value"] = .9
    assert store.get_or_create("role_a", "test", profile)["parts"]["new_part"]["value"] == 0
