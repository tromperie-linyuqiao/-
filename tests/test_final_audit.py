# -*- coding: utf-8 -*-
"""终审最小回归：P0/P1/P2 修复的封口测试（审查建议的四条 + set_room 幂等 + 混合标签）。"""
import json
import pathlib
import sys
import tempfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from astrbot_plugin_virtual_body.core import state as state_mod  # noqa: E402
from astrbot_plugin_virtual_body.core import space as space_mod  # noqa: E402
from astrbot_plugin_virtual_body.core import mood_signals as msig  # noqa: E402
from astrbot_plugin_virtual_body.core.inject import sanitize_outbound_mixed  # noqa: E402

PROFILE = {"id": "role_a", "parts": []}


def test_p0_list_states_excludes_space_files(tmp_path):
    """P0 封口：身体层 list_states 不得收编 .space.json（每夜日切覆写数据损坏）。"""
    with tempfile.TemporaryDirectory() as td:
        st = state_mod.StateStore(td)
        st.save("role_a", "demo-user", state_mod.new_state("role_a", "demo-user"))
        pathlib.Path(td, "state", "role_a_demo-user.space.json").write_text(
            json.dumps({"she": {"room": "living"}}), encoding="utf-8")
        lst = st.list_states()
        assert lst == [("role_a", "demo-user")], lst   # 幻影会话被排除


def test_p1_mood_dir_autocreate(tmp_path):
    """P1 封口：mood 子目录不存在时写盘自建（全新安装不再静默死亡）。"""
    from astrbot_plugin_virtual_body.core import mood_store as mst
    d = tmp_path / "fresh"   # 无 mood/
    mst.save_triggers(str(d), [{"id": "x", "expires_at": 9999999999.0}])
    assert (d / "triggers.json").exists()
    assert len(mst.load_triggers(str(d))) == 1


def test_p2_nested_array_rain(tmp_path):
    """P2 封口：嵌套数组里的雨词不再漏。"""
    wz = tmp_path / "vb" / "weather"
    wz.mkdir(parents=True)
    (wz / "weather_last.json").write_text(json.dumps(
        {"forecast": [{"day": "今天", "dayText": "小雨转阴"}]}), encoding="utf-8")
    out = msig.read_weather(str(tmp_path / "vb"))
    assert out is not None and out["is_rain"] is True


def test_p3_set_room_same_target_idempotent():
    """P3-17 封口：同目标重复起程幂等（首次落位无过程属原语义，先落 living 再起程）。"""
    manor = {"rooms": [{"id": "living"}, {"id": "kitchen"}], "edges": [{"a": "living", "b": "kitchen", "seconds": 5}]}
    st = space_mod.new_state("x")
    assert space_mod.set_room(st, "she", "living", manor, 900.0, "rp") is None   # 首次落位直接放（原语义）
    r1 = space_mod.set_room(st, "she", "kitchen", manor, 1000.0, "rp")
    assert r1 is not None and r1["seconds"] == 5
    moves_after_first = st["events_today"]["moves"]
    r2 = space_mod.set_room(st, "she", "kitchen", manor, 1002.0, "rp")
    assert isinstance(r2, dict) and r2.get("to") == "kitchen"
    assert r2["seconds"] == 3 and r2["changed"] is False
    assert st["events_today"]["moves"] == moves_after_first   # 不因重复说而翻倍
    assert (st["she"].get("moving") or {}).get("from") == "living"


def test_p3_mixed_width_tags():
    """P3-9 封口：半角开+全角闭混合标签对整段清除。"""
    leak = "<身体状态 角色=\"示例甲\">泄漏内容＜/身体状态>"
    out = sanitize_outbound_mixed(leak)
    assert "泄漏内容" not in out and "身体状态" not in out
    mirror = "＜身体状态 角色=\"示例甲\"＞泄漏内容</身体状态>"
    assert "泄漏内容" not in sanitize_outbound_mixed(mirror)
