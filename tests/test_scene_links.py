# -*- coding: utf-8 -*-
"""迭代1 测试：场景词联动 + 心绪 hint 占位符。"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from astrbot_plugin_virtual_body.core import space as sp  # noqa: E402
from astrbot_plugin_virtual_body.core import mood_engine as me  # noqa: E402

MANOR = {"rooms": [
    {"id": "bar-corner", "label": "酒角", "scene_words": [
        {"word": "倒酒", "gain": 0.15, "note": "威士忌"},
        {"word": "碰杯", "gain": 0.1},
    ]},
    {"id": "living", "label": "客厅"},
]}


def test_scene_words_read():
    ws = sp.scene_words(MANOR, "bar-corner")
    assert ws and ws[0]["word"] == "倒酒" and ws[0]["gain"] == 0.15
    assert sp.scene_words(MANOR, "living") == []
    assert sp.scene_words(MANOR, None) == []
    assert sp.scene_words(MANOR, "unknown") == []


def test_scene_words_clamp():
    m = {"rooms": [{"id": "x", "scene_words": [{"word": "a", "gain": 9.0}, {"word": "b", "gain": -1}]}]}
    ws = sp.scene_words(m, "x")
    assert ws[0]["gain"] == 0.3 and ws[1]["gain"] == 0.0


def test_apply_scene_gain_hit():
    evs = [{"part": "ear_tip", "s": 0.4}]
    out = sp.apply_scene_gain(evs, "他在酒角给她倒酒，两人碰杯", sp.scene_words(MANOR, "bar-corner"))
    assert out[0]["s"] == pytest.approx(0.4 * 1.15, abs=1e-4)   # 取最高增益 0.15
    assert "倒酒/碰杯" in out[0]["scene"] or "碰杯/倒酒" in out[0]["scene"]
    assert evs[0]["s"] == 0.4   # 纯函数：入参未改


def test_apply_scene_gain_miss():
    evs = [{"part": "ear_tip", "s": 0.4}]
    out = sp.apply_scene_gain(evs, "聊天而已", sp.scene_words(MANOR, "bar-corner"))
    assert out[0]["s"] == 0.4 and "scene" not in out[0]


def test_render_hint_placeholders():
    sig = {"presence": {"she_room": "檐廊", "body_phase": "building"},
           "weather": {"city": "示例城市", "is_rain": True},
           "reading": {"book_title": "小王子"}, "mood": {"score": -0.6}}
    out = me.render_hint("她在{she_room}听雨（{city}，雨={is_rain}），读《{book}》，分{score}", sig)
    assert out == "她在檐廊听雨（示例城市，雨=是），读《小王子》，分-0.6"
    out2 = me.render_hint("{unknown_key} 原样保留", sig)
    assert out2 == "{unknown_key} 原样保留"
    assert me.render_hint("无占位符", sig) == "无占位符"


def test_evaluate_renders_hint():
    rules = [{"id": "r", "enabled": True, "signal": "weather",
              "conditions": [{"field": "weather.is_rain", "op": "eq", "value": True}],
              "cooldown_minutes": 1, "priority": 3, "template_hint": "她在{she_room}，雨中{city}"}]
    fired, _ = me.evaluate(rules, {"weather": {"is_rain": True},
                                   "presence": {"she_room": "檐廊"}}, {}, {}, now=1000.0)
    assert fired and fired[0]["template_hint"] == "她在檐廊，雨中"
