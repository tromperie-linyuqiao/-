"""0.29.0 参数体系深化测试：动作词参数化（旧格式兼容/新格式解析/type 倍率/词级冷却）
与材质参数扩展（四参数中性不变/非中性生效/method_gains 覆盖/归一化）。

锚点：friction/thickness/warm_hold/pattern_mm/method_gains 公式见 core/material.py 与
CONFIG.MATERIAL_* 常量；type 倍率见 CONFIG.TYPE_* 与 decay.type_gain_mult。
"""
import copy
import json
import pathlib

import pytest

from astrbot_plugin_virtual_body import CONFIG
from astrbot_plugin_virtual_body.core import decay, events, material
from astrbot_plugin_virtual_body.core.state import normalize, validate_action_words

PROF_DIR = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "profiles"
DEMO_A = json.loads((PROF_DIR / "role_a.json").read_text(encoding="utf-8"))
PARTS = {p["id"]: p for p in DEMO_A["parts"]}
EAR, BACK = PARTS["ear_tip"], PARTS["back"]   # sens 9 / 3（高敏/低敏代表）
T0 = 1_000_000.0

MATS = material.load_materials()
SILK, COTTON, METAL = MATS["silk"], MATS["cotton"], MATS["metal"]


def mk_state(updated_at=T0):
    st = normalize(None, DEMO_A)
    st["updated_at"] = decay.iso_ts(updated_at)
    return st


def mk_profile(action_words):
    return {"id": "t", "parts": DEMO_A["parts"], "tiers": DEMO_A["tiers"],
            "action_words": action_words}


# ---- 旧格式兼容：数字形态与 {"s": v} 逐位等价，行为与 0.28.1 一致 ----

def test_legacy_number_format_bit_identical():
    # 同一文本：profile 数字形态 vs dict 形态 vs 无 profile（全局表）——事件逐位一致
    p_num = mk_profile({"揉": 0.6})
    p_dict = mk_profile({"揉": {"s": 0.6}})
    for text in ("揉揉耳朵", "狠狠揉耳朵", "别揉耳朵"):
        assert events.extract_events(text, p_num) == events.extract_events(text, p_dict)
    # 旧词表（无任何参数键）事件形态不变：只有 part/s/type 三键
    ev = events.extract_events("揉揉耳朵", DEMO_A)
    assert ev == [{"part": "ear_tip", "s": 0.6, "type": "touch"}]
    # 演算端：数字形态与 dict 形态推进结果逐位一致
    st1, st2 = mk_state(), mk_state()
    decay.advance_state(st1, p_num, [{"part": "ear_tip", "s": 0.6, "type": "touch", "t": T0}], T0)
    decay.advance_state(st2, p_dict, [{"part": "ear_tip", "s": 0.6, "type": "touch", "t": T0}], T0)
    assert st1["parts"]["ear_tip"]["value"] == st2["parts"]["ear_tip"]["value"]


def test_action_words_normalization():
    # CONFIG 全局词归一为 {"s": v}；profile 覆盖/追加/禁用/坏值路径
    words = events._action_words(mk_profile({
        "薅": 0.7,                                   # 数字追加
        "揉": {"s": 0.75, "method": "rub"},          # dict 覆盖
        "摸": 0,                                     # 数字 0 = 禁用
        "拍": {"s": 0},                              # dict s=0 = 禁用（同语义）
        "捏": {"s": 2.0},                            # s 越界 = 坏值丢弃（保留全局 0.7）
        "扯": "很用力",                              # 非数字非 dict = 丢弃（词不存在，无全局可留）
    }))
    assert words["揉"] == {"s": 0.75, "method": "rub"}
    assert words["薅"] == {"s": 0.7}
    assert "摸" not in words and "拍" not in words    # 两形态的显式禁用都生效
    assert words["捏"] == {"s": 0.7}                  # 坏 dict 丢弃后保留全局值
    assert "扯" not in words
    # dict 内坏键丢弃、好键保留（NaN/越界/未知枚举）
    w = events._action_words(mk_profile({
        "电": {"s": 0.5, "method": "乱写", "type": "痛", "freq_hz": 999, "speed_cm_s": 5.0,
               "cooldown_s": 30, "temp_c": 32.0, "depth": 0.5}}))[ "电"]
    assert w == {"s": 0.5, "speed_cm_s": 5.0, "cooldown_s": 30, "temp_c": 32.0, "depth": 0.5}


def test_new_format_params_attached_to_event():
    # 词级参数命中时附到事件；缺省键不落（事件最小形态）
    prof = mk_profile({"电": {"s": 0.5, "method": "tap", "freq_hz": 200, "type": "pain",
                             "cooldown_s": 30},
                       "薅": {"s": 0.6}})
    assert events.extract_events("电了一下耳朵", prof) == [
        {"part": "ear_tip", "s": 0.5, "type": "pain", "method": "tap", "freq_hz": 200.0,
         "cooldown_s": 30.0, "word": "电"}]
    ev = events.extract_events("薅了一把耳朵", prof)
    assert ev == [{"part": "ear_tip", "s": 0.6, "type": "touch"}]   # 只带 s 的 dict = 无附加键
    # 强度修饰词照样乘算（参数词也吃"轻轻"×0.55）
    ev2 = events.extract_events("轻轻电了下耳朵", prof)
    assert ev2[0]["s"] == pytest.approx(0.5 * 0.55, abs=1e-4)


def test_event_method_and_params_feed_material_gain():
    # 事件级 method/freq 直进 material_gain：同材质下 揉搓带内频率 > 带外频率
    def run(method, freq):
        st = mk_state()
        decay.advance_state(st, DEMO_A, [
            {"part": "ear_tip", "s": 0.5, "type": "摸", "material": "silk",
             "method": method, "freq_hz": freq, "t": T0}], T0)
        return st["parts"]["ear_tip"]["value"]
    assert run("rub", 20.0) > run("rub", 250.0)     # Meissner 5-50Hz 带内满增益、带外缓降
    # 事件无 method 时按 type 词映射（"揉"→rub）——旧行为不变
    st_old = mk_state()
    decay.advance_state(st_old, DEMO_A, [
        {"part": "ear_tip", "s": 0.5, "type": "揉", "material": "silk", "t": T0}], T0)
    st_new = mk_state()
    decay.advance_state(st_new, DEMO_A, [
        {"part": "ear_tip", "s": 0.5, "type": "摸", "material": "silk",
         "method": "rub", "freq_hz": 8.0, "t": T0}], T0)
    # 显式 rub@8Hz（core 缺省频率）与按 type 词映射（"揉"→rub→缺省 8Hz）逐位一致——兼容锚
    assert st_new["parts"]["ear_tip"]["value"] == st_old["parts"]["ear_tip"]["value"]


# ---- type 语义类别倍率（默认值下旧词表全 touch，行为不变） ----

def test_type_pain_low_sens_mult():
    # 痛类对 sens≤4 低敏部位 ×1.2（痛觉广谱受体）；高敏部位不加成
    def run(part, etype):
        st = mk_state()
        decay.advance_state(st, DEMO_A, [{"part": part["id"], "s": 0.5, "type": etype, "t": T0}], T0)
        return st["parts"][part["id"]]["value"]
    assert run(BACK, "pain") == pytest.approx(run(BACK, "touch") * CONFIG.TYPE_PAIN_LOW_SENS_MULT, rel=1e-9)
    assert run(EAR, "pain") == pytest.approx(run(EAR, "touch"), rel=1e-9)


def test_type_temp_thermal_modulation():
    # 温度类受材质 thermal 调制放大：×(1+0.4×thermal)；无材质不放大
    def run(etype, mat=None):
        ev = {"part": "ear_tip", "s": 0.5, "type": etype, "t": T0}
        if mat:
            ev["material"] = mat
        st = mk_state()
        decay.advance_state(st, DEMO_A, [ev], T0)
        return st["parts"]["ear_tip"]["value"]
    metal = material.get_material("metal")   # thermal 0.95
    expect = run("touch", "metal") * (1.0 + CONFIG.TYPE_TEMP_THERMAL_COEFF * 0.95)
    assert run("temp", "metal") == pytest.approx(expect, rel=1e-9)
    assert run("temp") == pytest.approx(run("touch"), rel=1e-9)   # 无材质：恒 1.0


def test_type_air_exposed_mult():
    # 气流类对 flags 含 exposed 的暴露部位 ×1.1；无 flag 不加成
    prof = copy.deepcopy(DEMO_A)
    for p in prof["parts"]:
        if p["id"] == "ear_tip":
            p["flags"] = ["instinct", "exposed"]
    st1, st2, st3 = mk_state(), mk_state(), mk_state()
    decay.advance_state(st1, prof, [{"part": "ear_tip", "s": 0.5, "type": "air", "t": T0}], T0)
    decay.advance_state(st2, prof, [{"part": "ear_tip", "s": 0.5, "type": "touch", "t": T0}], T0)
    decay.advance_state(st3, DEMO_A, [{"part": "ear_tip", "s": 0.5, "type": "air", "t": T0}], T0)
    assert st1["parts"]["ear_tip"]["value"] == pytest.approx(
        st2["parts"]["ear_tip"]["value"] * CONFIG.TYPE_AIR_EXPOSED_MULT, rel=1e-9)
    assert st3["parts"]["ear_tip"]["value"] == pytest.approx(st2["parts"]["ear_tip"]["value"], rel=1e-9)


def test_type_gain_mult_pure_function():
    # 纯函数口径：touch/constrain 恒 1.0；未知 type 恒 1.0（旧词表自定义 type 词安全）
    assert decay.type_gain_mult("touch", EAR, SILK) == 1.0
    assert decay.type_gain_mult("constrain", BACK, METAL) == 1.0
    assert decay.type_gain_mult("乱写的类型", EAR, None) == 1.0
    assert decay.type_gain_mult("pain", EAR, None) == 1.0     # 高敏部位不吃痛觉加成
    assert decay.type_gain_mult("pain", {"sens": 4}, None) == CONFIG.TYPE_PAIN_LOW_SENS_MULT
    assert decay.type_gain_mult("air", {"sens": 5, "flags": ["exposed"]}, None) == CONFIG.TYPE_AIR_EXPOSED_MULT
    assert decay.type_gain_mult("air", {"sens": 5, "flags": ["instinct"]}, None) == 1.0


# ---- 词级冷却 ----

def test_word_cooldown_halves_repeat_gain():
    # 同词冷却内命中：本事件增益 ×0.5（只削增益不削耐受）；无冷却词不受影响
    ev = {"part": "ear_tip", "s": 0.5, "type": "摸", "word": "戳", "cooldown_s": 30.0, "t": T0}
    st_cd = mk_state()
    st_cd["word_cd"] = {"戳": decay.iso_ts(T0 + 30)}   # 预置：冷却中
    decay.advance_state(st_cd, DEMO_A, [dict(ev)], T0)
    st_full = mk_state()
    decay.advance_state(st_full, DEMO_A, [dict(ev)], T0)
    assert st_cd["parts"]["ear_tip"]["value"] == pytest.approx(
        CONFIG.ACTION_WORD_CD_FACTOR * st_full["parts"]["ear_tip"]["value"], rel=1e-9)
    assert st_cd["tolerance"] == st_full["tolerance"]   # 耐不受词冷却影响
    # 命中后账本更新：until = T0 + 30
    assert decay.parse_ts(st_cd["word_cd"]["戳"]) == pytest.approx(T0 + 30.0)
    # 冷却过后（T0+31 > until）再命中恢复全增益
    st_late = copy.deepcopy(st_full)
    st_late["word_cd"] = {"戳": decay.iso_ts(T0 + 30)}
    decay.advance_state(st_late, DEMO_A, [dict(ev, t=T0 + 31)], T0 + 31)
    v_gain = st_late["parts"]["ear_tip"]["value"]
    assert v_gain > 0   # 冷却外不折（数值对比见下：与无冷却账本一致）
    st_none = copy.deepcopy(st_full)
    decay.advance_state(st_none, DEMO_A, [dict(ev, t=T0 + 31)], T0 + 31)
    assert st_late["parts"]["ear_tip"]["value"] == pytest.approx(st_none["parts"]["ear_tip"]["value"], rel=1e-9)


def test_word_cooldown_default_off_and_state_normalize():
    # 无 cooldown_s 的词（旧行为）：不落 word_cd 账本、不衰减增益
    st = mk_state()
    decay.advance_state(st, DEMO_A, [
        {"part": "ear_tip", "s": 0.5, "type": "摸", "t": T0}], T0)
    assert st.get("word_cd") == {}
    # normalize 保留账本、清洗坏值（词截 32 字符、非字符串终点丢弃、条目上限 200）
    raw = {"word_cd": {"戳": decay.iso_ts(T0 + 30), "坏": "", "长" * 40: decay.iso_ts(T0), "x" * 33: decay.iso_ts(T0)}}
    st2 = normalize(raw, DEMO_A)
    assert st2["word_cd"]["戳"] == decay.iso_ts(T0 + 30)
    assert len(st2["word_cd"]) == 3   # 空终点丢弃；超长词截 32 字符（"长"*40 与 "x"*33 截后仍互异）


# ---- 材质参数扩展 ----

def test_material_neutral_defaults_bit_identical():
    # 中性缺省（无新键）与显式中性（friction 0.5/thickness 0/warm_hold 0/pattern 0/空 method_gains）逐位一致
    neutral = {"friction": 0.5, "thickness": 0.0, "warm_hold": 0.0, "pattern_mm": 0.0, "method_gains": {}}
    for part in (EAR, BACK, PARTS["palm"], PARTS["nape"]):
        for mat in (SILK, COTTON, METAL, MATS["wool"], MATS["feather"], MATS["nylon_rope"]):
            for method in ("stroke", "press", "rub", "tap", "blow"):
                for params in (None, {"speed_cm_s": 5, "depth": 0.4, "freq_hz": 200, "temp_c": 40}):
                    base = material.material_gain(part, mat, method, params)
                    ext = material.material_gain(part, {**mat, **neutral}, method, params)
                    assert base == ext


def test_material_friction_rub():
    # rub ×(0.7+friction×0.6)：高摩擦放大、低摩擦抑制、非 rub 方式不受影响
    def gain(mat):
        return material.material_gain(PARTS["palm"], mat, "rub", {"freq_hz": 20})
    hi = material.material_gain(PARTS["palm"], {**COTTON, "friction": 1.0}, "rub", {"freq_hz": 20})
    lo = material.material_gain(PARTS["palm"], {**COTTON, "friction": 0.0}, "rub", {"freq_hz": 20})
    assert hi > gain(COTTON) > lo
    assert hi == pytest.approx(min(CONFIG.MATERIAL_GAIN_MAX,
        gain(COTTON) * (0.7 + 0.6 * 1.0)), rel=1e-9)
    assert lo == pytest.approx(max(CONFIG.MATERIAL_GAIN_MIN,
        gain(COTTON) * (0.7 + 0.6 * 0.0)), rel=1e-9)
    # stroke 不吃摩擦参数
    assert material.material_gain(EAR, {**SILK, "friction": 1.0}, "stroke", {"speed_cm_s": 5}) == \
        material.material_gain(EAR, SILK, "stroke", {"speed_cm_s": 5})


def test_material_thickness_damps_tap():
    # tap ×(1-thickness×0.4)：厚材质削弱高频拍打；非 tap 不受影响
    base = material.material_gain(EAR, COTTON, "tap", {"freq_hz": 200})
    thick = material.material_gain(EAR, {**COTTON, "thickness": 0.5}, "tap", {"freq_hz": 200})
    full = material.material_gain(EAR, {**COTTON, "thickness": 1.0}, "tap", {"freq_hz": 200})
    assert thick == pytest.approx(max(CONFIG.MATERIAL_GAIN_MIN, base * (1 - 0.4 * 0.5)), rel=1e-9)
    assert full == pytest.approx(max(CONFIG.MATERIAL_GAIN_MIN, base * (1 - 0.4)), rel=1e-9)
    assert material.material_gain(EAR, {**COTTON, "thickness": 1.0}, "press", {"depth": 0.4}) == \
        material.material_gain(EAR, COTTON, "press", {"depth": 0.4})


def test_material_warm_hold_widens_blow_window():
    # 保温让温差衰减变慢：远离 32°C 的吹气在保温材质上更温和（comfort 更高→增益更高）
    g0 = material.material_gain(PARTS["nape"], MATS["feather"], "blow", {"temp_c": 48})
    g1 = material.material_gain(PARTS["nape"], {**MATS["feather"], "warm_hold": 0.9}, "blow", {"temp_c": 48})
    assert g1 > g0
    # 峰温处（32°C）两者同值（comfort 已顶格，窗宽无差别）
    assert material.material_gain(PARTS["nape"], {**MATS["feather"], "warm_hold": 0.9}, "blow", {"temp_c": 32}) == \
        material.material_gain(PARTS["nape"], MATS["feather"], "blow", {"temp_c": 32})


def test_material_pattern_mm_pacinian_interaction():
    # 粗纹理（>2mm）强化 tap：×(1+0.25×clamp01((mm-2)/10))；≤2mm 恒中性
    base = material.material_gain(EAR, METAL, "tap", {"freq_hz": 200})
    assert material.material_gain(EAR, {**METAL, "pattern_mm": 2.0}, "tap", {"freq_hz": 200}) == base
    p12 = material.material_gain(EAR, {**METAL, "pattern_mm": 12.0}, "tap", {"freq_hz": 200})
    assert p12 == pytest.approx(min(CONFIG.MATERIAL_GAIN_MAX, base * 1.25), rel=1e-9)
    p99 = material.material_gain(EAR, {**METAL, "pattern_mm": 99.0}, "tap", {"freq_hz": 200})
    assert p99 == pytest.approx(min(CONFIG.MATERIAL_GAIN_MAX, base * 1.25), rel=1e-9)   # 归一上限
    # 纹理不作用于 rub
    assert material.material_gain(PARTS["palm"], {**COTTON, "pattern_mm": 12.0}, "rub", {"freq_hz": 20}) == \
        material.material_gain(PARTS["palm"], COTTON, "rub", {"freq_hz": 20})


def test_material_method_gains_override():
    # 材质级方式乘数：只覆盖指定方式（羽毛 tap 放大 1.3、皮革 rub 抑制 0.8）；越界值钳 0.4-1.8
    feather = MATS["feather"]
    tap_base = material.material_gain(EAR, feather, "tap", {"freq_hz": 200})
    tap_up = material.material_gain(EAR, {**feather, "method_gains": {"tap": 1.3}}, "tap", {"freq_hz": 200})
    assert tap_up == pytest.approx(min(CONFIG.MATERIAL_GAIN_MAX, tap_base * 1.3), rel=1e-9)
    # 未覆盖方式不受影响
    assert material.material_gain(EAR, {**feather, "method_gains": {"tap": 1.3}}, "stroke", {"speed_cm_s": 5}) == \
        material.material_gain(EAR, feather, "stroke", {"speed_cm_s": 5})
    # 值域外钳制（web 层拦 400，core 层兜底钳制）
    assert material.material_gain(EAR, {**feather, "method_gains": {"tap": 99.0}}, "tap", {"freq_hz": 200}) == \
        min(CONFIG.MATERIAL_GAIN_MAX, tap_base * CONFIG.MATERIAL_METHOD_GAIN_RANGE[1])
    # 坏值（非数值）当 1.0
    assert material.material_gain(EAR, {**feather, "method_gains": {"tap": "高"}}, "tap", {"freq_hz": 200}) == tap_base


def test_validate_action_words_two_forms():
    # 校验层：数字形态照旧；dict 形态结构校验（s/method/type/参数值域）
    ok, err = validate_action_words({"揉": 0.6, "薅": {"s": 0.7, "method": "rub", "freq_hz": 10,
                                                       "cooldown_s": 30, "type": "pain"}})
    assert ok, err
    assert not validate_action_words({"x": {"s": 1.5}})[0]           # s 越界
    assert not validate_action_words({"x": {"method": "乱写"}})[0]    # 未知方式
    assert not validate_action_words({"x": {"type": "魔法"}})[0]      # 未知类别
    assert not validate_action_words({"x": {"freq_hz": 999}})[0]     # 参数越界
    assert not validate_action_words({"x": [1, 2]})[0]                # 数组既非数字也非 dict
    assert validate_action_words({"x": {"s": 0}})[0]                  # s=0 允许（禁用语义）


def test_full_chain_profile_word_params_to_value():
    # 全链路：profile 词参数 → 文本抽取 → decay（材质×方式×参数×类别）→ 数值可区分
    prof = mk_profile({"梳": {"s": 0.5, "method": "stroke", "speed_cm_s": 3, "temp_c": 32}})
    evs = events.extract_events("用丝巾梳了梳她的耳朵", prof)
    assert evs and evs[0]["material"] == "silk" and evs[0]["method"] == "stroke"
    assert evs[0]["speed_cm_s"] == 3.0
    st = mk_state()
    decay.advance_state(st, prof, evs and [dict(evs[0], t=T0)], T0)
    assert st["parts"]["ear_tip"]["value"] > 0
    # 与带外速度（60cm/s 快扫衰减）对比：3cm/s（CT 峰值平台）更强
    evs_fast = [dict(evs[0], speed_cm_s=60.0, t=T0)]
    st_fast = mk_state()
    decay.advance_state(st_fast, prof, evs_fast, T0)
    assert st["parts"]["ear_tip"]["value"] > st_fast["parts"]["ear_tip"]["value"]


# ---- 0.29.1 部位级细调参数：五键缺省中性（行为逐位不变）+ 非中性生效 + 坏值回中性 ----

def _part_profile(pid, **extra):
    prof = copy.deepcopy(DEMO_A)
    prof["parts"] = [dict(p, **extra) if p["id"] == pid else dict(p) for p in prof["parts"]]
    return prof


def _one_touch(profile, s=0.3, ev_type="touch", mat=None, t=T0):
    st = mk_state(t)
    ev = {"part": "ear_tip", "s": s, "type": ev_type, "t": t}
    if mat:
        ev["material"] = mat
    decay.advance_state(st, profile, [ev], t)
    return st


def test_part_fine_tuning_neutral_identity():
    # 五键全部中性（1.0 / 空 dict）与不带键：全状态逐位一致（含 tolerance/sens_boost/history）
    neutral = _part_profile("ear_tip", gain_mult=1.0, tolerance_mult=1.0, windup_mult=1.0,
                            type_mults={}, material_affinity={})
    evs = [{"part": "ear_tip", "s": 0.5, "type": "pain", "t": T0},
           {"part": "ear_tip", "s": 0.4, "type": "touch", "t": T0 + 10}]
    a, b = mk_state(), mk_state()
    decay.advance_state(a, DEMO_A, copy.deepcopy(evs), T0 + 60)
    decay.advance_state(b, neutral, copy.deepcopy(evs), T0 + 60)
    assert a == b


def test_part_gain_mult_doubles_direct_gain():
    a = _one_touch(DEMO_A, s=0.3)
    b = _one_touch(_part_profile("ear_tip", gain_mult=2.0), s=0.3)
    assert abs(b["parts"]["ear_tip"]["value"] - 2 * a["parts"]["ear_tip"]["value"]) < 1e-9


def test_part_gain_mult_applies_to_radiate_target():
    # 传导落点用自己的 gain_mult：源部位无键、tail_root 2 倍 → 传导增量 ×2
    a = _one_touch(DEMO_A, s=0.5)
    b = _one_touch(_part_profile("tail_root", gain_mult=2.0), s=0.5)
    ra, rb = a["parts"]["tail_root"]["value"], b["parts"]["tail_root"]["value"]
    assert ra > 0 and abs(rb - 2 * ra) < 1e-9


def test_part_tolerance_mult_doubles_accumulation():
    a = _one_touch(DEMO_A, s=0.5)
    b = _one_touch(_part_profile("ear_tip", tolerance_mult=2.0), s=0.5)
    assert a["tolerance"] > 0 and abs(b["tolerance"] - 2 * a["tolerance"]) < 1e-9


def test_part_windup_mult_doubles_sens_boost():
    evs = [{"part": "ear_tip", "s": 0.5, "t": T0}, {"part": "ear_tip", "s": 0.5, "t": T0 + 10}]
    a, b = mk_state(), mk_state()
    decay.advance_state(a, DEMO_A, copy.deepcopy(evs), T0 + 10)
    decay.advance_state(b, _part_profile("ear_tip", windup_mult=2.0), copy.deepcopy(evs), T0 + 10)
    ba = a["parts"]["ear_tip"]["sens_boost"]
    bb = b["parts"]["ear_tip"]["sens_boost"]
    assert ba > 0 and abs(bb - 2 * ba) < 1e-6


def test_part_type_mults_category_interaction():
    # pain 类 ×1.5 生效；未列出的 touch 类不受影响
    a = _one_touch(DEMO_A, s=0.4, ev_type="pain")
    b = _one_touch(_part_profile("ear_tip", type_mults={"pain": 1.5}), s=0.4, ev_type="pain")
    assert abs(b["parts"]["ear_tip"]["value"] - 1.5 * a["parts"]["ear_tip"]["value"]) < 1e-9
    c = _one_touch(_part_profile("ear_tip", type_mults={"pain": 1.5}), s=0.4, ev_type="touch")
    d = _one_touch(DEMO_A, s=0.4, ev_type="touch")
    assert abs(c["parts"]["ear_tip"]["value"] - d["parts"]["ear_tip"]["value"]) < 1e-9


def test_part_material_affinity():
    a = _one_touch(DEMO_A, s=0.3, mat="silk")
    b = _one_touch(_part_profile("ear_tip", material_affinity={"silk": 1.4}), s=0.3, mat="silk")
    assert a["parts"]["ear_tip"]["value"] > 0
    assert abs(b["parts"]["ear_tip"]["value"] - 1.4 * a["parts"]["ear_tip"]["value"]) < 1e-9


def test_part_fine_tuning_bad_values_neutral():
    # 越界/坏值全部回中性（演算层防御）：与无键行为逐位一致
    bad = _part_profile("ear_tip", gain_mult=9.0, tolerance_mult=0.0, windup_mult=-1,
                        type_mults={"pain": 99}, material_affinity={"silk": "x"})
    evs = [{"part": "ear_tip", "s": 0.5, "type": "pain", "t": T0, "material": "silk"}]
    a, b = mk_state(), mk_state()
    decay.advance_state(a, DEMO_A, copy.deepcopy(evs), T0)
    decay.advance_state(b, bad, copy.deepcopy(evs), T0)
    assert a == b
