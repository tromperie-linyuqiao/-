# -*- coding: utf-8 -*-
"""0.27.2 联动深化测试：环境底色/情感词/渴慕邀约/氛围动机。"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from astrbot_plugin_virtual_body.core import space as sp  # noqa: E402
from astrbot_plugin_virtual_body.core import mood_engine as me  # noqa: E402
from astrbot_plugin_virtual_body.core import mood_signals as msig  # noqa: E402

MANOR = {"rooms": [{"id": "living", "mood_bias": [0.4, -0.2]},
                   {"id": "bar-corner", "mood_bias": [0.3, 0.15]},
                   {"id": "study", "mood_bias": [0.2, -0.3]},
                   {"id": "plain"}]}


def test_room_mood_bias():
    assert sp.room_mood_bias(MANOR, "living") == (0.4, -0.2)
    assert sp.room_mood_bias(MANOR, "plain") == (0.0, 0.0)   # 无 bias 中性
    assert sp.room_mood_bias(MANOR, None) == (0.0, 0.0)
    assert sp.room_mood_bias(MANOR, "unknown") == (0.0, 0.0)
    bad = {"rooms": [{"id": "x", "mood_bias": [9, "a"]}]}
    assert sp.room_mood_bias(bad, "x") == (1.0, 0.0)   # 钳制+坏值宽容


def test_mood_word_quadrants():
    assert sp.mood_word(0.0, 0.0) == "平静"
    assert sp.mood_word(0.5, 0.3) == "雀跃"    # 高V高A
    assert sp.mood_word(0.5, -0.2) == "满足"   # 高V低A
    assert sp.mood_word(0.2, 0.0) == "安心"    # 中V
    assert sp.mood_word(-0.5, 0.3) == "不安"   # 低V高A
    assert sp.mood_word(-0.5, -0.2) == "低落"  # 低V低A


def test_presence_env_synthesis(tmp_path):
    pd = tmp_path / "vb" / "state"
    pd.mkdir(parents=True)
    (pd / "role_a_1.space.json").write_text(json.dumps(
        {"she": {"room": "living", "moving": None}, "he": {"room": "living"}}), encoding="utf-8")
    (pd / "role_a_1.json").write_text(json.dumps({"phase": "building", "arousal": 0.62}), encoding="utf-8")
    out = msig.read_presence(str(tmp_path / "vb"), MANOR)
    assert out["env_valence"] == 0.4 and out["env_arousal"] == -0.2
    assert out["mood_word"] == "满足"
    assert out["same_room"] is True and out["arousal"] == 0.62


def _rules():
    # 相对定位（原写死本机Windows绝对路径，容器内不可移植）
    return json.loads((pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "rules.json"
                       ).read_text(encoding="utf-8"))["rules"]


def test_longing_invite_rule():
    rule = next(r for r in _rules() if r["id"] == "longing_invite")
    sig_hi = {"presence": {"arousal": 0.62, "body_phase": "building", "mood_word": "雀跃", "same_room": True}}
    fired, _ = me.evaluate([rule], sig_hi, {}, {}, now=1000.0)
    assert fired and "邀约" in fired[0]["template_hint"] and "雀跃" in fired[0]["template_hint"]
    # 未过阈不触发（双控：SIS 制动）
    sig_lo = {"presence": {"arousal": 0.3, "body_phase": "building"}}
    fired2, _ = me.evaluate([rule], sig_lo, {}, {}, now=1000.0)
    assert fired2 == []
    # 余韵期不触发（邀约是 building 期的主动）
    sig_ag = {"presence": {"arousal": 0.9, "body_phase": "afterglow"}}
    fired3, _ = me.evaluate([rule], sig_ag, {}, {}, now=1000.0)
    assert fired3 == []


def test_mood_warm_and_rain_rules():
    warm = next(r for r in _rules() if r["id"] == "mood_warm")
    fired, _ = me.evaluate([warm], {"presence": {"env_valence": 0.4, "same_room": True, "mood_word": "满足"}},
                           {}, {}, now=1000.0)
    assert fired and "暖意" in fired[0]["template_hint"]
    fired2, _ = me.evaluate([warm], {"presence": {"env_valence": 0.1, "same_room": True}}, {}, {}, now=1000.0)
    assert fired2 == []
    # P4 规则精简后天气规则已从内置删除（TTL 易过期前提），此处内联等价规则
    # 保留"天气×空间"双信号组合的引擎覆盖
    rain = {"id": "rain_veranda_inline", "enabled": True, "signal": "weather",
            "conditions": [{"field": "weather.is_rain", "op": "eq", "value": True},
                           {"field": "presence.she_room", "op": "eq", "value": "veranda"}],
            "cooldown_minutes": 300, "priority": 3,
            "template_hint": "雨落在檐廊（{city}），她在藤椅上听雨", "note": "雨声檐下"}
    fired3, _ = me.evaluate([rain], {"weather": {"is_rain": True, "city": "示例城市"},
                                     "presence": {"she_room": "veranda"}}, {}, {}, now=1000.0)
    assert fired3 and "檐廊" in fired3[0]["template_hint"] and "示例城市" in fired3[0]["template_hint"]


def test_new_placeholders():
    sig = {"presence": {"env_valence": 0.4, "mood_word": "满足", "same_room": True}}
    out = me.render_hint("V={env_valence} 词={mood_word} 同室={same_room}", sig)
    assert out == "V=0.4 词=满足 同室=是"
