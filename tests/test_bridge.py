"""L2-B 存在桥测试：接近→事件产生且强度=增益；离开→回落；同室再落位冷却期内不重复；
开关关闭→零事件；on_arrival 纯函数（两状态互不被修改）。
B7 对偶：他离开她所在房间 → leave_he 事件（微落 0.02，冷却独立账本）。"""
import copy
import json
import pathlib

from astrbot_plugin_virtual_body import CONFIG
from astrbot_plugin_virtual_body.core import bridge, decay, space
from astrbot_plugin_virtual_body.core.state import normalize

DATA_DIR = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures"
DEMO_A = json.loads((DATA_DIR / "profiles" / "role_a.json").read_text(encoding="utf-8"))
MANOR = json.loads((DATA_DIR / "manor.json").read_text(encoding="utf-8"))

T0 = 1_000_000.0
CD = CONFIG.PRESENCE_COOLDOWN_SECONDS


def mk_space(she_room=None, he_room=None, since_ts=None, moving=None):
    st = space.new_state("role_a_demo-user")
    if she_room:
        st["she"]["room"] = she_room
    if since_ts is not None:
        st["she"]["since"] = space.iso(since_ts)
    if he_room:
        st["he"]["room"] = he_room
    if moving:
        st["she"]["moving"] = moving
    return st


def mk_body(ear=0.0):
    st = normalize(None, DEMO_A)
    st["parts"]["ear_tip"]["value"] = ear
    st["updated_at"] = decay.iso_ts(T0)
    return st


# ---- 接近：事件产生且强度=增益，部位=最敏感非占位部位 ----

def test_proximity_event_matches_gain_and_part():
    st = mk_space("study", "study", since_ts=T0)
    evs = bridge.on_arrival(st, mk_body(), DEMO_A, MANOR, T0)
    assert len(evs) == 1
    ev = evs[0]
    assert ev["part"] == "ear_tip"          # sens=9 的非占位部位；n1 sens=10 是占位 "01"，不选
    assert ev["s"] == CONFIG.PRESENCE_PROXIMITY_GAIN
    assert ev["type"] == "space" and ev["t"] == T0


def test_apply_proximity_raises_value_and_books_cooldown():
    st = mk_space("study", "study", since_ts=T0)
    body = mk_body()
    evs = bridge.on_arrival(st, body, DEMO_A, MANOR, T0)
    summary = bridge.apply_events(body, DEMO_A, evs, T0)
    assert body["parts"]["ear_tip"]["value"] > 0.0            # 经 decay 管线真的加了值
    assert any(h.get("src") == "space" for h in body["history"])   # 冷却账本落账
    assert isinstance(summary, dict) and summary["overload"] is False


# ---- 离开：回落 ----

def test_leave_event_falls_value():
    st = mk_space(she_room="study", he_room="study", since_ts=T0 - 600,
                  moving={"from": "study", "to": "kitchen", "until_ts": T0 + 60})
    body = mk_body(ear=0.20)
    evs = bridge.on_arrival(st, body, DEMO_A, MANOR, T0)
    assert len(evs) == 1
    ev = evs[0]
    assert ev["type"] == "space_leave" and ev["s"] == -CONFIG.PRESENCE_LEAVE_FALL
    assert ev["part"] == "ear_tip"
    bridge.apply_events(body, DEMO_A, evs, T0)
    assert abs(body["parts"]["ear_tip"]["value"] - (0.20 - CONFIG.PRESENCE_LEAVE_FALL)) < 1e-9
    assert any(h.get("src") == "space_leave" for h in body["history"])   # 回落也落冷却账


def test_leave_not_fired_when_departing_other_room():
    # 她从厨房起程去花园（他一直在书房）——从未同室，不算"离开他"
    st = mk_space(she_room="kitchen", he_room="study", since_ts=T0 - 600,
                  moving={"from": "kitchen", "to": "garden", "until_ts": T0 + 60})
    assert bridge.on_arrival(st, mk_body(), DEMO_A, MANOR, T0) == []


def test_visit_then_leave_both_counted():
    # 一次真实到访：来了（+gain）又走（-fall）两侧都记账——冷却按类各管各的
    body = mk_body()
    evs = bridge.on_arrival(mk_space("study", "study", since_ts=T0), body, DEMO_A, MANOR, T0)
    assert len(evs) == 1
    bridge.apply_events(body, DEMO_A, evs, T0)
    st = mk_space(she_room="study", he_room="study", since_ts=T0 - 60,
                  moving={"from": "study", "to": "kitchen", "until_ts": T0 + 160})
    evs2 = bridge.on_arrival(st, body, DEMO_A, MANOR, T0 + 100)
    assert len(evs2) == 1 and evs2[0]["type"] == "space_leave"


def test_repeated_departure_not_double_fall():
    # 长途移动中每轮 tick 都看到 moving——同类冷却防重复回落
    body = mk_body(ear=0.30)
    mv = {"from": "study", "to": "gatehouse", "until_ts": T0 + 300}
    st = mk_space(she_room="study", he_room="study", since_ts=T0 - 60, moving=mv)
    evs = bridge.on_arrival(st, body, DEMO_A, MANOR, T0)
    assert len(evs) == 1
    bridge.apply_events(body, DEMO_A, evs, T0)
    assert bridge.on_arrival(st, body, DEMO_A, MANOR, T0 + 60) == []


# ---- 冷却：同室再落位不重复；久处同室不重计 ----

def test_cooldown_blocks_reentry():
    body = mk_body()
    evs = bridge.on_arrival(mk_space("study", "study", since_ts=T0), body, DEMO_A, MANOR, T0)
    assert len(evs) == 1
    bridge.apply_events(body, DEMO_A, evs, T0)
    # 冷却期内再落位同室 → 不重复
    t1 = T0 + 100
    assert bridge.on_arrival(mk_space("study", "study", since_ts=t1), body, DEMO_A, MANOR, t1) == []
    # 冷却期外的落位 → 重新计
    t2 = T0 + CD + 60
    evs2 = bridge.on_arrival(mk_space("study", "study", since_ts=t2), body, DEMO_A, MANOR, t2)
    assert len(evs2) == 1


def test_stale_same_room_not_refired():
    # 久处同室（since 超出新鲜窗）→ 每轮 tick 不反复加值
    st = mk_space("study", "study", since_ts=T0 - 3600)
    assert bridge.on_arrival(st, mk_body(), DEMO_A, MANOR, T0) == []


# ---- 开关关 → 零事件 ----

def test_switch_off_zero_events():
    st = mk_space("study", "study", since_ts=T0)
    assert bridge.on_arrival(st, mk_body(), DEMO_A, MANOR, T0, enabled=False) == []
    assert bridge.should_bridge(False) is False
    assert bridge.should_bridge(True) is True


# ---- 纯函数验证：两状态互不被修改 ----

def test_on_arrival_is_pure():
    for st in (mk_space("study", "study", since_ts=T0),                       # proximity 路径
               mk_space("study", "study", since_ts=T0 - 600,
                        moving={"from": "study", "to": "kitchen", "until_ts": T0 + 60})):  # leave 路径
        body = mk_body(ear=0.3)
        snap_space, snap_body = copy.deepcopy(st), copy.deepcopy(body)
        assert bridge.on_arrival(st, body, DEMO_A, MANOR, T0)      # 有事件产生的路径才考验纯度
        assert st == snap_space and body == snap_body


# ---- 边界：位置未知/落位别的房间/他在移动 ----

def test_no_event_when_rooms_unknown_or_different():
    assert bridge.on_arrival(mk_space("study", None, T0), mk_body(), DEMO_A, MANOR, T0) == []   # 他位置未知
    assert bridge.on_arrival(mk_space(None, "study", None), mk_body(), DEMO_A, MANOR, T0) == []  # 她位置未知
    # 落位到别的房间（非起程离开他）→ 无事件
    assert bridge.on_arrival(mk_space("kitchen", "study", T0), mk_body(), DEMO_A, MANOR, T0) == []


def test_proximity_blocked_when_he_moving():
    st = mk_space("study", "study", since_ts=T0)
    st["he"]["moving"] = {"to": "kitchen", "from": "study", "until_ts": T0 + 30}
    assert bridge.on_arrival(st, mk_body(), DEMO_A, MANOR, T0) == []


# ---- B7 他离开她所在房间：leave_he 事件（检测时机由 main._tick_space 掌握）----

def test_departure_event_falls_slightly():
    # 他在厨房=刚从她的书房走开（调用方已核实旧 he.room==study）
    st = mk_space("study", "kitchen", since_ts=T0 - 600)
    body = mk_body(ear=0.30)
    evs = bridge.on_departure(st, body, DEMO_A, MANOR, T0)
    assert len(evs) == 1
    ev = evs[0]
    assert ev["type"] == "leave_he" and ev["s"] == -bridge.DEPARTURE_FALL
    assert bridge.DEPARTURE_FALL == 0.02          # 敏感微落 0.02（模块常量，不动 CONFIG）
    assert ev["part"] == "ear_tip"
    bridge.apply_events(body, DEMO_A, evs, T0)
    assert abs(body["parts"]["ear_tip"]["value"] - 0.28) < 1e-9
    assert any(h.get("src") == "leave_he" for h in body["history"])   # 冷却账本按 leave_he 记


def test_departure_cooldown_blocks_refire():
    body = mk_body(ear=0.30)
    st = mk_space("study", "kitchen", since_ts=T0 - 600)
    evs = bridge.on_departure(st, body, DEMO_A, MANOR, T0)
    bridge.apply_events(body, DEMO_A, evs, T0)
    assert bridge.on_departure(st, body, DEMO_A, MANOR, T0 + 60) == []       # 冷却内不重复回落
    assert len(bridge.on_departure(st, body, DEMO_A, MANOR, T0 + CD + 60)) == 1


def test_departure_not_confused_with_her_leave():
    # 她侧离开（space_leave）已记账，不挤占他侧离场（leave_he）的冷却——账本各记各的
    body = mk_body(ear=0.40)
    her_leave = mk_space(she_room="study", he_room="study", since_ts=T0 - 600,
                         moving={"from": "study", "to": "kitchen", "until_ts": T0 + 60})
    evs1 = bridge.on_arrival(her_leave, body, DEMO_A, MANOR, T0)
    assert len(evs1) == 1 and evs1[0]["type"] == "space_leave"
    bridge.apply_events(body, DEMO_A, evs1, T0)
    departed = mk_space("study", "kitchen", since_ts=T0 - 600)      # 他随后也走开
    assert len(bridge.on_departure(departed, body, DEMO_A, MANOR, T0)) == 1


def test_departure_requires_him_elsewhere():
    assert bridge.on_departure(mk_space("study", "study", T0), mk_body(), DEMO_A, MANOR, T0) == []    # 还同室
    assert bridge.on_departure(mk_space("study", None, T0), mk_body(), DEMO_A, MANOR, T0) == []      # 他位置未知
    assert bridge.on_departure(mk_space(None, "kitchen", None), mk_body(), DEMO_A, MANOR, T0) == []  # 她位置未知


def test_departure_blocked_when_he_moving():
    st = mk_space("study", "kitchen", since_ts=T0 - 600)
    st["he"]["moving"] = {"to": "hall", "from": "kitchen", "until_ts": T0 + 30}
    assert bridge.on_departure(st, mk_body(), DEMO_A, MANOR, T0) == []


def test_departure_switch_off_and_pure():
    st = mk_space("study", "kitchen", since_ts=T0 - 600)
    assert bridge.on_departure(st, mk_body(), DEMO_A, MANOR, T0, enabled=False) == []
    body = mk_body(ear=0.3)
    snap_space, snap_body = copy.deepcopy(st), copy.deepcopy(body)
    assert bridge.on_departure(st, body, DEMO_A, MANOR, T0)          # 有事件产生的路径才考验纯度
    assert st == snap_space and body == snap_body
