"""存在层呈现、动作归属与管理入口回归；仅使用虚构会话和临时目录。"""
import asyncio
import importlib.util
import json
import pathlib
import sys
import types
from concurrent.futures import ThreadPoolExecutor

import pytest

from astrbot_plugin_virtual_body.core import extract, inject, render, state
from astrbot_plugin_virtual_body.core.store import ManorStore


PROFILE = {
    "id": "role_demo", "name": '测试"角色', "whitelist": ["synthetic"],
    "parts": [{"id": "shoulder", "label": "肩膀", "sens": 8,
               "half_life": 1200, "radiate": {}, "aliases": []}],
    "tiers": [{"key": "notice", "min": .15}, {"key": "react", "min": .3},
              {"key": "lose", "min": .5}, {"key": "overload", "min": .85}],
}
MANOR = {
    "id": "manor", "schema": 1, "floors": [{"id": "L1", "name": "一层"}],
    "rooms": [{"id": "a", "label": "甲室", "floor": "L1", "center": [1, 2], "size": [3, 4]},
              {"id": "b", "label": "乙室", "floor": "L1", "center": [4, 5], "size": [3, 4]}],
    "edges": [{"a": "a", "b": "b", "seconds": 30}],
}


@pytest.fixture
def api_runtime(monkeypatch, tmp_path):
    api_root = types.ModuleType("astrbot.api")
    api_root.logger = types.SimpleNamespace(**{k: lambda *a, **kw: None for k in ("info", "warning", "error")})
    web = types.ModuleType("astrbot.api.web")
    web.request = types.SimpleNamespace()
    web.json_response = lambda data: data
    web.error_response = lambda error: {"error": error}
    monkeypatch.setitem(sys.modules, "astrbot", types.ModuleType("astrbot"))
    monkeypatch.setitem(sys.modules, "astrbot.api", api_root)
    monkeypatch.setitem(sys.modules, "astrbot.api.web", web)
    name = "astrbot_plugin_virtual_body._surface_api_test"
    path = pathlib.Path(__file__).resolve().parents[1] / "web_api.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    plugin = types.SimpleNamespace(
        _manor=json.loads(json.dumps(MANOR)), _manor_source="synthetic",
        _profiles={PROFILE["id"]: PROFILE}, config={},
        _space_data_dir=str(tmp_path), _mood_dir=lambda: str(tmp_path / "mood"),
        _store=state.StateStore(str(tmp_path)),
        _manor_store=ManorStore(str(tmp_path), ".space.json"),
    )
    return plugin, module, module.VirtualBodyWebApi(plugin), tmp_path


def post(monkeypatch, module, handler, body):
    async def read():
        return body
    monkeypatch.setattr(module, "_safe_json", read)
    return asyncio.run(handler())


@pytest.mark.parametrize("text", [
    "她去甲室", "你去甲室吧", "他回甲室了", "我问她要去甲室吗",
    "我去甲室吗？", "如果我去甲室", "我不想去甲室", "我没有打算去甲室",
    "我在想甲室", "她待在甲室", '她说「我在甲室」',
])
def test_other_actor_question_hypothesis_does_not_move_user(text):
    assert extract.extract_him(text, MANOR) is None


@pytest.mark.parametrize("text", [
    "她没有打算去甲室", "她去甲室吗？", "如果她去甲室", "她叫我去甲室",
    '她说「去甲室」', '她说"去甲室', "她不去甲室",
])
def test_unobserved_or_quoted_move_does_not_move_role(text):
    assert extract.extract_her_move(text, MANOR) is None


def test_last_concrete_action_wins_and_long_alias_survives():
    assert extract.extract_him("我在甲室，我回乙室", MANOR) == {"room": "b", "direct": False}
    manor = json.loads(json.dumps(MANOR))
    manor["rooms"][1]["aliases"] = ["很长很长的测试房间"]
    assert extract.extract_him("我去很长很长的测试房间", manor)["room"] == "b"
    assert extract.extract_her_move("她去很长很长的测试房间", manor)["room"] == "b"
    assert extract.extract_him("她自己去甲室", MANOR) is None
    assert extract.extract_her_move("她自己去甲室", MANOR)["room"] == "a"


def test_body_budget_keeps_full_tags_constraint_or_skips():
    st = state.normalize(None, PROFILE)
    st.update(arousal=.5, tolerance=.1)
    st["parts"]["shoulder"]["value"] = .5
    for limit in (0, 10, 50, 80, 100, 160, 400):
        result = inject.render(st, PROFILE, max_chars=limit)
        assert len(result) <= limit
        if result:
            assert result.endswith("</身体状态>") and "约束：" in result
            assert '角色="测试&quot;角色"' in result
    assert inject.render(st, PROFILE, max_chars=80, constraint_text="必须完整保留的自定义约束" * 30) == ""
    assert "他仰头" in inject.posture_hints({"body": {"height_cm": 180}}, 160)[0]


def test_space_budget_is_real_cap_and_unknown_graph_not_false_distance():
    manor = {**MANOR, "render": {"max_chars": 200},
             "characters": {"she_name": '测试"角色'}}
    st = {"she": {"room": "a"}, "he": {"room": "b", "confidence": .9}}
    for limit in (0, 10, 50, 80, 160):
        result = render.render_injection(st, manor, 10000, max_chars=limit)
        assert len(result) <= limit
        if result:
            assert result.endswith("</空间>") and "临时快照" in result
    assert render.distance_band({**MANOR, "edges": []}, "a", "b") == "不明"


def test_manor_omitted_edges_are_preserved(api_runtime, monkeypatch):
    plugin, module, api, tmp_path = api_runtime
    result = post(monkeypatch, module, api.save_manor, {"rooms": MANOR["rooms"]})
    assert result.get("ok") is True
    assert plugin._manor["edges"] == MANOR["edges"]


@pytest.mark.parametrize("change", [
    {"edges": [{"a": "a", "b": "b", "seconds": float("nan")}]},
    {"edges": [{"a": "a", "b": "b", "seconds": 1.5}]},
    {"edges": [{"a": "a", "b": "b", "seconds": True}]},
    {"edges": None},
    {"rooms": [{**MANOR["rooms"][0], "center": [1]}]},
    {"rooms": [{**MANOR["rooms"][0], "size": [0, 3]}]},
    {"rooms": [{**MANOR["rooms"][0], "aliases": "甲室"}]},
])
def test_invalid_manor_is_rejected_before_storage(api_runtime, monkeypatch, change):
    plugin, module, api, tmp_path = api_runtime
    body = {"rooms": MANOR["rooms"], "edges": MANOR["edges"], **change}
    assert "error" in post(monkeypatch, module, api.save_manor, body)
    assert plugin._manor == MANOR
    assert not list(tmp_path.rglob("manor.json"))


@pytest.mark.parametrize("change", [
    {"cooldown_minutes": 0}, {"cooldown_minutes": 1.5}, {"cooldown_minutes": True},
    {"priority": 0}, {"priority": 6}, {"priority": float("nan")},
    {"conditions": [{}]}, {"conditions": "bad"}, {"conditions": []},
    {"conditions": [{"field": "weather.is_rain", "op": "invalid", "value": True}]},
    {"conditions": [{"field": "weather.is_rain", "op": "gt", "value": float("nan")}]},
])
def test_mood_invalid_rule_does_not_replace_file(api_runtime, monkeypatch, change):
    _, module, api, tmp_path = api_runtime
    rule = {"id": "rain", "signal": "weather", "cooldown_minutes": 60, "priority": 3,
            "conditions": [{"field": "weather.is_rain", "op": "eq", "value": True}], **change}
    assert "error" in post(monkeypatch, module, api.save_mood_rules, {"rules": [rule]})
    assert not list(tmp_path.rglob("rules.json"))


def test_valid_mood_rule_is_saved_and_forward_compatible_field_retained(api_runtime, monkeypatch):
    _, module, api, tmp_path = api_runtime
    rule = {"id": "custom", "signal": "presence", "cooldown_minutes": 1, "priority": 5,
            "conditions": [{"field": "presence.custom_field", "op": "eq", "value": "demo"}]}
    assert post(monkeypatch, module, api.save_mood_rules, {"rules": [rule]})["ok"] is True
    assert json.loads((tmp_path / "mood" / "rules.json").read_text(encoding="utf-8"))["rules"] == [rule]


def test_object_duplicates_and_unknown_room_rejected(api_runtime, monkeypatch):
    _, module, api, tmp_path = api_runtime
    for items in ([{"id": "demo", "room": "missing"}],
                  [{"id": "demo", "room": "a"}, {"id": "demo", "room": "a"}]):
        assert "error" in post(monkeypatch, module, api.save_manor_objects, {"items": items})
    assert not list(tmp_path.rglob("objects.json"))


def test_presence_union_body_space_no_creation_and_underscore_character(api_runtime):
    plugin, _, api, _ = api_runtime
    body = plugin._store.get_or_create("role_demo", "body_only", PROFILE)
    plugin._store.save("role_demo", "body_only", body)
    space = plugin._manor_store.get_or_create("role_demo_space_only", MANOR)
    plugin._manor_store.save("role_demo_space_only", space)
    before_body = plugin._store.list_states()
    before_space = plugin._manor_store.list_sessions()
    plugin.runtime_diagnostics = lambda: {"counts": {"body_sessions": 1, "space_sessions": 1}}
    result = asyncio.run(api.presence_overview())
    states = {entry["session"]: entry for entry in result["states"]}
    assert set(states) == {"body_only", "space_only"}
    assert states["body_only"]["has_space"] is False
    assert states["space_only"]["has_body"] is False
    assert all(entry["character"] == "role_demo" for entry in states.values())
    assert result["runtime"] == plugin.runtime_diagnostics()
    assert plugin._store.list_states() == before_body
    assert plugin._manor_store.list_sessions() == before_space


def test_atomic_requests_do_not_share_temp_file(api_runtime):
    _, module, _, tmp_path = api_runtime
    destination = tmp_path / "document.json"
    docs = [{"number": n, "text": str(n) * 4096} for n in range(12)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda data: module._write_json_atomic(str(destination), data), docs))
    assert json.loads(destination.read_text(encoding="utf-8")) in docs
    assert not list(tmp_path.glob("*.tmp"))


def replace_environment(module, monkeypatch, replace, *, platform="nt"):
    """Patch this module only; leave the process-wide os/time modules intact."""
    original = module.os
    monkeypatch.setattr(module, "os", types.SimpleNamespace(
        name=platform, replace=replace, path=original.path,
        fdopen=original.fdopen, unlink=original.unlink,
    ))
    delays = []
    monkeypatch.setattr(module, "time", types.SimpleNamespace(sleep=delays.append))
    return delays


def sharing_failure(code):
    import errno
    error = PermissionError(errno.EACCES, "synthetic file sharing failure")
    error.winerror = code
    return error


@pytest.mark.parametrize("code", [5, 32, 33])
def test_windows_replace_recovers_only_after_transient_failure(api_runtime, monkeypatch, code):
    _, module, _, _ = api_runtime
    calls = []
    failure = sharing_failure(code)
    def replace(source, destination):
        calls.append((source, destination))
        if len(calls) == 1:
            raise failure
    delays = replace_environment(module, monkeypatch, replace)
    module._replace_json_file("source", "destination")
    assert calls == [("source", "destination"), ("source", "destination")]
    assert delays == [.01]


@pytest.mark.parametrize("code", [5, 32, 33])
def test_windows_replace_is_bounded_and_preserves_original_exception(api_runtime, monkeypatch, code):
    _, module, _, _ = api_runtime
    calls = []
    failure = sharing_failure(code)
    def replace(*args):
        calls.append(args)
        raise failure
    delays = replace_environment(module, monkeypatch, replace)
    with pytest.raises(PermissionError) as caught:
        module._replace_json_file("source", "destination")
    assert caught.value is failure
    assert len(calls) == 5
    assert delays == [.01, .02, .04, .05]
    assert sum(round(delay * 1000) for delay in delays) <= 120


@pytest.mark.parametrize("code", [1, 2, 13, 87])
def test_windows_replace_does_not_retry_other_errors(api_runtime, monkeypatch, code):
    _, module, _, _ = api_runtime
    calls = []
    failure = sharing_failure(code)
    def replace(*args):
        calls.append(args)
        raise failure
    delays = replace_environment(module, monkeypatch, replace)
    with pytest.raises(PermissionError) as caught:
        module._replace_json_file("source", "destination")
    assert caught.value is failure
    assert len(calls) == 1
    assert not delays


@pytest.mark.parametrize("code", [5, 32, 33])
def test_non_windows_replace_does_not_retry(api_runtime, monkeypatch, code):
    _, module, _, _ = api_runtime
    calls = []
    failure = sharing_failure(code)
    def replace(*args):
        calls.append(args)
        raise failure
    delays = replace_environment(module, monkeypatch, replace, platform="posix")
    with pytest.raises(PermissionError) as caught:
        module._replace_json_file("source", "destination")
    assert caught.value is failure
    assert len(calls) == 1
    assert not delays


def test_atomic_request_permanent_failure_keeps_original_and_cleans_temp(api_runtime, monkeypatch):
    _, module, _, tmp_path = api_runtime
    destination = tmp_path / "document.json"
    destination.write_text('{"original": true}', encoding="utf-8")
    failure = sharing_failure(5)
    calls = []
    def replace(*args):
        calls.append(args)
        raise failure
    delays = replace_environment(module, monkeypatch, replace)
    with pytest.raises(PermissionError) as caught:
        module._write_json_atomic(str(destination), {"replacement": True})
    assert caught.value is failure
    assert len(calls) == 5
    assert sum(round(delay * 1000) for delay in delays) <= 120
    assert json.loads(destination.read_text(encoding="utf-8")) == {"original": True}
    assert not list(tmp_path.glob("*.tmp"))


def test_curve_nan_and_invalid_points_rejected(api_runtime, monkeypatch):
    _, module, api, _ = api_runtime
    monkeypatch.setattr(module.material_mod, "load_materials", lambda: {"demo": {"label": "演示"}})
    base = {"method": "stroke", "part_sens": 3, "material_id": "demo"}
    for extra in ({"x_min": float("nan")}, {"points": 1.5}, {"points": True}):
        assert "error" in post(monkeypatch, module, api.materials_curve, {**base, **extra})


def test_presence_material_lifetime_matches_runtime_setting(api_runtime, monkeypatch):
    plugin, module, api, _ = api_runtime
    st = plugin._store.get_or_create("role_demo", "demo", PROFILE)
    st["updated_at"] = "2026-01-01T00:02:00+00:00"
    st["parts"]["shoulder"].update(last_material="demo", last_material_at="2026-01-01T00:00:00+00:00")
    plugin._store.save("role_demo", "demo", st)
    monkeypatch.setattr(module.material_mod, "get_material", lambda _: {"label": "演示"})
    monkeypatch.setattr(module.material_mod, "feel_word", lambda *args: "轻柔")
    plugin._feel_word_ttl = lambda: 60
    assert not api._presence_body(plugin._store, plugin._profiles, "role_demo", "demo")["recent_materials"]
    plugin._feel_word_ttl = lambda: 300
    assert api._presence_body(plugin._store, plugin._profiles, "role_demo", "demo")["recent_materials"]
