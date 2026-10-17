"""WebUI API 层——AstrBot 官方插件拓展页面 API（register_web_api），合规约定：
handler 用 astrbot.api.web 的 request/json_response/error_response，不暴露 quart/freeapi 原生对象；
页面端经 window.AstrBotPluginPage bridge 调用（endpoint = 注册路由去掉 /{PLUGIN_NAME} 前缀）。
两套路由统一前缀 /astrbot_plugin_virtual_body/page/：
身体 7 路由（原样）+ 空间 6 路由（manor 并入 P2，挂 /manor* 子路径）。
空间路由数据访问走 plugin 实例的 _manor_store/_manor/_space_data_dir（P1 已挂载）。
AstrBot 未提供 register_web_api 时整体跳过（main._register_web_api_if_available 降级，不崩）。
"""
from __future__ import annotations

import json
import os
import re
import time
import math
import tempfile
import threading
from typing import Any

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from . import CONFIG
from .core import material as material_mod
from .core import mood_engine, mood_store as mood_store_mod
from .core import state as state_mod
from .core.decay import overload_threshold, parse_ts, tier_of
from .core.inject import expression_params
from .core.render import _props_hint, distance_band, eta_band, parse_iso_local, room_label
from .core.store import load_manor, load_objects, save_objects

PLUGIN_NAME = "astrbot_plugin_virtual_body"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/page"



async def _safe_json() -> Any:
    """request.json 兜底：无 body/坏 JSON → {}，由各 handler 的结构校验返回 400。
    终审修复：此前误替换成自递归（保存路由全坏）——这里必须真实读 request.json。"""
    try:
        data = await request.json(default={})
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_FILE_WRITE_LOCK = threading.RLock()


def _replace_json_file(source: str, destination: str) -> None:
    """Bound Windows sharing failures; keep other failures visible to the caller."""
    # Windows can temporarily refuse rename while a handle disallows deletion.
    # Five attempts and at most 120 ms of requested backoff, never a blind retry.
    for delay in (0.01, 0.02, 0.04, 0.05, None):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32, 33) or delay is None:
                raise
            time.sleep(delay)


def _write_json_atomic(path: str, data: Any) -> None:
    """单次请求使用独立临时文件，避免并发保存共享 .tmp 丢失或串写。"""
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp",
                               dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=1, allow_nan=False)
        with _FILE_WRITE_LOCK:
            _replace_json_file(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


class VirtualBodyWebApi:
    def __init__(self, plugin):
        self.plugin = plugin




    def register_routes(self) -> None:
        register = self.plugin.context.register_web_api
        routes = [
            # ---- 身体层（原样 7 路由）----
            ("/overview", self.get_overview, ["GET"], "VirtualBody page overview"),
            ("/profiles", self.get_profiles, ["GET"], "VirtualBody page profiles"),
            ("/profile/save", self.save_profile, ["POST"], "VirtualBody page save profile parts/action_words/params/tiers"),
            ("/profile/names/save", self.save_profile_names, ["POST"], "VirtualBody page save naming overlay"),
            ("/bindings", self.get_bindings, ["GET"], "VirtualBody page session bindings"),
            ("/binding/delete", self.delete_binding, ["POST"], "VirtualBody page delete one session binding"),
            ("/state/reset", self.reset_state, ["POST"], "VirtualBody page reset one session state"),
            # ---- 空间层（manor 并入 P2：6 路由挂 /manor* 子路径）----
            ("/manor/overview", self.manor_overview, ["GET"], "VirtualBody page manor overview"),
            ("/manor/manor", self.get_manor, ["GET"], "VirtualBody page manor structure"),
            ("/manor/manor/save", self.save_manor, ["POST"], "VirtualBody page save manor structure"),
            ("/manor/objects", self.get_manor_objects, ["GET"], "VirtualBody page manor objects"),
            ("/manor/objects/save", self.save_manor_objects, ["POST"], "VirtualBody page save manor objects"),
            ("/manor/state/reset", self.reset_manor_state, ["POST"], "VirtualBody page reset one space session state"),
            # ---- 材质工坊（材质触觉层 0.25.x：3 路由）----
            ("/materials", self.get_materials, ["GET"], "VirtualBody page materials library"),
            ("/materials/save", self.save_materials, ["POST"], "VirtualBody page save materials library"),
            ("/materials/curve", self.materials_curve, ["POST"], "VirtualBody page material gain curve sampling"),
            # ---- 存在层总览（空间 × 身体一屏）----
            ("/presence/overview", self.presence_overview, ["GET"], "VirtualBody page presence overview"),
            # ---- 心绪编排（0.27.0 并入：6 路由挂 /mood* 子路径）----
            ("/mood/rules", self.get_mood_rules, ["GET"], "VirtualBody page mood rules"),
            ("/mood/rules/save", self.save_mood_rules, ["POST"], "VirtualBody page save mood rules"),
            ("/mood/signals", self.get_mood_signals, ["GET"], "VirtualBody page live mood signals"),
            ("/mood/triggers", self.get_mood_triggers, ["GET"], "VirtualBody page current mood triggers"),
            ("/mood/history", self.get_mood_history, ["GET"], "VirtualBody page mood trigger history"),
            ("/mood/state/reset", self.reset_mood_state, ["POST"], "VirtualBody page reset mood cooldowns/history"),
        ]
        for path, handler, methods, desc in routes:
            register(PAGE_API_PREFIX + path, handler, methods, desc)
        logger.info(f"[VirtualBody] 插件拓展页面 API 已注册: {[r[0] for r in routes]}")

    # ---- 心绪编排六路由（0.27.0 并入；数据落 plugin_data/mood/） ----

    def _mood_dir(self) -> str:
        fn = getattr(self.plugin, "_mood_dir", None)
        return fn() if callable(fn) else ""

    async def get_mood_rules(self):
        import os as _os
        doc = mood_store_mod.load_rules(self._mood_dir(), _os.path.dirname(_os.path.abspath(__file__)))
        return json_response({"version": CONFIG.VERSION, **doc})

    async def save_mood_rules(self):
        data = await _safe_json()
        if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
            return error_response("body must be {rules: [...]}")
        if len(data["rules"]) > 64:
            return error_response("rules 过多（≤64）")
        seen = set()
        for r in data["rules"]:
            if not isinstance(r, dict) or not r.get("id") or str(r["id"]) in seen:
                return error_response("rule id missing or duplicated")
            if len(str(r["id"])) > 64:
                return error_response("rule id 过长（≤64 字符）")
            seen.add(str(r["id"]))
            if str(r.get("signal") or "") not in ("weather", "mood", "reading", "presence"):
                return error_response("bad signal: %s" % r.get("signal"))
            cd = self._integer(r.get("cooldown_minutes", 60), 1, 10080)
            pr = self._integer(r.get("priority", 3), 1, 5)
            if cd is None:
                return error_response("cooldown_minutes 须为 1-10080 的整数")
            if pr is None:
                return error_response("priority 须为 1-5 的整数")
            if "enabled" in r and not isinstance(r["enabled"], bool):
                return error_response("enabled 必须为布尔值")
            conditions = r.get("conditions")
            if not isinstance(conditions, list) or not 1 <= len(conditions) <= 16:
                return error_response("conditions 须为非空列表（≤16 项）")
            for c in conditions:
                if not isinstance(c, dict):
                    return error_response("conditions 元素必须是对象")
                field = c.get("field")
                if not isinstance(field, str) or not re.fullmatch(r"(weather|mood|reading|presence)\.[a-zA-Z_][a-zA-Z0-9_]{0,63}", field):
                    return error_response("conditions.field 须为信号源.字段")
                op = str(c.get("op", "eq"))
                if op not in ("eq", "ne", "gt", "lt", "gte", "lte", "changed"):
                    return error_response(f"conditions.op 未知: {op}")
                if op != "changed":
                    if "value" not in c or not isinstance(c["value"], (str, int, float, bool, type(None))):
                        return error_response("conditions.value 须为单值")
                    value = c["value"]
                    if isinstance(value, float) and not math.isfinite(value):
                        return error_response("conditions.value 须为有限值")
                    if isinstance(value, str) and len(value) > 200:
                        return error_response("conditions.value 过长（≤200 字符）")
                    if op in ("gt", "lt", "gte", "lte") and self._num(value, -1e9, 1e9) is None:
                        return error_response("数值比较条件的 value 须为有限数字")
            th = r.get("template_hint")
            if th is not None and len(str(th)) > 200:
                return error_response("template_hint 过长（≤200 字符）")
        doc = {"schema": 1, "rules": data["rules"]}
        try:
            mood_store_mod.save_rules(self._mood_dir(), doc)
        except (OSError, ValueError):
            return error_response("规则保存失败，请检查存储可用性后重试")
        return json_response({"ok": True, "count": len(doc["rules"])})

    async def get_mood_signals(self):
        sig = getattr(self.plugin, "_last_signals", {}) or {}
        return json_response({k: (v if isinstance(v, dict) else None) for k, v in sig.items()})

    async def get_mood_triggers(self):
        import datetime as _dt
        import time as _time
        trig = getattr(self.plugin, "_last_triggers", None)
        if trig is None:
            trig = [x for x in mood_store_mod.load_triggers(self._mood_dir()) if isinstance(x, dict)]
        alive = mood_engine.prune_expired(trig, _time.time())

        def _pub(t):
            out = dict(t)
            for k in ("created_at", "expires_at"):
                v = out.get(k)
                if isinstance(v, (int, float)):
                    out[k] = _dt.datetime.fromtimestamp(v).replace(microsecond=0).isoformat()
            return out

        return json_response({
            "updated_at": mood_store_mod.load_json(self._mood_dir(), CONFIG.MO_TRIGGER_FILE, {}).get("updated_at"),
            "triggers": [_pub(t) for t in alive],
        })

    async def get_mood_history(self):
        hist = mood_store_mod.load_json(self._mood_dir(), CONFIG.MO_HISTORY_FILE, {"items": []})
        items = hist.get("items") if isinstance(hist, dict) and isinstance(hist.get("items"), list) else []
        return json_response({"items": items[-50:]})

    async def reset_mood_state(self):
        mood_store_mod.save_json(self._mood_dir(), CONFIG.MO_COOLDOWNS_FILE, {})
        mood_store_mod.save_json(self._mood_dir(), CONFIG.MO_HISTORY_FILE, {"items": []})
        return json_response({"ok": True})

    # ---- helpers ----

    def _store(self):
        return getattr(self.plugin, "_store", None)

    def _space_store(self):
        """空间层状态仓（P1 挂载的 _manor_store，.space.json 分立）。"""
        return getattr(self.plugin, "_manor_store", None)

    def _space_data_dir(self) -> str:
        """空间层数据目录（plugin_data 根，P1 挂载）。"""
        return str(getattr(self.plugin, "_space_data_dir", "") or "")

    def _profiles(self) -> dict[str, dict[str, Any]]:
        return getattr(self.plugin, "_profiles", {}) or {}

    def _profiles_base(self) -> dict[str, dict[str, Any]]:
        """参数层基底（未合并命名层）——编辑器与保存路径专用，防命名烤入覆盖文件。"""
        return getattr(self.plugin, "_profiles_base", None) or self._profiles()

    # ---- 输入校验共享件（0.27.8 加固：类型/长度/值域统一，错误一律 error_response） ----

    _PARAM_RANGES: dict[str, tuple[float, float]] = {
        # profile.params 已知键值域（锚 CONFIG 默认；未知键放行——前向兼容）
        "gain": (0.0, 1.0), "cool_seconds": (0.0, 3600.0), "tolerance_gain": (0.0, 1.0),
        "night_mult": (0.1, 3.0), "habit_streak": (1.0, 20.0), "habit_window": (5.0, 600.0),
        "habit_factor": (0.0, 1.0), "radiate_hops": (1.0, 3.0),   # 0.28.1 联动最大跳数（默认 2）
        # 0.30.0 语言调情→身体兴奋（advance_state/_verbal_cfg 经 profile_param 链消费）
        "verbal_arousal_rate": (0.0, 0.05), "verbal_arousal_cap": (0.1, 0.6),
        "verbal_arousal_tau": (300.0, 7200.0), "verbal_arousal_weight": (0.2, 1.5),
    }
    _PARAM_ENUMS: dict[str, tuple[str, ...]] = {
        # 字符串枚举参数（0.28.1）：联动衰减模式 mul=连乘取最强路径（默认）/ sum=多路径求和
        "radiate_mode": ("mul", "sum"),
    }
    _TIER_KEYS = ("notice", "react", "lose", "overload")

    @staticmethod
    def _num(v: Any, lo: float, hi: float) -> float | None:
        """有限数值且落在 [lo, hi] 才返回 float；NaN/Inf/越界/非数值一律 None。"""
        if isinstance(v, bool):
            return None
        try:
            x = float(v)
        except (TypeError, ValueError, OverflowError):
            return None
        if x != x or x in (float("inf"), float("-inf")) or not lo <= x <= hi:
            return None
        return x

    @staticmethod
    def _integer(v: Any, lo: int, hi: int) -> int | None:
        x = VirtualBodyWebApi._num(v, lo, hi)
        return int(x) if x is not None and x.is_integer() else None

    def _runtime_view(self) -> dict[str, Any]:
        fn = getattr(self.plugin, "runtime_diagnostics", None)
        if callable(fn):
            try:
                result = fn()
                return result if isinstance(result, dict) else {}
            except Exception:
                logger.warning("[VirtualBody] 运行诊断暂不可用")
        return {}

    def _config_view(self) -> dict[str, Any]:
        cfg = getattr(self.plugin, "config", None)
        out: dict[str, Any] = {}
        if cfg is not None:
            for key in ("vb_enabled", "vb_default_character", "vb_inject_enabled", "vb_tick_seconds",
                        "vb_render_max_chars", "vb_show_numbers", "vb_llm_fallback_enabled",
                        "vb_llm_fallback_daily_limit", "sp_enabled", "mo_enabled",
                        "presence_bridge_enabled", "vb_tz_utc_offset", "sp_pc_plan_enabled",
                        "presence_layer_enabled", "presence_context_enabled",
                        "presence_context_max_chars", "presence_repeat_seconds"):
                try:
                    out[key] = cfg.get(key)
                except Exception:
                    out[key] = None
        return out

    def _part_view(self, pid: str, st: dict[str, Any], profile: dict[str, Any], now_ts: float | None) -> dict[str, Any]:
        meta = next((p for p in profile.get("parts") or [] if str(p.get("id")) == pid), {}) or {}
        try:
            value = float((st or {}).get("value") or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        cool_until = parse_ts((st or {}).get("cool_until"))
        return {
            "label": str(meta.get("label") or pid),
            "sens": meta.get("sens"),
            "half_life": meta.get("half_life"),
            "radiate": meta.get("radiate") or {},
            "value": round(value, 4),
            "tier": tier_of(value, profile.get("tiers") or []),
            "trend_delta": (st or {}).get("trend_delta") or 0.0,
            "last_event_type": (st or {}).get("last_event_type") or "",
            "last_event_at": (st or {}).get("last_event_at"),
            "cooling": bool(cool_until is not None and now_ts is not None and now_ts < cool_until),
            "streak_count": (st or {}).get("streak_count") or 0,
        }

    # ---- GET /overview ----

    async def get_overview(self):
        store = self._store()
        profiles = self._profiles()
        states: list[dict[str, Any]] = []
        if store is not None:
            for character, session in store.list_states():
                profile = profiles.get(character)
                if profile is None:
                    continue
                try:
                    st = store.get_or_create(character, session, profile)
                    now_ts = parse_ts(st.get("updated_at"))
                    parts_view = {
                        pid: self._part_view(pid, pst, profile, now_ts)
                        for pid, pst in (st.get("parts") or {}).items()
                    }
                    ev_today = st.get("events_today") if isinstance(st.get("events_today"), dict) else {}
                    states.append({
                        "character": character,
                        "character_name": profile.get("name") or character,
                        "session": session,
                        "arousal": st.get("arousal"),
                        "tolerance": st.get("tolerance"),
                        "phase": st.get("phase"),
                        "phase_until": st.get("phase_until"),
                        "overload_threshold": overload_threshold(profile),
                        "events_today": ev_today.get("count") if isinstance(ev_today.get("count"), int) else 0,
                        "updated_at": st.get("updated_at"),
                        "parts": parts_view,
                        "expression": expression_params(float(st.get("arousal") or 0.0), profile),
                        "history": (st.get("history") or [])[-20:] if isinstance(st.get("history"), list) else [],
                    })
                except Exception as exc:
                    logger.warning(f"[VirtualBody] overview 状态汇总失败 {character}/{session}: {exc}")
        states.sort(key=lambda s: (s["character"], s["session"]))
        return json_response({
            "version": CONFIG.VERSION,
            "runtime": self._runtime_view(),
            "profiles": [
                {"id": pid, "name": (p or {}).get("name") or pid, "race": (p or {}).get("race") or "",
                 "whitelist": (p or {}).get("whitelist") or [], "parts_count": len((p or {}).get("parts") or [])}
                for pid, p in sorted(profiles.items())
            ],
            "states": states,
            "config": self._config_view(),
        })

    # ---- GET /profiles ----

    async def get_profiles(self):
        out = {}
        names_overlay: dict[str, dict[str, Any]] = {}
        store = self._store()
        data_dir = os.path.dirname(store._dir) if store is not None else ""
        # 参数层基底：部位 label 展示参数层原文（敏感占位=数字编号），
        # 用户命名只经命名层管理——否则保存路径会把命名烤进参数覆盖文件
        for pid, prof in sorted(self._profiles_base().items()):
            out[pid] = {
                "id": pid,
                "name": (prof or {}).get("name") or pid,
                "race": (prof or {}).get("race") or "",
                "whitelist": (prof or {}).get("whitelist") or [],
                "forbidden": (prof or {}).get("forbidden") or [],
                "params": (prof or {}).get("params") or {},
                "action_words": (prof or {}).get("action_words") or {},
                "tiers": (prof or {}).get("tiers") or [],
                "parts": [
                    {"id": str(p.get("id")), "label": p.get("label"), "sens": p.get("sens"),
                     "half_life": p.get("half_life"), "aliases": p.get("aliases") or [],
                     "flags": p.get("flags") or [], "radiate": p.get("radiate") or {},
                     "viz": p.get("viz") if isinstance(p.get("viz"), list) else [],
                     # 0.29.1 部位级细调键（缺省 None=中性；保存链路经 state.validate_parts 校验值域）
                     "gain_mult": p.get("gain_mult"), "tolerance_mult": p.get("tolerance_mult"),
                     "windup_mult": p.get("windup_mult"),
                     "type_mults": p.get("type_mults") if isinstance(p.get("type_mults"), dict) else {},
                     "material_affinity": p.get("material_affinity") if isinstance(p.get("material_affinity"), dict) else {}}
                    for p in (prof or {}).get("parts") or []
                ],
                "render": (prof or {}).get("render") or {},
            }
            if data_dir:
                names_overlay[pid] = state_mod.load_names_overlay(data_dir, pid)
        return json_response({
            "profiles": out,
            "sens_scale": [
                {"level": lv, "name": name, "desc": desc} for lv, name, desc in CONFIG.SENS_SCALE
            ],
            "names": names_overlay,
        })

    # ---- POST /profile/save ----

    async def save_profile(self):
        store = self._store()
        if store is None:
            return error_response("store_not_ready")
        body = await _safe_json()
        if not isinstance(body, dict):
            return error_response("body 必须是 JSON 对象")
        character = str(body.get("character") or "").strip()
        base = self._profiles_base().get(character)
        if base is None:
            return error_response(f"未知角色: {character or '(空)'}")
        # ---- 分键校验（0.27.8 输入加固 + tiers/params 自定义入口契约） ----
        if "parts" in body:
            parts = body["parts"]
            if not isinstance(parts, list) or len(parts) > 200:
                return error_response("parts 必须是列表（≤200 项）")
            for p in parts:
                if not isinstance(p, dict):
                    return error_response("parts 元素必须是对象")
                pid = str(p.get("id") or "")
                if len(pid) > 64:
                    return error_response(f"部位 id 过长（≤64 字符）: {pid[:24]}…")
                # 注：空 id 不在此拦截——状态层 _auto_part_ids 兜底（0.14.0 契约）
        if "action_words" in body:
            aw = body["action_words"]
            if not isinstance(aw, dict) or len(aw) > 200:
                return error_response("action_words 必须是对象（≤200 词）")
            for w in aw:
                if not str(w).strip() or len(str(w)) > 32:
                    return error_response(f"动作词非法: {w!r}")
            # 0.29.0 值两形态：数字（旧）或参数 dict；结构校验与保存层同源（state.validate_action_words）
            ok, err = state_mod.validate_action_words(aw)
            if not ok:
                return error_response(err)
        if "params" in body:
            params = body["params"]
            if not isinstance(params, dict) or len(params) > 64:
                return error_response("params 必须是对象（≤64 键）")
            for k, v in params.items():
                rng = self._PARAM_RANGES.get(str(k))
                if rng is not None and self._num(v, rng[0], rng[1]) is None:
                    return error_response(f"params.{k} 须在 {rng[0]:g}-{rng[1]:g}")
                enum = self._PARAM_ENUMS.get(str(k))
                if enum is not None and str(v) not in enum:
                    return error_response(f"params.{k} 须为 {'/'.join(enum)}")
        if "tiers" in body:
            tiers = body["tiers"]
            if not isinstance(tiers, list) or len(tiers) != 4:
                return error_response("tiers 必须是 4 项列表（notice/react/lose/overload）")
            prev = -1.0
            for i, t in enumerate(tiers):
                if not isinstance(t, dict) or str(t.get("key")) != self._TIER_KEYS[i]:
                    return error_response(f"tiers[{i}].key 必须是 {self._TIER_KEYS[i]}")
                mn = self._num(t.get("min"), 0.0, 1.0)
                if mn is None:
                    return error_response(f"tiers[{i}].min 须在 0-1")
                if mn <= prev:
                    return error_response("tiers 的 min 须严格升序（notice < react < lose < overload）")
                prev = mn
            body["tiers"] = [{"key": self._TIER_KEYS[i], "min": round(float(tiers[i]["min"]), 4)}
                             for i in range(4)]   # 归一化：只落 key/min
        patch = {k: body.get(k) for k in ("parts", "action_words", "params", "tiers") if k in body}
        if not patch:
            return error_response("没有可保存的内容（parts/action_words/params/tiers 至少一项）")
        data_dir = os.path.dirname(store._dir)  # store._dir = <data_dir>/state → 还原 <data_dir>
        ok, err = state_mod.save_profile_override(data_dir, base, patch)
        if not ok:
            logger.warning(f"[VirtualBody] WebUI 保存 profile 失败 {character}: {err}")
            return error_response(err)
        self.plugin._load_profiles()   # 热生效
        logger.info(f"[VirtualBody] WebUI 已保存并热载 profile: {character} keys={sorted(patch)}")
        return json_response({"ok": True, "reloaded": sorted(self._profiles())})

    # ---- POST /profile/names/save ----

    async def save_profile_names(self):
        """命名层保存：{character, names: {pid: {label, aliases}}} → <id>.names.json + 热载。
        参数层不读写此文件（分层纪律）。"""
        store = self._store()
        if store is None:
            return error_response("store_not_ready")
        body = await _safe_json()
        if not isinstance(body, dict):
            return error_response("body 必须是 JSON 对象")
        character = str(body.get("character") or "").strip()
        if character not in self._profiles():
            return error_response(f"未知角色: {character or '(空)'}")
        names = body.get("names")
        if not isinstance(names, dict):
            return error_response("names 必须是对象")
        if len(names) > 500:
            return error_response("names 条目过多（≤500）")
        for pid, nv in names.items():
            if not isinstance(nv, dict):
                return error_response(f"names[{pid}] 必须是对象")
            if len(str(nv.get("label") or "").strip()) > 64:
                return error_response(f"names[{pid}].label 过长（≤64 字符）")
            al = nv.get("aliases")
            if al is not None and (not isinstance(al, list) or len(al) > 24
                                   or any(len(str(a)) > 32 for a in al)):
                return error_response(f"names[{pid}].aliases 须为列表（≤24 项、每项 ≤32 字符）")
        # 允许空对象=清空全部命名（回落参数层 label）；未知部位 id 仍拒绝
        known = {str(p.get("id")) for p in (self._profiles().get(character) or {}).get("parts") or []}
        bad = [pid for pid in names if str(pid) not in known]
        if bad:
            return error_response(f"未知部位 id: {','.join(map(str, bad[:5]))}")
        data_dir = os.path.dirname(store._dir)
        ok, err = state_mod.save_names_overlay(data_dir, character, names)
        if not ok:
            return error_response(err)
        self.plugin._load_profiles()   # 热载（合并命名层）
        logger.info(f"[VirtualBody] WebUI 已保存命名层并热载: {character} entries={len(names)}")
        return json_response({"ok": True})

    # ---- GET /bindings ----

    async def get_bindings(self):
        store = self._store()
        bindings = store.load_bindings() if store is not None else {}
        default_character = ""
        try:
            default_character = self.plugin._default_character()
        except Exception:
            pass
        return json_response({"bindings": bindings, "default_character": default_character})

    # ---- POST /binding/delete ----

    async def delete_binding(self):
        store = self._store()
        if store is None:
            return error_response("store_not_ready")
        body = await _safe_json()
        if not isinstance(body, dict):
            return error_response("body 必须是 JSON 对象")
        session = str(body.get("session") or "").strip()
        if not session:
            return error_response("session 必填")
        if len(session) > 200:
            return error_response("session 过长（≤200 字符）")
        bindings = store.load_bindings()
        removed = bindings.pop(session, None)
        store.save_bindings(bindings)
        self.plugin._bindings = dict(bindings)   # 运行中会话解析使用内存表，解除绑定立即生效。
        logger.info(f"[VirtualBody] WebUI 解除绑定: {session} (was {removed})")
        return json_response({"ok": True, "removed": removed})

    # ---- POST /state/reset ----

    async def reset_state(self):
        store = self._store()
        if store is None:
            return error_response("store_not_ready")
        body = await _safe_json()
        if not isinstance(body, dict):
            return error_response("body 必须是 JSON 对象")
        character = str(body.get("character") or "").strip()
        session = str(body.get("session") or "").strip()
        if not character or not session:
            return error_response("character 与 session 必填")
        if not re.fullmatch(r"[A-Za-z0-9_:\-]+", session):
            return error_response("session 含非法字符")   # 防路径拼接越出 state 目录
        if character not in self._profiles():
            return error_response(f"未知角色: {character}")
        removed = store.delete_state(character, session)
        logger.info(f"[VirtualBody] WebUI 重置状态: {character}/{session} removed={removed}")
        return json_response({"ok": True, "removed": removed})

    # ================================================================
    # 空间层路由（manor 并入 P2）：逻辑照搬 manor 原版 ManorWebApi，
    # 数据访问改走 plugin 实例 _manor_store/_manor/_space_data_dir；
    # CONFIG 键用 SP_ 前缀合并版（SP_ROOM_ID_RE/SP_MANOR_OVERRIDE_DIR/SP_SESSION_RE）。
    # ================================================================

    # ---- GET /manor/overview ----

    async def manor_overview(self):
        plugin = self.plugin
        store = self._space_store()
        states = []
        if store is not None:
            for session in store.list_sessions():
                try:
                    st = store.get_or_create(session, plugin._manor)
                    states.append({
                        "session": session,
                        "she": st.get("she") or {},
                        "he": st.get("he") or {},
                        "events_today": st.get("events_today") or {},
                        "updated_at": st.get("updated_at"),
                    })
                except Exception as exc:
                    logger.warning(f"[VirtualBody] manor overview 状态汇总失败 {session}: {exc}")
        return json_response({
            "version": CONFIG.VERSION,
            "manor_source": getattr(plugin, "_manor_source", "none"),
            "states": states,
        })

    # ---- GET /manor/manor ----

    async def get_manor(self):
        return json_response(getattr(self.plugin, "_manor", None) or {})

    # ---- POST /manor/manor/save ----

    async def save_manor(self):
        data_dir = self._space_data_dir()
        if not data_dir:
            return error_response("store_not_ready")
        body = await _safe_json()
        if not isinstance(body, dict):
            return error_response("body 必须是 JSON 对象")
        rooms = body.get("rooms")
        edges = body.get("edges", (getattr(self.plugin, "_manor", None) or {}).get("edges", []))
        if not isinstance(rooms, list) or not rooms:
            return error_response("rooms 必须是非空列表")
        if len(rooms) > 200:
            return error_response("rooms 过多（≤200）")
        if not isinstance(edges, list) or len(edges) > 600:
            return error_response("edges 必须是列表（≤600）")
        seen = set()
        for r in rooms:
            if not isinstance(r, dict):
                return error_response("rooms 元素必须是对象")
            rid = str((r or {}).get("id") or "")
            if not re.fullmatch(CONFIG.SP_ROOM_ID_RE, rid) or len(rid) > 48:
                return error_response(f"房间 id 非法: {rid!r}")
            if rid in seen:
                return error_response(f"房间 id 重复: {rid}")
            seen.add(rid)
            if "label" in r and (not isinstance(r["label"], str) or not r["label"].strip() or len(r["label"]) > 64):
                return error_response(f"{rid}: label 须为非空字符串（≤64 字符）")
            for key, lo, hi in (("center", -10000, 10000), ("size", .1, 10000)):
                if key in r and (not isinstance(r[key], list) or len(r[key]) != 2
                                 or any(self._num(v, lo, hi) is None for v in r[key])):
                    return error_response(f"{rid}: {key} 须为两个有限数字，范围 {lo:g}-{hi:g}")
            for key, limit, word_limit in (("aliases", 24, 64), ("props", 64, 200)):
                if key in r and (not isinstance(r[key], list) or len(r[key]) > limit
                                 or any(not isinstance(v, str) or len(v) > word_limit for v in r[key])):
                    return error_response(f"{rid}: {key} 须为字符串列表（≤{limit} 项，每项≤{word_limit} 字符）")
            if "scene_words" in r:
                scene_words = r["scene_words"]
                if not isinstance(scene_words, list) or len(scene_words) > 64:
                    return error_response(f"{rid}: scene_words 须为场景词对象列表（≤64 项）")
                for entry in scene_words:
                    if (not isinstance(entry, dict)
                            or not isinstance(entry.get("word"), str)
                            or not entry["word"].strip() or len(entry["word"]) > 64
                            or self._num(entry.get("gain", 0), 0, .5) is None
                            or ("note" in entry and (not isinstance(entry["note"], str)
                                                       or len(entry["note"]) > 200))):
                        return error_response(f"{rid}: 场景词须含非空 word（≤64 字符）、gain（0–0.5）及可选 note（≤200 字符）")
            if "ambiance" in r and (not isinstance(r["ambiance"], str) or len(r["ambiance"]) > 1000):
                return error_response(f"{rid}: ambiance 须为字符串（≤1000 字符）")
            if "floor" in r:
                floor_ids = {str(f.get("id")) for f in (getattr(self.plugin, "_manor", {}) or {}).get("floors", []) if isinstance(f, dict)}
                if floor_ids and str(r["floor"]) not in floor_ids:
                    return error_response(f"{rid}: floor 引用未知楼层")
        # 悬空引用拦截（规范：悬空引用禁止保存）——边引用的房间 id 必须都存在
        for e in edges or []:
            if not isinstance(e, dict):
                return error_response("edges 元素必须是对象")
            for k in ("a", "b"):
                if str((e or {}).get(k)) not in seen:
                    return error_response(f"边引用未知房间: {e}")
            if self._integer(e.get("seconds", 0), 0, 86400) is None:
                return error_response("edges.seconds 须为 0-86400 的整数")
        manor = dict(getattr(self.plugin, "_manor", None) or {})
        manor["rooms"] = rooms
        manor["edges"] = edges
        # 页面提交编辑后的整表；只更新明确提交的键，旧客户端缺省时保留原值。
        for key in ("activity_map", "default_schedule"):
            if key in body:
                if not isinstance(body[key], list):
                    return error_response(f"{key} 必须是列表")
                if any(not isinstance(entry, dict) for entry in body[key]):
                    return error_response(f"{key} 元素必须是对象")
                manor[key] = body[key]
        # 悬空 activity_map / default_schedule 校验（动线映射与日程引用的房间 id 必须存在）
        for m in manor.get("activity_map") or []:
            if str((m or {}).get("room")) not in seen:
                return error_response(f"activity_map 引用未知房间: {(m or {}).get('room')}——先清理映射再删房间")
            if not isinstance(m.get("match"), list) or any(not isinstance(kw, str) for kw in m["match"]):
                return error_response("activity_map.match 必须是字符串列表")
        for s in manor.get("default_schedule") or []:
            if str((s or {}).get("room")) not in seen:
                return error_response(f"default_schedule 引用未知房间: {(s or {}).get('room')}")
            try:
                from_h, to_h = float(s.get("from_h")), float(s.get("to_h"))
            except (TypeError, ValueError):
                return error_response("default_schedule 时段须为 0-24 的整数且从时 < 到时")
            if not (0 <= from_h < to_h <= 24 and from_h.is_integer() and to_h.is_integer()):
                return error_response("default_schedule 时段须为 0-24 的整数且从时 < 到时")
        overrides_dir = os.path.join(data_dir, CONFIG.SP_MANOR_OVERRIDE_DIR)
        path = os.path.join(overrides_dir, "manor.json")
        try:
            os.makedirs(overrides_dir, exist_ok=True)
            _write_json_atomic(path, manor)
        except (OSError, ValueError):
            return error_response("庄园保存失败，请检查存储可用性后重试")
        # 热载：重走 load_manor（覆盖层优先），刷新 plugin._manor/_manor_source
        self.plugin._manor, self.plugin._manor_source = load_manor(
            data_dir, os.path.dirname(os.path.abspath(__file__)))
        logger.info(f"[VirtualBody] WebUI 已保存庄园结构并热载 rooms={len(rooms)}")
        return json_response({"ok": True, "rooms": len(rooms)})

    # ---- GET /manor/objects ----

    async def get_manor_objects(self):
        data_dir = self._space_data_dir()
        if not data_dir:
            return json_response({"schema": 1, "items": []})
        return json_response(load_objects(data_dir) or {"schema": 1, "items": []})

    # ---- POST /manor/objects/save ----

    async def save_manor_objects(self):
        data_dir = self._space_data_dir()
        if not data_dir:
            return error_response("store_not_ready")
        body = await _safe_json()
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list):
            return error_response("items 必须是列表")
        if len(items) > 500:
            return error_response("items 过多（≤500）")
        if any(not isinstance(x, dict) for x in items):
            return error_response("items 元素必须是对象")
        known_rooms = {str(r.get("id")) for r in (getattr(self.plugin, "_manor", {}) or {}).get("rooms", []) if isinstance(r, dict)}
        seen = set()
        for item in items:
            oid = item.get("id")
            if not isinstance(oid, str) or not oid.strip() or len(oid) > 64 or oid in seen:
                return error_response("物品 id 须唯一、非空且≤64 字符")
            seen.add(oid)
            if str(item.get("room")) not in known_rooms:
                return error_response(f"{oid}: room 引用未知房间")
            for key, limit in (("label", 200), ("note", 1000)):
                if key in item and (not isinstance(item[key], str) or len(item[key]) > limit):
                    return error_response(f"{oid}: {key} 须为字符串（≤{limit} 字符）")
        try:
            save_objects(data_dir, {"schema": 1, "items": items})
        except (OSError, ValueError):
            return error_response("物品保存失败，请检查存储可用性后重试")
        return json_response({"ok": True})

    # ---- POST /manor/state/reset ----

    async def reset_manor_state(self):
        store = self._space_store()
        if store is None:
            return error_response("store_not_ready")
        body = await _safe_json()
        if not isinstance(body, dict):
            return error_response("body 必须是 JSON 对象")
        session = str(body.get("session") or "").strip()
        if not session:
            return error_response("session 必填")
        if not re.fullmatch(CONFIG.SP_SESSION_RE, session):
            return error_response("session 含非法字符")
        removed = store.delete(session)
        return json_response({"ok": True, "removed": removed})

    # ================================================================
    # 材质工坊路由（材质触觉层）：库读写走 data/materials.json 原文件
    # （保留 _refs 与列表顺序）；曲线采样内部调 core/material.material_gain
    # 纯函数——公式只有一份，前端不复制。热生效依赖 load_materials 的
    # mtime 缓存（mtime 变化自动重载，material.py 既有机制）。
    # ================================================================

    # ---- GET /materials ----

    async def get_materials(self):
        """读材质库原文件（含 _refs 与顺序）+ 材质层运行参数（前端图例/钳制范围用）。
        method_params 为顶层"方式参数"（rub/tap 频率窗、stroke 速度窗）——可能缺省（页面显示默认，首存生成）。"""
        raw: dict[str, Any] = {}
        try:
            raw = json.loads(material_mod.default_materials_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        refs = raw.get("_refs") if isinstance(raw.get("_refs"), list) else []
        mats = raw.get("materials") if isinstance(raw.get("materials"), list) else []
        mp = raw.get("method_params") if isinstance(raw.get("method_params"), dict) else None
        if mp is not None:
            mp = dict(mp)
            effective = material_mod.load_method_params()
            for method, axis in (("rub", "freq"), ("tap", "freq"), ("stroke", "speed")):
                row = dict(mp.get(method) or {}) if isinstance(mp.get(method), dict) else {}
                row[axis + "_lo"] = effective[method][axis + "_min"]
                row[axis + "_hi"] = effective[method][axis + "_max"]
                mp[method] = row
        return json_response({
            "version": CONFIG.VERSION,
            "enabled": bool(CONFIG.MATERIAL_ENABLED),
            "gain_range": [float(CONFIG.MATERIAL_GAIN_MIN), float(CONFIG.MATERIAL_GAIN_MAX)],
            "match_window": int(CONFIG.MATERIAL_MATCH_WINDOW),
            "stale_seconds": int(CONFIG.MATERIAL_STALE_SECONDS),
            "method_params": mp,
            "_refs": refs,
            "materials": mats,
        })

    # ---- POST /materials/save ----

    _METHOD_PARAM_SPEC: dict[str, dict[str, tuple[float, float]]] = {
        # 方式参数（材质工坊"方式参数"卡）：method → {字段: (lo, hi)}——值域与 _METHOD_AXIS 同锚
        "rub":    {"freq_lo": (0.0, 300.0), "freq_hi": (0.0, 300.0)},
        "tap":    {"freq_lo": (20.0, 500.0), "freq_hi": (20.0, 500.0)},
        "stroke": {"speed_lo": (0.5, 80.0), "speed_hi": (0.5, 80.0)},
    }

    def _clean_method_params(self, mp: Any) -> tuple[dict[str, dict[str, float]] | None, str | None]:
        """方式参数校验：{rub/tap: {freq_lo, freq_hi}, stroke: {speed_lo, speed_hi}}，窗内 lo<hi。
        返回 (归一化数据, 错误)；错误非 None 即 400。"""
        if not isinstance(mp, dict):
            return None, "method_params 必须是对象"
        extra = {str(k) for k in mp} - set(self._METHOD_PARAM_SPEC)
        if extra:
            return None, f"未知方式: {','.join(sorted(extra))}（仅 rub/tap/stroke）"
        clean: dict[str, dict[str, float]] = {}
        for method, fields in self._METHOD_PARAM_SPEC.items():
            src = mp.get(method)
            if src is None:
                continue
            if not isinstance(src, dict):
                return None, f"method_params.{method} 必须是对象"
            out: dict[str, float] = {}
            for field, (lo, hi) in fields.items():
                v = self._num(src.get(field), lo, hi)
                if v is None:
                    return None, f"method_params.{method}.{field} 须在 {lo:g}-{hi:g}"
                out[field] = round(v, 4)
            lo_key, hi_key = list(fields)
            if out[lo_key] >= out[hi_key]:
                return None, f"method_params.{method} 窗口下限须小于上限"
            clean[method] = out
        return clean, None

    async def save_materials(self):
        """材质库落盘：校验（id 唯一/合法、基础三参数在值域 roughness 0-1000 / thermal 0-1 /
        ct 0-1、0.29.0 可选 friction/thickness/warm_hold 0-1、pattern_mm 0-50、method_gains
        {五方式: 0.4-1.8}、跨材质词不共享、方式参数窗口合法）→ 原子写入 plugin_data/materials.json
        （_refs/method_params 未随 body 提交时保留原文件既有值，不接改）。"""
        body = await _safe_json()
        if not isinstance(body, dict):
            return error_response("body 必须是 JSON 对象")
        mats = body.get("materials")
        if not isinstance(mats, list) or not mats:
            return error_response("materials 必须是非空列表")
        if len(mats) > 200:
            return error_response("materials 过多（≤200）")
        mp_clean: dict[str, dict[str, float]] | None = None
        if "method_params" in body:
            mp_clean, mp_err = self._clean_method_params(body.get("method_params"))
            if mp_err:
                return error_response(mp_err)
        seen: set[str] = set()
        owner: dict[str, str] = {}
        clean: list[dict[str, Any]] = []
        for m in mats:
            if not isinstance(m, dict):
                return error_response("materials 元素必须是对象")
            mid = str(m.get("id") or "").strip()
            if not re.fullmatch(r"[a-z][a-z0-9_]*", mid):
                return error_response(f"材质 id 非法: {mid!r}（小写字母开头，仅小写字母/数字/下划线）")
            if mid in seen:
                return error_response(f"材质 id 重复: {mid}")
            seen.add(mid)
            label = str(m.get("label") or "").strip()
            if not label or len(label) > 48:
                return error_response(f"{mid}: label 不能为空且 ≤48 字符")
            vals: dict[str, float] = {}
            for key, lo, hi in (("roughness_um", 0.0, 1000.0), ("thermal", 0.0, 1.0), ("ct_affinity", 0.0, 1.0)):
                v = self._num(m.get(key), lo, hi)
                if v is None:
                    return error_response(f"{mid}: {key} 须在 {lo:g}-{hi:g}")
                vals[key] = v
            # 0.29.0 可选扩展参数：缺省不落键（core 回中性值，现行为不变）
            extras: dict[str, Any] = {}
            for key, lo, hi in (("friction", 0.0, 1.0), ("thickness", 0.0, 1.0),
                                ("warm_hold", 0.0, 1.0), ("pattern_mm", 0.0, 50.0)):
                if m.get(key) is None:
                    continue
                x = self._num(m.get(key), lo, hi)
                if x is None:
                    return error_response(f"{mid}: {key} 须在 {lo:g}-{hi:g}")
                extras[key] = round(x, 4)
            mg_raw = m.get("method_gains")
            if mg_raw is not None:
                if not isinstance(mg_raw, dict):
                    return error_response(f"{mid}: method_gains 必须是对象")
                mg_lo, mg_hi = CONFIG.MATERIAL_METHOD_GAIN_RANGE
                mg_clean: dict[str, float] = {}
                for meth, gv in mg_raw.items():
                    if str(meth) not in ("stroke", "press", "rub", "tap", "blow"):
                        return error_response(f"{mid}: method_gains 未知方式 {meth!r}（仅五方式）")
                    g = self._num(gv, mg_lo, mg_hi)
                    if g is None:
                        return error_response(f"{mid}: method_gains.{meth} 须在 {mg_lo:g}-{mg_hi:g}")
                    mg_clean[str(meth)] = round(g, 4)
                if mg_clean:
                    extras["method_gains"] = mg_clean
            raw_aliases = m.get("aliases", [])
            if not isinstance(raw_aliases, list) or len(raw_aliases) > 12 or any(not isinstance(a, str) for a in raw_aliases):
                return error_response(f"{mid}: aliases 须为字符串列表（≤12 项）")
            aliases = [a.strip() for a in raw_aliases if a.strip()]
            if any(len(a) > 32 for a in aliases):
                return error_response(f"{mid}: 别名每项 ≤32 字符")
            # 跨材质词查重（同 profile 部位纪律：共享词会让文本抽取就近配对错绑材质）
            for word in [label] + aliases:
                if word in owner and owner[word] != mid:
                    return error_response(f"词 {word!r} 同时属于 {owner[word]} 与 {mid}")
                owner[word] = mid
            entry: dict[str, Any] = {
                "id": mid, "label": label, "aliases": aliases[:12],
                "roughness_um": vals["roughness_um"], "thermal": vals["thermal"],
                "ct_affinity": vals["ct_affinity"],
                "note": str(m.get("note") or "")[:200],
            }
            entry.update(extras)
            clean.append(entry)
        # 原文件既有层（_refs / method_params）：body 未提交则原样保留
        raw_existing: dict[str, Any] = {}
        try:
            raw_existing = json.loads(material_mod.default_materials_path().read_text(encoding="utf-8"))
            if not isinstance(raw_existing, dict):
                raw_existing = {}
        except (OSError, ValueError):
            raw_existing = {}
        refs = body.get("_refs")
        if not isinstance(refs, list):
            refs = raw_existing.get("_refs") if isinstance(raw_existing.get("_refs"), list) else []
        doc: dict[str, Any] = {"_refs": refs, "materials": clean}
        if mp_clean is not None:
            previous = raw_existing.get("method_params")
            merged = dict(previous) if isinstance(previous, dict) else {}
            aliases = {"freq_lo": "freq_min", "freq_hi": "freq_max",
                       "speed_lo": "speed_min", "speed_hi": "speed_max"}
            for method, values in mp_clean.items():
                row = dict(merged.get(method) or {}) if isinstance(merged.get(method), dict) else {}
                row.update(values)
                row.update({aliases[key]: value for key, value in values.items()})
                merged[method] = row
            doc["method_params"] = merged
        elif isinstance(raw_existing.get("method_params"), dict):
            doc["method_params"] = raw_existing["method_params"]
        path = material_mod.materials_write_path(self._space_data_dir())
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(str(path), doc)
        except (OSError, ValueError):
            return error_response("材质保存失败，请检查存储可用性后重试")
        material_mod.configure_storage(self._space_data_dir())
        material_mod.load_materials(force=True)   # 热生效：刷新 mtime 缓存（事件抽取/增益立即用新库）
        logger.info(f"[VirtualBody] WebUI 已保存材质库并热载 materials={len(clean)}"
                    f" method_params={'yes' if 'method_params' in doc else 'keep'}")
        return json_response({"ok": True, "materials": len(clean), "method_params": doc.get("method_params")})

    # ---- POST /materials/curve ----

    _METHOD_AXIS: dict[str, tuple[str, float, float, str, str]] = {
        # method → (material_gain 参数键, 横轴下限, 上限, 轴名, 单位)——与页面曲线预览一致
        "stroke": ("speed_cm_s", 0.5, 80.0, "速度", "cm/s"),
        "press": ("depth", 0.0, 1.0, "压深", ""),
        "rub": ("freq_hz", 0.0, 300.0, "频率", "Hz"),
        "tap": ("freq_hz", 20.0, 500.0, "频率", "Hz"),
        "blow": ("temp_c", 0.0, 60.0, "温度", "°C"),
    }

    async def materials_curve(self):
        """曲线采样：{part_sens, material_id|material_ids, method, points?, x_min?, x_max?} →
        {curves: [{material_id, label, points: [{x, gain}]}]}。内部调 material_gain 纯函数。"""
        body = await _safe_json()
        if not isinstance(body, dict):
            return error_response("body 必须是 JSON 对象")
        method = str(body.get("method") or "")
        axis = self._METHOD_AXIS.get(method)
        if axis is None:
            return error_response(f"未知方式: {method!r}（须 stroke/press/rub/tap/blow）")
        sens = self._num(body.get("part_sens"), 0.0, 10.0)
        if sens is None:
            return error_response("part_sens 须在 0-10")
        raw_ids = body.get("material_ids")
        if isinstance(raw_ids, list):
            mids = [str(x) for x in raw_ids if str(x or "").strip()][:16]
        else:
            mids = [str(body.get("material_id") or "").strip()] if str(body.get("material_id") or "").strip() else []
        if not mids:
            return error_response("material_id(s) 必填")
        mats = material_mod.load_materials()
        for mid in mids:
            if mid not in mats:
                return error_response(f"未知材质: {mid}")
        n_pts = self._integer(body.get("points", 40), 1, 100000)
        if n_pts is None:
            return error_response("points 须为正整数")
        n_pts = max(10, min(n_pts, 200))
        x_key, x_lo, x_hi, x_name, x_unit = axis
        try:
            lo_req = float(body.get("x_min")) if body.get("x_min") is not None else x_lo
            hi_req = float(body.get("x_max")) if body.get("x_max") is not None else x_hi
        except (TypeError, ValueError):
            return error_response("x_min/x_max 须为有限数字")
        if not math.isfinite(lo_req) or not math.isfinite(hi_req):
            return error_response("x_min/x_max 须为有限数字")
        lo_req, hi_req = max(x_lo, min(x_hi, lo_req)), max(x_lo, min(x_hi, hi_req))
        if hi_req <= lo_req:
            lo_req, hi_req = x_lo, x_hi
        part = {"sens": sens}
        curves = []
        for mid in mids:
            mat = mats[mid]
            pts = []
            for i in range(n_pts):
                x = lo_req + (hi_req - lo_req) * i / (n_pts - 1)
                gain = material_mod.material_gain(part, mat, method, {x_key: x})
                pts.append({"x": round(x, 4), "gain": round(float(gain), 4)})
            curves.append({"material_id": mid, "label": str(mat.get("label") or mid), "points": pts})
        return json_response({
            "method": method, "x_key": x_key, "x_name": x_name, "x_unit": x_unit,
            "x_min": lo_req, "x_max": hi_req, "points": n_pts,
            "enabled": bool(CONFIG.MATERIAL_ENABLED),
            "gain_range": [float(CONFIG.MATERIAL_GAIN_MIN), float(CONFIG.MATERIAL_GAIN_MAX)],
            "curves": curves,
        })

    # ================================================================
    # 存在层总览（空间 × 身体一屏）：空间态遍历 _manor_store.list_sessions
    # （键 <character>_<session> 首下划线拆分），身体态 _store.get_or_create；
    # profile 不存在的会话跳过 body 段。渲染细节全部复用 core/render 纯函数。
    # ================================================================

    # ---- GET /presence/overview ----

    async def presence_overview(self):
        plugin = self.plugin
        manor = getattr(plugin, "_manor", None) or {}
        space_store = self._space_store()
        body_store = self._store()
        profiles = self._profiles()
        now = time.time()
        states: list[dict[str, Any]] = []
        if space_store is not None:
            for key in space_store.list_sessions():
                # 角色 id 可含下划线；优先按已知档案的最长前缀识别。
                known = next((pid for pid in sorted(profiles, key=len, reverse=True)
                              if key.startswith(pid + "_")), None)
                idx = len(known) if known else key.find("_")
                if idx <= 0:
                    continue
                character, session = key[:idx], key[idx + 1:]
                try:
                    st = space_store.get_or_create(key, manor)
                except Exception as exc:
                    logger.warning(f"[VirtualBody] presence overview 空间态汇总失败 {key}: {exc}")
                    continue
                she = st.get("she") or {}
                he = st.get("he") or {}
                mv = she.get("moving") if isinstance(she.get("moving"), dict) else None
                eta = None
                if mv is not None and isinstance(mv.get("until_ts"), (int, float)):
                    # ETA 面板键接线（C队扫描修复）：总览显示与注入渲染同阈值
                    try:
                        th = self.plugin._eta_thresholds() if hasattr(self.plugin, "_eta_thresholds") else None
                    except Exception:
                        th = None
                    eta = eta_band(max(0, int(float(mv["until_ts"]) - now)), thresholds=th)
                since_ts = parse_iso_local(she.get("since"))
                entry: dict[str, Any] = {
                    "character": character,
                    "session": session,
                    "she": {
                        "room": she.get("room"),
                        "room_label": room_label(manor, she.get("room")),
                        "moving": mv is not None,
                        "from_label": room_label(manor, mv.get("from")) if mv else None,
                        "to_label": room_label(manor, mv.get("to")) if mv else None,
                        "eta_band": eta if mv else None,
                        # "刚到"判定与 render.freshness_prefix 同阈值（<60s 且不在移动中）
                        "fresh": bool(mv is None and she.get("room")
                                      and since_ts is not None and 0 <= now - since_ts <= 60),
                        "source": she.get("source"),
                    },
                    "he": {
                        "room": he.get("room"),
                        "room_label": room_label(manor, he.get("room")),
                        "band": distance_band(manor, she.get("room"), he.get("room")),
                        "confidence": he.get("confidence"),
                        "moving": isinstance(he.get("moving"), dict),
                    },
                    "props_hint": _props_hint(manor, she.get("room")),
                    "updated_at": st.get("updated_at"),
                }
                entry["body"] = self._presence_body(body_store, profiles, character, session)
                entry["has_space"] = True
                entry["has_body"] = entry["body"] is not None
                states.append(entry)
        # 开关独立：身体已有状态、空间从未建立的会话也应出现在总览。
        # 只聚合现存状态，读面板不创建身体/空间状态或互动历史。
        existing = {(entry["character"], entry["session"]) for entry in states}
        if body_store is not None:
            for character, session in body_store.list_states():
                if (character, session) in existing or character not in profiles:
                    continue
                body = self._presence_body(body_store, profiles, character, session)
                if body is None:
                    continue
                states.append({
                    "character": character, "session": session,
                    "she": {"room": None, "room_label": None, "moving": False,
                            "from_label": None, "to_label": None, "eta_band": None,
                            "fresh": False, "source": None},
                    "he": {"room": None, "room_label": None, "band": "不明",
                           "confidence": None, "moving": False},
                    "props_hint": "", "updated_at": body.get("updated_at"),
                    "body": body, "has_space": False, "has_body": True,
                })
        states.sort(key=lambda s: (s["character"], s["session"]))
        # 联动链可视化（0.27.1）：当前生效的跨模块联动
        try:
            _links = {
                "rainy_active": bool(plugin._weather_is_rainy()),
                "weather_city": ((getattr(plugin, "_last_signals", {}) or {}).get("weather") or {}).get("city", ""),
                "triggers_active": len([x for x in (getattr(plugin, "_last_triggers", []) or [])
                                        if isinstance(x, dict) and float(x.get("expires_at") or 0) > now]),
                "last_trigger_hint": next((str(x.get("template_hint") or x.get("reason") or "")
                                           for x in reversed(getattr(plugin, "_last_triggers", []) or [])
                                           if isinstance(x, dict) and float(x.get("expires_at") or 0) > now), ""),
            }
        except Exception:
            _links = {}
        return json_response({
                "links": _links,
            "version": CONFIG.VERSION,
            "runtime": self._runtime_view(),
            "config": self._config_view(),   # 自定义参数速览（home 页只读卡）：无 config 键时页面显示"由后端版本决定"
            "manor": {
                "rooms": len(manor.get("rooms") or []),
                "edges": len(manor.get("edges") or []),
                "source": getattr(plugin, "_manor_source", "none"),
            },
            "states": states,
        })

    def _presence_body(self, body_store, profiles: dict[str, dict[str, Any]],
                       character: str, session: str) -> dict[str, Any] | None:
        """身体态聚合（profile 不存在 → None 跳过 body 段）：arousal/phase/前三部位/材质近况。"""
        profile = profiles.get(character)
        if body_store is None or profile is None:
            return None
        try:
            if callable(getattr(body_store, "load", None)) and body_store.load(character, session) is None:
                return None
            st = body_store.get_or_create(character, session, profile)
        except Exception as exc:
            logger.warning(f"[VirtualBody] presence overview 身体态汇总失败 {character}/{session}: {exc}")
            return None
        now_ts = parse_ts(st.get("updated_at"))
        parts_state = st.get("parts") or {}
        parts_meta = {str(p.get("id")): p for p in profile.get("parts") or [] if p.get("id")}
        views = {pid: self._part_view(pid, parts_state.get(pid), profile, now_ts) for pid in parts_meta}
        top_parts, recent_materials = [], []
        ttl = CONFIG.MATERIAL_STALE_SECONDS
        fn = getattr(self.plugin, "_feel_word_ttl", None)
        if callable(fn):
            try:
                ttl = fn()
            except Exception:
                pass
        for pid, pv in sorted(views.items(), key=lambda kv: -kv[1]["value"]):
            meta = parts_meta.get(pid) or {}
            pst = parts_state.get(pid) if isinstance(parts_state.get(pid), dict) else {}
            mat_word = self._material_word(meta, pst, now_ts, ttl=ttl)
            if len(top_parts) < 3:
                top_parts.append({"label": pv["label"], "value": pv["value"], "tier": pv["tier"],
                                  "sens": pv["sens"], "material": mat_word})
            if mat_word:
                recent_materials.append({"label": pv["label"], "material": mat_word,
                                         "at": pst.get("last_material_at")})
        recent_materials = recent_materials[:5]
        arousal = float(st.get("arousal") or 0.0)
        return {
            "character": character,
            "character_name": profile.get("name") or character,
            "arousal": round(arousal, 4),
            "tolerance": st.get("tolerance"),
            "phase": st.get("phase"),
            "phase_until": st.get("phase_until"),
            "tier": tier_of(arousal, profile.get("tiers") or []),
            "overload_threshold": overload_threshold(profile),
            "events_today": st.get("events_today", {}).get("count") if isinstance(st.get("events_today"), dict) else 0,
            "top_parts": top_parts,
            "recent_materials": recent_materials,
            "updated_at": st.get("updated_at"),
        }

    @staticmethod
    def _material_word(meta: dict[str, Any], part_state: dict[str, Any], now_ts: float | None,
                       ttl: int | None = None) -> str:
        """部位近期材质感受词（"丝绸·细滑"式，MATERIAL_STALE_SECONDS 时效内；与 inject 同规则）。"""
        if not CONFIG.MATERIAL_ENABLED:
            return ""
        ma = parse_ts(part_state.get("last_material_at"))
        ttl = CONFIG.MATERIAL_STALE_SECONDS if ttl is None else ttl
        if ma is None or now_ts is None or not 0 <= now_ts - ma <= ttl:
            return ""
        mat = material_mod.get_material(str(part_state.get("last_material") or ""))
        if mat is None:
            return ""
        return f"{mat.get('label')}·{material_mod.feel_word(meta, mat)}"[:12]
