"""Real parser, persistence and bounded graph regressions with synthetic data."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import random
import threading

import pytest

from astrbot_plugin_virtual_body.core import decay, events, state


PROFILE = {
    "id": "role_demo", "name": "示例角色", "whitelist": ["demo-user"],
    "parts": [{"id": "ear", "label": "耳朵", "aliases": [],
               "sens": 5, "half_life": 600, "radiate": {}}],
    "tiers": [{"key": "notice", "min": .15}, {"key": "react", "min": .3},
              {"key": "lose", "min": .5}, {"key": "overload", "min": .85}],
}


@pytest.mark.parametrize("text", [
    "可以摸耳朵吗？", "如果摸耳朵会怎样？", "假如揉耳朵会舒服吗？",
    "我想摸耳朵", "我想要摸耳朵", "想要摸耳朵吗？", "我准备摸耳朵", "我打算摸耳朵", "我没有摸耳朵", "我不会摸耳朵",
    "她引用了一句“轻轻摸耳朵", '她引用了一句"轻轻摸耳朵',
    "「摸耳朵」", "『摸耳朵", "‘摸耳朵’", "`摸耳朵`", "我说'摸耳朵'",
])
def test_only_current_unquoted_contacts_are_registered(text):
    assert events.extract_events(text, PROFILE) == []


@pytest.mark.parametrize("text", [
    "她摸耳朵。", "想要摸耳朵", "她摸耳朵，然后问明天会下雨吗？",
    "她问可以摸耳朵吗？随后她揉耳朵。", "“如果摸耳朵”只是引文，她随后揉耳朵。",
])
def test_independent_actual_contact_survives_question_or_quotation(text):
    found = events.extract_events(text, PROFILE)
    assert len(found) == 1 and found[0]["part"] == "ear" and found[0]["s"] > 0


@pytest.mark.parametrize("text", [
    "如果不要摸耳朵的话，她会休息。", "是不是别摸耳朵？", "别摸耳朵吗？",
    "她引用‘别摸耳朵", "假设停手就会休息。", "为什么停手？",
])
def test_discussing_a_stop_does_not_end_real_contact(text):
    assert events.extract_stops(text, PROFILE) == []


@pytest.mark.parametrize("text", [
    "请不要继续摸我的耳朵", "别摸耳朵", "希望你不要摸耳朵", "能不能别摸耳朵？",
    "停手", "“如果停手”只是引文。现在停手。",
])
def test_clear_and_polite_stop_requests_still_work(text):
    assert events.extract_stops(text, PROFILE) == ["ear"]


@pytest.mark.parametrize("field", ["sens", "half_life", "gain_mult", "tolerance_mult", "windup_mult"])
def test_oversized_part_numbers_return_validation_errors(field):
    part = deepcopy(PROFILE["parts"][0])
    part[field] = 10 ** 400
    ok, error = state.validate_parts([part])
    assert not ok and error


@pytest.mark.parametrize("value", [10 ** 400, {"s": 10 ** 400}, {"s": .5, "freq_hz": 10 ** 400}])
def test_oversized_action_numbers_return_validation_errors(value):
    ok, error = state.validate_action_words({"摸": value})
    assert not ok and error
    assert events._normalize_word_entry(value) is None or "freq_hz" not in events._normalize_word_entry(value)


def test_oversized_model_number_is_not_applied_as_a_contact():
    payload = json.dumps({"part": "ear", "intensity": 10 ** 400, "type": "touch"})
    assert events.parse_llm_json(payload, PROFILE) is None
    assert decay.clamp01(10 ** 400) == 0.0


@pytest.mark.parametrize("phase", [[], {}, 17, False, "invalid"])
def test_corrupt_phase_preserves_other_valid_state(phase):
    original = {"phase": phase, "session": "demo-user", "parts": {"ear": {"value": .4}},
                "longterm_mult": {"ear": 10 ** 400}}
    normalized = state.normalize(original, PROFILE)
    assert normalized["phase"] == "building"
    assert normalized["parts"]["ear"]["value"] == .4
    assert normalized["longterm_mult"] == {}
    assert original["parts"]["ear"]["value"] == .4


def test_invalid_profile_part_does_not_break_state_normalization():
    profile = deepcopy(PROFILE)
    profile["parts"].extend([None, "broken", 42])
    normalized = state.normalize({"parts": {"ear": {"value": .2}}}, profile)
    assert normalized["parts"]["ear"]["value"] == .2
    assert set(normalized["parts"]) == {"ear"}


def _simple_paths(meta, origin, hops_cap, mode):
    """Original bounded simple-path semantics, independent of optimized code."""
    best = {}
    queue = deque([(origin, 1.0, 0, frozenset({origin}))])
    while queue:
        current, cumulative, hops, seen = queue.popleft()
        if hops >= hops_cap:
            continue
        edges = meta.get(current, {}).get("radiate") or {}
        if not isinstance(edges, dict):
            continue
        for target, raw in edges.items():
            if target == origin or target not in meta or target in seen:
                continue
            try:
                ratio = float(raw)
            except (TypeError, ValueError, OverflowError):
                continue
            if not 0 < ratio <= 1:
                continue
            value = max(0.0, min(1.0, cumulative * ratio))
            if value <= 0:
                continue
            if mode == "sum":
                best[target] = max(0.0, min(1.0, best.get(target, 0) + value))
            elif value > best.get(target, 0):
                best[target] = value
            queue.append((target, value, hops + 1, seen | {target}))
    return best


@pytest.mark.parametrize("mode", ["mul", "sum"])
@pytest.mark.parametrize("hops", [1, 2, 3, 4, 5])
def test_graph_modes_match_simple_path_reference(mode, hops):
    rng = random.Random(5041 + hops)
    for _ in range(40):
        nodes = [f"p{i}" for i in range(rng.randint(2, 7))]
        meta = {node: {"radiate": {}} for node in nodes}
        for current in nodes:
            for target in nodes:
                if rng.random() < .65:
                    meta[current]["radiate"][target] = rng.choice([.1, .3, .5, .8, 1.0, 0, -1, 2, "bad", float("nan")])
        expected = _simple_paths(meta, nodes[0], hops, mode)
        actual = decay.radiate_map(meta, nodes[0], max_hops=hops, mode=mode)
        assert actual == pytest.approx(expected, abs=1e-14)


def test_mul_retains_weaker_shorter_prefix_with_remaining_hops():
    meta = {"a": {"radiate": {"b": .5, "c": 1}},
            "b": {"radiate": {"d": 1}}, "c": {"radiate": {"b": 1}},
            "d": {"radiate": {"e": 1}}, "e": {"radiate": {"b": 1}}}
    assert decay.radiate_map(meta, "a", max_hops=3, mode="mul")["e"] == .5


def test_dense_mul_has_per_hop_work_bound_without_timing_assertion():
    counts = []
    class CountingEdges(dict):
        def items(self):
            counts.append(1)
            return super().items()
    size, hops = 60, 3
    meta = {f"p{i}": {"radiate": CountingEdges({f"p{j}": .5 for j in range(size) if i != j})}
            for i in range(size)}
    found = decay.radiate_map(meta, "p0", max_hops=hops, mode="mul")
    assert found == {f"p{i}": .5 for i in range(1, size)}
    assert len(counts) <= size * hops


def test_multiple_store_instances_use_unique_atomic_temporaries(tmp_path, monkeypatch):
    seen, capture_lock = [], threading.Lock()
    original_replace = state.os.replace
    def capture(source, destination):
        with capture_lock:
            seen.append(str(source))
        return original_replace(source, destination)
    monkeypatch.setattr(state.os, "replace", capture)
    stores = [state.StateStore(str(tmp_path)) for _ in range(8)]
    gate = threading.Barrier(len(stores))
    def save(index):
        gate.wait(timeout=10)
        for _ in range(4):
            value = state.new_state("role_demo", "demo-user")
            value["writer"] = index
            stores[index].save("role_demo", "demo-user", value)
            stores[index].save_bindings({"demo-user": "role_demo"})
            ok, error = state.save_names_overlay(str(tmp_path), "role_demo", {"ear": {"label": f"示例{index}"}})
            assert ok, error
            ok, error = state.save_profile_override(str(tmp_path), PROFILE, {"params": {"gain": index / 10}})
            assert ok, error
    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        list(pool.map(save, range(len(stores))))
    assert len(seen) >= 8 * 4 * 4
    assert len(set(seen)) == len(seen)
    saved = json.loads((tmp_path / "state/role_demo_demo-user.json").read_text(encoding="utf-8"))
    assert saved["writer"] in range(8)
    for file in tmp_path.rglob("*.json"):
        assert isinstance(json.loads(file.read_text(encoding="utf-8")), dict)
    assert list(tmp_path.rglob("*.tmp")) == []
    assert (tmp_path / "profiles/role_demo.json.bak").is_file()


@pytest.mark.parametrize("kind", ["state", "bindings", "names", "profile"])
def test_failed_atomic_replace_keeps_original_and_cleans_temporary(tmp_path, monkeypatch, kind):
    store = state.StateStore(str(tmp_path))
    def save(second=False):
        if kind == "state":
            store.save("role_demo", "demo-user", dict(state.new_state("role_demo", "demo-user"), writer=int(second)))
        elif kind == "bindings":
            store.save_bindings({"demo-user": "role_demo" if not second else "other"})
        elif kind == "names":
            return state.save_names_overlay(str(tmp_path), "role_demo", {"ear": {"label": "原名" if not second else "新名"}})
        else:
            return state.save_profile_override(str(tmp_path), PROFILE, {"params": {"gain": .1 if not second else .2}})
    save()
    paths = {"state": "state/role_demo_demo-user.json", "bindings": "state/bindings.json",
             "names": "profiles/role_demo.names.json", "profile": "profiles/role_demo.json"}
    target = tmp_path / paths[kind]
    original = target.read_bytes()
    def fail_replace(source, destination):
        raise OSError("synthetic replace failure")
    monkeypatch.setattr(state.os, "replace", fail_replace)
    if kind in ("state", "bindings"):
        with pytest.raises(OSError, match="synthetic"):
            save(True)
    else:
        ok, error = save(True)
        assert not ok and "写入失败" in error
    assert target.read_bytes() == original
    assert list(tmp_path.rglob("*.tmp")) == []


def test_profile_backup_contains_previous_complete_file(tmp_path):
    assert state.save_profile_override(str(tmp_path), PROFILE, {"params": {"gain": .2}})[0]
    path = tmp_path / "profiles/role_demo.json"
    original = path.read_bytes()
    assert state.save_profile_override(str(tmp_path), PROFILE, {"params": {"gain": .3}})[0]
    assert path.with_suffix(".json.bak").read_bytes() == original
    assert json.loads(path.read_text(encoding="utf-8"))["params"]["gain"] == .3
