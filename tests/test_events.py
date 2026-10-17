"""事件解析测试：显式指令 / 文本抽取 / LLM 兜底 JSON。"""
import json
import pathlib

from astrbot_plugin_virtual_body.core import events

PROF_DIR = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "profiles"
DEMO_A = json.loads((PROF_DIR / "role_a.json").read_text(encoding="utf-8"))


# ---- 文本抽取：aliases + 动作词 ----

def test_extract_alias_and_action():
    assert events.extract_events("揉揉耳朵", DEMO_A) == [{"part": "ear_tip", "s": 0.6, "type": "touch"}]
    assert events.extract_events("咬耳朵", DEMO_A) == [{"part": "ear_tip", "s": 0.9, "type": "touch"}]
    assert events.extract_events("轻轻拍了后颈", DEMO_A) == [{"part": "nape", "s": 0.275, "type": "touch"}]  # 修饰词 轻轻×0.55
    assert events.extract_events("摸了摸尾巴，又揉了揉耳尖", DEMO_A) == [
        {"part": "tail_root", "s": 0.5, "type": "touch"},   # 邻近匹配：尾巴取"摸"0.5，不再吃全局"揉"0.6
        {"part": "ear_tip", "s": 0.6, "type": "touch"},
    ]


def test_extract_expanded_action_words():
    # 0.1.2 词表扩充：轻→重全梯度
    assert events.extract_events("蹭蹭耳朵", DEMO_A) == [{"part": "ear_tip", "s": 0.3, "type": "touch"}]
    assert events.extract_events("捏了捏耳尖", DEMO_A) == [{"part": "ear_tip", "s": 0.7, "type": "touch"}]
    assert events.extract_events("对着尾巴吹气", DEMO_A) == [{"part": "tail_root", "s": 0.25, "type": "touch"}]
    assert events.extract_events("吮吸耳尖", DEMO_A) == [{"part": "ear_tip", "s": 0.9, "type": "touch"}]
    # 多动作词取最高强度
    assert events.extract_events("先蹭后咬耳朵", DEMO_A) == [{"part": "ear_tip", "s": 0.9, "type": "touch"}]


def test_extract_rejects_partial_hits():
    assert events.extract_events("", DEMO_A) == []          # 空消息
    assert events.extract_events("今天天气不错", DEMO_A) == []  # 无关消息
    assert events.extract_events("耳朵有点痒", DEMO_A) == []    # 只有部位无动作词 → 不猜测
    assert events.extract_events("摸鱼摸了一下午", DEMO_A) == []  # 只有动作词无部位 → 无事件


# ---- 显式指令 ----

def test_parse_touch_command():
    # /touch 解析（单部位走 parse_touch_multi 的退化形式）
    assert events.parse_touch_multi("耳尖 重 舔", DEMO_A) == [{"part": "ear_tip", "s": 1.0, "type": "舔"}]
    assert events.parse_touch_multi("后颈 0.5", DEMO_A) == [{"part": "nape", "s": 0.5, "type": "touch"}]
    assert events.parse_touch_multi("尾巴 中", DEMO_A) == [{"part": "tail_root", "s": 0.6, "type": "touch"}]
    assert events.parse_touch_multi("不存在 重", DEMO_A) == []
    assert events.parse_touch_multi("耳尖", DEMO_A) == []           # 缺强度
    assert events.parse_touch_multi("耳尖 超重", DEMO_A) == []      # 强度词不认识
    assert events.parse_touch_multi("耳朵 2", DEMO_A) == []         # 数字越界
    assert events.parse_touch_multi("", DEMO_A) == []


# ---- LLM 兜底 JSON ----

def test_extract_ignores_quoted_hits():
    # 污染轮："" 引号内是歌词/诗词/转述约定，不触发身体事件
    assert events.extract_events('"风吹过耳朵的边疆"', DEMO_A) == []
    assert events.extract_events('她说"揉揉耳朵"只是比喻', DEMO_A) == []
    assert events.extract_events("一边摸耳朵，一边哼\"风吹过耳边\"", DEMO_A) == [
        {"part": "ear_tip", "s": 0.5, "type": "touch"}
    ]
    assert events.extract_events("摸了摸耳朵", DEMO_A) != []


def test_parse_touch_multi():
    # 细化轮：/touch 多部位（+ 连接，共享强度与类型），任一非法整条拒绝
    assert events.parse_touch_multi("耳尖+尾巴 重 舔", DEMO_A) == [
        {"part": "ear_tip", "s": 1.0, "type": "舔"},
        {"part": "tail_root", "s": 1.0, "type": "舔"},
    ]
    assert events.parse_touch_multi("后颈 0.5", DEMO_A) == [{"part": "nape", "s": 0.5, "type": "touch"}]
    assert events.parse_touch_multi("耳尖+不存在 重", DEMO_A) == []
    assert events.parse_touch_multi("耳尖+尾巴 超重", DEMO_A) == []


def test_intensity_modifiers():
    # 查漏补缺轮：强度修饰词乘算（动作词前 3 字内）
    assert events.extract_events("狠狠揉耳朵", DEMO_A) == [{"part": "ear_tip", "s": round(0.6 * 1.45, 6), "type": "touch"}]
    assert events.extract_events("用力咬耳朵", DEMO_A) == [{"part": "ear_tip", "s": 1.0, "type": "touch"}]  # 0.9×1.3 钳 1
    assert events.extract_events("慢慢摸尾巴", DEMO_A) == [{"part": "tail_root", "s": round(0.5 * 0.75, 6), "type": "touch"}]
    assert events.extract_events("揉耳朵", DEMO_A) == [{"part": "ear_tip", "s": 0.6, "type": "touch"}]   # 无修饰不乘


def test_extract_multi_occurrence_anchor():
    # 审查轮 P1 修复：部位只锚定最早出现会丢后半句的真实动作
    assert events.extract_events("耳朵很可爱，不过他更喜欢揉耳朵", DEMO_A) == [
        {"part": "ear_tip", "s": 0.6, "type": "touch"}]
    # 先拒绝后接受的句式：第一次出现被否定跳过，第二次真实动作仍触发
    assert events.extract_events("别摸耳朵了……好吧，那就摸耳朵", DEMO_A) == [
        {"part": "ear_tip", "s": 0.5, "type": "touch"}]


def test_extract_quoted_action_word_ignored():
    # 审查轮 P3：动作词在引号内（转述/强调）不算刺激
    assert events.extract_events('所谓"揉"耳朵根本不算数', DEMO_A) == []


def test_action_words_zero_strength_dropped():
    # 审查轮 P3：显式配 0 = 禁用该词（不再触发事件）
    prof = {"id": "t", "parts": DEMO_A["parts"], "tiers": DEMO_A["tiers"],
            "action_words": {"摸": 0}}
    assert events.extract_events("摸耳朵", prof) == []
    # 其他动作词不受影响
    assert events.extract_events("揉耳朵", prof) == [{"part": "ear_tip", "s": 0.6, "type": "touch"}]


def test_extract_negation_rejects():
    # 污染轮：拒绝不是刺激——否定前缀的动作词不触发事件
    assert events.extract_events("别摸耳朵", DEMO_A) == []
    assert events.extract_events("不许咬耳朵", DEMO_A) == []
    assert events.extract_events("不要揉我的尾巴", DEMO_A) == []
    # 拒绝按部位配对：尾巴照常、耳朵被拒绝
    assert events.extract_events("摸尾巴，别咬耳朵", DEMO_A) == [{"part": "tail_root", "s": 0.5, "type": "touch"}]
    # 普通修饰语不受影响
    assert events.extract_events("想要摸耳朵", DEMO_A) == [{"part": "ear_tip", "s": 0.5, "type": "touch"}]


def test_extract_quoted_then_real_hit():
    # 找bug 轮：引号内首现不埋没引号外的真实命中
    assert events.extract_events('他说"耳朵"这个词时，揉了揉我的耳朵', DEMO_A) == [
        {"part": "ear_tip", "s": 0.6, "type": "touch"}
    ]


def test_profile_action_words_override():
    # profile 可覆盖全局词强度、追加角色专属词；越界值丢弃；无 action_words 用全局表
    role_b = json.loads((PROF_DIR / "role_b.json").read_text(encoding="utf-8"))
    assert events.extract_events("顺着尾巴顺毛", role_b) == [{"part": "tail_base", "s": 0.4, "type": "touch"}]  # 专属词+部位
    assert events.extract_events("揉揉耳朵", role_b) == [{"part": "ear_tip", "s": 0.7, "type": "touch"}]    # 覆盖 0.6→0.7
    assert events.extract_events("揉揉耳朵", DEMO_A) == [{"part": "ear_tip", "s": 0.6, "type": "touch"}]     # role_a 仍用全局
    bad = {"id": "x", "parts": DEMO_A["parts"], "tiers": DEMO_A["tiers"],
           "action_words": {"碰": 5.0, "废": "高"}}   # 越界与非数值都被丢弃
    assert events.extract_events("碰一下耳朵", bad) == [{"part": "ear_tip", "s": 0.4, "type": "touch"}]


def test_extract_substring_shadowing():
    # 逻辑增强：alias 存在包含关系（"腰" ⊂ "腰侧"）时，长词占优不双计、不误配
    prof = {"id": "t", "parts": [
        {"id": "waist", "label": "腰", "sens": 6, "half_life": 90, "aliases": ["腰"]},
        {"id": "waist_side", "label": "腰侧", "sens": 6, "half_life": 90, "aliases": ["腰侧"]},
    ], "tiers": DEMO_A["tiers"]}
    assert events.extract_events("摸腰侧", prof) == [{"part": "waist_side", "s": 0.5, "type": "touch"}]
    assert events.extract_events("摸腰", prof) == [{"part": "waist", "s": 0.5, "type": "touch"}]
    assert events.parse_touch_multi("腰侧 0.5", prof) == [{"part": "waist_side", "s": 0.5, "type": "touch"}]
    assert events.parse_touch_multi("腰 中", prof) == [{"part": "waist", "s": 0.6, "type": "touch"}]
    assert events.parse_llm_json('{"part": "腰侧", "intensity": 0.5}', prof) == {"part": "waist_side", "s": 0.5, "type": "touch"}


def test_parse_llm_json():
    ok = events.parse_llm_json('{"part": "耳尖", "intensity": 0.7, "type": "舔"}', DEMO_A)
    assert ok == {"part": "ear_tip", "s": 0.7, "type": "舔"}
    wrapped = events.parse_llm_json('好的，抽取结果：{"part": "后颈", "intensity": 0.4} 以上。', DEMO_A)
    assert wrapped == {"part": "nape", "s": 0.4, "type": "touch"}
    assert events.parse_llm_json('{"none": true}', DEMO_A) is None
    assert events.parse_llm_json('不是接触事件', DEMO_A) is None
    assert events.parse_llm_json('{"part": "翅膀", "intensity": 0.5}', DEMO_A) is None   # 未知部位
    assert events.parse_llm_json('{"part": "耳尖", "intensity": 1.5}', DEMO_A) is None   # 越界
    assert events.parse_llm_json('{"part": "耳尖"}', DEMO_A) is None                     # 缺强度
    assert events.parse_llm_json('', DEMO_A) is None
