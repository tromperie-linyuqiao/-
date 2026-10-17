import asyncio
import concurrent.futures
import json
from types import SimpleNamespace

import pytest
from astrbot_plugin_virtual_body.core.runtime import DailyFallbackBudget, RecentMessageIds
from .test_rp_regressions import runtime, T0


def test_budget_is_retained_across_reload_and_date_change(tmp_path):
    one = DailyFallbackBudget(str(tmp_path))
    assert one.reserve("2026-10-13", 2)
    two = DailyFallbackBudget(str(tmp_path))
    assert two.used("2026-10-13") == 1
    assert two.reserve("2026-10-13", 2)
    assert not one.reserve("2026-10-13", 2)
    assert one.reserve("2026-10-14", 2)
    assert DailyFallbackBudget(str(tmp_path)).used("2026-10-13") == 2


@pytest.mark.parametrize("stored", [
    "{bad", '{"schema_version":true,"days":{}}',
    '{"schema_version":1,"days":{"2026-10-13":-1}}',
    '{"schema_version":1,"days":{"2026-10-13":"2"}}',
])
def test_bad_quota_is_not_overwritten_or_reset(tmp_path, stored):
    p = tmp_path / "runtime" / "fallback_budget.json"
    p.parent.mkdir(); p.write_text(stored, encoding="utf-8")
    ledger = DailyFallbackBudget(str(tmp_path))
    assert not ledger.reserve("2026-10-13", 20)
    assert ledger.error_type is not None
    assert p.read_text(encoding="utf-8") == stored


def test_budget_reserves_atomically_across_instances(tmp_path):
    ledgers = [DailyFallbackBudget(str(tmp_path)) for _ in range(8)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(lambda i: ledgers[i % 8].reserve("2026-10-13", 20), range(40)))
    assert sum(accepted) == 20
    assert DailyFallbackBudget(str(tmp_path)).used("2026-10-13") == 20


def test_storage_failure_does_not_reserve_a_model_attempt(tmp_path, monkeypatch):
    import astrbot_plugin_virtual_body.core.runtime as bookkeeping
    ledger = DailyFallbackBudget(str(tmp_path))
    def denied(*args):
        raise PermissionError("synthetic write failure")
    monkeypatch.setattr(bookkeeping.os, "replace", denied)
    assert not ledger.reserve("2026-10-13", 20)
    assert ledger.used("2026-10-13") == 0
    assert ledger.error_type == "PermissionError"
    assert list((tmp_path / "runtime").iterdir()) == []


def test_retention_keeps_current_day_after_clock_moves_back(tmp_path):
    ledger = DailyFallbackBudget(str(tmp_path))
    for n in range(1, 32):
        assert ledger.reserve(f"2026-12-{n:02}", 20)
    assert ledger.reserve("2026-10-13", 1)
    assert not ledger.reserve("2026-10-13", 1)
    assert DailyFallbackBudget(str(tmp_path)).used("2026-10-13") == 1


def test_message_identity_is_bounded_and_not_text_deduplication():
    seen = RecentMessageIds(maximum=2, ttl=10)
    assert seen.accept(("session", "one"), now=0)
    assert not seen.accept(("session", "one"), now=1)
    assert seen.accept(("session", "two"), now=1)
    assert seen.accept(("session", "three"), now=2)
    assert len(seen._seen) == 2
    assert seen.accept(("session", "one"), now=3)
    assert seen.accept(("session", "one"), now=13)
    assert seen.accept(None)
    assert seen.duplicates == 1


def _event(message_id, text="轻轻摸肩"):
    return SimpleNamespace(get_sender_id=lambda: "demo", get_self_id=lambda: "bot",
                           message_str=text, unified_msg_origin="qq:FriendMessage:demo",
                           message_obj=SimpleNamespace(message_id=message_id))


def test_repeated_delivery_does_not_apply_touch_twice(runtime):
    plugin = runtime[0]
    async def scenario():
        await plugin.observe_private_message(_event("one"))
        before = plugin._store.get_or_create("demo", "demo", plugin._profiles["demo"])["parts"]["shoulder"]["value"]
        await plugin.observe_private_message(_event("one"))
        duplicate = plugin._store.get_or_create("demo", "demo", plugin._profiles["demo"])["parts"]["shoulder"]["value"]
        await plugin.observe_private_message(_event("two"))
        new_message = plugin._store.get_or_create("demo", "demo", plugin._profiles["demo"])["parts"]["shoulder"]["value"]
        assert before > 0 and duplicate == before and new_message > before
        assert plugin._observations.duplicates == 1
    asyncio.run(scenario())


def _provider_setup(plugin, tmp_path, suppress_cancel=False):
    entered, release = asyncio.Event(), asyncio.Event()
    calls, applied = [], []
    async def chat(**kwargs):
        calls.append(kwargs)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            if not suppress_cancel:
                raise
            await release.wait()
        return SimpleNamespace(completion_text=json.dumps({"part": "shoulder", "intensity": .2, "type": "touch"}))
    plugin.context = SimpleNamespace(get_using_provider=lambda *args: SimpleNamespace(text_chat=chat))
    plugin._fallback_budget = DailyFallbackBudget(str(tmp_path))
    plugin._advance_session = lambda *args: applied.append(args)
    return entered, release, calls, applied


def test_same_input_uses_one_call_but_preserves_three_actual_actions(runtime, tmp_path):
    plugin = runtime[0]
    async def scenario():
        entered, release, calls, applied = _provider_setup(plugin, tmp_path)
        tasks = [asyncio.create_task(plugin._llm_fallback_extract("demo", "demo", "合成模糊动作")) for _ in range(3)]
        await asyncio.wait_for(entered.wait(), 1)
        release.set()
        await asyncio.gather(*tasks)
        assert len(calls) == 1 and len(applied) == 3
        assert "shoulder" in calls[0]["prompt"]
        assert plugin._fallback_budget.used(plugin._fallback_day()) == 1
        assert not plugin._fb_inflight
    asyncio.run(scenario())


def test_cancelled_waiter_does_not_cancel_other_valid_action(runtime, tmp_path):
    plugin = runtime[0]
    async def scenario():
        entered, release, calls, applied = _provider_setup(plugin, tmp_path)
        one = asyncio.create_task(plugin._llm_fallback_extract("demo", "demo", "合成模糊动作"))
        two = asyncio.create_task(plugin._llm_fallback_extract("demo", "demo", "合成模糊动作"))
        await asyncio.wait_for(entered.wait(), 1)
        one.cancel()
        with pytest.raises(asyncio.CancelledError):
            await one
        release.set(); await two
        assert len(calls) == 1 and len(applied) == 1
    asyncio.run(scenario())


def test_unload_rejects_provider_result_that_ignores_cancel(runtime, tmp_path):
    plugin = runtime[0]
    async def scenario():
        entered, release, _, applied = _provider_setup(plugin, tmp_path, suppress_cancel=True)
        waiter = asyncio.create_task(plugin._llm_fallback_extract("demo", "demo", "合成模糊动作"))
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(plugin.terminate(), 1.5)
        plugin._stopping = False
        plugin._lifecycle_epoch += 1  # same-instance reload must also reject the old task
        release.set(); await asyncio.wait_for(waiter, 1)
        assert applied == [] and plugin._fb_cache == {}
        assert not plugin._fb_stop_guard._sessions
    asyncio.run(scenario())


def test_revoked_binding_is_checked_after_waiting(runtime, tmp_path):
    plugin = runtime[0]
    async def scenario():
        entered, release, _, applied = _provider_setup(plugin, tmp_path)
        waiter = asyncio.create_task(plugin._llm_fallback_extract("demo", "demo", "合成模糊动作"))
        await asyncio.wait_for(entered.wait(), 1)
        plugin._profiles["other"] = dict(plugin._profiles["demo"], id="other")
        plugin._bindings["demo"] = "other"
        release.set(); await waiter
        assert applied == []
    asyncio.run(scenario())


def test_disabled_body_does_not_stop_enabled_space_and_mood(runtime):
    plugin = runtime[0]
    order = []
    plugin.config["vb_enabled"] = False
    plugin._tick_space = lambda now: order.append("space")
    plugin._tick_mood = lambda now: order.append("mood")
    plugin.tick_all(T0)
    assert order == ["space", "mood"]


def test_mood_is_evaluated_after_body_advance(runtime, monkeypatch):
    plugin = runtime[0]
    state = plugin._store.get_or_create("demo", "demo", plugin._profiles["demo"])
    plugin._store.save("demo", "demo", state)
    order = []
    plugin._tick_space = lambda now: order.append("space")
    plugin._apply_signal_couplings = lambda *args: order.append("body")
    plugin._tick_mood = lambda now: order.append("mood")
    plugin.tick_all(T0)
    assert order == ["space", "body", "mood"]


def test_primary_session_respects_binding_and_does_not_pick_latest_other_user(runtime):
    plugin = runtime[0]
    assert plugin._primary_presence_session() == "demo_demo"
    plugin._profiles["other"] = dict(plugin._profiles["demo"], id="other")
    plugin._bindings["demo"] = "other"
    assert plugin._primary_presence_session() == "other_demo"
    plugin._profiles["demo"] = dict(plugin._profiles["demo"], whitelist=["demo", "second"])
    assert plugin._primary_presence_session() is None


@pytest.mark.parametrize("key,value,expected", [
    ("float", float("nan"), 3.0), ("float", float("inf"), 3.0),
    ("int", float("inf"), 3), ("int", True, 3),
    ("bool", "false", False), ("bool", "true", True),
])
def test_config_bad_values_cannot_poison_runtime(runtime, key, value, expected):
    plugin = runtime[0]
    plugin.config["test"] = value
    method = getattr(plugin, "_cfg_" + key)
    assert method("test", 3 if key != "bool" else False) == expected


def test_quota_recovers_after_io_error_without_resetting_count(tmp_path, monkeypatch):
    import astrbot_plugin_virtual_body.core.runtime as bookkeeping
    ledger = DailyFallbackBudget(str(tmp_path))
    assert ledger.reserve("2026-10-13", 2)
    original = bookkeeping.os.replace
    monkeypatch.setattr(bookkeeping.os, "replace", lambda *args: (_ for _ in ()).throw(PermissionError()))
    assert not ledger.reserve("2026-10-13", 2)
    monkeypatch.setattr(bookkeeping.os, "replace", original)
    assert ledger.refresh() and ledger.used("2026-10-13") == 1
    assert ledger.reserve("2026-10-13", 2)
    assert not ledger.reserve("2026-10-13", 2)


def test_changed_profile_definition_drops_old_fallback_result(runtime, tmp_path):
    plugin = runtime[0]
    async def scenario():
        entered, release, _, applied = _provider_setup(plugin, tmp_path)
        waiter = asyncio.create_task(plugin._llm_fallback_extract("demo", "demo", "合成模糊动作"))
        await asyncio.wait_for(entered.wait(), 1)
        plugin._profiles["demo"] = dict(plugin._profiles["demo"],
                                       parts=[dict(plugin._profiles["demo"]["parts"][0], label="新定义")])
        release.set(); await waiter
        assert applied == [] and plugin._fb_cache == {}
    asyncio.run(scenario())


def test_stop_still_invalidates_fallback_when_continuation_is_disabled(runtime, tmp_path):
    plugin = runtime[0]
    plugin.config["cs_persist_enabled"] = False
    async def scenario():
        entered, release, _, applied = _provider_setup(plugin, tmp_path)
        waiter = asyncio.create_task(plugin._llm_fallback_extract("demo", "demo", "合成模糊动作"))
        await asyncio.wait_for(entered.wait(), 1)
        await plugin.observe_private_message(_event("stop", "别摸肩"))
        release.set(); await waiter
        assert applied == []
    asyncio.run(scenario())


def test_missing_presence_is_a_valid_unknown_signal(runtime):
    plugin = runtime[0]
    plugin._manor = {}
    plugin.config.update(vb_enabled=False, sp_enabled=False)
    plugin._tick_mood(T0)
    assert plugin._tick_health["errors"] == 0
    assert plugin._last_signals["presence"] == {"available": False}


def test_role_switch_rebuilds_location_baseline_and_removes_old_presence_trigger(runtime):
    from astrbot_plugin_virtual_body import CONFIG
    from astrbot_plugin_virtual_body.core import mood_store
    plugin = runtime[0]
    plugin._profiles["other"] = dict(plugin._profiles["demo"], id="other")
    def located(key, room):
        st = plugin._manor_store.get_or_create(key, plugin._manor)
        st["she"]["room"] = room
        st["she"]["moving"] = None
        plugin._manor_store.save(key, st)
    located("demo_demo", "a"); located("other_demo", "b")
    mood_store.save_json(plugin._mood_dir(), CONFIG.MO_RULES_FILE, {"rules": [
        {"id": "location_changed", "enabled": True, "signal": "presence",
         "conditions": [{"field": "presence.she_room", "op": "changed"}],
         "cooldown_minutes": 1, "priority": 3},
    ]})
    plugin._tick_mood(T0)
    mood_store.save_triggers(plugin._mood_dir(), [
        {"rule_id": "old_presence", "signal": "presence", "expires_at": T0+300},
        {"rule_id": "weather_kept", "signal": "weather", "expires_at": T0+300},
    ])
    plugin._bindings["demo"] = "other"
    plugin._tick_mood(T0+10)
    assert [t["rule_id"] for t in plugin._last_triggers] == ["weather_kept"]
    located("other_demo", "a")
    plugin._tick_mood(T0+120)
    assert "location_changed" in [t["rule_id"] for t in plugin._last_triggers]


def test_manor_whitelist_does_not_revive_denied_old_space(runtime):
    plugin = runtime[0]
    st = plugin._manor_store.get_or_create("demo_demo", plugin._manor)
    st["she"]["room"] = "a"; st["he"]["room"] = "a"
    plugin._manor_store.save("demo_demo", st)
    plugin._manor = dict(plugin._manor, whitelist=["somebody_else"])
    plugin._tick_mood(T0)
    assert "she_room" not in plugin._last_signals["presence"]
    assert "same_room" not in plugin._last_signals["presence"]
