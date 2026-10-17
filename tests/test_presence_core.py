"""The optional shared presence contract, independent of AstrBot and I/O."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from astrbot_plugin_virtual_body.core.presence import PresenceLayer, normalize_scope, scope_for_event


BASE = {"platform": "test", "bot_id": "bot", "user_id": "peer", "character_id": "role_a",
        "session_id": "test:FriendMessage:peer", "private": True}


def layer(**kwargs):
    result = PresenceLayer(clock=lambda: 1000.0, **kwargs)
    assert result.register("phone", domains={"music", "device"})
    return result


def music(result, value=None, **kwargs):
    return result.update("phone", BASE, "music", value if value is not None else {"playing": True, "title": "A"}, **kwargs)


def record(result, domain="music", **kwargs):
    return result.read(BASE, domain, **kwargs)["states"][domain]


def test_json_snapshot_and_detached_input_output():
    result = layer()
    value = {"playing": True, "title": "A", "tags": ["calm"]}
    assert music(result, value, observed_at=950, received_at=1000)
    value["tags"].append("mutated")
    first = result.snapshot(BASE)
    json.dumps(first, allow_nan=False)
    assert first["schema_version"] == 1
    assert first["states"]["music"]["meta"]["observed_at"] == "1970-01-01T00:15:50Z"
    assert first["states"]["music"]["value"]["tags"] == ["calm"]
    first["states"]["music"]["value"]["tags"].append("again")
    assert record(result)["value"]["tags"] == ["calm"]


@pytest.mark.parametrize("field,value", [("platform", "other"), ("bot_id", "other"), ("user_id", "other"),
                                        ("character_id", "role_b"), ("session_id", "group"), ("private", False)])
def test_every_identity_dimension_is_isolated(field, value):
    result = layer()
    assert music(result)
    scope = {**BASE, field: value}
    assert result.read(scope, "music")["states"]["music"]["status"] == "unknown"


def test_unknown_and_stale_do_not_turn_into_false_or_refresh_their_age():
    result = layer()
    assert record(result)["value"] is None
    assert music(result, {"playing": None}, observed_at=800, ttl_seconds=180)
    first = record(result, now=1000)
    second = record(result, now=1010)
    assert first["status"] == second["status"] == "stale"
    assert second["value"]["playing"] is None
    assert first["meta"]["observed_at"] == second["meta"]["observed_at"]
    assert first["meta"]["expires_at"] == second["meta"]["expires_at"]


def test_pause_watermark_rejects_older_and_equal_playback():
    result = layer()
    assert music(result, observed_at=900)
    assert music(result, {"playing": False, "title": "A"}, observed_at=950)
    assert not music(result, observed_at=940, received_at=1000)
    assert not music(result, observed_at=950)
    assert record(result)["value"]["playing"] is False
    assert music(result, {"playing": True, "title": "B"}, observed_at=960)
    assert record(result)["value"]["title"] == "B"


def test_monotonic_source_revision_can_order_same_timestamp_events():
    result = layer()
    assert music(result, observed_at=950, revision=1)
    assert music(result, {"playing": False}, observed_at=950, revision=2)
    assert not music(result, observed_at=960, revision=1)
    assert record(result)["value"]["playing"] is False


@pytest.mark.parametrize("paused", [{"playing": False}, {"event_type": "music_paused", "user_state": {"playing": False}},
                                  {"event_type": "music_stopped", "user_state": {"playing": False}}])
def test_same_timestamp_pause_wins_without_source_revision_and_blocks_late_play(paused):
    result = layer()
    assert music(result, observed_at=950)
    assert music(result, paused, observed_at=950)
    assert not music(result, observed_at=950, received_at=1000)
    assert record(result)["value"] == paused


@pytest.mark.parametrize("status", ["unknown", "unavailable", "stale"])
def test_availability_without_observation_does_not_fake_a_watermark(status):
    result = layer()
    assert music(result, observed_at=900)
    assert music(result, {"playing": None}, status=status, received_at=1000)
    control = record(result)
    assert control["status"] == status
    assert control["meta"]["observed_at"] is None
    assert control["meta"]["expires_at"] is None
    assert music(result, {"playing": True, "title": "B"}, observed_at=950)
    assert record(result)["value"]["title"] == "B"
    assert not music(result, observed_at=899)


@pytest.mark.parametrize("status", ["unknown", "unavailable", "stale"])
def test_same_observation_can_change_availability_then_recover(status):
    result = layer()
    value = {"playing": True, "title": "A"}
    assert music(result, value, observed_at=950)
    assert music(result, None if status == "unknown" else value, observed_at=950, status=status)
    assert record(result)["status"] == status
    assert music(result, value, observed_at=950)
    assert record(result)["status"] == "fresh"


def test_unknown_control_preserves_same_timestamp_pause_watermark():
    result = layer()
    assert music(result, observed_at=950)
    assert music(result, {"playing": False}, observed_at=950)
    assert music(result, {"playing": None}, status="unknown")
    assert not music(result, observed_at=950)
    assert music(result, {"playing": False}, observed_at=950)


def test_reported_evidence_and_boolean_ttl_boundary():
    result = layer()
    assert not music(result, ttl_seconds=True)
    assert music(result, evidence_kind="reported")
    assert record(result)["meta"]["evidence_kind"] == "reported"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), {"x": object()}, {1: "bad"}, ("tuple",), "a" * 40000],
                         ids=["nan", "infinity", "object", "key", "tuple", "oversized"])
def test_invalid_or_oversized_json_is_rejected(bad):
    result = layer()
    assert not music(result, bad)
    assert record(result)["status"] == "unknown"


def test_depth_limit_is_enforced_without_recursing_into_a_cycle():
    result = layer()
    deep = None
    for _ in range(15):
        deep = [deep]
    assert not music(result, deep)
    cycle = []
    cycle.append(cycle)
    assert not music(result, cycle)


@pytest.mark.parametrize("kwargs", [{"observed_at": 1040}, {"received_at": 1040}, {"observed_at": "nope"},
                                   {"observed_at": "2026-01-01T00:00:00"}, {"ttl_seconds": float("nan")},
                                   {"ttl_seconds": -1}, {"status": "fake"}, {"evidence_kind": "mind_reading"},
                                   {"revision": {"bad": True}}])
def test_timestamp_and_metadata_guards(kwargs):
    result = layer()
    assert not music(result, **kwargs)


def test_iso_and_numeric_timestamp_compatibility():
    result = layer()
    assert music(result, observed_at="1970-01-01T08:15:00+08:00", received_at="1000")
    assert record(result)["meta"]["observed_at"] == "1970-01-01T00:15:00Z"


def test_change_key_ignores_progress_but_keeps_latest_observation_and_value():
    result = layer()
    key = {"playing": True, "title": "A"}
    assert music(result, {**key, "progress": 1}, observed_at=950, received_at=950, change_key=key)
    first = result.snapshot(BASE)
    assert music(result, {**key, "progress": 2}, observed_at=960, received_at=1000, change_key=key)
    second = result.snapshot(BASE)
    assert first["revision"] == second["revision"]
    assert second["states"]["music"]["value"]["progress"] == 2
    assert second["states"]["music"]["meta"]["observed_at"] != first["states"]["music"]["meta"]["observed_at"]
    assert music(result, {"playing": True, "title": "B"}, observed_at=970, change_key={"title": "B"})
    assert result.snapshot(BASE)["revision"] > second["revision"]


def test_received_transport_time_alone_does_not_change_revision():
    result = layer()
    assert music(result, observed_at=950, received_at=950)
    first = result.snapshot(BASE)
    assert music(result, observed_at=950, received_at=1000)
    second = result.snapshot(BASE)
    assert first["revision"] == second["revision"]
    assert second["states"]["music"]["meta"]["received_at"] == "1970-01-01T00:16:40Z"


def test_expiry_changes_view_revision_once_and_filtered_reads_do_not_oscillate():
    result = layer()
    assert music(result, observed_at=950, ttl_seconds=80)
    first = result.snapshot(BASE, now=1000)
    second = result.snapshot(BASE, now=1040)
    assert second["revision"] > first["revision"]
    result.read(BASE, "device", now=1040)
    result.read(BASE, "music", now=1040)
    assert result.snapshot(BASE, now=1040)["revision"] == second["revision"]


def test_registry_domains_owner_replacement_and_unregister_guard():
    result = layer()
    assert not result.update("phone", BASE, "weather", {"rain": True})
    old, new = object(), object()
    assert result.register("new", owner=old, domains={"weather"})
    assert result.update("new", BASE, "weather", {"rain": True})
    assert result.register("new", owner=new, domains={"weather"})
    assert not result.unregister("new", owner=old)
    assert result.read(BASE, "weather")["states"]["weather"]["value"] is None
    assert result.unregister("new", owner=new)
    assert result.read(BASE, "weather")["states"]["weather"]["status"] == "unknown"


def test_enabled_false_invalidates_old_data_and_context_can_disable_independently():
    flags = {"enabled": True, "context": True, "phone": True}
    result = layer(enabled=lambda: flags["enabled"], context_enabled=lambda: flags["context"])
    result.register("phone", domains={"music"}, enabled=lambda: flags["phone"])
    assert music(result)
    flags["context"] = False
    assert result.active and not result.context_enabled
    assert record(result)["value"]["title"] == "A"
    flags["phone"] = False
    assert record(result)["status"] == "disabled"
    flags["phone"] = True
    assert record(result)["status"] == "unknown"
    assert music(result)
    flags["enabled"] = False
    assert not result.active and not music(result)
    flags["enabled"] = True
    assert record(result)["status"] == "unknown"


def test_capacity_limits_and_diagnostics_do_not_leak_scope_or_values():
    result = layer(max_sources=2, max_scopes=2, max_records=2)
    assert result.register("weather", domains={"weather"})
    assert not result.register("third", domains={"future"})
    for user in ("secret-peer-1", "secret-peer-2", "secret-peer-3"):
        assert result.update("phone", {**BASE, "user_id": user}, "music", {"title": "secret-song"})
    info = result.diagnostics()
    assert info["cached_scopes"] == info["cached_records"] == 2
    assert "secret" not in json.dumps(info)
    assert result.read({**BASE, "user_id": "secret-peer-1"}, "music")["states"]["music"]["status"] == "unknown"


def test_conflicting_equal_priority_sources_and_explicit_priority_selection():
    result = layer()
    assert result.register("other", domains={"music"})
    assert music(result, {"playing": True, "title": "A"})
    assert result.update("other", BASE, "music", {"playing": True, "title": "B"})
    assert record(result)["status"] == "conflict" and record(result)["value"] is None
    assert result.register("other", domains={"music"}, priority=1)
    assert result.update("other", BASE, "music", {"playing": True, "title": "B"})
    assert record(result)["status"] == "fresh" and record(result)["value"]["title"] == "B"


def test_renderer_receives_detached_record_and_failures_are_optional():
    result = layer()
    seen = []
    def renderer(domain, current, scope, query, maximum, now):
        seen.append((domain, scope, query, maximum, now))
        current["value"]["title"] = "changed"
        return "A" * 100
    result.register("phone", domains={"music"}, renderer=renderer)
    assert music(result)
    assert result.source_for(BASE, "music") == "phone"
    assert result.render(BASE, "music", query="now?", max_chars=12) == ""
    assert result.render(BASE, "music", query="now?", max_chars=100) == "A" * 100
    assert seen[0][0] == "music" and seen[0][2] == "now?"
    assert record(result)["value"]["title"] == "A"
    result.register("phone", domains={"music"}, renderer=lambda *args: 1 / 0)
    assert music(result)
    assert result.render(BASE, "music") == ""


def test_public_context_projection_is_optional_scoped_and_whole_budgeted():
    flags = {"context": True}
    calls = []
    def project(scope, **options):
        calls.append((scope, options))
        scope["user_id"] = "mutated"
        return "complete context"
    result = layer(context_enabled=lambda: flags["context"], context_renderer=project)
    assert result.render_context(BASE, query="music", channel="pc_proactive") == "complete context"
    assert calls[0][1]["channel"] == "pc_proactive" and calls[0][1]["now"] == 1000
    assert BASE["user_id"] == "peer"
    assert result.render_context(BASE, max_chars=5) == ""
    flags["context"] = False
    before = len(calls)
    assert result.render_context(BASE) == "" and len(calls) == before
    flags["context"] = True
    result.context_renderer = lambda *args, **kwargs: 1 / 0
    assert result.render_context(BASE) == ""


def test_event_scope_keeps_real_platform_bot_private_and_role_resolution():
    event = SimpleNamespace(get_platform_name=lambda: "aiocqhttp", get_self_id=lambda: "bot",
                            get_sender_id=lambda: "peer", unified_msg_origin="instance:FriendMessage:peer",
                            get_group_id=lambda: "", get_message_type=lambda: "FriendMessage")
    result = PresenceLayer(role_resolver=lambda peer: "role_a" if peer == "peer" else None)
    derived = result.scope_for_event(event)
    assert derived["platform"] == "aiocqhttp" and derived["bot_id"] == "bot"
    assert derived["private"] and derived["character_id"] == "role_a"
    group = SimpleNamespace(get_platform_name=lambda: "aiocqhttp", get_self_id=lambda: "bot",
                            get_sender_id=lambda: "peer", get_group_id=lambda: "group", get_message_type=lambda: "GroupMessage")
    assert not scope_for_event(group)["private"]
    assert not scope_for_event(SimpleNamespace())["private"]
    assert normalize_scope({"user_id": 1})["user_id"] == "1"


def test_session_lease_preserves_actual_identity_and_rechecks_current_role():
    selected = {"role": "role_a"}
    result = layer(role_resolver=lambda user: selected["role"] if user == "peer" else None)
    assert result.scope_for_session(BASE["session_id"]) is None
    assert result.remember_scope(BASE)
    assert music(result)
    actual = result.scope_for_session(BASE["session_id"])
    assert actual == BASE
    actual["bot_id"] = "forged"
    assert result.scope_for_session(BASE["session_id"])["bot_id"] == "bot"
    selected["role"] = "role_b"
    changed = result.scope_for_session(BASE["session_id"])
    assert changed["character_id"] == "role_b" and changed["bot_id"] == "bot"
    assert result.read(changed, "music")["states"]["music"]["status"] == "unknown"
    selected["role"] = None
    assert result.scope_for_session(BASE["session_id"]) is None


@pytest.mark.parametrize("field,value", [("private", False), ("platform", None), ("bot_id", None),
                                        ("user_id", None), ("session_id", None)])
def test_session_lease_rejects_incomplete_or_nonprivate_identity(field, value):
    result = layer(role_resolver=lambda user: "role_a")
    assert not result.remember_scope({**BASE, field: value})
    assert result.diagnostics()["known_private_sessions"] == 0


def test_session_leases_are_bounded_and_clear_or_close_removes_them():
    result = layer(max_scopes=2, role_resolver=lambda user: "role_a")
    for session in ("one", "two", "three"):
        assert result.remember_scope({**BASE, "session_id": session})
    assert result.scope_for_session("one") is None
    assert result.diagnostics()["known_private_sessions"] == 2
    result.clear(source="phone")
    assert result.scope_for_session("three") is None
    assert result.remember_scope(BASE)
    result.close()
    assert result.scope_for_session(BASE["session_id"]) is None


def test_async_collection_updates_snapshot_and_one_failure_does_not_break_other_sources():
    async def run():
        result = layer()
        async def good(scope, now):
            return [{"domain": "music", "value": {"playing": True, "title": "A"}, "observed_at": 950}]
        async def bad(scope, now):
            raise RuntimeError("sensor offline")
        result.register("phone", domains={"music"}, collector=good)
        result.register("weather", domains={"weather"}, collector=bad)
        snapshot = await result.refresh(BASE)
        assert snapshot["states"]["music"]["value"]["title"] == "A"
        assert snapshot["states"]["weather"]["status"] == "unknown"
        assert result.diagnostics()["metrics"]["collector_failures"] == 1
    asyncio.run(run())


def test_collector_selected_snapshot_replaces_old_device_when_provider_becomes_unknown():
    async def run():
        result = layer()
        selected = [{"domain": "music", "device_id": "first", "value": {"playing": True, "title": "A"}, "observed_at": 900}]
        async def collect(scope, now):
            return selected
        result.register("phone", domains={"music"}, collector=collect)
        assert (await result.refresh(BASE))["states"]["music"]["value"]["title"] == "A"
        selected[:] = [{"domain": "music", "value": {"playing": None}, "status": "unknown"}]
        unknown = (await result.refresh(BASE))["states"]["music"]
        assert unknown["status"] == "unknown" and unknown["value"]["playing"] is None
        assert result.diagnostics()["cached_records"] == 1
        selected[:] = [{"domain": "music", "device_id": "first", "value": {"playing": True, "title": "B"}, "observed_at": 950}]
        assert (await result.refresh(BASE))["states"]["music"]["value"]["title"] == "B"
        assert result.diagnostics()["cached_records"] == 1
    asyncio.run(run())


def test_selected_device_switch_does_not_conflict_and_rejected_old_device_cannot_erase_current():
    async def run():
        result = layer()
        selected = [{"domain": "music", "device_id": "first", "value": {"playing": True, "title": "A"}, "observed_at": 900}]
        async def collect(scope, now):
            return selected
        result.register("phone", domains={"music"}, collector=collect)
        await result.refresh(BASE)
        selected[:] = [{"domain": "music", "device_id": "second", "value": {"playing": True, "title": "B"}, "observed_at": 950}]
        current = (await result.refresh(BASE))["states"]["music"]
        assert current["status"] == "fresh" and current["value"]["title"] == "B"
        selected[:] = [{"domain": "music", "device_id": "first", "value": {"playing": True, "title": "old"}, "observed_at": 850}]
        preserved = (await result.refresh(BASE))["states"]["music"]
        assert preserved["status"] == "fresh" and preserved["value"]["title"] == "B"
        assert result.diagnostics()["cached_records"] == 1
        assert result.diagnostics()["cached_watermarks"] == 2
    asyncio.run(run())


def test_complete_collector_batch_preserves_multiple_devices_for_conflict_detection():
    async def run():
        result = layer()
        async def collect(scope, now):
            return [{"domain": "music", "device_id": "one", "value": {"playing": True, "title": "A"}, "observed_at": 950},
                    {"domain": "music", "device_id": "two", "value": {"playing": True, "title": "B"}, "observed_at": 950}]
        result.register("phone", domains={"music"}, collector=collect)
        current = (await result.refresh(BASE))["states"]["music"]
        assert current["status"] == "conflict" and current["value"] is None
        assert result.diagnostics()["cached_records"] == 2
    asyncio.run(run())


def test_hidden_device_pause_watermark_survives_unknown_selected_snapshot():
    async def run():
        result = layer()
        selected = [{"domain": "music", "device_id": "first", "value": {"playing": False}, "observed_at": 950}]
        async def collect(scope, now):
            return selected
        result.register("phone", domains={"music"}, collector=collect)
        await result.refresh(BASE)
        selected[:] = [{"domain": "music", "value": {"playing": None}, "status": "unknown"}]
        await result.refresh(BASE)
        selected[:] = [{"domain": "music", "device_id": "first", "value": {"playing": True, "title": "late"}, "observed_at": 950}]
        current = (await result.refresh(BASE))["states"]["music"]
        assert current["status"] == "unknown" and current["value"]["playing"] is None
    asyncio.run(run())


def test_shared_refresh_and_cancelled_caller_do_not_duplicate_sensor_work():
    async def run():
        calls = []
        started, release = asyncio.Event(), asyncio.Event()
        result = layer()
        async def collect(scope, now):
            calls.append(scope)
            started.set()
            await release.wait()
            return [{"domain": "music", "value": {"playing": False}, "observed_at": 950}]
        result.register("phone", domains={"music"}, collector=collect)
        first = asyncio.create_task(result.refresh(BASE))
        await started.wait()
        second = asyncio.create_task(result.refresh(BASE))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert (await second)["states"]["music"]["value"]["playing"] is False
        assert len(calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("action", ["unregister", "replace", "clear", "close", "disable"])
def test_late_collector_is_rejected_after_lifecycle_changes(action):
    async def run():
        flags = {"enabled": True}
        result = layer()
        started, release = asyncio.Event(), asyncio.Event()
        owner = object()
        async def collect(scope, now):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return [{"domain": "music", "value": {"playing": True, "title": "late"}, "observed_at": 950}]
        result.register("phone", owner=owner, domains={"music"}, collector=collect, enabled=lambda: flags["enabled"])
        waiting = asyncio.create_task(result.refresh(BASE))
        await started.wait()
        if action == "unregister":
            result.unregister("phone", owner=owner)
        elif action == "replace":
            result.register("phone", owner=object(), domains={"music"})
        elif action == "clear":
            result.clear()
        elif action == "close":
            result.close()
        else:
            flags["enabled"] = False
            assert record(result)["status"] == "disabled"
            flags["enabled"] = True
        release.set()
        try:
            await waiting
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)
        assert record(result)["value"] is None
    asyncio.run(run())


def test_timed_out_collector_cannot_write_even_when_it_suppresses_cancellation():
    async def run():
        result = layer()
        release = asyncio.Event()
        async def collect(scope, now):
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return [{"domain": "music", "value": {"playing": True}, "observed_at": 950}]
        result.register("phone", domains={"music"}, collector=collect)
        snapshot = await result.refresh(BASE, timeout_seconds=0.01)
        assert snapshot["states"]["music"]["status"] == "unknown"
        release.set()
        await asyncio.sleep(0.01)
        assert record(result)["status"] == "unknown"
        assert result.diagnostics()["metrics"]["collector_timeouts"] == 1
    asyncio.run(run())


def test_sync_collector_can_return_async_result_and_malformed_items_are_skipped():
    async def run():
        result = layer()
        async def contents():
            return [None, {"value": True}, {"domain": "music", "value": {"playing": None}, "status": "unknown"},
                    {"domain": "not_allowed", "value": True}]
        result.register("phone", domains={"music"}, collector=lambda scope, now: contents())
        snapshot = await result.refresh(BASE)
        assert snapshot["states"]["music"]["value"]["playing"] is None
        assert snapshot["states"]["music"]["status"] == "unknown"
        result.close()
        assert not result.active and not result.context_enabled
        assert not result.register("after", domains={"time"})
        assert not music(result)
    asyncio.run(run())
