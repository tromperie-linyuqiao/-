"""存储层：会话状态 + 庄园配置加载。mtime 缓存、原子写、指纹跳写（vb 同款纪律）。"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
from typing import Any

from .. import CONFIG
from .space import known_rooms, new_state

_WRITE_LOCK = threading.RLock()


def _atomic_write(path: str, data: str) -> None:
    _atomic_write_bytes(path, data.encode("utf-8"))


def _atomic_write_bytes(path: str, data: bytes) -> None:
    # 两个 store 实例/管理入口可写同一路径；共享固定 .tmp 会互相 replace 甚至丢写。
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".",
                               suffix=".tmp", dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        with _WRITE_LOCK:
            os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _mtime_ns(path: str) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


class ManorStore:
    """会话状态存取。session 白名单校验防路径穿越（规范 3.4 第 5 条）。

    suffix 分立：身体层用默认 .json，空间层用 .space.json——同一 state/ 目录下
    两层文件互不串扰（list_sessions 以键含"."为界排除对方）。
    """

    def __init__(self, data_dir: str, suffix: str = ".json"):
        self._dir = os.path.join(str(data_dir), CONFIG.SP_STATE_DIR_NAME)
        self._suffix = str(suffix or ".json")
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[int, dict[str, Any]]] = {}
        os.makedirs(self._dir, exist_ok=True)

    def _path(self, session: str) -> str:
        session = str(session)
        if not re.fullmatch(CONFIG.SP_SESSION_RE, session or ""):
            raise ValueError(f"非法 session: {session!r}")
        return os.path.join(self._dir, f"{session}{self._suffix}")

    def load(self, session: str) -> dict[str, Any] | None:
        try:
            path = self._path(session)
        except ValueError:
            return None
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def save(self, session: str, state: dict[str, Any]) -> None:
        try:
            path = self._path(session)
        except ValueError:
            return
        with self._lock, _WRITE_LOCK:
            _atomic_write(path, json.dumps(state, ensure_ascii=False, separators=(",", ":")))
            mtime = _mtime_ns(path)
            if mtime is not None:
                self._cache[str(session)] = (mtime, json.loads(json.dumps(state)))

    def get_or_create(self, session: str, manor: dict[str, Any]) -> dict[str, Any]:
        session = str(session)
        path = self._path(session)
        mtime = _mtime_ns(path)
        cached = self._cache.get(session) if mtime is not None else None
        raw = (json.loads(json.dumps(cached[1])) if cached and cached[0] == mtime
               else self.load(session))
        st = raw if isinstance(raw, dict) and raw.get("schema") == CONFIG.SP_STATE_SCHEMA else new_state(session)
        defaults = new_state(session)
        # 坏局部不清空整个会话；额外字段与有效位置/覆盖时间照原样保留。
        for key in ("she", "he"):
            if not isinstance(st.get(key), dict):
                st[key] = defaults[key]
            else:
                st[key] = {**defaults[key], **st[key]}
                if st[key].get("moving") is not None and not isinstance(st[key]["moving"], dict):
                    st[key]["moving"] = None
                moving = st[key].get("moving")
                if isinstance(moving, dict):
                    until = moving.get("until_ts")
                    if not isinstance(until, (int, float)) or isinstance(until, bool) \
                            or not math.isfinite(until) \
                            or str(moving.get("to") or "") not in known_rooms(manor) \
                            or str(moving.get("from") or "") not in known_rooms(manor):
                        st[key]["moving"] = None
        if not isinstance(st.get("events_today"), dict):
            st["events_today"] = defaults["events_today"]
        else:
            try:
                st["events_today"]["moves"] = max(0, int(st["events_today"].get("moves") or 0))
            except (TypeError, ValueError, OverflowError):
                st["events_today"]["moves"] = 0
        st["session"] = session
        if raw is not None and mtime is not None:
            self._cache[session] = (mtime, json.loads(json.dumps(st)))
        return st

    def list_sessions(self) -> list[str]:
        out = []
        try:
            names = os.listdir(self._dir)
        except OSError:
            return out
        slen = len(self._suffix)
        for n in names:
            # 键含"."的一律排除（另一层的 "x.space.json" 不被本层当作 "x.space" 收进来）
            if not n.endswith(self._suffix) or n.endswith(".tmp"):
                continue
            key = n[:-slen]
            if key and "." not in key and re.fullmatch(CONFIG.SP_SESSION_RE, key):
                out.append(key)
        return out

    def delete(self, session: str) -> bool:
        try:
            path = self._path(session)
        except ValueError:
            return False
        try:
            os.remove(path)
            self._cache.pop(str(session), None)
            return True
        except OSError:
            return False


def load_manor(data_dir: str, plugin_dir: str) -> tuple[dict[str, Any], str]:
    """加载 manor：plugin_data 覆盖层 > 插件自带。返回 (manor, 来源)。坏文件当缺省。"""
    candidates = [
        (os.path.join(str(data_dir), CONFIG.SP_MANOR_OVERRIDE_DIR, "manor.json"), "plugin_data"),
        (os.path.join(str(plugin_dir), "data", "manor.json"), "builtin"),
    ]
    for path, source in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("id") == "manor":
                return data, source
        except (OSError, ValueError):
            continue
    return {}, "none"


def load_manor_builtin(plugin_dir: str) -> dict[str, Any]:
    """只读插件自带 data/manor.json（覆盖层代际迁移的内置基线）。坏文件/缺失返回 {}。"""
    path = os.path.join(str(plugin_dir), "data", "manor.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("id") == "manor":
            return data
    except (OSError, ValueError):
        pass
    return {}


def _num_list(value: Any) -> list[float] | None:
    """纯数值列表判定（房间 center/size 布局字段）；坏结构返回 None。"""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    out = []
    for x in value:
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            return None
        out.append(float(x))
    return out


def merge_legacy_manor_override(override: dict[str, Any],
                                builtin: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """manor 覆盖层代际迁移（0.27.9，与 profile 覆盖层同机制，防"覆盖层旧参数压制内置新参数"）。

    触发：覆盖层无 schema 版本而内置有（视为 legacy）。动作（只对 rooms/edges 数值字段）：
    - 同 id 房间的 center/size、同端点边的 seconds 以内置为基线刷新（布局代际演进跟随内置）；
    - 用户自定义全部保留：label/aliases/ambiance/props/scene_words/mood_bias、房间/边的增删。
    - 补盖 schema 版本戳（写回后不再重触发）。
    返回 (合并后副本, 是否有改动)。纯函数：不改入参。"""
    if not isinstance(override, dict) or not isinstance(builtin, dict):
        return ({}, False)
    if override.get("schema") is not None or builtin.get("schema") is None:
        return (json.loads(json.dumps(override)), False)
    merged = json.loads(json.dumps(override))
    merged["schema"] = builtin.get("schema")
    base_rooms = {str(r.get("id")): r for r in builtin.get("rooms") or []
                  if isinstance(r, dict) and r.get("id")}
    for room in merged.get("rooms") or []:
        if not isinstance(room, dict):
            continue
        base = base_rooms.get(str(room.get("id")))
        if base is None:
            continue   # 用户自建房：布局就是用户自定义
        for field in ("center", "size"):
            bv = _num_list(base.get(field))
            if bv is not None and room.get(field) != bv:
                room[field] = bv
    base_edges: dict[frozenset[str], dict[str, Any]] = {}
    for e in builtin.get("edges") or []:
        if isinstance(e, dict) and e.get("a") and e.get("b"):
            base_edges[frozenset((str(e["a"]), str(e["b"])))] = e
    for edge in merged.get("edges") or []:
        if not isinstance(edge, dict) or not edge.get("a") or not edge.get("b"):
            continue
        base = base_edges.get(frozenset((str(edge["a"]), str(edge["b"]))))
        if base is None:
            continue
        try:
            bs = float(base.get("seconds"))
        except (TypeError, ValueError):
            continue
        try:
            cs = float(edge.get("seconds"))
        except (TypeError, ValueError):
            cs = None
        if cs != bs:
            edge["seconds"] = bs
    return (merged, True)


def manor_override_path(data_dir: str) -> str:
    return os.path.join(str(data_dir), CONFIG.SP_MANOR_OVERRIDE_DIR, "manor.json")


def save_manor_override(data_dir: str, manor: dict[str, Any]) -> bool:
    """写回 manor 覆盖层（代际迁移落盘；原子写 UTF-8）。失败返回 False，不抛。"""
    try:
        os.makedirs(os.path.dirname(manor_override_path(data_dir)), exist_ok=True)
        _atomic_write(manor_override_path(data_dir),
                      json.dumps(manor, ensure_ascii=False, indent=1))
        return True
    except OSError:
        return False


def load_objects(data_dir: str) -> dict[str, Any]:
    """物品账本 objects.json；坏文件当空。"""
    path = os.path.join(str(data_dir), "objects.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"schema": 1, "items": []}
    except (OSError, ValueError):
        return {"schema": 1, "items": []}


def save_objects(data_dir: str, obj: dict[str, Any]) -> None:
    path = os.path.join(str(data_dir), "objects.json")
    _atomic_write(path, json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


SPACE_SUFFIX = ".space.json"   # 空间层状态后缀（与 main.py ManorStore 挂载一致）


def migrate_legacy_manor(src_data_dir: str, dst_data_dir: str, character: str) -> int:
    """数据回退迁移（锚点 1）：旧 manor plugin_data → 新空间层路径。

    旧 state/<session>.json → 新 state/<character>_<session>.space.json；
    旧 objects.json → 新目录根。已存在同名目标则跳过该文件（保护新层已写数据，
    亦即天然幂等：重跑全跳过返回 0）；迁移成功后在旧文件同目录写 <原名>.migrated
    空标记（不删旧文件）。字节级复制，不解析不改写不转换行。返回迁移文件数。
    """
    character = str(character or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9-]*", character or ""):
        return 0   # 无合法默认角色（profile id 纪律：小写开头、禁下划线）无从建空间键
    src = str(src_data_dir)
    dst = str(dst_data_dir)
    count = 0
    src_state = os.path.join(src, CONFIG.SP_STATE_DIR_NAME)
    try:
        names = sorted(os.listdir(src_state))
    except OSError:
        names = []   # 旧目录无 state/（或不合法）→ 只看 objects.json
    dst_state = os.path.join(dst, CONFIG.SP_STATE_DIR_NAME)
    for name in names:
        if not name.endswith(".json") or name.endswith(".tmp"):
            continue   # 跳过 .tmp 残留；.migrated 标记不以 .json 结尾自然排除
        session = name[: -len(".json")]
        if not re.fullmatch(CONFIG.SP_SESSION_RE, session or ""):
            continue   # session 白名单纪律（防路径穿越）沿用
        src_path = os.path.join(src_state, name)
        if not os.path.isfile(src_path):
            continue
        dst_path = os.path.join(dst_state, f"{character}_{session}{SPACE_SUFFIX}")
        if os.path.isfile(dst_path):
            continue   # 新层已有同名数据 → 绝不覆盖
        try:
            os.makedirs(dst_state, exist_ok=True)
            with open(src_path, "rb") as f:
                raw = f.read()
            _atomic_write_bytes(dst_path, raw)
            _atomic_write_bytes(src_path + ".migrated", b"")   # 成功后才打标记
        except OSError:
            continue
        count += 1
    src_obj = os.path.join(src, "objects.json")
    if os.path.isfile(src_obj):
        dst_obj = os.path.join(dst, "objects.json")
        if not os.path.isfile(dst_obj):
            try:
                os.makedirs(dst, exist_ok=True)
                with open(src_obj, "rb") as f:
                    raw = f.read()
                _atomic_write_bytes(dst_obj, raw)
                _atomic_write_bytes(src_obj + ".migrated", b"")
                count += 1
            except OSError:
                pass
    return count
