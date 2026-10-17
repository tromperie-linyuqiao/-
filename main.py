"""astrbot_plugin_virtual_body —— 虚拟存在层（身体 ⊕ 空间 ⊕ 天气 ⊕ 心绪）。

0.27.0 大成版：吸收庄园空间层（0.24）、神经材质层（0.25）、天气与心绪编排（0.27）。
单注入点双层块（空间前·身体后），心绪触发供 外部调用方 主动消息消费。
只产出数据与约束（演算+注入），不写台词、不写剧情、不生成角色回复。
薄壳纪律：业务逻辑全在 core/ 纯函数层，本文件只做注册、调度、权限、落盘。
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
try:
    from astrbot.core.agent.message import TextPart
except ImportError:   # 旧版 AstrBot 无该模块时回退 prompt 拼接路径
    TextPart = None

from . import CONFIG
from .core import decay, events as events_mod, inject, state as state_mod
from .core import adaptation, bridge, extract as extract_mod, render as render_mod
from .core import persist_contact
from .core import material as material_mod
from .core.fallback_guard import FallbackStopGuard
from .core.runtime import DailyFallbackBudget, RecentMessageIds
from .core.presence_runtime import PresenceIntegration
from .core.presence_context import strip_context as strip_environment_context
from .core import space as space_mod, traverse as traverse_mod
from .core import proactive_intent as proactive_intent_mod
from .core.extract import room_words as _room_words
from .core.store import (
    ManorStore,
    load_manor,
    load_manor_builtin,
    load_objects,
    merge_legacy_manor_override,
    migrate_legacy_manor,
    save_manor_override,
    save_objects,
)
from .core import mood_engine, mood_signals as mood_signals_mod
from .core import mood_store as mood_store_mod

PLUGIN_NAME = "astrbot_plugin_virtual_body"
_TICK_IDLE_SKIP = 5

# ---- B4 天气→雨天动线：雨类关键词（中文直配 + 英文小写；识别 天气快照 若将来落盘的缓存 JSON）----
_WEATHER_RAIN_WORDS = ("雨", "雷暴", "thunder", "rain", "shower", "drizzle")
_WEATHER_TEXT_KEYS = ("weather", "condition", "text", "wtype", "day", "night", "desc")


def _weather_data_has_rain(data: Any, depth: int = 0) -> bool:
    """宽容识别天气 JSON 里的降雨迹象（纯函数）：递归（≤4 层）找 weather/condition 类
    字符串字段含雨词。任何结构缺失/不认识 → False，绝不抛。"""
    if depth > 4:
        return False
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, str) and any(w in str(k).lower() for w in _WEATHER_TEXT_KEYS):
                low = v.lower()
                if any(w in v or w in low for w in _WEATHER_RAIN_WORDS):
                    return True
            elif _weather_data_has_rain(v, depth + 1):
                return True
        return False
    if isinstance(data, list):
        return any(_weather_data_has_rain(v, depth + 1) for v in data)
    return False


# ---- 覆盖层代际迁移（0.27.9 结构健康轮：防"覆盖层旧参数压制内置新参数"复发）----

# 旧代判定：覆盖层无 base_version 视为 legacy；有版本但主参数与内置"代际差"过大同样处理。
# 代际差规则：同 id 部位的 half_life 比内置小 4 倍以上（旧代固化的衰减节奏，0.23.2 时代 τ 事故形态）。
_OVERRIDE_HL_GENERATION_RATIO = 4.0


def _migrate_legacy_override(builtin_profile: dict[str, Any],
                             override_profile: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """profile 覆盖层代际迁移（纯函数）：以覆盖层为差异、内置为基线做重 merge。

    - legacy 判定：覆盖层无 base_version（简单规则；有版本戳的新覆盖层也照样跑数值体检，幂等无害）。
    - 动作：覆盖层存在且内置同 id 也存在的部位，若其 half_life < 内置值/4（旧代固化）→
      half_life 以内置值覆盖；label/aliases/radiate/sens 等用户自定义全部保留。
      （sens 属用户手感的可能性高，不动；代际事故的形态是节奏参数 τ。）
    - 补盖 base_version=CONFIG.VERSION（写回后不再重触发；由调用方落盘）。
    返回 (合并后覆盖层副本, 数值是否有改动)。绝不抛出。"""
    try:
        out = json.loads(json.dumps(override_profile if isinstance(override_profile, dict) else {}))
    except (TypeError, ValueError):
        return (dict(override_profile or {}), False)
    if not isinstance(out, dict) or not isinstance(builtin_profile, dict):
        return (out, False)
    base_parts: dict[str, dict[str, Any]] = {}
    for p in builtin_profile.get("parts") or []:
        if isinstance(p, dict) and p.get("id"):
            try:
                hl = float(p.get("half_life"))
            except (TypeError, ValueError):
                continue
            if hl > 0:
                base_parts[str(p["id"])] = {"half_life": hl}
    changed = False
    parts = out.get("parts")
    if isinstance(parts, list):
        for p in parts:
            if not isinstance(p, dict) or not p.get("id"):
                continue
            base = base_parts.get(str(p["id"]))
            if base is None:
                continue   # 用户自建部位：无内置基线可对
            try:
                cur = float(p.get("half_life"))
            except (TypeError, ValueError):
                cur = None
            if cur is not None and cur > 0 and cur * _OVERRIDE_HL_GENERATION_RATIO < base["half_life"]:
                p["half_life"] = base["half_life"]
                changed = True
    if changed:
        out["base_version"] = CONFIG.VERSION   # 数值迁移过的覆盖层补盖版本戳（写回后不再重触发）
    return (out, changed)


@register(
    PLUGIN_NAME,
    "menglimi",
    "虚拟存在层：为角色维护身体数值与庄园空间状态（位置/移动/家具/体感联动），轮次前注入空间块+身体块。只产出数据与约束，表演交给模型。",
    CONFIG.VERSION,
)
class VirtualBodyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self._store: state_mod.StateStore | None = None
        self._profiles: dict[str, dict[str, Any]] = {}
        self._profiles_base: dict[str, dict[str, Any]] = {}   # 参数层基底（无命名层）
        self._tick_task: asyncio.Task | None = None
        self._fb_day = ""
        self._fb_count = 0
        self._fb_cache: dict[str, tuple[str, str]] = {}   # text → (day, raw)：同文本当日不重复调用
        self._fb_tasks: set[asyncio.Task] = set()          # 兜底后台任务引用（防 GC）
        self._fb_stop_guard = FallbackStopGuard()
        self._fb_inflight: dict[tuple[int, str, str], asyncio.Task] = {}
        self._fallback_budget: DailyFallbackBudget | None = None
        self._observations = RecentMessageIds()
        self._stopping = False
        self._initialized = False
        self._lifecycle_lock = asyncio.Lock()
        self._lifecycle_epoch = 0
        self._tick_health = {"last_at": None, "cycles": 0, "errors": 0, "last_error_type": None}
        self._bindings: dict[str, str] = {}   # session→character（/vbody 维护）
        self._web_api = None
        # ---- 空间层（存在层合并 P1：manor 并入，状态文件 .space.json 分立）----
        self._manor_store: ManorStore | None = None
        self._manor: dict[str, Any] = {}
        self._manor_source = "none"
        self._space_data_dir = ""
        self._last_change: dict[str, float] = {}             # 空间 session → 最近位置变化时刻
        self._last_signals: dict = {}                        # 心绪：最近一轮信号（WebUI 读）
        self._last_triggers: list = []                       # 心绪：当前触发（WebUI 读）
        self._weather_cache: tuple[int, bool] = (0, False)   # B4：(mtime_ns, rainy) 天气文件 mtime 缓存
        self._presence = None
        try:
            self._presence = PresenceIntegration(self, context)
        except Exception as exc:
            logger.warning('[VirtualBody] 统一环境接口暂不可用: %s', type(exc).__name__)

    # ---- 配置读取 ----

    def _cfg(self, key: str, default: Any = None) -> Any:
        val = None
        try:
            val = self.config.get(key, None)
        except Exception:
            val = None
        return default if val is None else val

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            value = self._cfg(key, default)
            return default if isinstance(value, bool) else int(value)
        except (TypeError, ValueError, OverflowError):
            return default

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            value = self._cfg(key, default)
            numeric = float(value)
            return default if isinstance(value, bool) or not math.isfinite(numeric) else numeric
        except (TypeError, ValueError, OverflowError):
            return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        val = self._cfg(key, default)
        if isinstance(val, str):
            token = val.strip().lower()
            if token in {"false", "0", "off", "no", ""}:
                return False
            if token in {"true", "1", "on", "yes"}:
                return True
            return default
        return bool(val) if val is not None else default

    # ---- 0.27.9 面板化：体验参数配置桥（面板键 > CONFIG 常量，坏值/越界回常量默认）----

    def _phase_cfg(self) -> dict[str, Any]:
        """phase 机器参数（decay.advance_state 关键字透传）：余韵/不应期时长与敏感倍数。"""
        ag = self._cfg_int("vb_afterglow_seconds", int(CONFIG.AFTERGLOW_SECONDS))
        cd = self._cfg_int("vb_cooldown_seconds", int(CONFIG.COOLDOWN_SECONDS))
        agm = self._cfg_float("vb_afterglow_mult", float(CONFIG.AFTERGLOW_SENS_MULT))
        cdm = self._cfg_float("vb_cooldown_mult", float(CONFIG.COOLDOWN_SENS_MULT))
        if not 300 <= ag <= 7200:
            ag = int(CONFIG.AFTERGLOW_SECONDS)
        if not 300 <= cd <= 7200:
            cd = int(CONFIG.COOLDOWN_SECONDS)
        if not 1.0 <= agm <= 2.0:
            agm = float(CONFIG.AFTERGLOW_SENS_MULT)
        if not 0.2 <= cdm <= 1.0:
            cdm = float(CONFIG.COOLDOWN_SENS_MULT)
        verbal_weight = self._cfg_float("vb_verbal_arousal_weight", float(CONFIG.VERBAL_AROUSAL_WEIGHT))
        if not CONFIG.VERBAL_AROUSAL_WEIGHT_RANGE[0] <= verbal_weight <= CONFIG.VERBAL_AROUSAL_WEIGHT_RANGE[1]:
            verbal_weight = float(CONFIG.VERBAL_AROUSAL_WEIGHT)
        return {"afterglow_seconds": ag, "cooldown_seconds": cd,
                "afterglow_mult": agm, "cooldown_mult": cdm,
                "night_window": self._night_window(), "verbal_weight": verbal_weight}

    def _night_window(self) -> tuple[int, int] | None:
        """昼夜节律窗口（vb_night_window，"23-5" 支持跨午夜）；坏值回 None=CONFIG 默认。"""
        return decay.parse_night_window(str(self._cfg("vb_night_window", CONFIG.VB_NIGHT_WINDOW_DEFAULT) or ""))

    # ---- 0.31.0 持续接触 + 亲密语境增益：面板配置桥（面板键 > CONFIG 常量，坏值/越界回常量） ----

    def _persist_on(self) -> bool:
        """持续接触总开关（CONFIG 常量与面板键 cs_persist_enabled 相与）。"""
        return CONFIG.CS_PERSIST_ENABLED and self._cfg_bool("cs_persist_enabled", True)

    def _persist_cfg(self) -> dict[str, Any]:
        """持续接触参数（persist_contact.persist_events 消费）：延续率/消息间隔窗/tick 频率。"""
        rate = self._cfg_float("cs_persist_rate", float(CONFIG.CS_PERSIST_RATE))
        gap = self._cfg_int("cs_persist_max_gap", int(CONFIG.CS_PERSIST_MAX_GAP))
        rlo, rhi = CONFIG.CS_PERSIST_RATE_RANGE
        glo, ghi = CONFIG.CS_PERSIST_MAX_GAP_RANGE
        if not rlo <= rate <= rhi:
            rate = float(CONFIG.CS_PERSIST_RATE)
        if not glo <= gap <= ghi:
            gap = int(CONFIG.CS_PERSIST_MAX_GAP)
        return {"rate": rate, "max_gap": float(gap),
                "tick_every": int(CONFIG.CS_PERSIST_TICK_EVERY),
                "tick_interval": float(self._tick_seconds())}

    def _intimacy_cfg(self) -> dict[str, float]:
        """亲密语境增益参数（decay.advance_state 消费）：L1/L2 倍率。"""
        l1 = self._cfg_float("vb_intimacy_boost_l1", float(CONFIG.INTIMACY_BOOST_L1))
        l2 = self._cfg_float("vb_intimacy_boost_l2", float(CONFIG.INTIMACY_BOOST_L2))
        if not CONFIG.INTIMACY_BOOST_L1_RANGE[0] <= l1 <= CONFIG.INTIMACY_BOOST_L1_RANGE[1]:
            l1 = float(CONFIG.INTIMACY_BOOST_L1)
        if not CONFIG.INTIMACY_BOOST_L2_RANGE[0] <= l2 <= CONFIG.INTIMACY_BOOST_L2_RANGE[1]:
            l2 = float(CONFIG.INTIMACY_BOOST_L2)
        return {"l1": l1, "l2": l2}

    def _constraint_text(self) -> str | None:
        """注入约束行文案（vb_constraint_text）：与内置默认相同或为空 → None（保持历史输出）。"""
        text = str(self._cfg("vb_constraint_text", CONFIG.RENDER_CONSTRAINT_FIXED) or "").strip()[:160]
        return text if text and text != CONFIG.RENDER_CONSTRAINT_FIXED else None

    def _feel_word_ttl(self) -> int:
        ttl = self._cfg_int("vb_feel_word_ttl", int(CONFIG.MATERIAL_STALE_SECONDS))
        return max(60, min(3600, ttl))

    def _eta_thresholds(self) -> tuple[int, int] | None:
        """空间距离档阈值（sp_eta_thresholds "15,60"）；坏值回 None=内置 15/60。"""
        return render_mod.parse_eta_thresholds(
            str(self._cfg("sp_eta_thresholds", CONFIG.SP_ETA_THRESHOLDS_DEFAULT) or ""))

    def _hearing_text(self, key: str, default: str) -> str | None:
        """听觉规则文案（sp_hearing_same_floor / sp_hearing_adjacent）：等于默认 → None 保持历史措辞。"""
        text = str(self._cfg(key, default) or "").strip()[:24]
        return text if text and text != default else None

    # ---- profile 加载 ----

    def _plugin_dir(self) -> str:
        return os.path.dirname(os.path.abspath(__file__))

    def _load_profiles(self) -> None:
        candidates: list[str] = [os.path.join(self._plugin_dir(), "data", "profiles")]
        override = str(self._cfg("vb_profiles_dir", "") or "").strip()
        if override:
            candidates.insert(0, override)
        # plugin_data 内的 profiles 优先级最高（本地迭代可不改插件目录）
        try:
            data_dir = StarTools.get_data_dir(PLUGIN_NAME)
            candidates.insert(0, os.path.join(str(data_dir), "profiles"))
        except Exception:
            pass
        loaded: dict[str, dict[str, Any]] = {}
        sources: dict[str, str] = {}   # pid → 实际加载文件夹（覆盖层代际体检用）
        data_dir = ""
        try:
            data_dir = str(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            pass
        for folder in reversed(candidates):   # 低优先级先处理，后写的覆盖先写的 → plugin_data 最高
            if not os.path.isdir(folder):
                continue
            for name in sorted(os.listdir(folder)):
                if not name.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(folder, name), "r", encoding="utf-8") as f:
                        prof = json.load(f)
                    pid = str(prof.get("id") or "").strip()
                    # 状态文件身份已按元数据校验；空间桥按已知角色前缀解析。
                    if pid and re.fullmatch(r"[a-z][a-z0-9_-]*", pid):
                        loaded[pid] = prof
                        sources[pid] = folder
                    elif pid:
                        logger.warning(f"[VirtualBody] profile id 非法（须小写字母开头，仅小写字母/数字/连字符/下划线）: {name} id={pid}")
                except (OSError, ValueError) as exc:
                    logger.warning(f"[VirtualBody] profile 加载失败 {name}: {exc}")
        # 覆盖层代际体检（0.27.9 结构健康轮）：非内置来源的 profile 与内置同 id 基线比对，
        # half_life 小 4 倍以上（旧代固化，0.23.2 时代 τ 形态）→ 数值以内置刷新、用户自定义保留并回写。
        # 无论覆盖层有无 base_version 都跑数值体检（幂等；版本戳只影响 save 端写入）。
        builtin_dir = os.path.join(self._plugin_dir(), "data", "profiles")
        for pid, prof in list(loaded.items()):
            folder = sources.get(pid) or ""
            if not folder or folder == builtin_dir:
                continue   # 内置基线自身不迁移；无来源记录（理论不可达）跳过
            base_path = os.path.join(builtin_dir, f"{pid}.json")
            if not os.path.isfile(base_path):
                continue   # 纯用户角色：无内置基线无从判代际
            try:
                with open(base_path, "r", encoding="utf-8") as f:
                    builtin_prof = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(builtin_prof, dict) or str(builtin_prof.get("id") or "") != pid:
                continue
            try:
                merged, changed = _migrate_legacy_override(builtin_prof, prof)
            except Exception as exc:
                logger.warning(f"[VirtualBody] 覆盖层代际体检异常 {pid}: {exc}")
                continue
            if not changed:
                continue
            loaded[pid] = merged
            try:
                tmp = os.path.join(folder, f"{pid}.json.tmp")
                with open(tmp, "w", encoding="utf-8", newline="") as f:
                    json.dump(merged, f, ensure_ascii=False, indent=1)
                os.replace(tmp, os.path.join(folder, f"{pid}.json"))
                logger.warning(f"[VirtualBody] 覆盖层代际迁移：{pid} 部位 half_life 旧代固化，"
                               "已按内置基线刷新并回写（label/别名/传导等自定义保留）")
            except OSError as exc:
                logger.warning(f"[VirtualBody] 覆盖层代际迁移回写失败 {pid}: {exc}（本次按迁移后内存值继续）")
        enabled = str(self._cfg("vb_enabled_profiles", "") or "").strip()
        if enabled:
            keep = {x.strip() for x in enabled.split(",") if x.strip()}
            loaded = {k: v for k, v in loaded.items() if k in keep}
        self._profiles = loaded
        # 参数层基底（未合并命名层）：WebUI 编辑/保存必须以此为准——
        # 否则命名会被烤进覆盖文件 plugin_data/profiles/<id>.json，之后清空命名也删不掉
        self._profiles_base = {pid: json.loads(json.dumps(prof)) for pid, prof in loaded.items()}
        # 命名层合并：label/aliases 独立文件（参数迭代不读写名称层）
        if data_dir:
            for pid, prof in self._profiles.items():
                state_mod.apply_names_overlay(prof, state_mod.load_names_overlay(data_dir, pid))
        logger.info(f"[VirtualBody] 已加载 profile: {sorted(self._profiles)}")

    def _default_character(self) -> str:
        cid = str(self._cfg("vb_default_character", "") or "").strip()
        if cid in self._profiles:
            return cid
        for pid in self._profiles:
            return pid
        return ""

    def _migrate_manor_override(self) -> None:
        """manor 覆盖层代际迁移（0.27.9，与 profile 覆盖层同机制）：覆盖层无 schema 版本而内置有 →
        只对 rooms/edges 数值字段（center/size/seconds）以内置为基线刷新（布局代际演进），
        label/aliases/ambiance/props/scene_words/房间增删等用户自定义全部保留，补盖 schema 并回写。"""
        if self._manor_source != "plugin_data" or not self._manor:
            return
        try:
            builtin = load_manor_builtin(os.path.dirname(os.path.abspath(__file__)))
            merged, changed = merge_legacy_manor_override(self._manor, builtin)
            if not changed:
                return
            self._manor = merged
            if save_manor_override(self._space_data_dir, merged):
                logger.warning("[VirtualBody] manor 覆盖层代际迁移：rooms/edges 数值已按内置基线刷新并回写"
                               "（label/氛围/场景词等自定义保留）")
            else:
                logger.warning("[VirtualBody] manor 覆盖层代际迁移回写失败（本次按迁移后内存值继续）")
        except Exception as exc:
            logger.warning(f"[VirtualBody] manor 覆盖层代际体检异常: {exc}")

    # ---- 生命周期 ----

    async def initialize(self):
        async with self._lifecycle_lock:
            if self._initialized and self._tick_task is not None and not self._tick_task.done():
                return
            try:
                await self._initialize_runtime()
                self._initialized = True
            except BaseException:
                # Preserve the exception for AstrBot, but invalidate partial
                # observations/tasks so retrying initialization is safe.
                self._stopping = True
                self._initialized = False
                self._lifecycle_epoch += 1
                if self._presence is not None:
                    self._presence.close()
                await self._cancel_runtime_tasks()
                raise

    async def _initialize_runtime(self):
        self._lifecycle_epoch += 1
        self._stopping = False
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self._store = state_mod.StateStore(str(data_dir))
        self._fallback_budget = DailyFallbackBudget(str(data_dir))
        # 时区部署口面板化：vb_tz_utc_offset 配置覆盖 CONFIG 常量（-1=用容器时区）
        tz_off = self._cfg_int("vb_tz_utc_offset", -1)
        if tz_off == -1:
            CONFIG.TZ_UTC_OFFSET = None
        elif -13 <= tz_off <= 14:
            CONFIG.TZ_UTC_OFFSET = tz_off
        # 空间层挂载（存在层合并 P1）：状态文件 .space.json 与身体层同目录分立
        self._space_data_dir = str(data_dir)
        material_mod.configure_storage(self._space_data_dir)
        self._manor_store = ManorStore(self._space_data_dir, suffix=".space.json")
        self._manor, self._manor_source = load_manor(
            self._space_data_dir, os.path.dirname(os.path.abspath(__file__)))
        self._migrate_manor_override()
        self._load_profiles()
        # 数据回退迁移（存在层合并 P3·锚点 1）：旧 manor plugin_data → 新路径，一次性；
        # 须在 _load_profiles 之后（默认角色取自 profiles）。旧目录不存在时静默跳过。
        legacy_dir = os.path.normpath(os.path.join(self._space_data_dir, "..", "astrbot_plugin_virtual_manor"))
        if self._cfg_bool("sp_migrate_legacy_manor", False) and os.path.isdir(legacy_dir):
            migrated = migrate_legacy_manor(legacy_dir, self._space_data_dir, self._default_character())
            if migrated > 0:
                logger.info("[VirtualBody] 旧庄园数据已回退迁移 %d 个文件（旧文件保留，.migrated 已标记）" % migrated)
        self._bindings = self._store.load_bindings()
        if self._presence is not None:
            self._presence.start()
        self._register_web_api_if_available()
        self._tick_task = asyncio.create_task(self._tick_loop())
        logger.info("[VirtualBody] 初始化完成，tick=%ss tz_offset=%s" % (self._tick_seconds(), tz_off))
        # 部署窗口期防护：manor 插件若仍在加载列表（未按安装提示停用），
        # 九指令与空间注入会双份（vb 吸收了 manor 全部功能）——警告提醒而非拒绝启动
        try:
            _peers = [str(getattr(star, "name", "")) for star in (self.context.get_all_stars() or [])]
            if any("astrbot_plugin_virtual_manor" in n for n in _peers):
                logger.warning("[VirtualBody] 检测到 astrbot_plugin_virtual_manor 仍在加载！"
                               "空间指令与注入将双份，请在面板停用 manor（安装提示第二节第 3 步）")
        except Exception:
            pass
        logger.info("[VirtualBody] 空间层已挂载 rooms=%s source=%s" % (
            len(self._manor.get("rooms") or []), self._manor_source))

    def _register_web_api_if_available(self) -> None:
        """AstrBot 官方插件拓展页面 API（模式照搬 外部调用方 integration_status.py）。"""
        if not hasattr(self.context, "register_web_api"):
            logger.debug("[VirtualBody] 当前 AstrBot 版本未提供 register_web_api，跳过 WebUI 注册")
            return
        try:
            from .web_api import VirtualBodyWebApi

            self._web_api = VirtualBodyWebApi(self)
            self._web_api.register_routes()
        except Exception as exc:
            logger.warning(f"[VirtualBody] WebUI API 注册失败: {exc}")

    async def terminate(self):
        async with self._lifecycle_lock:
            self._stopping = True
            self._initialized = False
            self._lifecycle_epoch += 1
            if getattr(self, '_presence', None) is not None:
                self._presence.close()
            await self._cancel_runtime_tasks()

    async def _cancel_runtime_tasks(self):
        tasks = set(self._fb_tasks) | set(self._fb_inflight.values())
        if self._tick_task is not None:
            tasks.add(self._tick_task)
        for task in tasks:
            task.cancel()
        if tasks:
            # A provider may suppress cancellation; bound unloading and reject its late result.
            await asyncio.wait(tasks, timeout=1.0)
            for task in tasks:
                task.add_done_callback(self._consume_runtime_task)
        self._fb_tasks.clear()
        self._fb_inflight.clear()
        self._tick_task = None

    @staticmethod
    def _consume_runtime_task(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()

    def _tick_seconds(self) -> int:
        return max(15, self._cfg_int("vb_tick_seconds", CONFIG.TICK_INTERVAL_SECONDS))

    # ---- 空间层基建（存在层合并 P1） ----

    def _tz(self) -> int | None:
        """空间层时区：sp_tz_utc_offset 显式配置（≠-1）时用它，否则跟随身体层 TZ_UTC_OFFSET。"""
        off = self._cfg_int("sp_tz_utc_offset", -1)
        if off != -1 and -13 <= off <= 14:
            return off
        return CONFIG.TZ_UTC_OFFSET

    def _space_enabled(self) -> bool:
        return self._manor_store is not None and self._cfg_bool("sp_enabled", True)

    def _space_key(self, character: str, sender_id: str) -> str | None:
        """空间层会话键 <character>_<sender>（锚点 2 命名纪律）。
        manor whitelist 非空时取与 vb 会话的交集，空表则跟随 vb 白名单（老用户零感知）。"""
        wl = {str(x) for x in (self._manor.get("whitelist") or [])}
        if wl and str(sender_id) not in wl:
            return None
        return f"{character}_{sender_id}"

    def _space_identity(self, key: str) -> tuple[str, str] | None:
        for character in sorted(self._profiles, key=len, reverse=True):
            prefix = character + "_"
            if key.startswith(prefix) and key[len(prefix):]:
                return character, key[len(prefix):]
        return None

    def _resolve_space(self, event: AstrMessageEvent) -> str | None:
        """空间指令会话解析：vb 角色绑定/白名单 × manor whitelist 交集 → 空间键；群聊一律 None。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if len(umo.split(":")) > 1 and "Group" in umo.split(":")[1]:
            return None
        sender = str(event.get_sender_id() or "")
        if sender and str(event.get_self_id() or "") == sender:
            return None
        resolved = self._resolve_session(sender)
        if resolved is None:
            return None
        if not self._space_enabled():
            return None
        return self._space_key(resolved[0], sender)

    def _resolve_room_arg(self, token: str) -> str | None:
        token = token.strip().lstrip("/")
        if token in space_mod.known_rooms(self._manor):
            return token
        for w, rid in _room_words(self._manor):
            if w == token or token in w:
                return rid
        return None

    def _pc_current_plan_text(self, now: float | None = None) -> str | None:
        """No implicit reads of another plugin's private schedule files."""
        return None

    def _weather_is_rainy(self, now: float | None = None) -> bool:
        info = mood_signals_mod.read_weather(self._space_data_dir, now=now)
        return bool(info.get("available") and info.get("is_rain"))

    def _persist_weather(self, city: str, text: str) -> None:
        """天气查询落盘（并入自 天气快照）：原子写 data_dir/weather/weather_last.json。"""
        try:
            folder = os.path.join(self._space_data_dir, CONFIG.WEATHER_SNAPSHOT_SUBDIR)
            os.makedirs(folder, exist_ok=True)
            import datetime as _dt
            observed = "\n".join(
                line for line in text.splitlines()
                if re.match(r"^(?:实时:|天气:|城市: .*温度:)", line)
            )[:CONFIG.WEATHER_SNAPSHOT_MAX_CHARS]
            if not observed:
                return
            snapshot = {
                "updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
                "city": city,
                "weather": observed,
                "condition": observed,
                "detail": text[:CONFIG.WEATHER_SNAPSHOT_MAX_CHARS],
            }
            final = os.path.join(folder, CONFIG.WEATHER_SNAPSHOT_FILE)
            tmp = "%s.%d.tmp" % (final, os.getpid())
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(snapshot, f, ensure_ascii=False)
            os.replace(tmp, final)
            self._weather_cache = (0, False)   # 失效缓存，下轮重读
        except Exception:
            pass

    @filter.llm_tool(name="weather_detail")
    async def weather_detail(self, event: AstrMessageEvent, city: str = "", source: str = "auto") -> str:
        """详细天气查询兼容入口；本地候选不包含未授权的多源查询器。

        Args:
            city(string): 希望查询的城市；当前不会对该城市发起网络请求。
            source(string): 保留兼容参数；没有配置的来源不会伪造查询结果。
        """
        return "当前未配置详细天气查询器，无法执行城市预报、预警或气象图片查询。已接入的有效天气观测可使用 presence_context_query 查询；无观测时天气未知。"

    # ---- 心绪编排（0.27.0 并入，数据落 data_dir/mood/） ----

    def _mood_dir(self) -> str:
        return os.path.join(self._space_data_dir, CONFIG.MO_DATA_SUBDIR)

    # ---- 0.30.0 存在深化：语言调情 / 情绪耦合 / 长期适应 / 主动意图（tick·注入·事件三路径共用） ----

    def _verbal_cfg(self, profile: dict[str, Any]) -> dict[str, float]:
        """语言调情参数：面板键 > CONFIG 常量 > profile.params 再覆盖（完整配置链，坏值回常量）。"""
        rate = self._cfg_float("vb_verbal_arousal_rate", float(CONFIG.VERBAL_AROUSAL_RATE))
        cap = self._cfg_float("vb_verbal_arousal_cap", float(CONFIG.VERBAL_AROUSAL_CAP))
        tau = self._cfg_float("vb_verbal_arousal_tau", float(CONFIG.VERBAL_AROUSAL_TAU))
        rlo, rhi = CONFIG.VERBAL_AROUSAL_RATE_RANGE
        clo, chi = CONFIG.VERBAL_AROUSAL_CAP_RANGE
        tlo, thi = CONFIG.VERBAL_AROUSAL_TAU_RANGE
        if not rlo <= rate <= rhi:
            rate = float(CONFIG.VERBAL_AROUSAL_RATE)
        if not clo <= cap <= chi:
            cap = float(CONFIG.VERBAL_AROUSAL_CAP)
        if not tlo <= tau <= thi:
            tau = float(CONFIG.VERBAL_AROUSAL_TAU)
        return {
            "rate": decay.profile_param(profile, "verbal_arousal_rate", rate, rlo, rhi),
            "cap": decay.profile_param(profile, "verbal_arousal_cap", cap, clo, chi),
            "tau": decay.profile_param(profile, "verbal_arousal_tau", tau, tlo, thi),
        }

    def _verbal_emotions(self) -> tuple[str, ...]:
        """亲密情绪集（vb_verbal_emotions 逗号分隔，支持中文逗号）；空/坏值回 CONFIG 默认。"""
        raw = str(self._cfg("vb_verbal_emotions", "") or "").strip()
        if not raw:
            return tuple(CONFIG.VERBAL_INTIMATE_EMOTIONS)
        words = tuple(w.strip() for w in raw.replace("，", ",").split(",") if w.strip())
        return words or tuple(CONFIG.VERBAL_INTIMATE_EMOTIONS)

    def _mood_score_now(self, now: float | None = None) -> float | None:
        """外部情绪 分（read_mood 已融合身体 warm hint）；不可用/坏值 → None（耦合走中性）。"""
        if not self._space_data_dir:
            return None
        info = mood_signals_mod.read_mood(self._space_data_dir, now=now)
        if not isinstance(info, dict) or not info.get("available"):
            return None
        try:
            return float(info.get("score"))
        except (TypeError, ValueError):
            return None

    def _apply_signal_couplings(self, st: dict[str, Any], profile: dict[str, Any], now: float) -> None:
        """推进前置的信号耦合（0.30.0）：①语言调情→verbal_arousal（外部调用方 情绪只读 mtime 缓存）；
        ②情绪→身体临时系数（advance_state 消费即清）。任何一路异常不阻塞演算。"""
        if not self._space_data_dir:
            return
        try:
            if self._cfg_bool("vb_verbal_arousal_enabled", True):
                wl = [str(x) for x in profile.get("whitelist") or []]
                session = str(st.get("session") or "")
                info = mood_signals_mod.read_verbal(self._space_data_dir, [session], self._verbal_emotions(), now=now) \
                    if session and session in wl else {"available": False, "is_intimate": False}
                decay.verbal_arousal_step(
                    st, bool(info.get("available") and info.get("is_intimate")), now,
                    self._verbal_cfg(profile))
        except Exception as exc:
            logger.debug(f"[VirtualBody] 语言调情信号异常: {exc}")
        try:
            if CONFIG.MOOD_COUPLE_ENABLED and self._cfg_bool("vb_mood_coupling_enabled", True):
                score = self._mood_score_now(now)
                if score is not None:
                    decay.apply_mood_coupling(st, score)
        except Exception as exc:
            logger.debug(f"[VirtualBody] 情绪耦合异常: {exc}")

    def _maybe_daily_adaptation(self, character: str, session: str, st: dict[str, Any], now: float) -> bool:
        """长期身体记忆（0.30.0）：每日一次（日键防重入，日切走 decay.tz_local_time 统一时区入口）。
        就地写 state.longterm_mult / longterm_day，返回是否执行了本日首轮。"""
        if not (CONFIG.ADAPT_ENABLED and self._cfg_bool("vb_adaptation_enabled", True)):
            return False
        today = time.strftime("%Y-%m-%d", decay.tz_local_time(now))
        if st.get("longterm_day") == today:
            return False
        try:
            _, changed = adaptation.daily_adaptation(st, self._profiles.get(str(character)) or {}, now)
            st["longterm_day"] = today
            logger.info(f"[VirtualBody] 长期适应：{changed} 部位更新（{character}/{session}）")
            return True
        except Exception as exc:
            logger.error(f"[VirtualBody] 长期适应异常 {character}/{session}: {exc}")
            return False

    def _maybe_proactive_intent(self, st: dict[str, Any], profile: dict[str, Any], now: float) -> None:
        """主动触碰意图（0.30.0）：三条件命中且冷却外 → state.proactive_intent={ts,hint} 落账
        （inject 渲染"她<短语>"尾巴；外部调用方 快照按字段读 state 文件同样可见——vb 侧自洽不依赖 外部调用方 改动）。"""
        if not (CONFIG.PROACTIVE_TOUCH_ENABLED and self._cfg_bool("vb_proactive_touch_enabled", True)):
            return
        try:
            hint = proactive_intent_mod.should_intent_touch(
                st, profile, now, cfg={
                    "arousal_min": self._cfg_float("vb_proactive_touch_arousal", float(CONFIG.PROACTIVE_TOUCH_AROUSAL)),
                    "verbal_min": self._cfg_float("vb_proactive_touch_verbal_min", float(CONFIG.PROACTIVE_TOUCH_VERBAL_MIN)),
                    "cooldown": self._cfg_float("vb_proactive_touch_cooldown", float(CONFIG.PROACTIVE_TOUCH_COOLDOWN)),
                })
            if hint:
                st["proactive_intent"] = {"ts": decay.iso_ts(now), "hint": hint}
                logger.debug(f"[VirtualBody] 主动触碰意图: {hint}")
        except Exception as exc:
            logger.debug(f"[VirtualBody] 主动意图异常: {exc}")

    def _write_body_mood_hint(self, now: float | None = None) -> None:
        """身体→情绪（0.30.0）：overload 进余韵 → mood/body_mood_hint.json {ts,hint:warm}，
        read_mood 融合（2h 线性衰减 +0.15 封顶）。失败静默（hint 只是增益不是必需）。"""
        try:
            mood_store_mod.save_json(self._mood_dir(), CONFIG.MO_BODY_HINT_FILE,
                                     {"ts": decay.iso_ts(time.time() if now is None else now), "hint": "warm"})
        except Exception:
            pass

    def _primary_presence_session(self) -> str | None:
        profile = self._profiles.get(self._default_character()) or {}
        users = {str(v) for v in profile.get("whitelist") or [] if str(v).strip()}
        if len(users) != 1:
            return None
        resolved = self._resolve_session(next(iter(users)))
        return f"{resolved[0]}_{resolved[1]}" if resolved is not None else None

    def _record_runtime_error(self, exc: Exception) -> None:
        health = self._tick_health
        health["errors"] += 1
        health["last_error_type"] = type(exc).__name__

    def runtime_diagnostics(self) -> dict[str, Any]:
        day = self._fallback_day()
        budget = self._fallback_budget
        used = budget.used(day) if budget is not None else self._fb_count
        limit = max(0, self._cfg_int("vb_llm_fallback_daily_limit", CONFIG.LLM_FALLBACK_DAILY_LIMIT))
        return {
            "lifecycle": {"stopping": self._stopping},
            "tick": dict(self._tick_health),
            "fallback": {"enabled": self._cfg_bool("vb_llm_fallback_enabled", False),
                         "daily_limit": limit, "used": used, "remaining": max(0, limit-used),
                         "pending_requests": len(self._fb_inflight), "cache_entries": len(self._fb_cache),
                         "budget_persisted": budget.persisted if budget is not None else False,
                         "budget_error_type": budget.error_type if budget is not None else None},
            "observation": {"duplicate_messages_ignored": self._observations.duplicates},
            "modules": {"body": self._cfg_bool("vb_enabled", True), "space": self._space_enabled(),
                        "mood": self._cfg_bool("mo_enabled", True)},
            "counts": {"profiles": len(self._profiles),
                       "body_sessions": len(self._store.list_states()) if self._store is not None else 0,
                       "space_sessions": len(self._manor_store.list_sessions()) if self._manor_store is not None else 0},
            "primary_session_selected": self._primary_presence_session() is not None,
            "presence": self._presence.diagnostics() if getattr(self, '_presence', None) is not None else {"active": False},
        }

    def _tick_mood(self, now: float) -> None:
        """心绪轮询：四信号 → 规则评估 → triggers 产出（供 外部调用方 主动消息消费）。零 LLM。"""
        if not self._cfg_bool("mo_enabled", True) or not self._space_data_dir:
            return
        try:
            primary = self._primary_presence_session()
            sig = mood_signals_mod.read_all(self._space_data_dir, self._manor,
                                          session_key=primary or "__unresolved__", now=now)
            if not isinstance(sig.get("presence"), dict):
                sig["presence"] = {"available": False}
            if primary is None:
                sig["presence"] = {"available": False}
            elif not self._cfg_bool("vb_enabled", True):
                for field in ("arousal", "body_phase", "overload_threshold"):
                    sig["presence"].pop(field, None)
            identity = self._space_identity(primary) if primary is not None else None
            space_allowed = self._space_enabled() and identity is not None and self._space_key(*identity) is not None
            if not space_allowed:
                for field in ("she_room", "he_room", "same_room", "she_moving", "he_moving",
                              "env_valence", "env_arousal", "mood_word"):
                    sig["presence"].pop(field, None)
            self._last_signals = sig
            rules_doc = mood_store_mod.load_rules(self._mood_dir(), os.path.dirname(os.path.abspath(__file__)))
            last_state = mood_store_mod.load_json(self._mood_dir(), CONFIG.MO_LAST_STATE_FILE, {})
            if not isinstance(last_state, dict):
                last_state = {}
            import hashlib
            scope = hashlib.sha256((primary or "unresolved").encode("utf-8")).hexdigest()
            scope_changed = last_state.get("_presence_scope") != scope
            snapshot = mood_engine.snapshot(sig)
            if scope_changed:
                last_state = {key: value for key, value in last_state.items() if not key.startswith("presence_")}
                last_state.update({key: value for key, value in snapshot.items() if key.startswith("presence_")})
            cooldowns = mood_store_mod.load_json(self._mood_dir(), CONFIG.MO_COOLDOWNS_FILE, {})
            fired, new_cd = mood_engine.evaluate(rules_doc.get("rules") or [], sig, last_state, cooldowns, now)
            old = [t for t in mood_engine.prune_expired(mood_store_mod.load_triggers(self._mood_dir()), now)
                   if t.get("rule_id") not in {f.get("rule_id") for f in fired}
                   and not (scope_changed and t.get("signal") == "presence")]
            merged = old + fired
            mood_store_mod.save_triggers(self._mood_dir(), merged)
            self._last_triggers = merged
            if fired:
                mood_store_mod.append_history(self._mood_dir(), fired)
            mood_store_mod.save_json(self._mood_dir(), CONFIG.MO_COOLDOWNS_FILE, new_cd)
            snapshot["_presence_scope"] = scope
            mood_store_mod.save_json(self._mood_dir(), CONFIG.MO_LAST_STATE_FILE, snapshot)
        except Exception as exc:
            self._record_runtime_error(exc)
            logger.error(f"[VirtualBody] 心绪轮询失败: {exc}")

    def _tick_space(self, now: float) -> None:
        """空间推进：落位/日切/RP 过期回落（只更新数据不发消息）。"""
        if not self._space_enabled() or self._manor_store is None:
            return
        rainy = self._weather_is_rainy(now)   # B4：雨天动线——户外兜底换室内近义房
        for key in self._manor_store.list_sessions():
            try:
                st = self._manor_store.get_or_create(key, self._manor)
                _sig_before = space_mod.state_fingerprint(st)
                old_he_room = str((st.get("he") or {}).get("room") or "")   # B7：advance 前记旧他房间
                summary = space_mod.advance(st, self._manor, now, self._tz(),
                                            self._pc_current_plan_text(now),
                                            self._cfg_int("sp_rp_override_minutes", CONFIG.SP_RP_OVERRIDE_MINUTES),
                                            rainy)
                if space_mod.state_fingerprint(st) != _sig_before:   # 0.27.3：指纹跳写（对齐身体层纪律）
                    self._manor_store.save(key, st)
                # 端审补全：日程/兜底驱动的位置变化（含移动落位）也计"近变化"——
                # 否则门控①错过 tick 路径的变化，LLM 拿不到她刚换房的底色
                if summary.get("she_moved") or summary.get("arrived"):
                    arrivals = summary.get("arrived_at") or {}
                    self._last_change[key] = now if summary.get("she_moved") else max(arrivals.values(), default=now)
                # L2-B 桥挂点（锚点 8）：落位/起程 → 身体事件；键 <character>_<session> 按第一个下划线拆
                identity = self._space_identity(key)
                if identity is not None:
                    character, session = identity
                    self._bridge_arrival(character, session, st, now)
                    # B7 他离开她所在房间：advance 后他 room 变了且旧值==她房间 → leave_he 事件
                    new_he_room = str((st.get("he") or {}).get("room") or "")
                    if old_he_room and new_he_room and new_he_room != old_he_room \
                            and old_he_room == str((st.get("she") or {}).get("room") or ""):
                        self._bridge_departure(character, session, st, now)
            except Exception as exc:
                self._record_runtime_error(exc)
                logger.error(f"[VirtualBody] 空间 tick 推进失败 {key}: {exc}")

    def _bridge_arrival(self, character: str, session: str, space_st: dict[str, Any], now: float) -> None:
        """L2-B 存在桥（锚点 8）：空间落位/起程 → 身体事件第四来源。
        接近=最敏感非占位部位微升（走 decay 完整管线），离开=微回落；冷却 300s 防反复进出重复计。"""
        if not bridge.should_bridge(self._cfg_bool("presence_bridge_enabled", CONFIG.PRESENCE_BRIDGE_ENABLED)):
            return
        profile = self._profiles.get(str(character))
        if profile is None or self._store is None or not self._cfg_bool("vb_enabled", True):
            return
        try:
            body_st = self._store.get_or_create(character, session, profile)
            evs = bridge.on_arrival(space_st, body_st, profile, self._manor, now)
            if evs:
                bridge.apply_events(body_st, profile, evs, now, phase_params=self._phase_cfg())
                self._store.save(character, session, body_st)
                idle = getattr(self, "_tick_idle", None)
                if isinstance(idle, dict):
                    idle.pop((str(character), str(session)), None)   # 事件路径唤醒降频会话
                logger.debug(f"[VirtualBody] 存在桥事件 {character}/{session}: {evs}")
        except Exception as exc:
            logger.error(f"[VirtualBody] 存在桥异常 {character}/{session}: {exc}")

    def _bridge_departure(self, character: str, session: str, space_st: dict[str, Any], now: float) -> None:
        """B7 存在桥对偶：他离开她所在房间 → leave_he 事件（敏感微落 DEPARTURE_FALL=0.02 级，
        冷却按 space_leave 之外的独立账本 space_leave→leave_he 各记各）。检测时机由 _tick_space 掌握：
        advance 前后他 room 变更且旧值==她房间。"""
        if not bridge.should_bridge(self._cfg_bool("presence_bridge_enabled", CONFIG.PRESENCE_BRIDGE_ENABLED)):
            return
        profile = self._profiles.get(str(character))
        if profile is None or self._store is None or not self._cfg_bool("vb_enabled", True):
            return
        try:
            body_st = self._store.get_or_create(character, session, profile)
            evs = bridge.on_departure(space_st, body_st, profile, self._manor, now)
            if evs:
                bridge.apply_events(body_st, profile, evs, now, phase_params=self._phase_cfg())
                self._store.save(character, session, body_st)
                idle = getattr(self, "_tick_idle", None)
                if isinstance(idle, dict):
                    idle.pop((str(character), str(session)), None)   # 事件路径唤醒降频会话
                logger.debug(f"[VirtualBody] 存在桥离场事件 {character}/{session}: {evs}")
        except Exception as exc:
            logger.error(f"[VirtualBody] 存在桥离场异常 {character}/{session}: {exc}")

    # ---- tick 定时推进：只更新数据，不发消息 ----

    async def _tick_loop(self):
        while True:
            try:
                await asyncio.sleep(self._tick_seconds())
                self.tick_all()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._record_runtime_error(exc)
                logger.error(f"[VirtualBody] tick 异常: {exc}")
                await asyncio.sleep(5)

    def tick_all(self, now: float | None = None) -> None:
        if self._stopping:
            return
        now = float(time.time() if now is None else now)
        self._tick_health["last_at"] = now
        self._tick_health["cycles"] += 1
        self._tick_space(now)   # 空间先行：落位事件可供身体层后续消费（L2-B 桥挂点）
        if not self._store or not self._cfg_bool("vb_enabled", True):
            self._tick_mood(now)
            return
        idle: dict[tuple[str, str], int] = getattr(self, "_tick_idle", None) or {}
        self._tick_idle = idle
        for character, session in self._store.list_states():
            profile = self._profiles.get(character)
            if profile is None:
                continue
            key = (character, session)
            # 消耗优化：连续多轮无变化的静默会话降频（每 IDLE_SKIP 轮推进一次即可——
            # 指数衰减可从旧锚点无损补算，phase 到点转换由注入/指令路径即时兜底）
            if idle.get(key, 0) >= 5:
                idle[key] += 1
                if idle[key] < 5 + _TICK_IDLE_SKIP:
                    continue
                idle[key] = 5
            try:
                st = self._store.get_or_create(character, session, profile)
                sig_before = state_mod.state_signature(st)
                self._apply_signal_couplings(st, profile, now)   # 0.30.0：语言调情 + 情绪耦合前置
                # 0.31.0 持续接触：tick 通道延续（每 cs_persist_tick_every 个 tick 一发，不刷续期锚点）
                p_evs: list[dict[str, Any]] = []
                if self._persist_on():
                    p_evs = persist_contact.persist_events(st, None, now, self._persist_cfg(), tick=True)
                summary = decay.advance_state(st, profile, p_evs or None, now,
                                              intimacy_cfg=self._intimacy_cfg(), **self._phase_cfg())
                if summary.get("overload"):
                    self._write_body_mood_hint(now)
                self._maybe_daily_adaptation(character, session, st, now)   # 0.30.0：日级长期适应（日键防重入）
                self._maybe_proactive_intent(st, profile, now)              # 0.30.0：主动触碰意图落账
                sig_after = state_mod.state_signature(st)
                if sig_after != sig_before:
                    self._store.save(character, session, st)
                    idle[key] = 0
                else:
                    idle[key] = min(5, idle.get(key, 0) + 1)
            except Exception as exc:
                self._record_runtime_error(exc)
                logger.error(f"[VirtualBody] tick 推进失败 {character}/{session}: {exc}")
        self._tick_mood(now)   # 先衰减离线旧状态，再评估当前心绪；不开新消息入口。

    def _advance_session(self, character: str, session: str, evs: list[dict[str, Any]],
                         now: float | None = None) -> dict[str, Any] | None:
        """单会话推进+落盘。返回触发摘要；profile 不认识返回 None。
        消耗优化：事件路径会重置 tick 静默计数（唤醒降频会话）。"""
        profile = self._profiles.get(character)
        if profile is None or self._store is None or getattr(self, "_stopping", False):
            return None
        idle = getattr(self, "_tick_idle", None)
        if isinstance(idle, dict):
            idle.pop((str(character), str(session)), None)
        st = self._store.get_or_create(character, session, profile)
        now_t = float(time.time() if now is None else now)
        self._apply_signal_couplings(st, profile, now_t)   # 0.30.0：事件路径同样吃语言/情绪信号
        # 0.31.0 持续接触：消息通道延续（本轮直接事件 pid 不叠加）+ 直接事件落账（advance 后覆盖刷新）
        p_evs: list[dict[str, Any]] = []
        if self._persist_on():
            p_evs = persist_contact.persist_events(st, evs, now_t, self._persist_cfg())
        summary = decay.advance_state(st, profile, (evs or []) + p_evs, now_t,
                                      intimacy_cfg=self._intimacy_cfg(), **self._phase_cfg())
        if self._persist_on() and not summary.get("overload"):
            persist_contact.record_contacts(st, evs, now_t)
        if isinstance(summary, dict) and summary.get("overload"):
            self._write_body_mood_hint(now_t)   # 0.30.0：进余韵 → 身体→情绪 warm hint
        self._store.save(character, session, st)
        return summary

    def _resolve_session(self, sender_id: str) -> tuple[str, str] | None:
        """返回 (character, session)；非白名单一律 None——不注入、不加值、不留状态。
        会话绑定表优先；绑定失效（角色被禁用/删除）回退默认角色。"""
        sender_id = str(sender_id)
        character = self._bindings.get(sender_id, "")
        profile = self._profiles.get(character)
        if profile is None:
            character = self._default_character()
            profile = self._profiles.get(character)
            if profile is None:
                return None
        if not inject.should_inject(sender_id, profile):
            return None
        return (character, sender_id)

    # ---- 指令：/vbody [角色id] —— 查看/切换本会话绑定的角色 ----
    # 签名纪律：@filter.command 的 handler 不能带 *args（AstrBot 参数绑定会判死并中断管线），
    # 参数从 event.message_str 手工解析（外部调用方 惯例）。

    @staticmethod
    def _cmd_tokens(event: AstrMessageEvent) -> list[str]:
        text = str(getattr(event, "message_str", "") or "").strip()
        return text.lstrip("/").split()

    @filter.command("vbody", alias={"身体角色"})
    async def cmd_vbody(self, event: AstrMessageEvent):
        """查看或切换本会话绑定的角色。仅目标角色白名单内可切换。"""
        try:
            if not self._cfg_bool("vb_enabled", True) or not self._profiles:
                yield event.plain_result("身体数据未启用或无可用角色。")
                return
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if len(umo.split(":")) > 1 and "Group" in umo.split(":")[1]:
                yield event.plain_result("这个只在私聊里有效。")
                return
            sender = str(event.get_sender_id() or "")
            # 白名单前置：非白名单连角色清单都不给（防信息泄露，与 /vbstatus /touch 同口径）
            default_profile = self._profiles.get(self._default_character())
            if default_profile is None or not inject.should_inject(sender, default_profile):
                return
            tokens = self._cmd_tokens(event)
            if tokens and tokens[0].lower() in ("vbody", "身体角色"):
                tokens = tokens[1:]
            if not tokens:
                current = self._bindings.get(sender) or self._default_character()
                known = "、".join(self._profiles) or "（无）"
                yield event.plain_result(f"当前角色：{current}｜可用：{known}｜切换：/vbody <角色id>")
                return
            target = tokens[0]
            profile = self._profiles.get(target)
            if profile is None:
                yield event.plain_result(f"没有角色 {target}。可用：{'、'.join(self._profiles)}")
                return
            if not inject.should_inject(sender, profile):
                return
            self._bindings[sender] = target
            self._store.save_bindings(self._bindings)
            yield event.plain_result(f"已切换到 {profile.get('name') or target}。")
        except Exception as exc:
            logger.error(f"[VirtualBody] /vbody 异常: {exc}")
            yield event.plain_result("切换失败，稍后再试。")

    # ---- 指令：/vbstatus —— 聊天侧状态查询（白名单私聊） ----

    @filter.command("vbstatus", alias={"身体状态查询", "vbs"})
    async def cmd_vbstatus(self, event: AstrMessageEvent):
        """查看当前会话的身体状态概要（数值+档位，仅白名单可见）。"""
        try:
            if not self._cfg_bool("vb_enabled", True):
                return
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if len(umo.split(":")) > 1 and "Group" in umo.split(":")[1]:
                return
            sender = str(event.get_sender_id() or "")
            resolved = self._resolve_session(sender)
            if resolved is None:
                return
            character, session = resolved
            profile = self._profiles[character]
            st = self._store.get_or_create(character, session, profile)
            sig_before = state_mod.state_signature(st)
            decay.advance_state(st, profile, None, time.time(), **self._phase_cfg())   # 查询前推进到当前
            if state_mod.state_signature(st) != sig_before:
                self._store.save(character, session, st)
            tier_cn = {"notice": "注意", "react": "反应", "lose": "失控", "overload": "过载"}
            phase_cn = {"building": "平常", "afterglow": "余韵", "cooldown": "不应期"}
            rows = []
            for pid, meta in sorted(((str(p.get("id")), p) for p in profile.get("parts") or []),
                                     key=lambda x: -float((st.get("parts") or {}).get(x[0], {}).get("value") or 0)):
                v = float((st.get("parts") or {}).get(pid, {}).get("value") or 0.0)
                if v < 0.05:
                    continue
                tier = decay.tier_of(v, profile.get("tiers") or [])
                rows.append(f"{meta.get('label') or pid} {v:.2f}（{tier_cn.get(tier, '')}）" if tier else f"{meta.get('label') or pid} {v:.2f}")
            ev_today = st.get("events_today") if isinstance(st.get("events_today"), dict) else {}
            head = (f"【{profile.get('name') or character}】兴奋度 {float(st.get('arousal') or 0):.2f}｜"
                    f"{phase_cn.get(st.get('phase') or 'building', st.get('phase'))}｜"
                    f"耐受 {float(st.get('tolerance') or 0):.2f}｜今日 {ev_today.get('count') or 0} 次")
            yield event.plain_result(head + ("\n" + "、".join(rows[:8]) if rows else ""))
        except Exception as exc:
            logger.error(f"[VirtualBody] /vbstatus 异常: {exc}")

    # ---- 指令：/touch 部位 强度 [类型] ----

    @filter.command("touch", alias={"触碰"})
    async def cmd_touch(self, event: AstrMessageEvent):
        """显式刺激指令，仅白名单可用。用法：/touch 部位[+部位2] 强度 [类型]"""
        try:
            if not self._cfg_bool("vb_enabled", True):
                yield event.plain_result("身体数据未启用。")
                return
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if len(umo.split(":")) > 1 and "Group" in umo.split(":")[1]:
                yield event.plain_result("这个只在私聊里有效。")
                return
            sender = str(event.get_sender_id() or "")
            resolved = self._resolve_session(sender)
            if resolved is None:
                return
            character, session = resolved
            tokens = self._cmd_tokens(event)
            if tokens and tokens[0].lower() in ("touch", "触碰"):
                tokens = tokens[1:]
            arg_text = " ".join(tokens)
            profile = self._profiles[character]
            evs = events_mod.parse_touch_multi(arg_text, profile)
            if not evs:
                yield event.plain_result("格式：/touch 部位[+部位2] 强度 [类型]，强度用 轻/中/重 或 0-1。")
                return
            # 场景词联动（0.27.1）：她所在房间的自定义互动词 → 增益
            if self._space_enabled():
                try:
                    skey2 = self._space_key(character, sender) or ""
                    if skey2:
                        sst2 = self._manor_store.get_or_create(skey2, self._manor)
                        evs = space_mod.apply_scene_gain(
                            evs, arg_text, space_mod.scene_words(
                                self._manor, (sst2.get("she") or {}).get("room")))
                except Exception:
                    pass
            now = time.time()
            for ev in evs:
                ev["t"] = now
            summary = self._advance_session(character, session, evs, now)
            st = self._store.load(character, session) or {}
            labels = []
            for ev in evs:
                part_label = str((next((p for p in profile.get("parts") or [] if p.get("id") == ev["part"]), {})).get("label") or ev["part"])
                cur = float((st.get('parts') or {}).get(ev['part'], {}).get('value') or 0)
                cur_tier = decay.tier_of(cur, profile.get("tiers") or [])
                tier_cn = {"notice": "注意", "react": "反应", "lose": "失控", "overload": "过载"}
                labels.append(f"{part_label} +{ev['s']:.2f}（{cur:.2f}{'·' + tier_cn[cur_tier] if cur_tier else ''}）")
            reply = "已记录：" + "、".join(labels)
            if isinstance(summary, dict) and summary.get("overload"):
                reply += "（已过载，进入余韵）"
            yield event.plain_result(reply)
        except Exception as exc:
            logger.error(f"[VirtualBody] /touch 异常: {exc}")
            yield event.plain_result("记录失败，稍后再试。")

    # ---- 空间层指令（存在层合并 P1：manor 六件迁入，会话走 _resolve_space） ----

    @filter.command("where", alias={"位置"})
    async def cmd_where(self, event: AstrMessageEvent):
        """/where [房间]：查双方位置，带参=设定自己位置。"""
        try:
            session = self._resolve_space(event)
            if session is None:
                return
            tokens = self._cmd_tokens(event)
            if tokens and tokens[0].lower() in ("where", "位置"):
                tokens = tokens[1:]
            st = self._manor_store.get_or_create(session, self._manor)
            now = time.time()
            if tokens:
                room = self._resolve_room_arg(tokens[0])
                if room is None:
                    yield event.plain_result(f"没有这个房间：{tokens[0]}。/map 可看全部。")
                    return
                r = space_mod.set_room(st, "he", room, self._manor, now, "manual", 1.0)
                self._manor_store.save(session, st)
                self._last_change[session] = now
                label = render_mod.room_label(self._manor, room)
                yield event.plain_result(f"好，你在{label}。" if r is None else f"正在去{label}（约 {r['seconds']} 秒）。")
                return
            w_summary = space_mod.advance(st, self._manor, now, self._tz(), self._pc_current_plan_text(),
                                          self._cfg_int("sp_rp_override_minutes", CONFIG.SP_RP_OVERRIDE_MINUTES),
                                          self._weather_is_rainy())
            self._manor_store.save(session, st)
            if w_summary.get("she_moved") or w_summary.get("arrived"):
                self._last_change[session] = now
            she, he = st["she"], st["he"]
            sl = render_mod.room_label(self._manor, she.get("room")) or "?"
            hl = render_mod.room_label(self._manor, he.get("room"))
            lines = [f"她：{sl}" + ("（正在走动）" if she.get("moving") else "")]
            if hl:
                lines.append(f"你：{hl}")
            same = space_mod.same_room(st)
            if same is True:
                lines.append("你们同处一室。")
            elif same is False:
                secs = traverse_mod.travel_seconds(self._manor, she.get("room"), he.get("room"))
                lines.append(f"不同室｜她过来约 {secs if secs is not None else '?'} 秒。")
            ev = st.get("events_today") or {}
            lines.append(f"今日移动 {ev.get('moves') or 0} 次")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            logger.error(f"[VirtualBody] /where 异常: {exc}")

    @filter.command("map", alias={"庄园"})
    async def cmd_map(self, event: AstrMessageEvent):
        """/map：列楼层与房间。"""
        session = self._resolve_space(event)
        if session is None:
            return
        lines = []
        for floor in self._manor.get("floors") or []:
            fid = str(floor.get("id"))
            rooms = [str(r.get("label") or r.get("id")) for r in self._manor.get("rooms") or [] if str(r.get("floor")) == fid]
            lines.append(f"{floor.get('name')}：{'、'.join(rooms)}")
        yield event.plain_result("\n".join(lines))

    @filter.command("put", alias={"放"})
    async def cmd_put(self, event: AstrMessageEvent):
        """/put <物品> [房间]：物品入账。"""
        session = self._resolve_space(event)
        if session is None:
            return
        tokens = self._cmd_tokens(event)
        if tokens and tokens[0].lower() in ("put", "放"):
            tokens = tokens[1:]
        if not tokens:
            yield event.plain_result("用法：/put <物品> [房间]")
            return
        item = tokens[0]
        # 端审修复：带房间参数但没认出来时明确报错——原逻辑静默落到他所在房，物品被错放
        if len(tokens) > 1:
            room = self._resolve_room_arg(tokens[1])
            if room is None:
                yield event.plain_result(f"没有这个房间：{tokens[1]}。/map 可看全部。")
                return
        else:
            room = None
        if room is None:
            st = self._manor_store.get_or_create(session, self._manor)
            room = (st.get("he") or {}).get("room")
        if not room:
            yield event.plain_result("先设定位置（/where 房间）或指定房间。")
            return
        obj = load_objects(self._space_data_dir)
        obj["items"] = [i for i in obj.get("items") or [] if str(i.get("id")) != item]
        obj["items"].append({"id": item, "label": item, "room": room, "note": "", "updated_at": space_mod.iso(time.time())})
        save_objects(self._space_data_dir, obj)
        yield event.plain_result(f"已放好：{item} → {render_mod.room_label(self._manor, room)}")

    @filter.command("find", alias={"找"})
    async def cmd_find(self, event: AstrMessageEvent):
        """/find <关键词>：物品查询。"""
        session = self._resolve_space(event)
        if session is None:
            return
        tokens = self._cmd_tokens(event)
        if tokens and tokens[0].lower() in ("find", "找"):
            tokens = tokens[1:]
        if not tokens:
            yield event.plain_result("用法：/find <关键词>")
            return
        kw = tokens[0]
        obj = load_objects(self._space_data_dir)
        hits = [i for i in obj.get("items") or [] if kw in str(i.get("id")) or kw in str(i.get("label") or "")]
        if not hits:
            yield event.plain_result(f"没找到「{kw}」。")
            return
        yield event.plain_result("\n".join(
            f"{i.get('label') or i.get('id')} → {render_mod.room_label(self._manor, i.get('room')) or '?'}" for i in hits))

    @filter.command("hermove", alias={"移动她"})
    async def cmd_hermove(self, event: AstrMessageEvent):
        """/hermove <房间>：导演用，移动她。"""
        session = self._resolve_space(event)
        if session is None:
            return
        tokens = self._cmd_tokens(event)
        if tokens and tokens[0].lower() in ("hermove", "移动她"):
            tokens = tokens[1:]
        if not tokens:
            yield event.plain_result("用法：/hermove <房间>")
            return
        room = self._resolve_room_arg(tokens[0])
        if room is None:
            yield event.plain_result(f"没有这个房间：{tokens[0]}。/map 可看全部。")
            return
        st = self._manor_store.get_or_create(session, self._manor)
        # 端审修复：source 用 "rp"（导演放置＝用户定向覆盖，享 RP 覆盖窗）——
        # 原 "manual" 不受 advance 的 rp_fresh 保护，落地即被日程/兜底拽回，导演指令形同虚设
        space_mod.set_room(st, "she", room, self._manor, time.time(), "rp")
        self._manor_store.save(session, st)
        self._last_change[session] = time.time()
        yield event.plain_result(f"她正在去{render_mod.room_label(self._manor, room)}。")

    @filter.command("spacestatus", alias={"空间状态"})
    async def cmd_spacestatus(self, event: AstrMessageEvent):
        """/spacestatus：空间层调试总览。"""
        session = self._resolve_space(event)
        if session is None:
            return
        st = self._manor_store.get_or_create(session, self._manor)
        she, he = st.get("she") or {}, st.get("he") or {}
        yield event.plain_result(
            f"she={she.get('room')} src={she.get('source')} moving={bool(she.get('moving'))}\n"
            f"he={he.get('room')} src={he.get('source')} conf={he.get('confidence')}\n"
            f"manor_source={self._manor_source} rooms={len(self._manor.get('rooms') or [])}")

    # ---- 私聊消息观察：文本抽取 + LLM 兜底 ----

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=2)
    async def observe_presence_identity(self, event: AstrMessageEvent, *args, **kwargs):
        """只登记真实私聊身份，环境来源关闭或身体停用时也保持可选服务边界。"""
        try:
            sender = str(event.get_sender_id() or '')
            if not sender or sender == str(event.get_self_id() or '') or getattr(self,'_stopping',False):
                return
            if getattr(self,'_presence',None) is not None:
                self._presence.remember_event(event)
        except Exception:
            return

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=1)
    async def observe_private_message(self, event: AstrMessageEvent, *args, **kwargs):
        try:
            if getattr(self, "_stopping", False):
                return
            vb_on = self._cfg_bool("vb_enabled", True)
            if not vb_on and not self._space_enabled():
                return
            sender = str(event.get_sender_id() or "")
            if sender and str(event.get_self_id() or "") == sender:
                return   # bot 自身消息回流守卫（外部调用方 惯例）
            resolved = self._resolve_session(sender)
            if resolved is None:
                return
            character, session = resolved
            text = str(getattr(event, "message_str", "") or "")
            text = re.sub(r"\[[^\[\]\n]{1,24}\]", "", text)   # 剥 CQ 表情码（[捂脸] 等），防误触发
            # 九指令文本跳过：指令 handler 已施加事件，观察器再抽取会双计（外部调用方 同型惯例）
            # （hermove/spacestatus 的中文别名与英文主名同口径跳过）
            stripped = text.strip().lstrip("/")
            if stripped.startswith(("touch ", "触碰 ", "vbody ", "身体角色 ", "vbstatus ", "身体状态查询 ", "vbs ",
                                    "where ", "位置 ", "put ", "放 ", "find ", "找 ", "hermove ", "移动她 ", "spacestatus",
                                    "空间状态", "map", "庄园")) or \
                    stripped in ("touch", "触碰", "vbody", "身体角色", "vbstatus", "vbs", "身体状态查询",
                                 "where", "位置", "map", "庄园", "put", "放", "find", "找", "hermove", "移动她",
                                 "spacestatus", "空间状态"):
                return
            message_id = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
            observer = getattr(self, "_observations", None)
            if observer is not None and message_id and message_id != "0":
                key = (str(getattr(event, "unified_msg_origin", "") or ""), sender, message_id)
                if not observer.accept(key):
                    return
            now = time.time()
            st = None   # 空间状态（空间段填充；身体场景段复用）
            # ---- 空间抽取先行（她的移动/他的位置；L2-B 桥将来在此消费落位事件）----
            if self._space_enabled():
                skey = self._space_key(character, sender)
                if skey:
                    try:
                        st = self._manor_store.get_or_create(skey, self._manor)
                        space_before = space_mod.state_fingerprint(st)
                        his = extract_mod.extract_him(text, self._manor)
                        if his:
                            r = space_mod.set_room(st, "he", his["room"], self._manor, now, "rp",
                                                   CONFIG.SP_RP_CONFIDENCE_DIRECT if his["direct"] else CONFIG.SP_RP_CONFIDENCE_MOVE)
                        her = extract_mod.extract_her_move(text, self._manor)
                        if her:
                            r = space_mod.set_room(st, "she", her["room"], self._manor, now, "rp", CONFIG.SP_RP_CONFIDENCE_MOVE)
                        # set_room 的 None 也可代表首次落位或同房 RP 续期；按实际语义变化保存。
                        if space_mod.state_fingerprint(st) != space_before:
                            self._manor_store.save(skey, st)
                            self._last_change[skey] = now
                            # L2-B 桥挂点（锚点 8）：她的首落位/起程离开在此，移动落位由 tick 桥接
                            self._bridge_arrival(character, session, st, now)
                    except Exception as exc:
                        logger.error(f"[VirtualBody] 空间消息观察异常: {exc}")
            if not vb_on:
                return
            # ---- 身体抽取 ----
            profile = self._profiles[character]
            evs = events_mod.extract_events(text, profile=profile)
            # 0.31.0 持续接触否定面："别摸耳朵"（extract_stops 否定词+部位词邻近命中）
            # → 清除对应接触（extract_events 的否定前缀本就不产事件，这里清的是已建立的延续）
            stops = events_mod.extract_stops(text, profile)
            if stops:
                self._fb_stop_guard.stop(character, session, stops)
                evs = [ev for ev in evs if str(ev.get("part") or "") not in stops]
                try:
                    st_c = self._store.get_or_create(character, session, profile)
                    if persist_contact.clear_contacts(st_c, stops):
                        self._store.save(character, session, st_c)
                        logger.debug(f"[VirtualBody] 持续接触按否定清除 {character}/{session}: {stops}")
                except Exception as exc:
                    logger.error(f"[VirtualBody] 持续接触清除异常 {character}/{session}: {exc}")
                # 停止保护独立于持续接触开关；不让未完成的抽取再次建立接触。
                if not evs:
                    return
            # 场景词联动（0.27.1）：自定义房间互动词命中 → 事件增益
            if evs and self._space_enabled() and st is not None \
                    and not (st.get("she") or {}).get("moving") and not (st.get("he") or {}).get("moving"):
                # 0.27.3 优化：复用空间段已取的 st（一轮一读，不再二次 get_or_create）
                try:
                    evs = space_mod.apply_scene_gain(
                        evs, text,
                        space_mod.scene_words(self._manor, (st.get("she") or {}).get("room")))
                except Exception:
                    pass
            if evs:
                for ev in evs:
                    ev["t"] = now
                self._advance_session(character, session, evs, now)
                return
            # 兜底：开开关、超上限跳过，失败当无事件
            if self._cfg_bool("vb_llm_fallback_enabled", False):
                # Reserve before scheduling: a stop may arrive before the task starts.
                token = self._fb_stop_guard.begin(character, session)
                if token is None:
                    return
                task = asyncio.create_task(self._llm_fallback_extract(character, session, text, token))
                self._fb_tasks.add(task)   # 持引用防 GC（asyncio 文档明确的坑）
                task.add_done_callback(lambda _task, t=token: self._fb_stop_guard.finish(t))
                task.add_done_callback(self._fb_tasks.discard)
        except Exception as exc:
            logger.error(f"[VirtualBody] 消息观察异常: {exc}")

    async def _llm_fallback_extract(self, character: str, session: str, text: str, token=None) -> None:
        token = token or self._fb_stop_guard.begin(character, session)
        if token is None:
            return
        epoch = getattr(self, "_lifecycle_epoch", 0)
        try:
            if getattr(self, "_stopping", False) or not text.strip() or len(text) > 500:
                return
            raw = await self._fallback_raw_for_text(character, text, epoch)
            if raw is None or getattr(self, "_stopping", False) or epoch != getattr(self, "_lifecycle_epoch", 0):
                return
            if not self._cfg_bool("vb_enabled", True) or self._resolve_session(session) != (character, session):
                return
            profile = self._profiles.get(character)
            if profile is None:
                return
            ev = events_mod.parse_llm_json(raw, profile)
            if ev is not None and not self._fb_stop_guard.invalidated(token, ev["part"]):
                ev["t"] = time.time()
                self._advance_session(character, session, [ev])
        except Exception as exc:
            logger.error(f"[VirtualBody] 兜底抽取异常: {exc}")
        finally:
            self._fb_stop_guard.finish(token)

    async def _fallback_raw_for_text(self, character: str, text: str, epoch: int) -> str | None:
        cached = self._fb_cache_get(character, text)
        if cached is not None:
            return cached
        flights = getattr(self, "_fb_inflight", None)
        if not isinstance(flights, dict):
            flights = self._fb_inflight = {}
        key = (epoch, character, self._fallback_cache_key(character, text))
        task = flights.get(key)
        if task is None:
            task = asyncio.create_task(self._request_fallback_raw(character, text, epoch))
            flights[key] = task
            def finished(done):
                if flights.get(key) is done:
                    flights.pop(key, None)
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def _request_fallback_raw(self, character: str, text: str, epoch: int) -> str | None:
        if getattr(self, "_stopping", False) or not self._fb_budget_ok():
            return None
        provider = None
        get_using = getattr(self.context, "get_using_provider", None)
        if callable(get_using):
            try:
                provider = get_using()
            except TypeError:
                try:
                    provider = get_using(None)
                except Exception:
                    pass
            except Exception:
                pass
        if provider is None or not hasattr(provider, "text_chat"):
            return None
        profile = self._profiles.get(character) or {}
        parts = [{"id": str(p.get("id") or ""), "label": str(p.get("label") or "")[:24],
                  "aliases": [str(a)[:24] for a in (p.get("aliases") if isinstance(p.get("aliases"), (list, tuple)) else [])[:3]]}
                 for p in (profile.get("parts") or []) if isinstance(p, dict) and p.get("id")]
        if not parts:
            return None
        prompt = CONFIG.LLM_FALLBACK_PROMPT + "\n只抽取本轮已实际发生的接触，否定、停止、假设和引用不算动作。"
        prompt += "\n允许部位（id优先，也可用label/alias）：" + json.dumps(parts, ensure_ascii=False)
        prompt += "\n消息：" + text
        signature = self._fallback_cache_key(character, text)
        if self._fb_consume() is False:
            return None
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, session_id=f"{PLUGIN_NAME}_fallback"),
                timeout=CONFIG.LLM_FALLBACK_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.debug(f"[VirtualBody] 兜底抽取调用失败: {exc}")
            return None
        if getattr(self, "_stopping", False) or epoch != getattr(self, "_lifecycle_epoch", 0):
            return None
        raw = str(getattr(resp, "completion_text", "") or "")
        if self._fallback_cache_key(character, text) != signature:
            return None
        self._fb_cache_put(character, text, raw)
        return raw

    def _fallback_day(self) -> str:
        return time.strftime("%Y-%m-%d", decay.tz_local_time(time.time()))

    def _fb_budget_ok(self) -> bool:
        day = self._fallback_day()
        budget = getattr(self, "_fallback_budget", None)
        if self._fb_day != day:
            self._fb_day = day
            self._fb_count = budget.used(day) if budget is not None else 0
        if budget is not None:
            self._fb_count = budget.used(day)
            if budget.error_type is not None:
                if not budget.refresh():
                    return False
                self._fb_count = budget.used(day)
        return self._fb_count < max(0, self._cfg_int("vb_llm_fallback_daily_limit", CONFIG.LLM_FALLBACK_DAILY_LIMIT))

    def _fb_consume(self) -> bool:
        day = self._fallback_day()
        budget = getattr(self, "_fallback_budget", None)
        if budget is None:
            self._fb_day = day
            self._fb_count += 1
            return True
        if not budget.reserve(day, max(0, self._cfg_int("vb_llm_fallback_daily_limit", CONFIG.LLM_FALLBACK_DAILY_LIMIT))):
            return False
        self._fb_day, self._fb_count = day, budget.used(day)
        return True

    def _fallback_cache_key(self, character: str, text: str) -> str:
        import hashlib
        profile = self._profiles.get(character) or {}
        context = {"character": character, "parts": profile.get("parts") or [],
                   "message": text, "prompt": CONFIG.LLM_FALLBACK_PROMPT}
        return hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def _fb_cache_get(self, character: str, text: str) -> str | None:
        entry = self._fb_cache.get(self._fallback_cache_key(character, text))
        if entry and entry[0] == self._fallback_day():
            return entry[1]
        return None

    def _fb_cache_put(self, character: str, text: str, raw: str) -> None:
        key = self._fallback_cache_key(character, text)
        if len(self._fb_cache) >= 50:
            for old in list(self._fb_cache)[:len(self._fb_cache)-49]:
                self._fb_cache.pop(old, None)
        self._fb_cache[key] = (self._fallback_day(), raw)

    # ---- 出站清洗：剥掉模型模仿注入格式的标签残留（借鉴 clean_chatter 污染教训） ----

    @filter.on_decorating_result(priority=10000)
    async def sanitize_presence_tags_outbound(self, event: AstrMessageEvent, *args, **kwargs):
        """单出站清洗（锚点 6）：剥掉模型模仿注入格式的标签残留——身体块与空间块两套正则都跑。"""
        try:
            result = event.get_result()
            chain = list(getattr(result, "chain", []) or []) if result is not None else []
            changed = False
            for comp in chain:
                text = getattr(comp, "text", None)
                if not text or ("<" not in text and "＜" not in text and "身体状态" not in text and "空间" not in text):
                    continue
                cleaned = inject.sanitize_outbound_text(str(text))
                cleaned = strip_environment_context(cleaned)
                cleaned = inject.sanitize_outbound_mixed(cleaned)
                cleaned = render_mod.sanitize_outbound_text(cleaned)
                if cleaned != text:
                    comp.text = cleaned
                    changed = True
            if changed:
                logger.debug("[VirtualBody] 已清洗出站消息中的存在层标签残留")
        except Exception as exc:
            logger.error(f"[VirtualBody] 出站清洗异常: {exc}")

    # ---- LLM 请求前注入 ----

    @filter.on_llm_request()
    async def inject_presence_state(self, event: AstrMessageEvent, req: ProviderRequest, *args, **kwargs):
        """轮次开始前注入存在层（锚点 4：单注入点内硬编码顺序——空间块在前、身体块在后，
        彻底摆脱插件加载序依赖）。非白名单不注入；两块各走各的 gate。"""
        try:
            if req is None or event is None or getattr(self, "_stopping", False):
                return
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            parts_umo = umo.split(":")
            if len(parts_umo) > 1 and "Group" in parts_umo[1]:
                return
            sender = str(event.get_sender_id() or "")
            resolved = self._resolve_session(sender)
            if resolved is None:
                return
            character, session = resolved
            text = str(getattr(event, "message_str", "") or "")
            now = time.time()
            use_system = str(self._cfg("vb_inject_position", "user") or "user").lower() == "system"
            # ---- 空间块（四条件 gate：近变化/房间词/移动中/位置类问句）----
            if self._space_enabled() and self._cfg_bool("sp_inject_enabled", True):
                skey = self._space_key(character, sender)
                if skey:
                    try:
                        st = self._manor_store.get_or_create(skey, self._manor)
                        if render_mod.gate(st, now, text, self._manor, self._last_change.get(skey)):
                            sp_summary = space_mod.advance(st, self._manor, now, self._tz(), self._pc_current_plan_text(now),
                                                           self._cfg_int("sp_rp_override_minutes", CONFIG.SP_RP_OVERRIDE_MINUTES),
                                                           self._weather_is_rainy(now))
                            self._manor_store.save(skey, st)
                            # 端审补全：注入路径的 advance 本身可能触发日程/RP 过期换房——计"近变化"供后续轮门控
                            if sp_summary.get("she_moved") or sp_summary.get("arrived"):
                                arrivals = sp_summary.get("arrived_at") or {}
                                self._last_change[skey] = now if sp_summary.get("she_moved") else max(arrivals.values(), default=now)
                            block = render_mod.render_injection(
                                st, self._manor, now,
                                eta_thresholds=self._eta_thresholds(),
                                hearing_same_floor=self._hearing_text(
                                    "sp_hearing_same_floor", CONFIG.SP_HEARING_SAME_FLOOR_DEFAULT),
                                hearing_adjacent=self._hearing_text(
                                    "sp_hearing_adjacent", CONFIG.SP_HEARING_ADJACENT_DEFAULT),
                                max_chars=self._cfg_int("sp_render_max_chars", CONFIG.SP_RENDER_MAX_CHARS))
                            if block:
                                if getattr(self, '_presence', None) is not None:
                                    self._presence.publish_virtual(event, 'virtual.space', {
                                        'summary': block, 'she': st.get('she'), 'he': st.get('he'),
                                        'same_room': space_mod.same_room(st)}, now=now)
                                self._emit_block(req, block, use_system)
                    except Exception as exc:
                        logger.error(f"[VirtualBody] 空间注入异常: {exc}")
            # ---- 身体块 ----
            if not self._cfg_bool("vb_enabled", True) or not self._cfg_bool("vb_inject_enabled", True):
                return
            profile = self._profiles[character]
            st = self._store.get_or_create(character, session, profile)
            # 注入前推进到当前，保证数值新鲜（无事件纯衰减+phase 机器）；
            # 0.30.0 前置信号耦合（语言调情/情绪）+ 后置意图落账（"她想要"尾巴新鲜度跟上本轮）
            sig_before = state_mod.state_signature(st)
            self._apply_signal_couplings(st, profile, now)
            # 0.31.0 持续接触：纯聊天消息（无直接事件）也续期——对话在场接触不断，
            # 静默超 cs_persist_max_gap 才过期（观察器/注入双推进由消息通道最小间隔防双算）
            p_evs: list[dict[str, Any]] = []
            if self._persist_on():
                p_evs = persist_contact.persist_events(st, None, now, self._persist_cfg())
            decay.advance_state(st, profile, p_evs or None, now,
                                intimacy_cfg=self._intimacy_cfg(), **self._phase_cfg())
            self._maybe_proactive_intent(st, profile, now)
            # 消耗优化：注入路径也按指纹跳写（空闲请求不再无谓落盘）
            if state_mod.state_signature(st) != sig_before:
                self._store.save(character, session, st)
            prof_render = profile.get("render") or {}
            max_chars = self._cfg_int("vb_render_max_chars", CONFIG.RENDER_MAX_CHARS)
            try:
                max_chars = int(prof_render.get("max_chars") or max_chars)
            except (TypeError, ValueError):
                pass
            show_numbers = self._cfg_bool("vb_show_numbers", CONFIG.SHOW_NUMBERS_TO_MODEL)
            if isinstance(prof_render.get("show_numbers_to_model"), bool):
                show_numbers = prof_render["show_numbers_to_model"]
            block = inject.render(st, profile, max_chars=max_chars, show_numbers=show_numbers,
                                  user_height_cm=self._cfg_int("vb_user_height_cm", 0),
                                  constraint_text=self._constraint_text(),
                                  feel_word_ttl=self._feel_word_ttl())
            if block:
                if getattr(self, '_presence', None) is not None:
                    self._presence.publish_virtual(event, 'virtual.body', {
                        'summary': block, 'phase': st.get('phase'), 'arousal': st.get('arousal'),
                        'verbal_arousal': st.get('verbal_arousal')}, now=now)
                # 注入位置（LLM 注入调研·最高优先级）：默认注入本轮 user 消息——
                # system_prompt 与全部历史保持稳定前缀，前缀缓存全命中。
                # 合规关键：走 extra_user_content_parts + mark_as_temp（防落史通道）。
                self._emit_block(req, block, use_system)
        except Exception as exc:
            logger.error(f"[VirtualBody] 注入异常: {exc}")

    @filter.on_llm_request(priority=-20800)
    async def inject_environment_context(self, event: AstrMessageEvent, req: ProviderRequest, *args, **kwargs):
        """环境 Provider 汇聚后只增加临时 Context，保留原人格与身体/空间块。"""
        try:
            if event is not None and req is not None and getattr(self, '_presence', None) is not None:
                await self._presence.inject(event, req, TextPart)
        except Exception as exc:
            logger.warning('[VirtualBody] 环境 Context 暂不可用: %s', type(exc).__name__)

    @filter.llm_tool(name='presence_context_query')
    async def presence_context_query(self, event: AstrMessageEvent, aspect: str = '') -> str:
        """按需读取当前有效环境背景，不推测未知状态。

        Args:
            aspect(string): 想了解的方面，例如音乐、天气、时间、手机电量或活动。
        """
        if getattr(self, '_presence', None) is None:
            return '环境感知暂不可用，正常对话不受影响。'
        try:
            return await self._presence.query(event, aspect)
        except Exception:
            return '环境感知暂不可用，不能把旧观测当作当前事实。'

    def _emit_block(self, req: ProviderRequest, block: str, use_system: bool) -> None:
        """统一出块通道：user 位=TextPart.mark_as_temp（不落历史）；system 位=追加 system_prompt 尾部。"""
        if not use_system and TextPart is not None:
            try:
                part = TextPart(text=block).mark_as_temp()
                req.extra_user_content_parts.append(part)
                return
            except Exception:
                pass
        if use_system:
            base = str(getattr(req, "system_prompt", "") or "")
            req.system_prompt = (base + "\n\n" + block) if base else block
            return
        # 终审修复：删除 req.prompt 直拼旧通道（纪律 4 禁令；TextPart 不可用且非 system 位则宁可不注入）
        logger.warning("[VirtualBody] TextPart 不可用且非 system 位，跳过本轮注入（不再走 req.prompt 落历史通道）")
