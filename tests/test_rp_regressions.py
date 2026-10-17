"""Regression coverage for RP persistence and live handlers using synthetic data only."""
import asyncio
import importlib
import sys
import time
import types

import pytest

from astrbot_plugin_virtual_body.core import decay, events, render, space, state
from astrbot_plugin_virtual_body.core.store import ManorStore


T0 = 1_000_000.0
PROFILE = {
    "id": "demo", "name": "测试角色", "whitelist": ["demo"],
    "parts": [{"id": "shoulder", "label": "肩膀", "aliases": ["肩"], "sens": 9.6,
               "half_life": 2000, "radiate": {}}],
    "tiers": [{"key": "notice", "min": .15}, {"key": "react", "min": .3},
              {"key": "lose", "min": .5}, {"key": "overload", "min": .85}],
}
MANOR = {
    "rooms": [{"id": "a", "label": "甲室", "floor": "L1"},
              {"id": "b", "label": "乙室", "floor": "L1"}],
    "edges": [{"a": "a", "b": "b", "seconds": 90}],
}


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    """Exercise real plugin methods without loading AstrBot or making provider calls."""
    class Filters:
        EventMessageType = types.SimpleNamespace(PRIVATE_MESSAGE=1)

        def __getattr__(self, name):
            return lambda *args, **kwargs: lambda fn: fn

    class Star:
        def __init__(self, context):
            pass

    logger = types.SimpleNamespace(**{name: lambda *args, **kwargs: None
                                    for name in ("debug", "info", "warning", "error")})
    modules = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.ModuleType("astrbot.api"),
        "astrbot.api.event": types.ModuleType("astrbot.api.event"),
        "astrbot.api.provider": types.ModuleType("astrbot.api.provider"),
        "astrbot.api.star": types.ModuleType("astrbot.api.star"),
        "astrbot.api.web": types.ModuleType("astrbot.api.web"),
    }
    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = logger
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].filter = Filters()
    modules["astrbot.api.provider"].ProviderRequest = object
    star_api = modules["astrbot.api.star"]
    star_api.Context = object
    star_api.Star = Star
    star_api.StarTools = types.SimpleNamespace(get_data_dir=lambda name: tmp_path)
    star_api.register = lambda *args: lambda cls: cls
    web_api = modules["astrbot.api.web"]
    web_api.request = types.SimpleNamespace()
    web_api.json_response = lambda data: data
    web_api.error_response = lambda error: {"error": error}
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    for name in ("astrbot_plugin_virtual_body.main", "astrbot_plugin_virtual_body.web_api"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    main = importlib.import_module("astrbot_plugin_virtual_body.main")
    api = importlib.import_module("astrbot_plugin_virtual_body.web_api")
    monkeypatch.setattr(main, "time", types.SimpleNamespace(time=lambda: T0, strftime=time.strftime))
    plugin = main.VirtualBodyPlugin(types.SimpleNamespace(), {
        "vb_default_character": "demo", "vb_verbal_arousal_enabled": False,
        "vb_mood_coupling_enabled": False, "presence_bridge_enabled": False,
    })
    plugin._profiles = {"demo": PROFILE}
    plugin._store = state.StateStore(str(tmp_path))
    plugin._manor_store = ManorStore(str(tmp_path), ".space.json")
    plugin._manor = MANOR
    plugin._space_data_dir = str(tmp_path)
    yield plugin, api, main
    sys.modules.pop("astrbot_plugin_virtual_body.main", None)
    sys.modules.pop("astrbot_plugin_virtual_body.web_api", None)


class Event:
    unified_msg_origin = "test:FriendMessage:demo"

    def __init__(self, text):
        self.message_str = text

    def get_sender_id(self):
        return "demo"

    def get_self_id(self):
        return "bot"

    def plain_result(self, text):
        return text


def collect(generator):
    async def run():
        return [reply async for reply in generator]
    return asyncio.run(run())


def test_first_location_observation_is_saved(runtime):
    plugin, _, _ = runtime
    plugin.config["vb_enabled"] = False
    asyncio.run(plugin.observe_private_message(Event("我在甲室")))
    saved = plugin._manor_store.load("demo_demo")
    assert saved is not None
    assert saved["he"]["room"] == "a"
    assert saved["he"]["confidence"] == .9


def test_same_room_rp_renewal_survives_next_read(runtime):
    plugin, _, _ = runtime
    plugin.config["vb_enabled"] = False
    initial = space.new_state("demo_demo")
    space.set_room(initial, "she", "a", MANOR, T0 - 8000, "rp")
    plugin._manor_store.save("demo_demo", initial)
    asyncio.run(plugin.observe_private_message(Event("她回甲室")))
    restored = plugin._manor_store.get_or_create("demo_demo", MANOR)
    assert restored["she"]["since"] == space.iso(T0)
    space.advance(restored, {**MANOR, "default_schedule": [{"from_h": 0, "to_h": 24, "room": "b"}]},
                  T0 + 60, tz_offset=0)
    assert restored["she"]["room"] == "a" and restored["she"]["moving"] is None


def test_repeat_where_command_reports_remaining_seconds(runtime):
    plugin, _, _ = runtime
    st = space.new_state("demo_demo")
    space.set_room(st, "he", "a", MANOR, T0 - 100, "manual", 1.)
    space.set_room(st, "he", "b", MANOR, T0 - 30, "manual", 1.)
    plugin._manor_store.save("demo_demo", st)
    replies = collect(plugin.cmd_where(Event("/where 乙室")))
    assert len(replies) == 1 and "60 秒" in replies[0]
    assert plugin._manor_store.load("demo_demo")["events_today"]["moves"] == 1


def test_reassertion_after_arrival_lands_before_return():
    st = space.new_state("demo")
    space.set_room(st, "he", "a", MANOR, T0, "manual")
    space.set_room(st, "he", "b", MANOR, T0, "manual")
    assert space.set_room(st, "he", "b", MANOR, T0 + 91, "manual") is None
    assert st["he"]["moving"] is None and st["he"]["room"] == "b"


def test_eta_uses_remaining_travel_time():
    st = space.new_state("demo")
    space.set_room(st, "she", "a", MANOR, T0, "rp")
    space.set_room(st, "she", "b", MANOR, T0, "rp")
    assert "得走一阵" in render.render_injection(st, MANOR, T0)
    assert "还要一会儿" in render.render_injection(st, MANOR, T0 + 40)
    assert "马上就到" in render.render_injection(st, MANOR, T0 + 80)


def test_state_restart_preserves_adaptation_guard_and_material(tmp_path):
    original = state.normalize(None, PROFILE)
    original["longterm_day"] = "2026-10-13"
    original["parts"]["shoulder"].update(last_material="cotton", last_material_at=decay.iso_ts(T0))
    state.StateStore(str(tmp_path)).save("demo", "demo", original)
    restored = state.StateStore(str(tmp_path)).get_or_create("demo", "demo", PROFILE)
    assert restored["longterm_day"] == "2026-10-13"
    assert restored["parts"]["shoulder"]["last_material"] == "cotton"
    assert restored["parts"]["shoulder"]["last_material_at"] == decay.iso_ts(T0)
    assert state.normalize(restored, PROFILE) == restored


def test_adaptation_day_change_alone_triggers_persistence():
    st = state.normalize(None, PROFILE)
    before = state.state_signature(st)
    st["longterm_day"] = "2026-10-13"
    assert state.state_signature(st) != before


def test_overload_handler_does_not_recreate_ended_contact(runtime):
    plugin, _, _ = runtime
    st = state.normalize(None, PROFILE)
    st["updated_at"] = decay.iso_ts(T0)
    st["parts"]["shoulder"]["value"] = .9
    plugin._store.save("demo", "demo", st)
    result = plugin._advance_session("demo", "demo", [{"part": "shoulder", "s": .1, "t": T0}], T0)
    assert result["overload"] is True
    assert plugin._store.load("demo", "demo")["active_contact"] == {}


def test_delete_binding_updates_running_resolver(runtime, monkeypatch):
    plugin, api, _ = runtime
    other = {**PROFILE, "id": "other"}
    plugin._profiles["other"] = other
    plugin._bindings = {"demo": "other"}
    plugin._store.save_bindings(plugin._bindings)
    async def body():
        return {"session": "demo"}
    monkeypatch.setattr(api, "_safe_json", body)
    result = asyncio.run(api.VirtualBodyWebApi(plugin).delete_binding())
    assert result["removed"] == "other"
    assert plugin._resolve_session("demo") == ("demo", "demo")
    assert plugin._store.load_bindings() == {}


@pytest.mark.parametrize("text", ["停手", "我松开手", "请结束接触", "（停手，收回手）"])
def test_explicit_end_contact_matches_all_parts(text):
    assert events.extract_stops(text, PROFILE) == ["shoulder"]


@pytest.mark.parametrize("text", ["结束", "不要停手", "别松开手", "不结束接触", "没有停手",
                                 "我未停手", "如果停手的话", "怎么结束接触", "停手吗", "停手？",
                                 '她说“停手”', "歌词是'停手'", "引用「松开手」", "`结束接触`"])
def test_quoted_negated_or_ambiguous_end_does_not_clear(text):
    assert events.extract_stops(text, PROFILE) == []


def test_explicit_end_contact_clears_live_state(runtime):
    plugin, _, _ = runtime
    st = state.normalize(None, PROFILE)
    st["active_contact"] = {"shoulder": {"action": "摸", "s": .6, "since": decay.iso_ts(T0),
                                        "last_renew": decay.iso_ts(T0), "last_msg": decay.iso_ts(T0)}}
    plugin._store.save("demo", "demo", st)
    asyncio.run(plugin.observe_private_message(Event("停手")))
    assert plugin._store.load("demo", "demo")["active_contact"] == {}


def test_disabled_persistence_still_clears_old_contact_on_stop(runtime):
    plugin, _, _ = runtime
    plugin.config["cs_persist_enabled"] = False
    st = state.normalize(None, PROFILE)
    st["active_contact"] = {"shoulder": {"action": "摸", "s": .6, "last_renew": decay.iso_ts(T0)}}
    plugin._store.save("demo", "demo", st)
    asyncio.run(plugin.observe_private_message(Event("停手")))
    # A stale contact must not revive if persistence is enabled again later.
    assert plugin._store.load("demo", "demo")["active_contact"] == {}


def test_verbal_weight_panel_and_profile_precedence(runtime):
    plugin, _, _ = runtime
    plugin.config["vb_verbal_arousal_weight"] = 1.2
    st = state.normalize(None, PROFILE)
    st.update(verbal_arousal=.3, updated_at=decay.iso_ts(T0))
    decay.advance_state(st, PROFILE, None, T0, **plugin._phase_cfg())
    assert st["arousal"] == pytest.approx(.36)
    decay.advance_state(st, {**PROFILE, "params": {"verbal_arousal_weight": .5}}, None, T0,
                        **plugin._phase_cfg())
    assert st["arousal"] == pytest.approx(.15)


def test_space_render_budget_panel_and_manor_precedence():
    st = space.new_state("demo")
    space.set_room(st, "she", "a", MANOR, T0, "rp")
    bounded = render.render_injection(st, MANOR, T0, max_chars=60)
    assert len(bounded) <= 60 and bounded.endswith("</空间>")
    assert render.render_injection(st, MANOR, T0, max_chars=10) == ""
    override = {**MANOR, "render": {"max_chars": 50}}
    assert len(render.render_injection(st, override, T0, max_chars=120)) <= 50


@pytest.mark.parametrize("opening,closing", [("<", ">"), ("＜", "＞")])
@pytest.mark.parametrize("end_opening,end_closing", [("<", ">"), ("＜", "＞")])
@pytest.mark.parametrize("tag", ["身体状态", "空间"])
def test_real_outbound_chain_removes_complete_internal_block(runtime, opening, closing,
                                                            end_opening, end_closing, tag):
    plugin, _, _ = runtime
    component = types.SimpleNamespace(
        text=f'前面的台词。{opening}{tag} 角色="测试"{closing}内部提示{end_opening}/{tag}{end_closing}后面的动作。')
    event = Event("普通消息")
    event.get_result = lambda: types.SimpleNamespace(chain=[component])
    asyncio.run(plugin.sanitize_presence_tags_outbound(event))
    assert component.text == "前面的台词。后面的动作。"


def test_outbound_chain_keeps_ordinary_brackets_and_text(runtime):
    plugin, _, _ = runtime
    text = "（她停下）[心情不错]＜普通备注＞你好。<em>原样</em><空间说明>内容</空间说明><身体状态说明>内容</身体状态说明>"
    component = types.SimpleNamespace(text=text)
    event = Event("普通消息")
    event.get_result = lambda: types.SimpleNamespace(chain=[component])
    asyncio.run(plugin.sanitize_presence_tags_outbound(event))
    assert component.text == text
