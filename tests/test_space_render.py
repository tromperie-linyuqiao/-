import json

import pytest

from astrbot_plugin_virtual_body.core import render
from astrbot_plugin_virtual_body.core.space import new_state

from tests.test_space import MANOR, T0
from astrbot_plugin_virtual_body.core.space import iso


def mk_state(she="study", he="master-bed", moving=None):
    st = new_state("demo-user")
    st["she"] = {"room": she, "since": iso(T0), "source": "manual", "moving": moving}
    st["he"] = {"room": he, "since": iso(T0), "source": "rp", "confidence": 0.9, "moving": None}
    st["updated_at"] = iso(T0)
    return st


STUDY_AMB = next(str(r["ambiance"]) for r in MANOR["rooms"] if r["id"] == "study")


def test_render_basic_format():
    text = render.render_injection(mk_state(), MANOR, T0)
    assert text.startswith("<空间") and text.endswith("</空间>")
    assert "书房" in text                      # since=T0 → "刚到书房"（freshness 生效）
    assert "他在主卧（异层，隔层难闻声息）" in text   # B6 听觉规则
    assert "临时快照" in text


def test_render_same_room():
    text = render.render_injection(mk_state(he="study"), MANOR, T0)
    assert "他在身边" in text
    assert "不同室" not in text


def test_render_he_unknown_infers_by_side():
    """0.32.0 P0-2：他位置未知而她已落位 → 渲染"他在身边"
    （与正常同室完全相同的措辞，不暴露推断细节）。"""
    st = mk_state(he=None)                                  # he.room=None（从未声明位置）
    text = render.render_injection(st, MANOR, T0 + 120)     # 稳定在房（非刚到）
    assert "他在身边" in text
    assert "可能不在一处" not in text and "推断" not in text


def test_render_he_unknown_she_moving_no_inference():
    """P0-2：她正移动中不做"他在身边"推断——同 0.27.9 伪陈述封口口径（移动中同室语义模糊）。"""
    st = mk_state(he=None, moving={"to": "kitchen", "from": "study", "until_ts": T0 + 20})
    text = render.render_injection(st, MANOR, T0)
    assert "他在身边" not in text


def test_render_moving_process():
    st = mk_state(she="study", moving={"to": "kitchen", "from": "study", "until_ts": T0 + 20})
    text = render.render_injection(st, MANOR, T0)
    assert "正在从书房去厨房" in text
    assert "同处一室" not in text


def test_render_moving_away_not_by_side():
    """端审封口：她正移动离开出发房、他仍在该房——"他在身边"是伪陈述，须渲染"他还在"。
    （she.room 在移动中仍是出发地，distance_band 判同室——0.27.9 端审修复）"""
    st = mk_state(she="kitchen", he="kitchen",
                  moving={"to": "master-bed", "from": "kitchen", "until_ts": T0 + 20})
    text = render.render_injection(st, MANOR, T0)
    assert "他还在厨房" in text
    assert "他在身边" not in text


def test_both_blocks_silent_when_quiet():
    """端审门控总闸：平常静息（无近变化/无房间词/无移动）→ 空间 gate 关且身体 building 低值空串。
    不该注入时绝不注入（两块各走各的 gate，总闸在 main 注入点）。"""
    from astrbot_plugin_virtual_body.core import inject as inject_mod
    from astrbot_plugin_virtual_body.core.state import normalize
    import json as _json
    import pathlib
    prof = _json.loads((pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "profiles" / "role_a.json")
                       .read_text(encoding="utf-8"))
    st_space = mk_state(she="garden", he="study")
    assert render.gate(st_space, T0 + 3600, "随便聊聊", MANOR, None) is False
    body = normalize(None, prof)
    body["arousal"] = 0.03
    body["updated_at"] = "2026-09-21T00:00:00+00:00"
    assert inject_mod.render(body, prof) == ""


def test_render_low_confidence_downgrade():
    st = mk_state()
    st["he"]["confidence"] = 0.4
    text = render.render_injection(st, MANOR, T0)
    assert "他在主卧" not in text
    assert "可能不在一处" in text


def test_render_max_chars_cap():
    text = render.render_injection(mk_state(), MANOR, T0)
    assert len(text) <= MANOR["render"]["max_chars"]


# ---- B5 氛围上注入（刚到才带）----

def test_render_fresh_arrival_appends_ambiance():
    assert len(STUDY_AMB) > render.AMBIANCE_HINT_CHARS          # 本房氛围足够长 → 走截断分支
    text = render.render_injection(mk_state(), MANOR, T0)       # since=T0 → 刚到
    assert "；" + STUDY_AMB[:render.AMBIANCE_HINT_CHARS] + "…" in text


def test_render_settled_room_no_ambiance():
    text = render.render_injection(mk_state(), MANOR, T0 + 120)   # 稳定在房（非刚到）
    assert "刚到" not in text
    assert STUDY_AMB[:render.AMBIANCE_HINT_CHARS] not in text     # 氛围不带——控制字符预算


def test_render_fresh_arrival_room_without_ambiance():
    manor2 = json.loads(json.dumps(MANOR))
    for r in manor2["rooms"]:
        if r["id"] == "study":
            r.pop("ambiance", None)
    text = render.render_injection(mk_state(), manor2, T0)
    assert "刚到书房" in text and "；" not in text.split("｜")[0]   # 无氛围不追加空分号


def test_render_ambiance_short_enough_not_ellipsized():
    amb = "空气里有雨的味道"   # 8 字 < 18 → 原样不截断
    manor2 = json.loads(json.dumps(MANOR))
    for r in manor2["rooms"]:
        if r["id"] == "study":
            r["ambiance"] = amb
    text = render.render_injection(mk_state(), manor2, T0)
    assert f"；{amb}" in text and f"；{amb}…" not in text


# ---- B6 听觉规则 ----

def test_render_neighbor_band_audible():
    text = render.render_injection(mk_state(he="living"), MANOR, T0)   # study-living 直连边 → 隔壁
    assert "他在隔壁客厅（可闻动静）" in text


def test_render_same_floor_far_band_unchanged():
    text = render.render_injection(mk_state(he="dining"), MANOR, T0)   # 同层、非直连（11s>8）
    assert "他在餐厅（同层远处）" in text


# ---- 门控 ----

def test_gate_recent_change():
    st = mk_state()
    assert render.gate(st, T0 + 300, "随便聊聊", MANOR, T0 + 200) is True      # ① 近 10 分钟变过
    assert render.gate(st, T0 + 3600, "随便聊聊", MANOR, T0 + 300) is False    # 超窗且无其他条件


def test_gate_moving():
    st = mk_state(she="study", moving={"to": "kitchen", "from": "study", "until_ts": T0 + 30})
    assert render.gate(st, T0 + 10, "随便聊聊", MANOR, None) is True           # ③ 正在移动


def test_gate_position_question():
    assert render.gate(mk_state(), T0, "你在哪", MANOR, None) is True          # ④ 位置类问题
    assert render.gate(mk_state(), T0, "书房的窗户关了吗", MANOR, None) is True  # ② 房间词


# ---- 出站清洗 ----

def test_sanitize_outbound():
    full = "她在看书，书房门开着。\n<空间 角色=\"示例甲\">\n她在书房｜他在主卧\n</空间>\n然后抬起了头。"
    out = render.sanitize_outbound_text(full)
    assert "<空间" not in out and "她在看书" in out and "然后抬起了头。" in out
    assert "主卧" not in out               # 块内内容整块剥除
    assert out.count("书房") == 1          # 块外普通句子的词保留，块内的删
    assert render.sanitize_outbound_text("好。</空间>") == "好。"
    assert render.sanitize_outbound_text("看看＜空间＞标签") == "看看标签"
    assert render.sanitize_outbound_text("正常聊天") == "正常聊天"


# ---- ETA 分档边界锁定（回归锁：默认阈值 15/60 秒） ----

@pytest.mark.parametrize("secs,band", [
    (None, "得走一阵"),
    (0, "马上就到"),
    (15, "马上就到"),        # 上界含
    (16, "还要一会儿"),
    (60, "还要一会儿"),      # 上界含
    (61, "得走一阵"),
    (600, "得走一阵"),
])
def test_eta_band_boundaries(secs, band):
    assert render.eta_band(secs) == band


# ---- 0.27.9 面板化：sp_eta_thresholds 参数化 ----

def test_eta_band_custom_thresholds():
    th = (30, 120)
    assert render.eta_band(29, th) == "马上就到"
    assert render.eta_band(30, th) == "马上就到"      # 上界含
    assert render.eta_band(31, th) == "还要一会儿"
    assert render.eta_band(120, th) == "还要一会儿"   # 上界含
    assert render.eta_band(121, th) == "得走一阵"
    assert render.eta_band(None, th) == "得走一阵"    # 未知秒数与阈值无关
    assert render.eta_band(None) == "得走一阵"


def test_render_injection_eta_thresholds_passthrough():
    # 注入阈值贯穿到移动中渲染：剩余 10 秒默认"马上就到"，第一档缩到 5 秒后变"还要一会儿"。
    st = mk_state(she="study", moving={"to": "kitchen", "from": "study", "until_ts": T0 + 10})
    default_text = render.render_injection(st, MANOR, T0)
    assert "（马上就到）" in default_text
    tuned = render.render_injection(st, MANOR, T0, eta_thresholds=(5, 600))
    assert "（还要一会儿）" in tuned and "（马上就到）" not in tuned


@pytest.mark.parametrize("raw,expect", [
    ("15,60", (15, 60)),
    (" 20 , 90 ", (20, 90)),
    ("1,3600", (1, 3600)),
])
def test_parse_eta_thresholds_good(raw, expect):
    assert render.parse_eta_thresholds(raw) == expect


@pytest.mark.parametrize("bad", ["", None, "15", "15,60,90", "a,60", "60,15", "0,60", "15,3601", "15-60"])
def test_parse_eta_thresholds_bad(bad):
    # 非两段/非整数/非升序/越界/分隔符错 → None（调用方回内置默认）
    assert render.parse_eta_thresholds(bad) is None


# ---- 0.27.9 面板化：听觉文案参数化（sp_hearing_*） ----

def test_render_injection_hearing_texts():
    default_text = render.render_injection(mk_state(), MANOR, T0)          # study vs master-bed → 异层
    assert "（异层，隔层难闻声息）" in default_text
    tuned = render.render_injection(mk_state(), MANOR, T0,
                                    hearing_same_floor="楼下隐约有声")
    assert "（异层，楼下隐约有声）" in tuned
    neighbor = render.render_injection(mk_state(he="living"), MANOR, T0)   # study-living 直连 → 隔壁
    assert "他在隔壁客厅（可闻动静）" in neighbor
    # 自定义隔壁文案整体替换括号内容（含"隔壁"语义须自带）
    tuned2 = render.render_injection(mk_state(he="living"), MANOR, T0,
                                     hearing_adjacent="就在隔壁，可闻动静")
    assert "他在客厅（就在隔壁，可闻动静）" in tuned2
    assert "他在隔壁客厅（可闻动静）" not in tuned2
