"""Management payload -> persistent override -> actual core consumption.

All files are synthetic and temporary. AstrBot HTTP/auth is stubbed by the
existing API fixture; these are API/core contract tests, not live server tests.
"""
import asyncio
import copy
import json

import pytest

from astrbot_plugin_virtual_body.core import events, material, space
from astrbot_plugin_virtual_body.tests.test_presence_surface import (
    MANOR, PROFILE, api_runtime, post,
)


def clone(value):
    return copy.deepcopy(value)


def test_scene_word_page_payload_persists_and_drives_actual_event_gain(api_runtime, monkeypatch):
    plugin, module, api, directory = api_runtime
    rooms = clone(MANOR["rooms"])
    words = [{"word": "整理桌面", "gain": .2, "note": "示例场景"}]
    rooms[0]["scene_words"] = words
    result = post(monkeypatch, module, api.save_manor, {"rooms": rooms, "edges": MANOR["edges"]})
    assert result.get("ok") is True
    persisted = json.loads(next(directory.rglob("manor.json")).read_text(encoding="utf-8"))
    assert persisted["rooms"][0]["scene_words"] == words
    reloaded = asyncio.run(api.get_manor())
    assert reloaded["rooms"][0]["scene_words"] == words
    raw_event = [{"part": "shoulder", "s": .4}]
    consumed = space.apply_scene_gain(raw_event, "整理桌面", space.scene_words(reloaded, "a"))
    assert consumed[0]["s"] == pytest.approx(.48)
    assert consumed[0]["scene"] == "整理桌面"
    assert raw_event[0]["s"] == .4


@pytest.mark.parametrize("entry", [
    "plain string", {}, {"word": " "}, {"word": "a" * 65, "gain": .1},
    {"word": "sample", "gain": -.01}, {"word": "sample", "gain": .51},
    {"word": "sample", "gain": float("nan")}, {"word": "sample", "gain": float("inf")},
    {"word": "sample", "gain": True}, {"word": "sample", "gain": .1, "note": "a" * 201},
    {"word": "sample", "gain": .1, "note": []},
])
def test_invalid_scene_word_does_not_replace_existing_manor(api_runtime, monkeypatch, entry):
    plugin, module, api, directory = api_runtime
    assert post(monkeypatch, module, api.save_manor, {"rooms": MANOR["rooms"]})["ok"]
    destination = next(directory.rglob("manor.json"))
    before = destination.read_bytes()
    memory_before = clone(plugin._manor)
    rooms = clone(MANOR["rooms"])
    rooms[0]["scene_words"] = [entry]
    assert "error" in post(monkeypatch, module, api.save_manor, {"rooms": rooms})
    assert destination.read_bytes() == before
    assert plugin._manor == memory_before
    assert not list(directory.rglob("*.tmp"))


BUILTIN_DOC = {
    "_refs": [{"k": "sample", "ref": "示例", "url": "https://example.org/reference"}],
    "materials": [{"id": "cotton", "label": "棉布", "aliases": ["棉织物"],
                   "roughness_um": 2.0, "thermal": .2, "ct_affinity": .8}],
    "method_params": {"rub": {"freq_min": 5.0, "freq_max": 50.0}},
}
NEW_MATERIAL = {"id": "soft_fabric", "label": "柔软布", "aliases": ["测试布"],
                "roughness_um": 22.0, "thermal": .3, "ct_affinity": .7}


@pytest.fixture
def material_runtime(api_runtime, monkeypatch):
    plugin, module, api, directory = api_runtime
    package_dir = directory / "package"
    (package_dir / "core").mkdir(parents=True)
    (package_dir / "data").mkdir()
    builtin = package_dir / "data" / "materials.json"
    builtin.write_text(json.dumps(BUILTIN_DOC, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(material, "__file__", str(package_dir / "core" / "material.py"))
    monkeypatch.setattr(material, "_STORAGE_DIR", None)
    monkeypatch.setattr(material, "_MAT_CACHE", {})
    monkeypatch.setattr(material, "_METHOD_PARAMS_CACHE", {})
    return plugin, module, api, directory, builtin


def test_material_override_preserves_builtin_and_is_used_by_events_after_reload(material_runtime, monkeypatch):
    _, module, api, directory, builtin = material_runtime
    before = builtin.read_bytes()
    assert material.default_materials_path() == builtin
    assert material.get_material("cotton")["roughness_um"] == 2
    result = post(monkeypatch, module, api.save_materials, {"materials": [NEW_MATERIAL]})
    assert result.get("ok") is True
    destination = directory / "materials.json"
    assert destination.is_file()
    assert builtin.read_bytes() == before
    assert material.default_materials_path() == destination
    assert material.get_material("soft_fabric")["roughness_um"] == 22
    extracted = events.extract_events("用测试布摸她的肩膀", PROFILE)
    assert extracted and extracted[0]["material"] == "soft_fabric"
    assert asyncio.run(api.get_materials())["materials"][0]["id"] == "soft_fabric"
    # A plugin restart reconfigures the same directory, preserving the override.
    material.configure_storage(None)
    assert material.default_materials_path() == builtin
    material.configure_storage(directory)
    assert material.load_materials(force=True)["soft_fabric"]["roughness_um"] == 22
    assert "cotton" not in material.load_materials()
    assert builtin.read_bytes() == before


def test_material_save_retains_existing_references_and_method_params(material_runtime, monkeypatch):
    _, module, api, directory, builtin = material_runtime
    assert post(monkeypatch, module, api.save_materials, {"materials": [NEW_MATERIAL]})["ok"]
    doc = json.loads((directory / "materials.json").read_text(encoding="utf-8"))
    assert doc["_refs"] == BUILTIN_DOC["_refs"]
    assert doc["method_params"] == BUILTIN_DOC["method_params"]


def test_material_atomic_replace_failure_keeps_old_override_and_reports_failure(material_runtime, monkeypatch):
    _, module, api, directory, builtin = material_runtime
    assert post(monkeypatch, module, api.save_materials, {"materials": [NEW_MATERIAL]})["ok"]
    destination = directory / "materials.json"
    before, builtin_before = destination.read_bytes(), builtin.read_bytes()

    def fail_replace(*args):
        raise OSError("synthetic storage failure")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    changed = {**NEW_MATERIAL, "roughness_um": 88.0}
    result = post(monkeypatch, module, api.save_materials, {"materials": [changed]})
    assert "error" in result
    assert destination.read_bytes() == before
    assert builtin.read_bytes() == builtin_before
    assert material.get_material("soft_fabric")["roughness_um"] == 22
    assert not list(directory.glob("*.tmp"))


@pytest.mark.parametrize("payload", ["{invalid", "[]", "null", "42", '"wrong shape"', '{"materials":null}'])
def test_malformed_materials_degrade_without_management_or_core_exception(material_runtime, payload):
    _, _, api, directory, builtin = material_runtime
    destination = directory / "materials.json"
    destination.write_text(payload, encoding="utf-8")
    material.configure_storage(directory)
    result = asyncio.run(api.get_materials())
    assert result["materials"] == [] and result["_refs"] == []
    assert material.load_materials(force=True) == {}
    assert material.get_material("missing") is None
    assert material.load_method_params(force=True)["rub"]["freq_min"] == 5


def test_page_method_parameter_keys_change_runtime_window_and_curve(material_runtime, monkeypatch):
    _, module, api, directory, builtin = material_runtime
    material.configure_storage(None)
    old_gain = material.material_gain({"sens": 5}, BUILTIN_DOC["materials"][0], "rub", {"freq_hz": 150})
    page_params = {"rub": {"freq_lo": 120, "freq_hi": 200},
                   "tap": {"freq_lo": 40, "freq_hi": 80},
                   "stroke": {"speed_lo": 2, "speed_hi": 8}}
    assert post(monkeypatch, module, api.save_materials,
                {"materials": BUILTIN_DOC["materials"], "method_params": page_params})["ok"]
    current = material.load_method_params()
    assert current["rub"]["freq_min"] == 120
    assert current["rub"]["freq_max"] == 200
    assert current["tap"]["freq_min"] == 40
    assert current["tap"]["freq_max"] == 80
    assert current["stroke"]["speed_min"] == 2
    assert current["stroke"]["speed_max"] == 8
    new_gain = material.material_gain({"sens": 5}, material.get_material("cotton"), "rub", {"freq_hz": 150})
    assert new_gain > old_gain


def test_canonical_windows_round_trip_to_page_and_preserve_unedited_method_settings(material_runtime, monkeypatch):
    _, module, api, directory, builtin = material_runtime
    legacy = clone(BUILTIN_DOC)
    legacy["method_params"] = {
        "rub": {"freq_min": 20, "freq_max": 60},
        "tap": {"freq_min": 180, "freq_max": 240},
        "stroke": {"speed_min": 2, "speed_max": 6, "falloff": 70},
        "blow": {"peak_temp_c": 29},
        "press": {"extension_hint": "synthetic"},
    }
    builtin.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    material.configure_storage(directory)  # Same lifecycle hook as the actual plugin.
    before = builtin.read_bytes()
    view = asyncio.run(api.get_materials())
    assert view["method_params"]["rub"]["freq_lo"] == 20
    assert view["method_params"]["rub"]["freq_hi"] == 60
    assert view["method_params"]["stroke"]["speed_lo"] == 2
    assert view["method_params"]["stroke"]["speed_hi"] == 6
    # The existing page submits only these three windows; other fields are absent.
    page_params = {
        method: {key: view["method_params"][method][key] for key in keys}
        for method, keys in (("rub", ("freq_lo", "freq_hi")),
                             ("tap", ("freq_lo", "freq_hi")),
                             ("stroke", ("speed_lo", "speed_hi")))
    }
    page_params["rub"] = {"freq_lo": 25, "freq_hi": 65}
    assert post(monkeypatch, module, api.save_materials,
                {"materials": view["materials"], "method_params": page_params})["ok"]
    saved = json.loads((directory / "materials.json").read_text(encoding="utf-8"))
    assert saved["method_params"]["stroke"]["falloff"] == 70
    assert saved["method_params"]["blow"] == {"peak_temp_c": 29}
    assert saved["method_params"]["press"] == {"extension_hint": "synthetic"}
    assert saved["method_params"]["rub"]["freq_min"] == 25
    assert saved["method_params"]["rub"]["freq_lo"] == 25
    effective = material.load_method_params()
    assert effective["blow"]["peak_temp_c"] == 29
    assert effective["stroke"]["falloff"] == 70
    assert asyncio.run(api.get_materials())["method_params"]["rub"]["freq_lo"] == 25
    assert builtin.read_bytes() == before


@pytest.mark.parametrize("high", [20.0, .05])
def test_zero_frequency_lower_bound_is_hot_applied_including_small_valid_window(material_runtime, monkeypatch, high):
    _, module, api, directory, builtin = material_runtime
    material.configure_storage(directory)
    assert post(monkeypatch, module, api.save_materials, {
        "materials": BUILTIN_DOC["materials"],
        "method_params": {"rub": {"freq_lo": 0, "freq_hi": high}},
    })["ok"]
    effective = material.load_method_params()
    assert effective["rub"]["freq_min"] == 0
    assert effective["rub"]["freq_max"] == high
    assert asyncio.run(api.get_materials())["method_params"]["rub"]["freq_hi"] == high
    gain = material.material_gain({"sens": 5}, material.get_material("cotton"), "rub", {"freq_hz": 0})
    assert 0.4 <= gain <= 1.8
