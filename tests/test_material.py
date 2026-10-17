"""材质触觉层测试：材质库加载、五方式乘数窗口、部位×材质方向、缺省参数、纯函数、接线（events/decay/inject）。

文献锚点（data/materials.json _refs）：CT 1-10cm/s+32°C；Meissner 5-50Hz；Pacinian 100-300Hz；
Weinstein 2PD 指尖 2mm→背 40mm；丝绸纤维 10-20µm；羊毛 ≥30µm 刺痒阈值。
"""
import copy
import json
import pathlib

import pytest

from astrbot_plugin_virtual_body import CONFIG
from astrbot_plugin_virtual_body.core import decay, events, inject, material
from astrbot_plugin_virtual_body.core.state import normalize

PROF_DIR = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "profiles"
DEMO_A = json.loads((PROF_DIR / "role_a.json").read_text(encoding="utf-8"))
PARTS = {p["id"]: p for p in DEMO_A["parts"]}
EAR, NAPE, BACK, PALM = PARTS["ear_tip"], PARTS["nape"], PARTS["back"], PARTS["palm"]   # sens 9/5/3/6
T0 = 1_000_000.0

MATS = material.load_materials()
SILK, WOOL, FEATHER, METAL, VELVET, NYLON, HAIR = (
    MATS["silk"], MATS["wool"], MATS["feather"], MATS["metal"], MATS["velvet"],
    MATS["nylon_rope"], MATS["hair_fur"])


# ---- 材质库 ----

def test_library_loads_and_valid():
    # 12 种材质、字段量级合法、_refs 文献清单非空
    assert len(MATS) == 12
    refs = json.loads((pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "materials.json")
                      .read_text(encoding="utf-8")).get("_refs")
    assert refs and all(r.get("url") for r in refs)
    for m in MATS.values():
        assert m.get("label") and m.get("note")
        assert 0.0 <= float(m["roughness_um"]) <= 500.0
        assert 0.0 <= float(m["thermal"]) <= 1.0
        assert 0.0 <= float(m["ct_affinity"]) <= 1.0
    # 丝绸细（数µm）、羊毛粗（数十µm）、尼龙绳宏观纹理（数百µm）——量级方向对
    assert MATS["silk"]["roughness_um"] < MATS["wool"]["roughness_um"] < MATS["nylon_rope"]["roughness_um"]
    terms = dict(material.material_terms())
    assert "丝绸" in terms and terms["丝绸"] == "silk"
    assert "羊毛" in terms and terms["羊毛"] == "wool"


def test_gain_pure_and_bounded():
    # 纯函数：不改入参、可重复；全域乘数钳在 [0.4, 1.8]
    for part in (EAR, NAPE, BACK, PALM):
        for mat in MATS.values():
            for method in ("stroke", "press", "rub", "tap", "blow", "未知"):
                for params in (None, {}, {"speed_cm_s": 3, "depth": 0.9, "freq_hz": 250, "temp_c": 31}):
                    p0, m0 = copy.deepcopy(part), copy.deepcopy(mat)
                    g1 = material.material_gain(part, mat, method, params)
                    g2 = material.material_gain(part, mat, method, params)
                    assert g1 == g2
                    assert CONFIG.MATERIAL_GAIN_MIN <= g1 <= CONFIG.MATERIAL_GAIN_MAX
                    assert part == p0 and mat == m0


# ---- stroke：CT 速度窗 + 部位×细材质 ----

def test_stroke_ct_velocity_window():
    # 1-10cm/s CT 峰值平台；窗外衰减（慢于 1 静态化、快于 30 明显衰减）
    in_win = material.material_gain(EAR, FEATHER, "stroke", {"speed_cm_s": 5})
    assert in_win == material.material_gain(EAR, FEATHER, "stroke", {"speed_cm_s": 3})   # 窗内平台
    assert in_win > material.material_gain(EAR, FEATHER, "stroke", {"speed_cm_s": 60})   # 过快衰减
    assert in_win > material.material_gain(EAR, FEATHER, "stroke", {"speed_cm_s": 0.2})  # 过慢近静态
    # CT 愉悦度主导：羽毛在窗内 > 金属在窗内（同部位同速度）
    assert in_win > material.material_gain(EAR, METAL, "stroke", {"speed_cm_s": 5})


def test_stroke_silk_region_difference():
    # 区域×材质核心公式：丝绸（粗糙度小）只在指尖/唇级高敏区可辨——ear(sens9) > back(sens3)
    g_ear = material.material_gain(EAR, SILK, "stroke", {"speed_cm_s": 5})
    g_back = material.material_gain(BACK, SILK, "stroke", {"speed_cm_s": 5})
    assert g_ear > g_back
    # 纹理感知阈：sens^1.2 / log10(1+roughness/2)——丝绸在高敏区可辨度数倍于低敏区
    assert material.texture_salience(9, 2) > 3 * material.texture_salience(3, 2)


# ---- press：压深峰 + 高敏区过载 ----

def test_press_depth_peak_and_sens_interaction():
    # 乘数峰在 depth 0.3-0.5；重压与浅压都低于峰
    depths = [i / 20 for i in range(1, 21)]   # 0.05..1.0
    gs = [material.material_gain(EAR, MATS["cotton"], "press", {"depth": d}) for d in depths]
    peak_d = depths[gs.index(max(gs))]
    assert 0.25 <= peak_d <= 0.55
    assert max(gs) > material.material_gain(EAR, MATS["cotton"], "press", {"depth": 1.0})
    assert max(gs) > material.material_gain(EAR, MATS["cotton"], "press", {"depth": 0.05})
    # 高敏区轻压即强（峰值偏浅）、重压过载感更重：sens9 重压 < sens3 重压
    g9 = material.material_gain(EAR, MATS["cotton"], "press", {"depth": 1.0})
    g3 = material.material_gain(BACK, MATS["cotton"], "press", {"depth": 1.0})
    assert g9 < g3


# ---- rub：Meissner 频带 + 粗糙度×区域双向 ----

def test_rub_meissner_band_and_roughness_direction():
    # 频率落 5-50Hz（Meissner 颤动带）增益高于带外
    g_in = material.material_gain(PALM, MATS["cotton"], "rub", {"freq_hz": 20})
    assert g_in > material.material_gain(PALM, MATS["cotton"], "rub", {"freq_hz": 200})
    assert g_in > material.material_gain(PALM, MATS["cotton"], "rub", {"freq_hz": 1})
    # 粗糙度×区域：羊毛高敏区刺痒乘数降、低敏区信息量增——颈部刺感 > 背部（乘数反向单调）
    g_ear = material.material_gain(EAR, WOOL, "rub", {"freq_hz": 20})
    g_nape = material.material_gain(NAPE, WOOL, "rub", {"freq_hz": 20})
    g_back = material.material_gain(BACK, WOOL, "rub", {"freq_hz": 20})
    assert g_ear < g_nape < g_back


# ---- tap / blow ----

def test_tap_pacinian_band_and_region():
    # Pacinian 100-300Hz 振动放大只在高敏末梢（sens 代理）：ear 在带内 > 带外；ear > back 同带内
    g_in = material.material_gain(EAR, METAL, "tap", {"freq_hz": 200})
    assert g_in > material.material_gain(EAR, METAL, "tap", {"freq_hz": 50})
    assert g_in > material.material_gain(BACK, METAL, "tap", {"freq_hz": 200})


def test_blow_temperature_peak():
    # 32°C 附近峰值（CT 对肤温调谐，Ackerley 2014）：冷风/烫风都更低
    g_32 = material.material_gain(NAPE, FEATHER, "blow", {"temp_c": 32})
    assert g_32 > material.material_gain(NAPE, FEATHER, "blow", {"temp_c": 12})
    assert g_32 > material.material_gain(NAPE, FEATHER, "blow", {"temp_c": 48})


# ---- 缺省参数 / 方式映射 / 开关 ----

def test_default_params_and_method_mapping():
    # 缺省参数：speed 5 / depth 0.4 / rub freq 8 / tap freq 200 / blow temp 33；未知方式回 stroke
    assert material.material_gain(EAR, SILK, "stroke") == \
        material.material_gain(EAR, SILK, "stroke", {"speed_cm_s": 5})
    assert material.material_gain(EAR, MATS["cotton"], "press") == \
        material.material_gain(EAR, MATS["cotton"], "press", {"depth": 0.4})
    assert material.material_gain(PALM, MATS["cotton"], "rub") == \
        material.material_gain(PALM, MATS["cotton"], "rub", {"freq_hz": 8})
    assert material.material_gain(EAR, METAL, "tap") == \
        material.material_gain(EAR, METAL, "tap", {"freq_hz": 200})
    assert material.material_gain(NAPE, FEATHER, "blow") == \
        material.material_gain(NAPE, FEATHER, "blow", {"temp_c": 33})
    assert material.material_gain(EAR, SILK, "乱写") == material.material_gain(EAR, SILK, "stroke")
    # 事件 type 词 → 方式
    assert material.method_from_word("舔") == "stroke"
    assert material.method_from_word("捏") == "press"
    assert material.method_from_word("揉") == "rub"
    assert material.method_from_word("拍") == "tap"
    assert material.method_from_word("吹") == "blow"
    assert material.method_from_word("touch") == "stroke"
    assert material.method_from_word("") == "stroke"


def test_disabled_switch_bypass(monkeypatch):
    monkeypatch.setattr(CONFIG, "MATERIAL_ENABLED", False)
    assert material.material_gain(EAR, SILK, "stroke", {"speed_cm_s": 5}) == 1.0
    ev = events.extract_events("用丝巾轻抚耳朵", DEMO_A)
    assert ev and "material" not in ev[0]


# ---- 感受词 ----

def test_feel_word_directions():
    assert material.feel_word(EAR, SILK) == "细滑"     # 丝绸在高敏区可辨细滑
    assert material.feel_word(BACK, SILK) == "光滑"    # 低敏区只剩笼统光滑
    assert material.feel_word(NAPE, WOOL) == "刺痒"    # 羊毛颈部可觉刺
    assert material.feel_word(BACK, WOOL) == "温软"    # 羊毛在背部只觉温软
    assert material.feel_word(BACK, METAL) == "冰凉"   # 高热导优先
    assert material.feel_word(NAPE, VELVET) == "绒软"
    assert material.feel_word(NAPE, HAIR) == "绒软"    # 柔性毛发顺应性抵消刺痒
    assert material.feel_word(BACK, NYLON) == "粗粝"
    assert material.feel_word(EAR, None) == ""


# ---- 接线：events 材质识别 ----

def test_events_material_extraction():
    # 就近配对：材质词落在部位词窗口内 → 事件挂 material
    assert events.extract_events("用丝巾轻抚耳朵", DEMO_A) == [
        {"part": "ear_tip", "s": 0.4, "type": "touch", "material": "silk"}]
    assert events.extract_events("羊毛毯裹着，他揉了揉她的背", DEMO_A) == [
        {"part": "back", "s": 0.6, "type": "touch", "material": "wool"}]
    # 远处材质不算（跨句/不同物体）
    assert events.extract_events("羊毛毯叠在柜子最底层，他轻轻拍了拍她的头顶", DEMO_A) == [
        {"part": "head_top", "s": 0.275, "type": "touch"}]
    # 引号内材质词不算（转述/比喻）
    assert events.extract_events("“丝绸般顺滑”，他摸了摸她的头顶", DEMO_A) == [
        {"part": "head_top", "s": 0.5, "type": "touch"}]
    # /touch 第 4 参材质：精确与子串最长命中；认不出则静默忽略不拒指令
    assert events.parse_touch_multi("耳尖+尾巴 0.5 摸 丝绸", DEMO_A) == [
        {"part": "ear_tip", "s": 0.5, "type": "摸", "material": "silk"},
        {"part": "tail_root", "s": 0.5, "type": "摸", "material": "silk"}]
    assert events.parse_touch_multi("后颈 0.5 摸 羊毛毯", DEMO_A) == [
        {"part": "nape", "s": 0.5, "type": "摸", "material": "wool"}]
    assert events.parse_touch_multi("耳尖 0.5 摸 不认识的材质", DEMO_A) == [
        {"part": "ear_tip", "s": 0.5, "type": "摸"}]


# ---- 接线：decay 施加 + inject 渲染 ----

def mk_state(updated_at=T0):
    st = normalize(None, DEMO_A)
    st["updated_at"] = decay.iso_ts(updated_at)
    return st


def test_decay_applies_material_gain():
    # 有材质（丝绸抚摸乘数 >1）体感增量高于无材质；未知材质 id 静默回退
    st1, st2, st3 = mk_state(), mk_state(), mk_state()
    ev_base = {"part": "ear_tip", "s": 0.5, "type": "摸", "t": T0}
    decay.advance_state(st1, DEMO_A, [dict(ev_base, material="silk")], T0)
    decay.advance_state(st2, DEMO_A, [ev_base], T0)
    decay.advance_state(st3, DEMO_A, [dict(ev_base, material="查无此材")], T0)
    assert st1["parts"]["ear_tip"]["value"] > st2["parts"]["ear_tip"]["value"]
    assert abs(st3["parts"]["ear_tip"]["value"] - st2["parts"]["ear_tip"]["value"]) < 1e-9
    # 材质落状态：inject 渲染锚
    assert st1["parts"]["ear_tip"]["last_material"] == "silk"
    assert st1["parts"]["ear_tip"]["last_material_at"] is not None
    assert st2["parts"]["ear_tip"].get("last_material") is None


def test_inject_material_tag_fresh_and_stale():
    # 近期材质事件：部位行 tag 追加"丝绸·细滑"
    st = mk_state()
    decay.advance_state(st, DEMO_A, [{"part": "ear_tip", "s": 0.5, "type": "摸",
                                     "material": "silk", "t": T0}], T0)
    text = inject.render(st, DEMO_A)
    assert "丝绸·细滑" in text
    # 超过 MATERIAL_STALE_SECONDS 不再渲染（防过时信息）；部位行因新事件仍可见
    st2 = mk_state()
    decay.advance_state(st2, DEMO_A, [
        {"part": "ear_tip", "s": 0.5, "type": "摸", "material": "silk", "t": T0},
        {"part": "back", "s": 0.5, "type": "摸", "t": T0 + CONFIG.MATERIAL_STALE_SECONDS + 60},
    ], T0 + CONFIG.MATERIAL_STALE_SECONDS + 60)
    text2 = inject.render(st2, DEMO_A)
    assert "丝绸" not in text2 and "背" in text2
