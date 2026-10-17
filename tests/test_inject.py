"""渲染与权限测试：注入格式、上限裁剪、白名单、表达参数。"""
import json
import pathlib

from astrbot_plugin_virtual_body import CONFIG
from astrbot_plugin_virtual_body.core import inject
from astrbot_plugin_virtual_body.core.state import normalize

PROF_DIR = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "profiles"
DEMO_A = json.loads((PROF_DIR / "role_a.json").read_text(encoding="utf-8"))


def mk_state(ear=0.62, nape=0.31, tail=0.55, tol=0.17, arousal=None, phase="building"):
    st = normalize(None, DEMO_A)
    st["parts"]["ear_tip"].update(value=ear, trend_delta=0.0)
    st["parts"]["nape"].update(value=nape, trend_delta=0.0)
    st["parts"]["tail_root"].update(value=tail, trend_delta=0.0)
    st["tolerance"] = tol
    st["arousal"] = arousal if arousal is not None else 0.44
    st["phase"] = phase
    return st


# ---- 验收用例 5：非白名单不注入 ----

def test_whitelist_gate():
    assert inject.should_inject("demo-user", DEMO_A) is True
    assert inject.should_inject("999999", DEMO_A) is False
    assert inject.should_inject("", DEMO_A) is False


# ---- 验收用例 6：注入文本不超上限 ----

def test_render_format_and_cap():
    st = mk_state()
    text = inject.render(st, DEMO_A)
    assert text.startswith('<身体状态 角色="示例甲">')
    assert text.endswith("</身体状态>")
    assert "兴奋 0.4｜" in text          # 紧凑默认：一位小数锚点+裸阶段词
    assert "耳尖 0.6（失控）" in text
    assert "约束：" in text
    assert len(text) <= 400


def test_render_full_form_opt_in():
    # 完整形态对照（compact=False）：两位小数+阶段前缀+快照属性+长护栏
    text = inject.render(mk_state(), DEMO_A, compact=False)
    assert text.startswith('<身体状态 快照="临时" 角色="示例甲">')
    assert "兴奋度 0.44" in text
    assert "阶段 building" in text
    assert "耳尖 0.62（失控）" in text
    assert "系统临时状态快照" in text


def test_render_trims_low_parts_when_capped():
    st = mk_state()
    text = inject.render(st, DEMO_A, max_chars=80)
    assert len(text) <= 80
    assert "兴奋" in text           # 头两行必保留（紧凑无"度"字）
    assert "尾根" not in text        # 低位部位先被裁掉


def test_render_empty_state_returns_empty():
    st = normalize(None, DEMO_A)
    assert inject.render(st, DEMO_A) == ""


def test_render_folds_near_zero_parts():
    # token 轮：低位且无档位的部位折叠；带档位的低位保留
    st = mk_state(ear=0.62, nape=0.03, tail=0.55, arousal=0.44)
    text = inject.render(st, DEMO_A)
    assert "后颈" not in text
    assert "耳尖 0.6（失控）" in text
    st2 = mk_state(ear=0.62, nape=0.16, tail=0.55, arousal=0.44)  # 0.16 过 notice 档
    assert "后颈" in inject.render(st2, DEMO_A)


def test_render_gated_below_first_tier():
    # 污染轮：building 期 arousal 未过第一档不注入；过档注入；余韵期不受门控
    assert inject.render(mk_state(ear=0.03, nape=0.02, tail=0.01, arousal=0.03), DEMO_A) == ""
    text = inject.render(mk_state(ear=0.16, nape=0.0, tail=0.0, arousal=0.145), DEMO_A)
    assert text == ""                     # 部位值≠arousal，以 arousal 为准
    text2 = inject.render(mk_state(ear=0.2, nape=0.0, tail=0.0, arousal=0.16), DEMO_A)
    assert "耳尖" in text2
    text3 = inject.render(mk_state(arousal=0.02, phase="afterglow"), DEMO_A)
    assert "｜afterglow｜" in text3


def test_render_gated_bad_tiers():
    # 找bug 轮：tiers 的 min 是坏值时不得抛异常，回退 CONFIG 门控阈值
    bad = dict(DEMO_A)
    bad["tiers"] = [{"key": "notice", "min": "abc"}]
    assert inject.render(mk_state(arousal=0.03), bad) == ""
    assert "耳尖" in inject.render(mk_state(arousal=0.44), bad)


def test_render_includes_event_type():
    # 逻辑增强轮：/touch 的 type 渲染进部位标签
    st = mk_state(arousal=0.44)
    st["parts"]["ear_tip"]["last_event_type"] = "舔"
    st["parts"]["ear_tip"]["last_event_at"] = "2026-09-19T00:00:00+00:00"
    st["updated_at"] = "2026-09-19T00:01:00+00:00"   # 时效内
    text = inject.render(st, DEMO_A)
    assert "耳尖 0.6（失控·舔）" in text
    st["parts"]["nape"]["last_event_type"] = ""      # 无类型时保持原样
    assert "后颈 0.3" in text


def test_render_phase_hints():
    # 补充能力轮：afterglow/cooldown 约束行带阶段指向，building 无追加
    after = inject.render(mk_state(arousal=0.05, phase="afterglow"), DEMO_A)
    assert "余韵未散" in after
    cool = inject.render(mk_state(arousal=0.05, phase="cooldown"), DEMO_A)
    assert "不应期" in cool
    normal = inject.render(mk_state(arousal=0.44, phase="building"), DEMO_A)
    assert "余韵" not in normal and "不应期" not in normal


def test_render_profile_expression_override():
    # 自由度轮：profile.expressions 覆盖档位句长；坏值回 CONFIG
    prof = dict(DEMO_A)
    prof["expressions"] = {"lose": {"max_sentence_len": 12}, "react": {"max_sentence_len": "快"}}
    st = mk_state(arousal=0.70)   # lose 档：profile 覆盖 12 优先生效（用户显式硬边界尊重）
    assert "单句不超过12字" in inject.render(st, prof)
    st2 = mk_state(arousal=0.44)  # react 档覆盖值坏 → 回 CONFIG 区间语义
    assert "二十字上下" in inject.render(st2, prof)


def test_render_trend_arrows():
    # ST 借鉴轮：窗口增量渲染为 ↑/↓/→
    st = mk_state()
    st["parts"]["ear_tip"]["trend_delta"] = 0.12
    st["parts"]["nape"]["trend_delta"] = -0.08
    text = inject.render(st, DEMO_A)
    assert "耳尖 0.6↑（失控）" in text
    assert "后颈 0.3↓（反应）" in text      # 0.31.0 阈值 react=0.30 → 0.31 已入反应档
    assert "尾根 0.6（失控）" in text   # 平稳无箭头（token 纪律）；0.55 一位小数进位 0.6；0.31.0 lose=0.50 → 失控
    # 无数字模式箭头保留
    text2 = inject.render(st, DEMO_A, show_numbers=False)
    assert "耳尖↑（失控）" in text2


def test_render_today_count():
    # ST 借鉴轮：今日累计>0 显示在首行，0 不显示（省 token）
    st = mk_state()
    assert "今日" not in inject.render(st, DEMO_A)
    st["events_today"] = {"day": "2026-09-19", "count": 3}
    assert "今日 3 次" in inject.render(st, DEMO_A)


def test_render_overload_warning():
    # ST 借鉴轮：building 期 arousal 逼近 overload（0.85-0.10）注入预警；余韵期不注
    assert "快到极限" in inject.render(mk_state(arousal=0.78), DEMO_A)
    assert "快到极限" not in inject.render(mk_state(arousal=0.70), DEMO_A)
    assert "快到极限" not in inject.render(mk_state(arousal=0.80, phase="afterglow"), DEMO_A)


def test_render_cooling_tag():
    # 自由度轮：冷却中部位标注"平复中"（cool_until 晚于 updated_at）
    st = mk_state(arousal=0.44)
    st["parts"]["ear_tip"]["cool_until"] = "2099-01-01T00:00:00+00:00"
    text = inject.render(st, DEMO_A)
    assert "平复中" in text
    st2 = mk_state(arousal=0.44)   # 无冷却不显示
    assert "平复中" not in inject.render(st2, DEMO_A)


# ---- 出站清洗（clean_chatter 借鉴）：模型模仿注入格式的标签残留剥除 ----

def test_sanitize_outbound_tags():
    # 完整标签对：整块（含内容）移除，周边文字保留
    full = "他怔了一下。\n<身体状态 角色=\"示例甲\">\n兴奋度 0.44｜阶段 building\n</身体状态>\n然后移开了手。"
    out = inject.sanitize_outbound_text(full)
    assert "身体状态" not in out
    assert "兴奋度" not in out          # 块内容不得泄漏进用户消息
    assert "他怔了一下。" in out and "然后移开了手。" in out
    # 孤立闭标签 / 孤立开标签 / 全角变体
    assert "身体状态" not in inject.sanitize_outbound_text("好。\n</身体状态>")
    assert "身体状态" not in inject.sanitize_outbound_text("嗯。<身体状态 角色=\"示例甲\">")
    assert "身体状态" not in inject.sanitize_outbound_text("先看看＜身体状态＞再说")
    # 正常对话与其他标签不受影响
    assert inject.sanitize_outbound_text("今天天气不错") == "今天天气不错"
    assert inject.sanitize_outbound_text("我看看<x>标签</x>") == "我看看<x>标签</x>"


def test_render_event_type_stale():
    # 细化轮：接触类型超时不再渲染（防过时信息误导模型）
    st = mk_state(arousal=0.44)
    st["parts"]["ear_tip"]["last_event_type"] = "舔"
    st["parts"]["ear_tip"]["last_event_at"] = "2026-09-19T00:00:00+00:00"
    st["updated_at"] = "2026-09-19T01:00:00+00:00"   # 1 小时前的事件 → 超时
    assert "（失控·舔）" not in inject.render(st, DEMO_A)
    st["updated_at"] = "2026-09-19T00:05:00+00:00"   # 5 分钟内 → 显示
    assert "（失控·舔）" in inject.render(st, DEMO_A)


def test_render_somatic_line():
    # 筋膜链轮：体感行（自主神经分级+链张力+颤抖）派生渲染
    text = inject.render(mk_state(arousal=0.44), DEMO_A)
    assert "体感：" in text and "呼吸" in text and "心跳" in text
    hi = inject.render(mk_state(arousal=0.7, nape=0.9), DEMO_A)
    assert "呼吸急促" in hi and "心跳很快" in hi   # 0.7 落 0.6+ 档
    assert "绷紧" in hi                             # nape 0.9 → 后侧链张力过阈
    mid = inject.render(mk_state(arousal=0.5), DEMO_A)
    assert "呼吸变浅" in mid and "心跳偏快" in mid   # 0.5 落 0.35-0.6 档
    low = inject.render(mk_state(arousal=0.05, phase="afterglow"), DEMO_A)
    assert "呼吸平稳" in low and "心跳平稳" in low


def test_posture_hints_by_height():
    # 动作可行性轮：身高差>15cm 才注入姿态提示；无 body/身高合法域外不提示
    prof = dict(DEMO_A)
    prof["body"] = {"height_cm": 180}
    hints = inject.posture_hints(prof, 160)
    assert len(hints) == 1 and "矮20cm" in hints[0] and "俯身" in hints[0]
    assert inject.posture_hints(prof, 172) == []            # 差 8cm 不提示
    rev = inject.posture_hints({"body": {"height_cm": 155}}, 175)
    assert "高20cm" in rev[0] and "踮脚" in rev[0]
    assert inject.posture_hints({"body": {}}, 175) == []    # 无身高不提示


def test_render_reach_and_posture_line():
    # 注入块新增 可及/动作边界 行；身高差条件注入
    text = inject.render(mk_state(), DEMO_A, user_height_cm=160)   # role_a 180 vs 160
    assert "可及：" in text and "自手不可及" in text
    assert "矮20cm" in text
    text2 = inject.render(mk_state(), DEMO_A)                      # 无对方身高
    assert "可及：" in text2 and "cm" not in text2.split("可及：")[1].split("\n")[0]


def test_render_fact_guard():
    # 污染轮：注入块带临时快照护栏（紧凑默认短语；完整形态在 test_render_full_form_opt_in）
    text = inject.render(mk_state(), DEMO_A)
    assert "临时快照" in text and "随轮刷新" in text


def test_render_constraint_wording_locked():
    # 端审封口：紧凑约束行关键措辞锁定（"反应藏进动作"系列是身体块的核心指令，
    # 与 PC 回复风格（短陈述句优先）协同设计——误改会破坏两插件的一致性）
    text = inject.render(mk_state(), DEMO_A)
    constraint_line = [ln for ln in text.splitlines() if ln.startswith("约束：")][0]
    assert "反应藏进动作" in constraint_line
    assert "动作先于台词" in constraint_line
    assert "状态系统跟踪" in constraint_line
    # 完整形态走 CONFIG 默认（面板键 vb_constraint_text 的默认值）
    full = inject.render(mk_state(), DEMO_A, compact=False)
    assert "把反应藏进动作里" in full and "回复只含对话与动作" in full


def test_render_without_numbers():
    st = mk_state()
    text = inject.render(st, DEMO_A, show_numbers=False)
    assert "0.62" not in text
    assert "耳尖" in text


def test_render_afterglow_phase():
    st = mk_state(arousal=0.05, phase="afterglow")
    text = inject.render(st, DEMO_A)
    assert "｜afterglow｜" in text


# ---- 表达/语音参数映射（第 9 节）----

def test_expression_params_by_tier():
    assert inject.expression_params(0.10, DEMO_A)["max_sentence_len"] == 30   # notice
    assert inject.expression_params(0.40, DEMO_A)["pauses"] == 1              # react
    assert inject.expression_params(0.70, DEMO_A)["breath_ratio"] == 0.3      # lose
    p = inject.expression_params(0.90, DEMO_A)                                # overload
    assert p["tier"] == "overload"
    assert p["max_sentence_len"] == 10
    assert p["speech_rate"] == 0.7
    # 表完整：四档全有参数
    for key in ("notice", "react", "lose", "overload"):
        assert key in CONFIG.TIER_EXPRESSION
