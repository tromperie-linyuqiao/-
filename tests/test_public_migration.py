"""Explicit legacy migration boundary; synthetic sibling data only."""
import asyncio
import builtins
from copy import deepcopy
import json
import os

import pytest

from .test_rp_regressions import runtime, PROFILE


def fixture_paths(runtime, monkeypatch, tmp_path):
    plugin, _, module = runtime
    parent = tmp_path / 'isolated-plugin-data'
    owned = parent / 'astrbot_plugin_virtual_body'
    legacy = parent / 'astrbot_plugin_virtual_manor'
    (owned / 'profiles').mkdir(parents=True)
    (legacy / 'state').mkdir(parents=True)
    profile = deepcopy(PROFILE)
    (owned / 'profiles' / (profile['id'] + '.json')).write_text(json.dumps(profile), encoding='utf-8')
    raw_state = json.dumps({'she': {'room': 'room-a'}, 'he': {'room': None}}).encode()
    raw_objects = json.dumps({'items': [{'id': 'synthetic-notebook', 'room': 'room-a'}]}).encode()
    (legacy / 'state/demo-user.json').write_bytes(raw_state)
    (legacy / 'objects.json').write_bytes(raw_objects)
    monkeypatch.setattr(module.StarTools, 'get_data_dir', lambda name: owned)
    plugin.config['vb_default_character'] = profile['id']
    return plugin, module, owned, legacy, raw_state, raw_objects


@pytest.mark.parametrize('setting', ['absent', False, 'false', '0', 'off', 'no', '', None])
def test_default_or_false_initialize_never_reads_or_modifies_legacy(runtime, monkeypatch, tmp_path, setting):
    plugin, module, owned, legacy, raw_state, raw_objects = fixture_paths(runtime, monkeypatch, tmp_path)
    if setting != 'absent':
        plugin.config['sp_migrate_legacy_manor'] = setting
    # Check the actual I/O boundary as well as the resulting files. A default
    # initialization must not even stat this sibling directory to discover it.
    original_isdir, original_open, original_listdir = os.path.isdir, builtins.open, os.listdir
    legacy_root = str(legacy.resolve())
    def in_legacy(path):
        if isinstance(path, (str, bytes, os.PathLike)):
            resolved = os.path.abspath(os.fsdecode(path))
            return resolved == legacy_root or resolved.startswith(legacy_root + os.sep)
        return False
    def guarded_isdir(path):
        if in_legacy(path):
            raise AssertionError('default initialization inspected the legacy directory')
        return original_isdir(path)
    def guarded_open(path, *args, **kwargs):
        if in_legacy(path):
            raise AssertionError('default initialization opened a legacy file')
        return original_open(path, *args, **kwargs)
    def guarded_listdir(path):
        if in_legacy(path):
            raise AssertionError('default initialization enumerated legacy files')
        return original_listdir(path)
    monkeypatch.setattr(os.path, 'isdir', guarded_isdir)
    monkeypatch.setattr(builtins, 'open', guarded_open)
    monkeypatch.setattr(os, 'listdir', guarded_listdir)
    async def scenario():
        try:
            await plugin.initialize()
            assert plugin._initialized and plugin.context.presence_layer.active
            assert not (owned / 'state/demo_demo-user.space.json').exists()
            assert not (owned / 'objects.json').exists()
            assert (legacy / 'state/demo-user.json').read_bytes() == raw_state
            assert (legacy / 'objects.json').read_bytes() == raw_objects
            assert not (legacy / 'state/demo-user.json.migrated').exists()
            assert not (legacy / 'objects.json.migrated').exists()
        finally:
            await plugin.terminate()
    asyncio.run(scenario())


@pytest.mark.parametrize('setting', [True, 'true'])
def test_explicit_enable_migrates_only_once_and_preserves_legacy_files(runtime, monkeypatch, tmp_path, setting):
    plugin, _, owned, legacy, raw_state, raw_objects = fixture_paths(runtime, monkeypatch, tmp_path)
    plugin.config['sp_migrate_legacy_manor'] = setting
    async def scenario():
        try:
            await plugin.initialize()
            target_state = owned / 'state/demo_demo-user.space.json'
            target_objects = owned / 'objects.json'
            assert target_state.read_bytes() == raw_state
            assert target_objects.read_bytes() == raw_objects
            assert (legacy / 'state/demo-user.json').read_bytes() == raw_state
            assert (legacy / 'objects.json').read_bytes() == raw_objects
            assert (legacy / 'state/demo-user.json.migrated').read_bytes() == b''
            assert (legacy / 'objects.json.migrated').read_bytes() == b''
            tick = plugin._tick_task
            timestamps = (target_state.stat().st_mtime_ns, target_objects.stat().st_mtime_ns)
            await plugin.initialize()
            assert plugin._tick_task is tick
            assert (target_state.stat().st_mtime_ns, target_objects.stat().st_mtime_ns) == timestamps
            await plugin.terminate()
            # Explicit opt-in can remain enabled, but existing targets are
            # protected on a later genuine reload too.
            await plugin.initialize()
            assert target_state.read_bytes() == raw_state and target_objects.read_bytes() == raw_objects
            assert (target_state.stat().st_mtime_ns, target_objects.stat().st_mtime_ns) == timestamps
        finally:
            await plugin.terminate()
    asyncio.run(scenario())


def test_explicit_migration_never_overwrites_existing_targets(runtime, monkeypatch, tmp_path):
    plugin, _, owned, legacy, raw_state, raw_objects = fixture_paths(runtime, monkeypatch, tmp_path)
    plugin.config['sp_migrate_legacy_manor'] = True
    (owned / 'state').mkdir()
    existing_state = b'{"sentinel":"existing spatial state"}'
    existing_objects = b'{"items":[],"sentinel":"existing objects"}'
    (owned / 'state/demo_demo-user.space.json').write_bytes(existing_state)
    (owned / 'objects.json').write_bytes(existing_objects)
    async def scenario():
        try:
            await plugin.initialize()
            assert (owned / 'state/demo_demo-user.space.json').read_bytes() == existing_state
            assert (owned / 'objects.json').read_bytes() == existing_objects
            assert (legacy / 'state/demo-user.json').read_bytes() == raw_state
            assert (legacy / 'objects.json').read_bytes() == raw_objects
            assert not (legacy / 'state/demo-user.json.migrated').exists()
            assert not (legacy / 'objects.json.migrated').exists()
        finally:
            await plugin.terminate()
    asyncio.run(scenario())
