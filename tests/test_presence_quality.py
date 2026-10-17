"""Real contract regressions using synthetic observations and temporary stores."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from astrbot_plugin_virtual_body.core import presence_runtime
from astrbot_plugin_virtual_body.core.presence import PresenceLayer
from astrbot_plugin_virtual_body.core.presence_context import (
    CONTEXT_START, PresenceContextPolicy,
)
from astrbot_plugin_virtual_body.core.presence_runtime import PresenceIntegration
from .test_rp_regressions import runtime, MANOR


SCOPE = {'platform': 'test', 'bot_id': 'bot', 'user_id': 'peer',
         'character_id': 'demo', 'session_id': 'test:FriendMessage:peer', 'private': True}


class Event:
    unified_msg_origin = SCOPE['session_id']
    message_str = '你好'

    def get_sender_id(self): return 'peer'
    def get_platform_name(self): return 'test'
    def get_self_id(self): return 'bot'
    def is_private_chat(self): return True


class Part:
    def __init__(self, text): self.text = text
    def mark_as_temp(self): return self


class Plugin:
    def __init__(self):
        self.cfg = {}
        self.role = 'demo'
        self._stopping = False
        self._space_data_dir = ''
        self._last_signals = {}
        self._tick_health = {}

    def _cfg_bool(self, key, default): return self.cfg.get(key, default)
    def _cfg_int(self, key, default):
        try:
            value = self.cfg.get(key, default)
            return default if isinstance(value, bool) else int(value)
        except (ValueError, TypeError, OverflowError):
            return default
    def _resolve_session(self, user): return (self.role, user) if user == 'peer' and self.role else None
    def _space_enabled(self): return False
    def _tz(self): return 8


def request():
    return SimpleNamespace(extra_user_content_parts=[], prompt='原问题', system_prompt='原人格')


def setup_host(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(presence_runtime, 'time', SimpleNamespace(time=lambda: clock[0]))
    plugin = Plugin()
    integration = PresenceIntegration(plugin, SimpleNamespace())
    integration.api._clock = lambda: clock[0]
    current = {'playing': True, 'title': '合成曲目 A'}

    async def collect(scope, now):
        return [{'domain': 'music', 'value': dict(current), 'observed_at': now,
                 'ttl_seconds': 10, 'importance': 'important'}]

    def render(domain, record, scope, query, maximum, now):
        value = record['value']
        return ('音乐已暂停；不沿用旧曲。' if value.get('playing') is False
                else '当前音乐：' + value['title'] + '；仅作背景。')

    integration.api.register('test.music', domains=('music',), collector=collect, renderer=render)
    return plugin, integration, clock, current


def test_request_retry_keeps_one_current_fragment_but_new_request_stays_deduped(monkeypatch):
    async def scenario():
        _, host, _, _ = setup_host(monkeypatch)
        req = request()
        for _ in range(3):
            await host.inject(Event(), req, Part)
            assert len(req.extra_user_content_parts) == 1
            assert req.extra_user_content_parts[0].text.count(CONTEXT_START) == 1
            assert '合成曲目 A' in req.extra_user_content_parts[0].text
        next_req = request()
        await host.inject(Event(), next_req, Part)
        assert next_req.extra_user_content_parts == []
        assert req.prompt == '原问题' and req.system_prompt == '原人格'
        host.close()
    asyncio.run(scenario())


def test_retry_replaces_changed_and_paused_song_without_old_title(monkeypatch):
    async def scenario():
        _, host, clock, current = setup_host(monkeypatch)
        req = request()
        await host.inject(Event(), req, Part)
        clock[0] += 1
        current['title'] = '合成曲目 B'
        await host.inject(Event(), req, Part)
        assert len(req.extra_user_content_parts) == 1
        assert '合成曲目 B' in req.extra_user_content_parts[0].text
        assert '合成曲目 A' not in req.extra_user_content_parts[0].text
        clock[0] += 1
        current['playing'] = False
        await host.inject(Event(), req, Part)
        assert '已暂停' in req.extra_user_content_parts[0].text
        assert '合成曲目' not in req.extra_user_content_parts[0].text
        host.close()
    asyncio.run(scenario())


def test_retry_refreshes_expired_context_instead_of_reusing_old_text(monkeypatch):
    async def scenario():
        _, host, clock, _ = setup_host(monkeypatch)
        req = request()
        await host.inject(Event(), req, Part)
        clock[0] += 11
        # A sensor returning no new observations cannot extend its old TTL.
        descriptor = host.api._sources['test.music']
        descriptor.collector = None
        await host.inject(Event(), req, Part)
        assert len(req.extra_user_content_parts) == 1
        assert '无法确认' in req.extra_user_content_parts[0].text
        assert '合成曲目 A' not in req.extra_user_content_parts[0].text
        await host.inject(Event(), req, Part)
        assert len(req.extra_user_content_parts) == 1
        assert '无法确认' in req.extra_user_content_parts[0].text
        host.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('action', ['permission', 'layer', 'context', 'close'])
def test_retry_removes_context_when_permission_or_service_goes_away(monkeypatch, action):
    async def scenario():
        plugin, host, _, _ = setup_host(monkeypatch)
        req = request()
        await host.inject(Event(), req, Part)
        if action == 'permission': plugin.role = None
        elif action == 'layer': plugin.cfg['presence_layer_enabled'] = False
        elif action == 'context': plugin.cfg['presence_context_enabled'] = False
        else: host.close()
        await host.inject(Event(), req, Part)
        assert req.extra_user_content_parts == []
        assert req.prompt == '原问题' and req.system_prompt == '原人格'
        host.close()
    asyncio.run(scenario())


def test_failed_temporary_part_does_not_consume_injection(monkeypatch):
    class BrokenPart:
        def __init__(self, text): raise RuntimeError('synthetic part failure')

    async def scenario():
        _, host, _, _ = setup_host(monkeypatch)
        with pytest.raises(RuntimeError, match='synthetic part failure'):
            await host.inject(Event(), request(), BrokenPart)
        assert host.policy.metrics['injections'] == 0
        req = request()
        await host.inject(Event(), req, Part)
        assert len(req.extra_user_content_parts) == 1
        assert host.policy.metrics['injections'] == 1
        host.close()
    asyncio.run(scenario())


def test_concurrent_retry_of_same_request_keeps_exactly_one_fragment(monkeypatch):
    async def scenario():
        _, host, _, _ = setup_host(monkeypatch)
        req = request()
        await asyncio.gather(*(host.inject(Event(), req, Part) for _ in range(8)))
        assert len(req.extra_user_content_parts) == 1
        assert '合成曲目 A' in req.extra_user_content_parts[0].text
        host.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('key', ['presence_layer_enabled', 'presence_context_enabled'])
def test_reenable_does_not_keep_old_suppression(monkeypatch, key):
    async def scenario():
        plugin, host, _, _ = setup_host(monkeypatch)
        await host.inject(Event(), request(), Part)
        plugin.cfg[key] = False
        await host.inject(Event(), request(), Part)
        plugin.cfg[key] = True
        req = request()
        await host.inject(Event(), req, Part)
        assert len(req.extra_user_content_parts) == 1
        host.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('value', [0, -1, 1201, float('inf'), 'bad', True])
def test_invalid_context_budget_uses_safe_effective_default(monkeypatch, value):
    async def scenario():
        plugin, host, _, _ = setup_host(monkeypatch)
        plugin.cfg['presence_context_max_chars'] = value
        req = request()
        await host.inject(Event(), req, Part)
        assert 0 < len(req.extra_user_content_parts[0].text) <= 420
        assert host.diagnostics()['context']['max_chars'] == 420
        host.close()
    asyncio.run(scenario())


def test_too_small_external_budget_does_not_consume_projection(monkeypatch):
    async def scenario():
        _, host, _, _ = setup_host(monkeypatch)
        await host.api.refresh(SCOPE)
        assert host.api.render_context(SCOPE, max_chars=10) == ''
        assert host.policy.metrics['injections'] == 0
        assert host.api.render_context(SCOPE, max_chars=420)
        host.close()
    asyncio.run(scenario())


def test_projection_retry_cache_is_bounded_and_has_no_context_values():
    policy = PresenceContextPolicy(max_scopes=2)
    for n in range(3):
        policy.committed(SCOPE, [('music', str(n))], now=1000, chars=20,
                         projection='synthetic private observation ' + str(n))
    assert len(policy._projections) == 2
    assert all(len(key) == 64 for key in policy._projections)
    assert 'synthetic' not in repr(policy._projections)
    assert not policy.retry_domains(SCOPE, ['synthetic private observation 0'])
    assert policy.retry_domains(SCOPE, ['synthetic private observation 2']) == frozenset({'music'})
    assert not policy.retry_domains({**SCOPE, 'character_id': 'other'}, ['synthetic private observation 2'])
    policy.clear()
    assert not policy._projections


@pytest.mark.parametrize('budget', [0, 10, 80, 140, 160, 420])
def test_policy_never_exceeds_requested_budget(budget):
    api = PresenceLayer(clock=lambda: 1000)
    api.register('music', domains=('music',), renderer=lambda *args: '当前正在播放合成音乐。')
    assert api.update('music', SCOPE, 'music', {'playing': True}, importance='important')
    block, _ = PresenceContextPolicy().build(api, SCOPE, api.snapshot(SCOPE), now=1000, max_chars=budget)
    assert len(block) <= budget


def test_slow_weather_does_not_block_event_loop_and_preserves_observed_time(monkeypatch):
    async def scenario():
        _, host, _, _ = setup_host(monkeypatch)
        host.plugin._space_data_dir = 'synthetic'
        release = threading.Event()
        def slow(*args, **kwargs):
            release.wait(timeout=2)
            return {'available': True, 'city': '测试城', 'condition': '雨', 'observed_at': 950}
        monkeypatch.setattr(presence_runtime.mood_signals, 'read_weather', slow)
        pending = asyncio.create_task(host._collect_environment(SCOPE, 1000))
        try:
            await asyncio.sleep(.01)
            assert not pending.done(), 'the loop must run while the weather reader waits'
        finally:
            release.set()
            rows = await pending
            host.close()
        assert next(row for row in rows if row['domain'] == 'weather')['observed_at'] == 950
    asyncio.run(scenario())


def prepare_lifecycle(runtime, monkeypatch):
    plugin, _, main = runtime
    plugin._load_profiles = lambda: None
    plugin._migrate_manor_override = lambda: None
    plugin._register_web_api_if_available = lambda: None
    monkeypatch.setattr(main, 'load_manor', lambda *args: (MANOR, 'synthetic'))
    return plugin


def test_duplicate_and_concurrent_initialize_create_one_tick(runtime, monkeypatch):
    plugin = prepare_lifecycle(runtime, monkeypatch)
    async def scenario():
        await asyncio.gather(*(plugin.initialize() for _ in range(5)))
        first = plugin._tick_task
        epoch = plugin._lifecycle_epoch
        await plugin.initialize()
        assert plugin._tick_task is first
        assert plugin._lifecycle_epoch == epoch
        assert not first.done()
        await plugin.terminate()
        assert first.done() and plugin._tick_task is None
        assert not plugin._fb_tasks and not plugin._fb_inflight
    asyncio.run(scenario())


def test_same_instance_reload_creates_new_api_and_cleans_old_task(runtime, monkeypatch):
    plugin = prepare_lifecycle(runtime, monkeypatch)
    async def scenario():
        await plugin.initialize()
        old_task, old_api = plugin._tick_task, plugin.context.presence_layer
        await plugin.terminate()
        await plugin.terminate()
        assert old_task.done() and not old_api.active
        assert not hasattr(plugin.context, 'presence_layer')
        await plugin.initialize()
        assert plugin._tick_task is not old_task
        assert plugin.context.presence_layer is not old_api
        assert plugin.context.presence_layer.active
        await plugin.terminate()
    asyncio.run(scenario())


def test_failed_initialize_keeps_exception_and_can_retry(runtime, monkeypatch):
    plugin = prepare_lifecycle(runtime, monkeypatch)
    plugin._load_profiles = lambda: (_ for _ in ()).throw(ValueError('synthetic initialization failure'))
    async def scenario():
        with pytest.raises(ValueError, match='synthetic initialization failure'):
            await plugin.initialize()
        assert plugin._stopping and not plugin._initialized
        assert plugin._tick_task is None
        assert not hasattr(plugin.context, 'presence_layer')
        plugin._load_profiles = lambda: None
        await plugin.initialize()
        assert plugin._initialized and not plugin._stopping
        assert plugin.context.presence_layer.active
        await plugin.terminate()
    asyncio.run(scenario())


def test_concurrent_sources_keep_latest_state_and_cleanup():
    async def scenario():
        api = PresenceLayer(clock=lambda: 1000)
        release = asyncio.Event()
        async def music(scope, now):
            await release.wait()
            return [{'domain': 'music', 'value': {'playing': True}, 'observed_at': 1000}]
        async def weather(scope, now):
            await release.wait()
            return [{'domain': 'weather', 'value': {'condition': 'rain'}, 'observed_at': 1000}]
        api.register('one', domains=('music',), collector=music)
        api.register('two', domains=('weather',), collector=weather)
        pending = asyncio.create_task(api.refresh(SCOPE))
        await asyncio.sleep(0)
        release.set()
        snapshot = await pending
        assert snapshot['states']['music']['status'] == snapshot['states']['weather']['status'] == 'fresh'
        api.close()
        await asyncio.sleep(0)
        assert not api._inflight and not api._sources
        assert api.diagnostics()['cached_records'] == 0
    asyncio.run(scenario())
