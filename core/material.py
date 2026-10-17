"""材质触觉层：材质 × 部位 × 接触方式 × 频率 → 刺激乘数（纯函数，不 import astrbot）。

文献锚点（数字出处详见 data/materials.json 的 _refs）：
- CT 情感触觉纤维：1-10 cm/s 慢扫峰值、肤温 ~32°C 响应峰（Löken/Pawling 系；Ackerley 2014）
- Meissner FA1：动态轻触/颤动 5-50 Hz（Johnson 2001）
- Merkel SA1：静态压/形状 0.4-4 Hz（Vallbo/Johansson 微神经图）
- Pacinian FAII：高频振动 100-300 Hz 峰（40-500 Hz 带）
- 两点辨别阈：指尖 ~2mm → 背 ~40mm（Weinstein 1968 系），本插件 sens 即其对数映射
- 织物：丝绸纤维 10-20µm、羊毛 11-40µm（≥30µm 刺痒阈值）、纹理 <~100µm 走时间编码

区域×材质核心公式（用户点名）——"纹理感知阈"：
    texture_salience = (sens/10)^1.2 / log10(1 + roughness_um/2)
    · 分子是部位敏锐度（2PD 对数映射的幂律化，1.2 次方拉开高敏区优势）
    · 分母是材质粗糙度的对数（丝绸 2µm→0.30、羊毛 45µm→1.39）
    ⇒ 丝绸（粗糙度小）只在指尖/唇级高敏区可辨细滑（sens9≈2.9 vs 背 sens3≈0.78）；
      羊毛在各区 salience 都低——粗感受走 prickle（刺痒）通道而非细辨通道。
刺痒通道：prickle_index = min(1,(rough/100)^1.2) × (sens/10)^1.5 × (1-0.7×ct_affinity)
    （羊毛系"≥30µm 纤维占比>5% 即痒"的连续化；柔性高 CT 材质被顺应性抵消大半）
"""
from __future__ import annotations

import json
import math
import pathlib
from typing import Any

from .. import CONFIG

# 接触方式词表：事件 type（动作词/指令第三参）→ 方式；未命中回 stroke（动态滑移是默认触碰形态）
METHOD_WORDS: dict[str, tuple[str, ...]] = {
    "stroke": ("摸", "抚", "拂", "蹭", "顺", "滑", "舔", "吮", "亲", "触", "碰", "touch"),
    "press":  ("按", "压", "掐", "捏", "咬", "抓", "揪", "拽", "顶"),
    "rub":    ("揉", "搓", "挠", "磨", "擦"),
    "tap":    ("拍", "敲", "打", "弹", "叩", "点"),
    "blow":   ("吹", "呼"),
}


def clamp01(x: Any) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if v != v or v in (float("inf"), float("-inf")):
        return 0.0
    return max(0.0, min(1.0, v))


def clamp(x: float, lo: float, hi: float) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return lo
    if not math.isfinite(v):
        return lo
    return max(lo, min(hi, v))


def _sens_of(part: dict[str, Any] | None) -> float:
    """部位 sens（0-10，Weinstein 2PD 对数映射）；坏值/缺省回 0（与 part_gain_coeff 同约定）。"""
    try:
        return clamp(float((part or {}).get("sens") or 0), 0.0, 10.0)
    except (TypeError, ValueError):
        return 0.0


def _num(val: Any, default: float) -> float:
    try:
        v = float(val)
    except (TypeError, ValueError, OverflowError):
        return default
    return v if math.isfinite(v) else default


# ---- 材质库加载（mtime 缓存；失败返回空 dict，调用方降级为"无材质"）----

_MAT_CACHE: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}
_STORAGE_DIR: pathlib.Path | None = None


def configure_storage(data_dir: str | pathlib.Path | None) -> None:
    """Use the host's plugin-data directory without modifying packaged defaults.

    Existing installations keep reading their current packaged library until an
    override is saved. Core remains independent of AstrBot and creates no files.
    """
    global _STORAGE_DIR
    _STORAGE_DIR = pathlib.Path(data_dir) if data_dir else None


def materials_write_path(data_dir: str | pathlib.Path | None = None) -> pathlib.Path:
    directory = pathlib.Path(data_dir) if data_dir else _STORAGE_DIR
    return directory / 'materials.json' if directory is not None else default_materials_path()


def default_materials_path() -> pathlib.Path:
    if _STORAGE_DIR is not None:
        override = _STORAGE_DIR / 'materials.json'
        if override.is_file():
            return override
    return pathlib.Path(__file__).resolve().parents[1] / "data" / "materials.json"


def load_materials(path: str | pathlib.Path | None = None, force: bool = False) -> dict[str, dict[str, Any]]:
    """读 data/materials.json → {id: 材质dict}。mtime 缓存；IO/解析失败返回 {}（不抛）。"""
    p = pathlib.Path(path) if path is not None else default_materials_path()
    key = str(p)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}
    cached = _MAT_CACHE.get(key)
    if cached is not None and not force and cached[0] == mtime:
        return cached[1]
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        mats: dict[str, dict[str, Any]] = {}
        rows = raw.get("materials") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            return {}
        for m in rows:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("id") or "")
            if mid:
                mats[mid] = m
        _MAT_CACHE[key] = (mtime, mats)
        return mats
    except (OSError, ValueError):
        return {}


# ---- 方式曲线参数（0.27.9 面板化）：materials.json 顶层 method_params，缺省回内置默认 ----
# 文献锚点：CT 情感触觉 1-10cm/s 慢扫峰（Löken/Pawling）；Meissner 5-50Hz（Johnson 2001）；
# Pacinian 100-300Hz 峰；肤温 32°C 响应峰（Ackerley 2014）。falloff=快扫衰减区上沿（cm/s）。
_DEFAULT_METHOD_PARAMS: dict[str, dict[str, float]] = {
    "stroke": {"speed_min": 1.0, "speed_max": 10.0, "falloff": 30.0},
    "press": {},
    "rub": {"freq_min": 5.0, "freq_max": 50.0},
    "tap": {"freq_min": 100.0, "freq_max": 300.0},
    "blow": {"peak_temp_c": 32.0},
}
_MP_LIMITS = {   # 参数键 → 合法区间（越界/坏值回内置默认）
    "speed_min": (0.05, 200.0), "speed_max": (0.1, 200.0), "falloff": (0.1, 200.0),
    "freq_min": (0.0, 500.0), "freq_max": (0.0, 500.0), "peak_temp_c": (0.0, 60.0),
}
_METHOD_PARAMS_CACHE: dict[str, tuple[float, dict[str, dict[str, float]]]] = {}


def _copy_default_method_params() -> dict[str, dict[str, float]]:
    return {m: dict(kv) for m, kv in _DEFAULT_METHOD_PARAMS.items()}


def _validate_method_params(raw: Any) -> dict[str, dict[str, float]]:
    """把文件里的 method_params 校验合并到内置默认上（存在则用、缺省回内置）。
    关系约束（min<max、max≤falloff）不满足时整组回内置默认。纯函数。"""
    out = _copy_default_method_params()
    if not isinstance(raw, dict):
        return out
    for method, kv in raw.items():
        base = out.get(str(method))
        if not isinstance(base, dict) or not isinstance(kv, dict):
            continue   # 未知方式/坏结构忽略
        normalized = dict(kv)
        for alias, canonical in (("freq_lo", "freq_min"), ("freq_hi", "freq_max"),
                                 ("speed_lo", "speed_min"), ("speed_hi", "speed_max")):
            if canonical not in normalized and alias in normalized:
                normalized[canonical] = normalized[alias]
        for k, v in normalized.items():
            limits = _MP_LIMITS.get(str(k))
            if limits is None or isinstance(v, bool):
                continue
            try:
                x = float(v)
            except (TypeError, ValueError, OverflowError):
                continue
            if x != x or not limits[0] <= x <= limits[1]:
                continue
            base[str(k)] = x
    for m, kmin, kmax in (("stroke", "speed_min", "speed_max"),
                          ("rub", "freq_min", "freq_max"), ("tap", "freq_min", "freq_max")):
        if not out[m][kmin] < out[m][kmax]:
            out[m][kmin] = _DEFAULT_METHOD_PARAMS[m][kmin]
            out[m][kmax] = _DEFAULT_METHOD_PARAMS[m][kmax]
    if out["stroke"]["speed_max"] > out["stroke"]["falloff"]:
        out["stroke"]["falloff"] = _DEFAULT_METHOD_PARAMS["stroke"]["falloff"]
    return out


def load_method_params(path: str | pathlib.Path | None = None,
                       force: bool = False) -> dict[str, dict[str, float]]:
    """读 materials.json 顶层 method_params → {方式: {参数: 值}}（已校验合并内置默认）。
    mtime 缓存；IO/解析失败返回内置默认副本。返回值只读（调用方不得就地修改）。"""
    p = pathlib.Path(path) if path is not None else default_materials_path()
    key = str(p)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return _copy_default_method_params()
    cached = _METHOD_PARAMS_CACHE.get(key)
    if cached is not None and not force and cached[0] == mtime:
        return cached[1]
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        mp = _validate_method_params(raw.get("method_params") if isinstance(raw, dict) else None)
    except (OSError, ValueError):
        mp = _copy_default_method_params()
    _METHOD_PARAMS_CACHE[key] = (mtime, mp)
    return mp


def get_material(mat_id: str, path: str | pathlib.Path | None = None) -> dict[str, Any] | None:
    return load_materials(path).get(str(mat_id or "")) or None


def material_terms(path: str | pathlib.Path | None = None) -> list[tuple[str, str]]:
    """材质匹配词表 [(词, 材质id)]，长词在前（"羊毛毯"里优先命中长别名，防短词抢配）。"""
    mats = load_materials(path)
    terms: list[tuple[str, str]] = []
    for mid, m in mats.items():
        words = [str(m.get("label") or "")] + [str(a) for a in m.get("aliases") or []]
        for w in words:
            if w and w not in [t for t, _ in terms]:
                terms.append((w, mid))
    terms.sort(key=lambda t: -len(t[0]))
    return terms


def method_from_word(word: str) -> str:
    """事件 type/动作词 → 接触方式（stroke/press/rub/tap/blow）；默认 stroke。"""
    w = str(word or "").strip()
    if not w:
        return "stroke"
    for method, words in METHOD_WORDS.items():
        for kw in words:
            if kw in w:
                return method
    return "stroke"


# ---- 核心公式 ----

def texture_salience(sens: float, roughness_um: float) -> float:
    """纹理感知阈（区域×材质核心公式）：粗糙度可辨度 ∝ sens^1.2 / roughness 对数。
    丝绸(2µm)：分母 0.30——sens9≈2.9、sens3≈0.78（细滑只在高敏区可辨）；
    羊毛(45µm)：分母 1.39——各区都低，粗感受走 prickle 通道。
    依据：SA1 空间编码界限 ~100µm（Johnson & Hsiao）；2PD 部位梯度 2mm→40mm（Weinstein）。"""
    s = clamp(sens, 0.0, 10.0) / 10.0
    r = max(0.5, _num(roughness_um, 10.0))
    return (s ** 1.2) / math.log10(1.0 + r / 2.0)


def prickle_index(sens: float, roughness_um: float, ct_affinity: float = 0.5) -> float:
    """刺痒指数 0-1：粗纤维机械刺入 nociceptor（羊毛 ≥30µm 纤维即痒的连续化）。
    (rough/100)^1.2 × (sens/10)^1.5 × (1-0.7×ct)——柔性高 CT 材质（羽毛/毛发）顺应性抵消大半。"""
    r = clamp(roughness_um, 0.0, 300.0) / 100.0
    s = clamp(sens, 0.0, 10.0) / 10.0
    return min(1.0, r ** 1.2) * (s ** 1.5) * (1.0 - 0.7 * clamp01(ct_affinity))


def _vel_factor(v: float, speed_min: float = 1.0, speed_max: float = 10.0,
                falloff: float = 30.0) -> float:
    """CT 速度调谐窗：[speed_min, speed_max] 峰值平台（Löken 2009/Pawling 2017）；
    慢于 speed_min 退静态压、speed_max..falloff 线性衰减、再快缓降至 0.3 地板。"""
    v = clamp(v, 0.0, 200.0)
    if v <= 0.0:
        return 0.5
    if v < speed_min:
        return 0.5 + 0.5 * (v / speed_min)
    if v <= speed_max:
        return 1.0
    if v <= falloff:
        return 1.0 - 0.3 * (v - speed_max) / (falloff - speed_max)
    return max(0.3, 0.7 - (v - falloff) / 60.0)


def _freq_factor_meissner(f: float, freq_min: float = 5.0, freq_max: float = 50.0) -> float:
    """Meissner [freq_min, freq_max] 带内满增益，带外缓降（Johnson 2001 颤动带）；
    下沿 0.4×min 起缓升、上沿 2×max 止缓降，更远回 0.6 地板。"""
    f = clamp(f, 0.0, 500.0)
    if freq_min <= f <= freq_max:
        return 1.0
    lo_edge = 0.4 * freq_min
    if lo_edge <= f < freq_min:
        return 0.7 + 0.3 * (f - lo_edge) / (freq_min - lo_edge)
    hi_edge = 2.0 * freq_max
    if freq_max < f <= hi_edge:
        return 1.0 - 0.3 * (f - freq_max) / (hi_edge - freq_max)
    return 0.6


def _gain_stroke(sens: float, rough: float, ct: float, speed: float,
                 mp: dict[str, float] | None = None) -> float:
    """抚摸/滑移：CT 纤维主导——速度窗内 ct_affinity 主导增益，窗外衰减；
    细材质在高敏区额外细辨加成（texture_salience，丝绸指尖效应）。mp=方式曲线参数。"""
    p = mp or _DEFAULT_METHOD_PARAMS["stroke"]
    vf = _vel_factor(speed, p.get("speed_min", 1.0), p.get("speed_max", 10.0), p.get("falloff", 30.0))
    salience = min(1.0, texture_salience(sens, rough) / 3.0)
    return 0.7 + 0.8 * ct * vf + 0.15 * salience


def _gain_press(sens: float, th: float, depth: float) -> float:
    """按压：Merkel SA1 静态压通道——最适压深随敏感度左移（高敏区轻压即强：sens9 峰位~0.32），
    重压在敏感区有过载惩罚；凉材质深压更激灵（热导与接触面积同步升）。"""
    d = clamp01(depth)
    d_opt = 0.5 - 0.02 * clamp(sens, 0.0, 10.0)   # sens0→0.50、sens10→0.30（敏感区轻压即强）
    if d <= d_opt:
        curve = 0.6 + 0.4 * (d / d_opt) ** 0.7
    else:
        curve = 1.0 - 0.5 * ((d - d_opt) / (1.0 - d_opt)) ** 1.5
    overload_pen = 0.3 * d * d * (clamp(sens, 0.0, 10.0) / 10.0)
    return 0.6 + 0.5 * curve - overload_pen + 0.25 * th * d


def _rough_adjust(sens: float, rough: float, ct: float) -> float:
    """粗糙度×区域交互（区域×材质公式的 rub 分支）：
    高敏区(sens≥5)：粗糙材质刺痒→乘数降（prickle 越大降越多）；
    低敏区(sens≤4)：粗糙材质反而信息量增→乘数升（钝区要大纹理才有反馈，对数饱和）。"""
    if sens >= 5.0:
        return 1.0 - 0.9 * prickle_index(sens, rough, ct)
    if sens <= 4.0:
        return 1.0 + 0.25 * math.log10(1.0 + max(0.0, rough) / 10.0)
    return 1.0


def _gain_rub(sens: float, rough: float, ct: float, freq: float,
              mp: dict[str, float] | None = None) -> float:
    """揉搓：Meissner 频带内摩擦增益满档；粗糙度按区域双向调整（羊毛高敏刺/低敏增信息）。
    0.29.0 摩擦参数：×(0.7+friction×0.6)——织物-皮肤摩擦系数 µ≈0.5-0.7（Temel 2023 体区测绘），
    低摩擦（冰/丝绸打滑）揉不动、高摩擦（橡胶/皮革涩滞）揉感强；缺省 friction=0.5 恰 ×1.0。"""
    p = mp or _DEFAULT_METHOD_PARAMS["rub"]
    ff = _freq_factor_meissner(freq, p.get("freq_min", 5.0), p.get("freq_max", 50.0))
    return (0.7 + 0.4 * ff) * _rough_adjust(sens, rough, ct) + 0.15 * ct


def _gain_tap(sens: float, ct: float, rough: float, freq: float,
              mp: dict[str, float] | None = None) -> float:
    """拍点：Pacinian [freq_min, freq_max] 振动放大只在指尖/耳后类高敏末梢（sens 代理）；
    硬光滑材质敲击振动更清脆（金属效应）。"""
    p = mp or _DEFAULT_METHOD_PARAMS["tap"]
    if p.get("freq_min", 100.0) <= freq <= p.get("freq_max", 300.0):
        amp = 0.7 + 0.6 * (clamp(sens, 0.0, 10.0) / 10.0) ** 2
    else:
        amp = 0.7
    hardness = clamp01((1.0 - ct) * (1.0 - clamp(rough, 0.0, 300.0) / 300.0))
    return amp * (0.85 + 0.3 * hardness)


def _gain_blow(ct: float, th: float, temp_c: float, peak_temp_c: float = 32.0,
               warm_hold: float = 0.0) -> float:
    """吹气：气流+温度——peak_temp_c（默认 32°C，CT 对肤温调谐，Ackerley 2014）附近峰值；
    凉材质凉气带激灵感。
    0.29.0 保温参数：舒适窗宽 ×(1+0.6×warm_hold)——保温层热阻大、皮肤-气流温差衰减慢
    （热扩散时间常数 ∝ 热阻×热容，传热学常识），羊毛盖毯下的气流比裸 skin 上温和得更久。"""
    t = clamp(temp_c, 0.0, 60.0)
    window = 20.0 * (1.0 + CONFIG.MATERIAL_WARM_HOLD_WINDOW * clamp01(warm_hold))
    comfort = max(0.0, 1.0 - abs(t - float(peak_temp_c)) / window)
    return 0.6 + 0.5 * comfort + 0.15 * ct + 0.1 * th * (1.0 - comfort)


def material_gain(part: dict[str, Any] | None, material: dict[str, Any] | None,
                  method: str, params: dict[str, Any] | None = None) -> float:
    """材质×部位×方式 → 刺激乘数（钳 CONFIG.MATERIAL_GAIN_MIN..MAX，默认 0.4-1.8）。

    part：部位 meta（用 sens）；material：材质 dict（roughness_um/thermal/ct_affinity +
    0.29.0 可选 friction/thickness/warm_hold/pattern_mm/method_gains，缺省即中性值不改现行为）；
    method：stroke/press/rub/tap/blow（未知回 stroke）；
    params：{speed_cm_s, depth, freq_hz, temp_c} 可选覆盖（缺省 5/0.4/方式相关/33）。
    方式曲线窗口（速度窗/频带/温度峰）读 materials.json 顶层 method_params（缺省回内置默认）。

    0.29.0 材质参数乘子（全部 CONFIG 常量锚，公式见各常量注释）：
    · friction 摩擦：rub ×(0.7+friction×0.6)，缺省 0.5 恰中性
    · thickness 厚度：tap ×(1-thickness×0.4)——厚织物衬垫吸收高频振动
    · pattern_mm 纹理间距：>2mm 粗纹理 tap ×(1+0.25×clamp01((mm-2)/10))——宏观纹与 Pacinian 频带交互
    · warm_hold 保温：blow 舒适窗加宽（见 _gain_blow）
    · method_gains：材质级方式乘数覆盖（0.4-1.8，如羽毛 tap 放大、皮革 rub 抑制）
    MATERIAL_ENABLED=False 时恒 1.0（整体旁路）。纯函数：不改入参、无全局副作用。
    """
    if not CONFIG.MATERIAL_ENABLED or not isinstance(material, dict):
        return 1.0
    sens = _sens_of(part)
    rough = max(0.0, _num(material.get("roughness_um"), 10.0))
    ct = clamp01(material.get("ct_affinity"))
    th = clamp01(material.get("thermal"))
    friction = clamp01(_num(material.get("friction"), CONFIG.MATERIAL_FRICTION_NEUTRAL))
    thickness = clamp01(material.get("thickness"))
    warm_hold = clamp01(material.get("warm_hold"))
    pattern_mm = max(0.0, _num(material.get("pattern_mm"), 0.0))
    p = params if isinstance(params, dict) else {}
    mp = load_method_params()
    m = str(method or "stroke")
    if m == "press":
        g = _gain_press(sens, th, _num(p.get("depth"), 0.4))
    elif m == "rub":
        g = _gain_rub(sens, rough, ct, _num(p.get("freq_hz"), 8.0), mp.get("rub"))
        # 摩擦系数乘子：0.7+friction×0.6（缺省 0.5 → ×1.0 中性；量纲 µ 无量纲摩擦系数）
        g *= CONFIG.MATERIAL_FRICTION_RUB_LO + CONFIG.MATERIAL_FRICTION_RUB_SPAN * friction
    elif m == "tap":
        g = _gain_tap(sens, ct, rough, _num(p.get("freq_hz"), 200.0), mp.get("tap"))
        # 厚度衬垫：吸收高频振动 ×(1-thickness×0.4)
        g *= 1.0 - CONFIG.MATERIAL_THICK_TAP_DAMP * thickness
        # 粗纹理交互：>2mm（2PD 上限量级）宏观纹在拍打时产生宏观振动，与 Pacinian 频带叠加强化
        if pattern_mm > CONFIG.MATERIAL_PATTERN_BASE_MM:
            g *= 1.0 + CONFIG.MATERIAL_PATTERN_TAP_COEFF * clamp01(
                (pattern_mm - CONFIG.MATERIAL_PATTERN_BASE_MM) / CONFIG.MATERIAL_PATTERN_SPAN_MM)
    elif m == "blow":
        g = _gain_blow(ct, th, _num(p.get("temp_c"), 33.0),
                       (mp.get("blow") or {}).get("peak_temp_c", 32.0), warm_hold)
    else:   # stroke 及未知方式
        g = _gain_stroke(sens, rough, ct, _num(p.get("speed_cm_s"), 5.0), mp.get("stroke"))
    # 材质级方式乘数覆盖（method_gains）：如羽毛 {"tap":1.3}、皮革 {"rub":0.8}
    mg_map = material.get("method_gains")
    if isinstance(mg_map, dict):
        try:
            mg = float(mg_map.get(m))
        except (TypeError, ValueError):
            mg = 1.0
        if mg == mg:
            g *= clamp(mg, CONFIG.MATERIAL_METHOD_GAIN_RANGE[0], CONFIG.MATERIAL_METHOD_GAIN_RANGE[1])
    return clamp(g, CONFIG.MATERIAL_GAIN_MIN, CONFIG.MATERIAL_GAIN_MAX)


def feel_word(part: dict[str, Any] | None, material: dict[str, Any] | None) -> str:
    """部位×材质 → 2 字感受词（注入 tag 用，控长）。
    优先级：冰凉（高热导）> 刺痒（prickle≥0.08 且 sens≥5，羊毛颈/尖效应）> 细滑（细粗糙度+高敏）>
    绒软（高 CT 柔性，羽毛/天鹅绒/毛发）> 光滑（细材质在低敏区只剩笼统感）> 温软（低敏区粗织物只剩保暖）> 粗粝。"""
    if not isinstance(material, dict):
        return ""
    sens = _sens_of(part)
    rough = max(0.0, _num(material.get("roughness_um"), 10.0))
    ct = clamp01(material.get("ct_affinity"))
    th = clamp01(material.get("thermal"))
    if th >= 0.85:
        return "冰凉"
    if prickle_index(sens, rough, ct) >= 0.08 and sens >= 5.0:
        return "刺痒"
    if rough <= 4.0 and sens >= 8.0:
        return "细滑"
    if ct >= 0.85:
        return "绒软"
    if rough <= 4.0:
        return "光滑"
    if ct >= 0.3 and rough <= 50.0:
        return "温软"
    return "粗粝"
