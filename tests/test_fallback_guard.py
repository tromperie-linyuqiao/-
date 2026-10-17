import ast
import asyncio
import json
import re
import time
import types
from pathlib import Path
from typing import Any

import pytest
from astrbot_plugin_virtual_body import CONFIG
from astrbot_plugin_virtual_body.core import decay, events as events_mod, persist_contact, state
from astrbot_plugin_virtual_body.core.fallback_guard import FallbackStopGuard


def _harness():
    source = Path(__file__).resolve().parents[1] / 'main.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    selected = []
    names = {'observe_private_message', '_llm_fallback_extract', '_advance_session',
             '_fallback_raw_for_text', '_request_fallback_raw', '_fallback_cache_key'}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            node.decorator_list = []
            selected.append(node)
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                              ast.ClassDef(name='Harness', bases=[], keywords=[], body=selected, decorator_list=[])], type_ignores=[])
    env = dict(asyncio=asyncio, time=time, re=re, json=json, Any=Any, CONFIG=CONFIG, events_mod=events_mod,
               persist_contact=persist_contact, decay=decay, PLUGIN_NAME='fixture',
               logger=types.SimpleNamespace(error=lambda *_: None, debug=lambda *_: None))
    exec(compile(ast.fix_missing_locations(module), str(source), 'exec'), env)
    bot = env['Harness']()
    profile = {'id': 'fixture', 'parts': [
        {'id': 'ear', 'label': '耳朵', 'aliases': ['耳朵'], 'sens': 5, 'half_life': 90},
        {'id': 'tail', 'label': '尾巴', 'aliases': ['尾巴'], 'sens': 5, 'half_life': 90}],
        'tiers': [], 'action_words': {'摸': 0.2}}
    st = state.normalize(state.new_state('fixture', 'session'), profile)
    persist_contact.record_contacts(st, [{'part': 'ear', 's': 0.2, 'type': 'touch'}])
    bot._store = types.SimpleNamespace(get_or_create=lambda *_: st, save=lambda *_: None)
    bot._profiles = {'fixture': profile}
    bot._tick_idle = {}
    bot._fb_tasks = set()
    bot._fb_stop_guard = FallbackStopGuard()
    bot._cfg_bool = lambda key, default=False: {'vb_enabled': True, 'vb_llm_fallback_enabled': False}.get(key, default)
    bot._space_enabled = lambda: False
    bot._persist_on = lambda: True
    bot._persist_cfg = lambda: {}
    bot._phase_cfg = lambda: {}
    bot._intimacy_cfg = lambda: {}
    bot._apply_signal_couplings = lambda *_: None
    bot._resolve_session = lambda *_: ('fixture', 'session')
    bot._fb_cache_get = lambda *_: None
    bot._fb_budget_ok = lambda: True
    bot._fb_consume = lambda: None
    bot._fb_cache_put = lambda *_: None
    return bot, st


@pytest.mark.parametrize('later_text,expected', [('别摸耳朵', []), ('别摸尾巴', ['ear']), ('谢谢你', ['ear'])])
def test_late_fallback_respects_later_stop_only_for_same_part(later_text, expected):
    async def scenario():
        bot, st = _harness()
        started, release = asyncio.Event(), asyncio.Event()
        async def chat(**kwargs):
            started.set()
            await release.wait()
            return types.SimpleNamespace(completion_text=json.dumps({'part': '耳朵', 'intensity': 0.2, 'type': 'touch'}))
        bot.context = types.SimpleNamespace(get_using_provider=lambda *_: types.SimpleNamespace(text_chat=chat))
        pending = asyncio.create_task(bot._llm_fallback_extract('fixture', 'session', 'earlier ambiguous action'))
        await started.wait()
        event = types.SimpleNamespace(message_str=later_text, get_sender_id=lambda: 'user', get_self_id=lambda: 'bot')
        await bot.observe_private_message(event)
        release.set()
        await pending
        assert sorted(st['active_contact']) == expected
        assert not bot._fb_stop_guard._sessions
    asyncio.run(scenario())


def test_guard_new_action_after_stop_survives_and_other_session_isolated():
    guard = FallbackStopGuard()
    old = guard.begin('a', 'one')
    other = guard.begin('a', 'two')
    guard.stop('a', 'one', ['ear'])
    fresh = guard.begin('a', 'one')
    assert guard.invalidated(old, 'ear')
    assert not guard.invalidated(fresh, 'ear')
    assert not guard.invalidated(other, 'ear')
    for token in (old, other, fresh):
        guard.finish(token)
        guard.finish(token)
    assert not guard._sessions


def test_guard_is_bounded_and_reservation_before_task_start_is_invalidated():
    guard = FallbackStopGuard(max_sessions=1, max_pending=1)
    token = guard.begin('a', 'one')
    assert guard.begin('a', 'one') is None
    assert guard.begin('a', 'two') is None
    guard.stop('a', 'one', ['ear'])
    assert guard.invalidated(token, 'ear')
    guard.finish(token)
    assert guard.begin('a', 'two') is not None
