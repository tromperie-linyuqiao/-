import pytest

from astrbot_plugin_virtual_body import CONFIG
from astrbot_plugin_virtual_body.core.store import (
    ManorStore,
    load_manor,
    load_objects,
    migrate_legacy_manor,
    save_objects,
)

from tests.test_space import MANOR


def test_store_roundtrip(tmp_path):
    store = ManorStore(str(tmp_path))
    st = store.get_or_create("demo-user", MANOR)
    st["she"]["room"] = "study"
    store.save("demo-user", st)
    loaded = store.get_or_create("demo-user", MANOR)
    assert loaded["she"]["room"] == "study"
    assert store.load("nosuch") is None


def test_store_session_path_traversal_rejected(tmp_path):
    store = ManorStore(str(tmp_path))
    with pytest.raises(ValueError):
        store._path("../../etc/passwd")
    assert store.load("../../x") is None
    store.save("../../x", {})   # 不抛不落
    assert not list((tmp_path / CONFIG.SP_STATE_DIR_NAME).iterdir()) or all(
        f.name != "x.json" for f in (tmp_path / CONFIG.SP_STATE_DIR_NAME).iterdir())


def test_store_mtime_cache_external_change(tmp_path):
    import json, os, time
    store = ManorStore(str(tmp_path))
    st = store.get_or_create("s1", MANOR)
    st["he"]["room"] = "study"
    store.save("s1", st)
    assert store.get_or_create("s1", MANOR)["he"]["room"] == "study"
    # 外部直改文件 → mtime 变 → 重读
    path = tmp_path / CONFIG.SP_STATE_DIR_NAME / "s1.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["he"]["room"] = "kitchen"
    path.write_text(json.dumps(raw, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert store.get_or_create("s1", MANOR)["he"]["room"] == "kitchen"


def test_load_manor_builtin_and_bad_override(tmp_path):
    import pathlib
    plugin_dir = pathlib.Path(__file__).resolve().parents[1]
    manor, source = load_manor(str(tmp_path), str(plugin_dir))
    assert source == "builtin" and manor["id"] == "manor"
    bad = tmp_path / CONFIG.SP_MANOR_OVERRIDE_DIR
    bad.mkdir(parents=True)
    (bad / "manor.json").write_text("{broken", encoding="utf-8")
    manor2, source2 = load_manor(str(tmp_path), str(plugin_dir))
    assert source2 == "builtin"   # 坏覆盖层回退自带


def test_objects_roundtrip(tmp_path):
    assert load_objects(str(tmp_path))["items"] == []
    save_objects(str(tmp_path), {"schema": 1, "items": [{"id": "talisker", "room": "bar-corner"}]})
    assert load_objects(str(tmp_path))["items"][0]["id"] == "talisker"


def test_migrate_legacy_manor_roundtrip(tmp_path):
    import json, os
    src = tmp_path / "astrbot_plugin_virtual_manor"
    dst = tmp_path / "astrbot_plugin_virtual_body"
    state_dir = src / CONFIG.SP_STATE_DIR_NAME
    state_dir.mkdir(parents=True)
    dst.mkdir()
    old_st = {"schema": CONFIG.SP_STATE_SCHEMA, "session": "demo-user",
              "she": {"room": "study"}, "he": {"room": "kitchen"}}
    old_bytes = json.dumps(old_st, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    (state_dir / "demo-user.json").write_bytes(old_bytes)
    obj_bytes = b'{"schema":1,"items":[{"id":"talisker","room":"bar-corner"}]}'
    (src / "objects.json").write_bytes(obj_bytes)
    assert migrate_legacy_manor(str(src), str(dst), "aria") == 2
    # 新文件内容 = 旧文件字节（不解析、不改写、不转行）
    new_st = dst / CONFIG.SP_STATE_DIR_NAME / "aria_demo-user.space.json"
    assert new_st.read_bytes() == old_bytes
    assert (dst / "objects.json").read_bytes() == obj_bytes
    # 空间层 store 可读，身体层 store 不串扰
    space_store = ManorStore(str(dst), suffix=".space.json")
    assert space_store.list_sessions() == ["aria_demo-user"]
    assert space_store.load("aria_demo-user")["she"]["room"] == "study"
    assert ManorStore(str(dst)).list_sessions() == []
    # 旧文件不删 + 同目录空标记
    assert (state_dir / "demo-user.json").read_bytes() == old_bytes
    assert (state_dir / "demo-user.json.migrated").read_bytes() == b""
    assert (src / "objects.json").read_bytes() == obj_bytes
    assert (src / "objects.json.migrated").read_bytes() == b""
    # 幂等重跑：目标全在 → 0，且无新增文件
    assert migrate_legacy_manor(str(src), str(dst), "aria") == 0
    assert sorted(os.listdir(dst / CONFIG.SP_STATE_DIR_NAME)) == ["aria_demo-user.space.json"]


def test_migrate_legacy_manor_skip_existing_and_guards(tmp_path):
    src = tmp_path / "astrbot_plugin_virtual_manor"
    dst = tmp_path / "astrbot_plugin_virtual_body"
    (src / CONFIG.SP_STATE_DIR_NAME).mkdir(parents=True)
    (dst / CONFIG.SP_STATE_DIR_NAME).mkdir(parents=True)
    (src / CONFIG.SP_STATE_DIR_NAME / "demo-user.json").write_text('{"schema":1,"old":true}', encoding="utf-8")
    (src / CONFIG.SP_STATE_DIR_NAME / "10086.json").write_text('{"schema":1,"old":true}', encoding="utf-8")
    (src / CONFIG.SP_STATE_DIR_NAME / "junk.json.tmp").write_text("garbage", encoding="utf-8")
    (src / CONFIG.SP_STATE_DIR_NAME / "notes.txt").write_text("x", encoding="utf-8")
    (src / "objects.json").write_text('{"schema":1,"items":[]}', encoding="utf-8")
    # 新层已有同名目标（P1/P2 时代写过）→ 跳过不覆盖，且不给被跳过的旧文件打标记
    (dst / CONFIG.SP_STATE_DIR_NAME / "aria_demo-user.space.json").write_text('{"schema":1,"new":true}', encoding="utf-8")
    (dst / "objects.json").write_text('{"schema":1,"items":[{"id":"new"}]}', encoding="utf-8")
    assert migrate_legacy_manor(str(src), str(dst), "aria") == 1   # 只迁了 10086
    assert (dst / CONFIG.SP_STATE_DIR_NAME / "aria_demo-user.space.json").read_text(encoding="utf-8") == '{"schema":1,"new":true}'
    assert (dst / "objects.json").read_text(encoding="utf-8") == '{"schema":1,"items":[{"id":"new"}]}'
    assert not (src / CONFIG.SP_STATE_DIR_NAME / "demo-user.json.migrated").exists()
    assert not (src / "objects.json.migrated").exists()
    assert (src / CONFIG.SP_STATE_DIR_NAME / "10086.json.migrated").read_bytes() == b""
    assert not (dst / CONFIG.SP_STATE_DIR_NAME / "aria_junk.space.json").exists()   # .tmp/.txt 不迁
    # 旧目录不存在 → 静默 0
    assert migrate_legacy_manor(str(tmp_path / "nosuch"), str(dst), "aria") == 0
    # 非法 character（空 / 带下划线）→ 0 且零副作用（不建目录不写文件）
    fresh = tmp_path / "fresh_dst"
    fresh.mkdir()
    assert migrate_legacy_manor(str(src), str(fresh), "") == 0
    assert migrate_legacy_manor(str(src), str(fresh), "bad_id") == 0
    assert not (fresh / CONFIG.SP_STATE_DIR_NAME).exists()
