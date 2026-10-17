"""状态层测试：roundtrip、normalize 补缺、list_states、原子写。"""
import json
import os

from astrbot_plugin_virtual_body import CONFIG
from astrbot_plugin_virtual_body.core.state import StateStore, new_state, normalize

DEMO_A_MIN = {
    "id": "role_a", "name": "示例甲",
    "parts": [
        {"id": "ear_tip", "label": "耳尖", "sens": 9, "half_life": 60, "aliases": ["耳朵"]},
        {"id": "nape", "label": "后颈", "sens": 7, "half_life": 90, "aliases": ["后颈"]},
    ],
    "tiers": [{"key": "notice", "min": 0.15}],
}


def test_new_state_shape():
    st = new_state("role_a", "demo-user")
    assert st["character"] == "role_a"
    assert st["session"] == "demo-user"
    assert st["schema"] == CONFIG.STATE_SCHEMA
    assert st["phase"] == "building"
    assert st["parts"] == {}


def test_normalize_fills_missing_and_drops_unknown_parts():
    dirty = {
        "character": "role_a", "session": "123", "phase": "weird_phase",
        "arousal": 3.7, "tolerance": -5,
        "parts": {
            "ear_tip": {"value": 0.7, "last_event_at": "2026-09-19T00:00:00+00:00"},
            "tail_gone": {"value": 0.9},   # profile 已删除的部位
            "nape": "corrupted",
        },
    }
    st = normalize(dirty, DEMO_A_MIN)
    assert st["phase"] == "building"                       # 非法 phase 回 building
    assert 0.0 <= st["arousal"] <= 1.0 and st["arousal"] == 1.0
    assert st["tolerance"] == 0.0                          # 负值钳 0
    assert set(st["parts"]) == {"ear_tip", "nape"}         # 未知部位清除，缺失补齐
    assert st["parts"]["ear_tip"]["value"] == 0.7
    assert st["parts"]["nape"]["value"] == 0.0             # 损坏条目回默认
    assert st["parts"]["ear_tip"]["cool_until"] is None
    # 幂等
    assert normalize(st, DEMO_A_MIN) == st


def test_normalize_none_and_garbage():
    st = normalize(None, DEMO_A_MIN)
    assert st["character"] == "role_a"
    assert normalize("garbage", DEMO_A_MIN)["phase"] == "building"   # type: ignore[arg-type]


def test_state_signature_stability():
    # 优化轮：指纹不含 updated_at；<1e-4 的衰减漂移不触发；phase/档位/冷却变化触发
    a = normalize(None, DEMO_A_MIN)
    a["parts"]["ear_tip"]["value"] = 0.4
    a["updated_at"] = "2026-09-19T00:00:00+00:00"
    b = json.loads(json.dumps(a))
    b["updated_at"] = "2026-09-19T01:00:00+00:00"
    assert normalize(a, DEMO_A_MIN) is not None
    from astrbot_plugin_virtual_body.core.state import state_signature
    assert state_signature(a) == state_signature(b)
    b2 = json.loads(json.dumps(a))
    b2["parts"]["ear_tip"]["value"] = 0.4 + 1e-6   # 末位漂移
    assert state_signature(b2) == state_signature(a)
    b3 = json.loads(json.dumps(a))
    b3["parts"]["ear_tip"]["value"] = 0.41          # 实质变化
    assert state_signature(b3) != state_signature(a)
    b4 = json.loads(json.dumps(a))
    b4["phase"] = "afterglow"
    assert state_signature(b4) != state_signature(a)
    # 找bug 轮：今日累计的日切/计数变化触发写盘
    b5 = json.loads(json.dumps(a))
    b5["events_today"] = {"day": "2000-01-01", "count": 7}
    assert state_signature(b5) != state_signature(a)


def test_get_or_create_reanchors_identity(tmp_path):
    # 找bug 轮：文件内容里的 character/session 与锚点错位时以调用方为准
    store = StateStore(str(tmp_path))
    st = normalize(None, DEMO_A_MIN)
    st["character"] = "wrong"
    st["session"] = "000"
    store.save("role_a", "111", st)
    got = store.get_or_create("role_a", "111", DEMO_A_MIN)
    assert got["character"] == "role_a" and got["session"] == "111"


def test_bindings_roundtrip_and_garbage(tmp_path):
    # 补充能力轮：session→character 绑定表；坏文件当空表
    store = StateStore(str(tmp_path))
    assert store.load_bindings() == {}
    store.save_bindings({"111": "role_a", "222": "role_b"})
    assert store.load_bindings() == {"111": "role_a", "222": "role_b"}
    (tmp_path / CONFIG.STATE_DIR_NAME / CONFIG.BINDINGS_FILE).write_text("not json", encoding="utf-8")
    assert store.load_bindings() == {}
    (tmp_path / CONFIG.STATE_DIR_NAME / CONFIG.BINDINGS_FILE).write_text('["list"]', encoding="utf-8")
    assert store.load_bindings() == {}


def test_delete_state(tmp_path):
    # WebUI 重置用：删除存在返回 True、不存在返回 False
    store = StateStore(str(tmp_path))
    st = normalize(None, DEMO_A_MIN)
    store.save("role_a", "111", st)
    assert store.delete_state("role_a", "111") is True
    assert store.load("role_a", "111") is None
    assert store.delete_state("role_a", "111") is False


# ---- WebUI 轮：profile 编辑校验与覆盖落盘 ----

def test_validate_parts():
    from astrbot_plugin_virtual_body.core.state import validate_parts
    good = [{"id": "ear_tip", "label": "耳尖", "sens": 9, "half_life": 60,
             "aliases": ["耳朵"], "radiate": {"tail_root": 0.3}},
            {"id": "tail_root", "label": "尾根", "sens": 6, "half_life": 90, "aliases": ["尾巴"]}]
    ok, err = validate_parts(good)
    assert ok, err
    cases = [
        ([], "非空"),                                                        # 空
        ([{"id": "Bad", "label": "x", "sens": 5, "half_life": 60, "aliases": ["x"]}], "id 大写"),   # id 非法
        ([{"id": "ear_tip", "label": "", "sens": 5, "half_life": 60, "aliases": ["x"]}], "label"),  # label 空
        ([{"id": "ear_tip", "label": "x", "sens": 11, "half_life": 60, "aliases": ["x"]}], "sens"), # sens 越界
        ([{"id": "ear_tip", "label": "x", "sens": 5, "half_life": 1, "aliases": ["x"]}], "half_life"), # τ 越界
        ([{"id": "ear_tip", "label": "x", "sens": 5, "half_life": 60, "aliases": ["x"], "radiate": {"ear_tip": 0.5}}], "自环"),
        ([{"id": "ear_tip", "label": "x", "sens": 5, "half_life": 60, "aliases": ["x"], "radiate": {"tail_root": 0.3}}], "目标缺失"),
    ]
    for parts, tag in cases:
        ok, err = validate_parts(parts)
        assert not ok, f"{tag} 应校验失败"


def test_save_profile_override(tmp_path):
    from astrbot_plugin_virtual_body.core.state import save_profile_override
    base = json.loads(json.dumps(DEMO_A_MIN))
    base["parts"] = [{"id": "ear_tip", "label": "耳尖", "sens": 9, "half_life": 60, "aliases": ["耳朵"]}]
    # 第一次保存
    patch = {"parts": base["parts"] + [{"id": "nape", "label": "后颈", "sens": 7, "half_life": 90, "aliases": ["后颈"]}],
             "action_words": {"蹭": 0.4}}
    ok, err = save_profile_override(str(tmp_path), base, patch)
    assert ok, err
    saved = json.loads((tmp_path / "profiles" / "role_a.json").read_text(encoding="utf-8"))
    assert len(saved["parts"]) == 2 and saved["action_words"]["蹭"] == 0.4
    assert saved["id"] == "role_a"
    # 第二次保存：旧文件备份为 .bak
    patch2 = {"action_words": {"蹭": 0.5}}
    ok, err = save_profile_override(str(tmp_path), base, patch2)
    assert ok, err
    assert (tmp_path / "profiles" / "role_a.json.bak").is_file()
    saved2 = json.loads((tmp_path / "profiles" / "role_a.json").read_text(encoding="utf-8"))
    assert saved2["action_words"]["蹭"] == 0.5
    assert len(saved2["parts"]) == 1   # 未提交 parts 键则保留 base 原样
    # 校验失败不落盘
    ok, err = save_profile_override(str(tmp_path), base, {"parts": []})
    assert not ok and "parts" in err


def test_normalize_nan_and_inf():
    # 审查轮：NaN/Inf 不走钳制链（NaN 比较恒 False 会顶到上界 1.0）
    st = normalize({"character": "role_a", "session": "1",
                    "arousal": float("nan"), "tolerance": float("inf"),
                    "parts": {"ear_tip": {"value": float("nan")}}}, DEMO_A_MIN)
    assert st["arousal"] == 0.0
    assert st["tolerance"] == 0.0
    assert st["parts"]["ear_tip"]["value"] == 0.0


def test_validate_parts_fine_tuning_keys():
    # 0.29.1 部位级细调键：合法值放行、越界/坏类型/未知 type 键拒绝（保存口显式拦截）
    from astrbot_plugin_virtual_body.core.state import validate_parts
    base = {"id": "ear_tip", "label": "耳尖", "sens": 9, "half_life": 600, "aliases": ["耳朵"], "radiate": {}}
    ok, err = validate_parts([dict(base, gain_mult=1.5, tolerance_mult=0.8, windup_mult=1.2,
                                   type_mults={"air": 1.5}, material_affinity={"silk": 1.4})])
    assert ok, err
    cases = [
        ("gain_mult", 9.9, "gain_mult"),                    # 越上界
        ("tolerance_mult", 0.1, "tolerance_mult"),          # 越下界
        ("windup_mult", "x", "windup_mult"),                # 非数字
        ("type_mults", {"laser": 1.5}, "type_mults"),       # 未知类别键
        ("type_mults", {"pain": 9.9}, "type_mults"),        # 值越界
        ("material_affinity", {"silk": 9.9}, "material_affinity"),   # 值越界
        ("material_affinity", "x", "material_affinity"),    # 非 dict
    ]
    for key, val, frag in cases:
        ok, err = validate_parts([dict(base, **{key: val})])
        assert not ok and frag in err, f"{key}={val!r} 应校验失败: {err}"


def test_save_profile_override_keeps_fine_tuning_keys(tmp_path):
    # 0.29.1 保存链路透传：parts[] 细调键经 web_api → save_profile_override 原样落盘（不被剥除）
    from astrbot_plugin_virtual_body.core.state import save_profile_override
    base = json.loads(json.dumps(DEMO_A_MIN))
    part = {"id": "ear_tip", "label": "耳尖", "sens": 9, "half_life": 600, "aliases": ["耳朵"],
            "gain_mult": 1.8, "tolerance_mult": 0.5, "windup_mult": 1.2,
            "type_mults": {"air": 1.5}, "material_affinity": {"silk": 1.4}}
    ok, err = save_profile_override(str(tmp_path), base, {"parts": [part]})
    assert ok, err
    saved = json.loads((tmp_path / "profiles" / "role_a.json").read_text(encoding="utf-8"))
    sp = next(p for p in saved["parts"] if p["id"] == "ear_tip")
    assert sp["gain_mult"] == 1.8 and sp["tolerance_mult"] == 0.5 and sp["windup_mult"] == 1.2
    assert sp["type_mults"] == {"air": 1.5} and sp["material_affinity"] == {"silk": 1.4}
    # 越界键保存被拒（web_api 链路同一入口）
    ok2, err2 = save_profile_override(str(tmp_path), base, {"parts": [dict(part, gain_mult=9.9)]})
    assert not ok2 and "gain_mult" in err2


def test_validate_parts_alias_duplicate():
    # 审查轮：跨部位共享 label/alias 会双计
    from astrbot_plugin_virtual_body.core.state import validate_parts
    dup = [
        {"id": "a", "label": "耳朵", "sens": 5, "half_life": 60, "aliases": ["耳"]},
        {"id": "b", "label": "耳根", "sens": 5, "half_life": 60, "aliases": ["耳"]},
    ]
    ok, err = validate_parts(dup)
    assert not ok and "耳" in err


def test_store_mtime_cache_consistency(tmp_path):
    # 消耗优化轮：mtime 缓存命中省 json.load；外部改文件（mtime 变）必须重读；调用方改动不得污染缓存
    store = StateStore(str(tmp_path))
    st = normalize(None, DEMO_A_MIN)
    st["parts"]["ear_tip"]["value"] = 0.4
    store.save("role_a", "111", st)
    got = store.get_or_create("role_a", "111", DEMO_A_MIN)
    assert got["parts"]["ear_tip"]["value"] == 0.4
    got["parts"]["ear_tip"]["value"] = 0.99      # 污染尝试（不 save）
    got2 = store.get_or_create("role_a", "111", DEMO_A_MIN)
    assert got2["parts"]["ear_tip"]["value"] == 0.4   # 缓存未被污染
    # 外部直接改文件 → mtime 变 → 重读
    import time as _t
    raw = json.loads((tmp_path / CONFIG.STATE_DIR_NAME / "role_a_111.json").read_text(encoding="utf-8"))
    raw["parts"]["ear_tip"]["value"] = 0.7
    (tmp_path / CONFIG.STATE_DIR_NAME / "role_a_111.json").write_text(
        json.dumps(raw, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.utime(tmp_path / CONFIG.STATE_DIR_NAME / "role_a_111.json", (_t.time() + 5, _t.time() + 5))
    got3 = store.get_or_create("role_a", "111", DEMO_A_MIN)
    assert got3["parts"]["ear_tip"]["value"] == 0.7
    # save 写的是紧凑 JSON，roundtrip 语义不变
    store.save("role_a", "222", got3)
    assert store.load("role_a", "222") == got3


def test_save_profile_auto_ids(tmp_path):
    # 轮 72：空/占位 id 保存时自动生成唯一 part_N
    from astrbot_plugin_virtual_body.core.state import save_profile_override
    base = json.loads(json.dumps(DEMO_A_MIN))
    base["parts"] = []
    patch = {"parts": [
        {"id": "", "label": "甲", "sens": 5, "half_life": 60, "aliases": ["x"]},
        {"id": "new_part_1", "label": "乙", "sens": 5, "half_life": 60, "aliases": ["y"]},
        {"id": "ear_tip", "label": "耳", "sens": 9, "half_life": 60, "aliases": ["耳"]},
    ]}
    ok, err = save_profile_override(str(tmp_path), base, patch)
    assert ok, err
    saved = json.loads((tmp_path / "profiles" / "role_a.json").read_text(encoding="utf-8"))
    ids = [p["id"] for p in saved["parts"]]
    assert ids == ["part_1", "part_2", "ear_tip"]   # 占位→自动；已有 id 保留
    assert len(set(ids)) == 3


def test_names_overlay_roundtrip(tmp_path):
    # 命名层轮：独立文件与参数层分离；坏结构丢弃；apply 只覆盖已有部位
    from astrbot_plugin_virtual_body.core.state import apply_names_overlay, load_names_overlay, save_names_overlay
    assert load_names_overlay(str(tmp_path), "role_a") == {}
    ok, err = save_names_overlay(str(tmp_path), "role_a", {
        "n1": {"label": "测试名", "aliases": ["甲", "乙"]},
        "n2": {"label": "   "},                 # 空名不落
        "n3": "garbage",                        # 非对象丢弃
    })
    assert ok, err
    loaded = load_names_overlay(str(tmp_path), "role_a")
    assert loaded == {"n1": {"label": "测试名", "aliases": ["甲", "乙"]}}
    prof = {"parts": [{"id": "n1", "label": "01"}, {"id": "n9", "label": "09"}]}
    apply_names_overlay(prof, loaded)
    assert prof["parts"][0]["label"] == "测试名"
    assert prof["parts"][1]["label"] == "09"    # 无 overlay 不动


def test_store_roundtrip(tmp_path):
    store = StateStore(str(tmp_path))
    st = normalize(None, DEMO_A_MIN)
    st["parts"]["ear_tip"]["value"] = 0.42
    store.save("role_a", "demo-user", st)
    loaded = store.load("role_a", "demo-user")
    assert loaded == st
    assert store.load("role_a", "no_such") is None


def test_store_list_and_missing(tmp_path):
    store = StateStore(str(tmp_path))
    st = normalize(None, DEMO_A_MIN)
    store.save("role_a", "111", st)
    store.save("role_b", "222", st)
    pairs = sorted(store.list_states())
    assert ("role_a", "111") in pairs and ("role_b", "222") in pairs
    # 临时文件不入列
    (tmp_path / CONFIG.STATE_DIR_NAME / "role_a_333.json.tmp").write_text("{}", encoding="utf-8")
    assert ("role_a", "333") not in store.list_states()


# ---- 0.27.8 点位失效回归：_auto_part_ids 兜底唯一性 ----
# 事故背景：前端 slugLabel 兜底生成光杆 part id，二次添加撞名。后端防线是
# _auto_part_ids——空/占位 id 自动生成全局唯一的 part_N。此处直测各混排场景。

def test_auto_part_ids_uniqueness_mixed():
    from astrbot_plugin_virtual_body.core.state import _auto_part_ids
    parts = [
        {"id": "", "label": "甲"},
        {"id": "part_1", "label": "乙"},        # 显式占用 part_1（前端光杆 part 形态）
        {"id": "", "label": "丙"},
        {"id": "new_part_3", "label": "丁"},     # 前端占位前缀
        {"id": "ear_tip", "label": "耳"},
        "garbage",                               # 非对象元素原样跳过
        {"id": "   ", "label": "戊"},            # 空白视为空
    ]
    out = _auto_part_ids(parts)
    ids = [p["id"] for p in out if isinstance(p, dict)]
    # 生成跳过已占用的 part_1，且与既有合法 id 互不碰撞
    assert ids == ["part_2", "part_1", "part_3", "part_4", "ear_tip", "part_5"]
    assert len(set(ids)) == len(ids)             # 全局唯一
    assert "garbage" in out                      # 非对象不动
    # 幂等：再跑一遍不再改动
    assert [p["id"] for p in _auto_part_ids(out) if isinstance(p, dict)] == ids


def test_auto_part_ids_keeps_explicit_duplicates_for_validator():
    # 显式重复 id 不由兜底静默去重（防悄悄改写用户数据）——交 validate_parts 明确拒绝
    from astrbot_plugin_virtual_body.core.state import _auto_part_ids, validate_parts
    parts = [{"id": "dup", "label": "甲", "sens": 5, "half_life": 600, "aliases": ["x"]},
             {"id": "dup", "label": "乙", "sens": 5, "half_life": 600, "aliases": ["y"]}]
    out = _auto_part_ids(parts)
    assert [p["id"] for p in out] == ["dup", "dup"]
    ok, err = validate_parts(out)
    assert not ok and "重复" in err


def test_save_profile_rejects_duplicate_ids(tmp_path):
    # 0.27.8 撞名事故的后端闭环：重复显式 id 保存被拒、不落盘
    from astrbot_plugin_virtual_body.core.state import save_profile_override
    base = json.loads(json.dumps(DEMO_A_MIN))
    base["parts"] = []
    patch = {"parts": [
        {"id": "dup", "label": "甲", "sens": 5, "half_life": 600, "aliases": ["x"]},
        {"id": "dup", "label": "乙", "sens": 5, "half_life": 600, "aliases": ["y"]},
    ]}
    ok, err = save_profile_override(str(tmp_path), base, patch)
    assert not ok and "重复" in err
    assert not (tmp_path / "profiles" / "role_a.json").exists()


def test_save_profile_override_stamps_base_version(tmp_path):
    # 0.27.9 覆盖层版本化：覆盖层落盘必须带 base_version=CONFIG.VERSION——
    # 加载端据此识别旧代覆盖层并做 τ 数值迁移（0.27.8『覆盖层旧 τ 压制内置新 τ』事故的复发防线；
    # 迁移合并逻辑本体在 main._migrate_legacy_override，因 astrbot 依赖不在本测试可达范围）
    from astrbot_plugin_virtual_body.core.state import save_profile_override
    base = json.loads(json.dumps(DEMO_A_MIN))
    base["parts"] = [{"id": "ear_tip", "label": "耳尖", "sens": 9, "half_life": 900, "aliases": ["耳朵"]}]
    ok, err = save_profile_override(str(tmp_path), base, {"action_words": {"蹭": 0.4}})
    assert ok, err
    saved = json.loads((tmp_path / "profiles" / "role_a.json").read_text(encoding="utf-8"))
    assert saved["base_version"] == CONFIG.VERSION
