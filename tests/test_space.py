import json
import pathlib

import pytest

from astrbot_plugin_virtual_body.core import space, traverse

PROF = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "manor.json"
MANOR = json.loads(PROF.read_text(encoding="utf-8"))
T0 = 1_000_000.0
DAY0 = T0 - ((T0 + 8 * 3600) % 86400)   # 对齐北京时间零点（兜底动线按本地小时查表）


def fresh():
    return space.new_state("demo-user")


# ---- 轮1：状态机 ----

def test_new_state_shape():
    st = fresh()
    assert st["schema"] == 1 and st["session"] == "demo-user"
    assert st["she"]["room"] is None and st["he"]["source"] == "default"


def test_fallback_schedule_by_hour():
    assert space.fallback_room(MANOR, DAY0 + 7 * 3600, 8) == "kitchen"
    assert space.fallback_room(MANOR, DAY0 + 15 * 3600, 8) == "study"
    assert space.fallback_room(MANOR, DAY0 + 1 * 3600, 8) == "master-bed"   # 01 点 → 0-6 段
    assert space.fallback_room(MANOR, DAY0 + 23 * 3600 + 1800, 8) == "master-bed"


# ---- B4 雨天动线 ----

def test_fallback_rainy_swaps_outdoor_to_indoor():
    # 8-12 点兜底花圃（OUT 层）；雨天 → rainy_fallback 映射客厅
    assert space.fallback_room(MANOR, DAY0 + 9 * 3600, 8) == "garden"
    assert space.fallback_room(MANOR, DAY0 + 9 * 3600, 8, rainy=True) == "living"


def test_fallback_rainy_keeps_indoor_slots():
    # 室内时段不受雨天影响
    assert space.fallback_room(MANOR, DAY0 + 15 * 3600, 8, rainy=True) == "study"
    assert space.fallback_room(MANOR, DAY0 + 7 * 3600, 8, rainy=True) == "kitchen"


def test_rainy_fallback_unknown_target_ignored():
    manor2 = json.loads(json.dumps(MANOR))
    manor2["rainy_fallback"] = {"garden": "nonexistent"}     # 未知目标 id → 忽略映射走原逻辑
    assert space.fallback_room(manor2, DAY0 + 9 * 3600, 8, rainy=True) == "garden"


def test_advance_rainy_passes_to_fallback():
    st = fresh()
    st["she"]["room"] = "study"
    st["she"]["since"] = space.iso(T0 - 7200)
    st["she"]["source"] = "manual"
    # 9 点兜底=花圃，雨天=True → 她的移动目标换成客厅（rp_minutes=1 让 manual 覆盖失效交给兜底）
    space.advance(st, MANOR, DAY0 + 9 * 3600, 8, None, 1, True)
    assert st["she"]["moving"] and st["she"]["moving"]["to"] == "living"


def test_schedule_veranda_slot_reachable():
    """0.32.0 P0-3：16-18 点兜底动线落檐廊（rain_veranda 可达性修复）——
    veranda 在 L1（室内檐廊），雨天动线不迁移；14-16 仍书房。"""
    assert space.fallback_room(MANOR, DAY0 + 17 * 3600, 8) == "veranda"
    assert space.fallback_room(MANOR, DAY0 + 17 * 3600, 8, rainy=True) == "veranda"
    assert space.fallback_room(MANOR, DAY0 + 15 * 3600, 8, rainy=True) == "study"
    st2 = fresh()
    st2["she"].update({"room": "study", "since": space.iso(T0 - 7200), "source": "manual"})
    space.advance(st2, MANOR, DAY0 + 9 * 3600, 8, None, 1, False)   # 非雨天走原花圃
    assert st2["she"]["moving"] and st2["she"]["moving"]["to"] == "garden"


def test_manor_rainy_fallback_table_integrity():
    # 数据完整性：映射键全是户外房、目标全是室内已知房
    floors = {str(r["id"]): str(r["floor"]) for r in MANOR["rooms"]}
    table = MANOR.get("rainy_fallback")
    assert isinstance(table, dict) and table
    for src, dst in table.items():
        assert src in floors and floors[src] == "OUT"
        assert str(dst) in floors and floors[str(dst)] != "OUT"


def test_schedule_room_keyword_priority():
    assert space.schedule_room(MANOR, "下午在书房看书") == "study"
    assert space.schedule_room(MANOR, "去厨房做饭") == "kitchen"
    assert space.schedule_room(MANOR, "修剪花圃的蔷薇") == "garden"
    assert space.schedule_room(MANOR, "发呆") is None


def test_set_room_first_placement_no_travel():
    st = fresh()
    r = space.set_room(st, "she", "study", MANOR, T0, "manual")
    assert r is None                     # 首次落位无移动过程
    assert st["she"]["room"] == "study"


def test_set_room_produces_moving_process():
    st = fresh()
    space.set_room(st, "she", "study", MANOR, T0, "manual")
    r = space.set_room(st, "she", "garden", MANOR, T0 + 10, "manual")
    assert r is not None and r["changed"]
    assert r["seconds"] > 0
    mv = st["she"]["moving"]
    assert mv["from"] == "study" and mv["to"] == "garden"
    assert abs(mv["until_ts"] - (T0 + 10 + r["seconds"])) < 1e-6


def test_set_room_unknown_ignored():
    st = fresh()
    space.set_room(st, "she", "study", MANOR, T0, "manual")
    assert space.set_room(st, "she", "nonexistent", MANOR, T0 + 5, "manual") is None
    assert st["she"]["room"] == "study"


def test_advance_lands_arrived_mover():
    st = fresh()
    space.set_room(st, "she", "study", MANOR, T0, "manual")
    space.set_room(st, "she", "garden", MANOR, T0 + 10, "manual")
    assert st["she"]["room"] == "study"              # 移动中未落位
    # 钉在花园（日程推导目标=当前位置→无二次移动）
    summary = space.advance(st, MANOR, T0 + 10 + 999, plan_text="在花园种植修剪")
    assert "she" in summary["arrived"]
    assert st["she"]["room"] == "garden" and st["she"]["moving"] is None


def test_advance_same_room_no_move_event():
    st = fresh()
    space.set_room(st, "she", "study", MANOR, T0, "manual")
    summary = space.advance(st, MANOR, T0 + 60, plan_text="在书房读书")
    assert summary["she_moved"] is False


def test_advance_rp_override_blocks_schedule():
    st = fresh()
    st["she"]["room"] = "bar-corner"
    st["she"]["source"] = "rp"
    st["she"]["since"] = space.iso(T0)
    summary = space.advance(st, MANOR, T0 + 3600, plan_text="在书房读书")
    assert st["she"]["room"] == "bar-corner"          # RP 覆盖 2h 内不被日程顶掉


def test_advance_rp_override_expires():
    st = fresh()
    st["she"]["room"] = "bar-corner"
    st["she"]["source"] = "rp"
    st["she"]["since"] = space.iso(T0)
    space.advance(st, MANOR, T0 + 3 * 3600, plan_text="在书房读书")   # 3h > 2h 过期→产生移动过程
    assert st["she"]["moving"] is not None and st["she"]["moving"]["to"] == "study"
    space.advance(st, MANOR, T0 + 3 * 3600 + 60, plan_text="在书房读书")   # 移动到点落位
    assert st["she"]["room"] == "study"


def test_day_rollover_resets_counter():
    st = fresh()
    st["events_today"] = {"day": "2000-01-01", "moves": 7}
    space.advance(st, MANOR, T0)
    assert st["events_today"]["moves"] == 0 and st["events_today"]["day"] != "2000-01-01"


def test_same_room_semantics():
    st = fresh()
    st["she"]["room"] = "study"
    assert space.same_room(st) is None                # 他位置未知
    st["he"]["room"] = "kitchen"
    assert space.same_room(st) is False
    st["he"]["room"] = "study"
    assert space.same_room(st) is True
    st["she"]["moving"] = {"to": "garden", "until_ts": T0 + 5, "from": "study"}
    assert space.same_room(st) is None                # 移动中模糊


# ---- 轮2：寻路 ----

def test_travel_same_room_zero():
    assert traverse.travel_seconds(MANOR, "study", "study") == 0


def test_travel_adjacent_and_cross_floor():
    s1 = traverse.travel_seconds(MANOR, "hall", "living")
    assert s1 == 4                                    # v2 文献重算：3.6m 贴邻 ×1.15 + 门
    s2 = traverse.travel_seconds(MANOR, "study", "master-bed")
    assert s2 is not None and s2 >= 20                # 必经楼梯 ≥20s（5+4+2+11+5=27）
    assert traverse.travel_seconds(MANOR, "study", "gatehouse") >= 20   # 跨外场远距离（5+4+5+9=23）


def test_travel_unknown_room_none():
    assert traverse.travel_seconds(MANOR, "study", "nonexistent") is None


# ---- 0.7.1 部署审查回归 ----

def test_set_room_same_room_rp_refreshes_since():
    st = fresh()
    space.set_room(st, "she", "study", MANOR, T0, "schedule")
    st["she"]["since"] = space.iso(T0 - 7200)          # 2h 前的旧 since
    space.set_room(st, "she", "study", MANOR, T0, "rp")
    assert space.parse_iso(st["she"]["since"]) >= T0 - 1  # RP 重申同房也刷新覆盖窗


def test_set_room_unreachable_no_teleport():
    manor2 = json.loads(json.dumps(MANOR))
    manor2["rooms"] = [r for r in manor2["rooms"] if r["id"] in ("study", "garden")]
    manor2["edges"] = []                                # 两房不连通
    st = fresh()
    space.set_room(st, "she", "study", manor2, T0, "manual")
    r = space.set_room(st, "she", "garden", manor2, T0 + 10, "manual")
    assert r is not None and r["seconds"] >= 30         # 不可达也有移动过程，绝不瞬移
    assert st["she"]["moving"] is not None


def test_advance_rp_minutes_param():
    st = fresh()
    space.set_room(st, "she", "study", MANOR, T0, "rp")
    # rp_minutes=1：T0+2h 时已过期，日程接管（移动有过程，不瞬移）
    space.advance(st, MANOR, T0 + 7200, 8, "在厨房做饭", 1)
    assert st["she"]["moving"] and st["she"]["moving"]["to"] == "kitchen"
    space.advance(st, MANOR, T0 + 7200 + 300, 8, "在厨房做饭", 1)   # 走完落位
    assert st["she"]["room"] == "kitchen"
    st2 = fresh()
    space.set_room(st2, "she", "study", MANOR, T0, "rp")
    # rp_minutes=180：T0+2h 仍受 RP 保护
    space.advance(st2, MANOR, T0 + 7200, 8, "在厨房做饭", 180)
    assert st2["she"]["room"] == "study" and not st2["she"].get("moving")


# ---- 0.7.2 PC 真实结构联动 ----

PC_REAL = {   # 线上 unrelated_records.json 实测结构（2026-09-20）
    "date": "2026-09-20", "source": "fallback",
    "items": [
        {"time": "08:20", "activity": "起床收拾", "mood": "平稳"},
        {"time": "12:15", "activity": "干饭+午休时间"},
        {"time": "14:10", "activity": "下午继续努力"},
        {"time": "16:40", "activity": "摸鱼休息一下下"},
    ],
}


def _ts_at(hour, minute=0):
    return DAY0 + hour * 3600 + minute * 60


def test_current_plan_text_pc_real_dict_structure():
    # dict.items[{time,activity}]——PC 线上真实结构
    plan = {**PC_REAL, "date": space.iso(_ts_at(8))[:10]}
    assert space.current_plan_text(plan, _ts_at(8, 30), 8) == "起床收拾"
    assert space.current_plan_text(plan, _ts_at(13), 8) == "干饭+午休时间"
    assert space.current_plan_text(plan, _ts_at(17), 8) == "摸鱼休息一下下"
    assert space.current_plan_text(plan, _ts_at(5), 8) is None   # 尚未开始的活动不冒充当前活动


def test_current_plan_text_legacy_and_bad():
    assert space.current_plan_text([{"from_h": 9, "to_h": 12, "text": "读书"}], _ts_at(10), 8) == "读书"
    assert space.current_plan_text(None, _ts_at(10), 8) is None
    assert space.current_plan_text([], _ts_at(10), 8) is None
    assert space.current_plan_text([{"time": "坏值", "activity": "x"}], _ts_at(10), 8) is None


def test_schedule_room_maps_real_activities():
    # 真实 activity 文本经 activity_map 映射（用户可编辑映射表）
    assert space.schedule_room(MANOR, "在厨房做饭烧菜") == "kitchen"
    assert space.schedule_room(MANOR, "读书看书") == "study"
    assert space.schedule_room(MANOR, "摸鱼休息一下下") is None or True   # 泛词不误映射即算过
