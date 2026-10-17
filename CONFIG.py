# astrbot_plugin_virtual_body 全参数集中地。
# 纪律：改参数只改这个文件；其他模块只读不写；每轮迭代在 CHANGELOG 追加一行。

VERSION = "0.35.1"   # 独立源码发行，AGPL-3.0-or-later

CHANGELOG = ["0.35.1 独立源码发行：通用核心、状态接口、管理页面及安装教程；采用 AGPL-3.0-or-later。"]

# ---- tick 与演算 ----
TICK_INTERVAL_SECONDS = 60          # 定时推进周期（秒），无消息也照跑
DECAY_TAU_DEFAULT = 900.0            # 部位缺省 τ（秒）；profile 里 part.half_life 优先于本值
GAIN_DEFAULT = 0.25                 # 事件增益系数 gain
SENS_GAIN_MIN = 0.6                 # 部位增益系数下限（数值细化：敏感部位升得更快）
SENS_GAIN_SPAN = 0.4                # 部位增益系数跨度：系数 = MIN + (sens/10) × SPAN，sens 9 → 0.96、sens 6 → 0.84
RADIATE_MAX_HOPS = 2                # 神经链传导最大跳数（耳尖→尾根→腰 = 2 跳；防环防噪声）
RADIATE_HOPS_RANGE = (1, 3)         # 0.28.1 自定义联动：profile.params.radiate_hops 可调范围（默认 2）
RADIATE_MODES = ("mul", "sum")      # 0.28.1 传导聚合模式：mul=连乘取最强路径（默认）/ sum=多路径求和
# sens 直观判定标尺（WebUI 图例与编辑器共用；增益系数 = 0.6+0.04×sens，arousal 权重 = sens/10）
SENS_SCALE = [
    (1, "麻木", "几乎无感，重刺激也只有微弱反应"),
    (2, "迟钝", "要很用力才有可察觉的变化"),
    (3, "偏钝", "普通触碰基本无感，持续刺激才累积"),
    (4, "常规", "普通人的平均水平，稳定但不出挑"),
    (5, "略敏", "轻碰就有感觉，容易慢慢升温"),
    (6, "敏感", "日常动作就能引起明显反应"),
    (7, "很敏感", "轻触即颤，躲避反应强烈"),
    (8, "极敏感", "几乎一碰就藏不住，容易过档"),
    (9, "神经密集", "本能反射区，碰一下就到注意档边缘"),
    (10, "末梢聚集", "全身神经最尖端，轻触即强烈反应"),
]

# 神经学参考数据（两点辨别觉 TPD，经典 Weinstein 系测量；用于 sens 校准与自填部位参考）
# 映射公式：sens = round(10 - 8 × log(mm/2) / log(45/2))——2mm→10、45mm→2 的对数刻度
# 来源：Weinstein 1968（经典卡尺系）；Sato 1999（舌1.7/上唇2.4/下颌5.5/掌7.5）；
# Won 2017 PMC5595116（唇~6/舌尖~5/指尖~5，力控仪器，提示卡尺值可能高估）；
# 汇总见 Physiopedia Weber 2PD 词条；指尖静态 2PD <6mm 为临床正常（Washington University Nerve Surgery）
TPD_REFERENCE = {   # 部位 → (典型 mm, 映射 sens)
    "唇": (3.2, 9), "指尖": (3.0, 9), "舌尖": (1.7, 10),
    "面颊": (5.5, 7), "手掌": (9.0, 6), "前额": (15.0, 5),
    "颈": (17.0, 5), "锁骨区": (20.0, 4), "肩": (22.0, 4),
    "腕": (20.0, 4), "膝": (25.0, 4), "踝/足背": (23.0, 4),
    "脚底": (20.0, 4), "胸": (30.0, 3), "腹": (32.0, 3),
    "上臂/前臂": (35.0, 3), "小腿": (35.0, 3), "背": (38.0, 3),
    "腰": (36.0, 3), "大腿": (42.0, 2),
}
# 皮节神经链走向（相邻脊神经节段 = 天然传导链；来源 StatPearls Dermatomes / Kenhub / Medscape，
# Keegan&Garrett 1948 与 Foerster 1933 两图系并存有个体差异）：C4 锁骨→T4 乳头→T6 剑突→T10 脐→L1 腹股沟；
# 颈 C2-C4；臂 C5-T1；S2 大腿后侧-小腿。建 radiate 建议：同链相邻节段 0.2-0.3，跨链 0.1 左右
DERMATOME_NOTES = "C4=锁骨线 T4=乳头线 T6=剑突 T10=脐 L1=腹股沟 S2=大腿后-小腿；相邻节段传导 0.2-0.3"
TOLERANCE_GAIN = 0.03               # 每次事件耐受增量 = TOLERANCE_GAIN * S
TOLERANCE_FALL_PER_MIN = 0.01       # 耐受每分钟回落量
TOLERANCE_MAX = 0.6
COOL_SECONDS_AFTER_LOSE = 20        # lose 档后部位冷却时长（秒）
COOL_GAIN_FACTOR = 0.5              # 冷却内 gain 减半
HABITUATION_STREAK = 3              # 同一部位连续快速刺激达到此次数后触发习惯化
HABITUATION_WINDOW = 60             # 习惯化计数窗口（秒）：间隔超过则重新计数；同刻事件同组不重复计数
HABITUATION_FACTOR = 0.7            # 习惯化后 gain 乘数（收益递减，防连击堆数值）
# 敏感化 wind-up（文献：频率依赖时间总和，C 纤维 wind-up；重标定到 RP 分钟尺度——
# 突触 τ_rec≈0.75s→行为层 60s 的重标定同思路）：短间隔连击累积增益放大，与习惯化对偶，
# 使"重度持续刺激→过载"路径可达（修复基线发现的三重压制下过载不可达）
SENS_WINDOW = 45                    # 敏感化累积窗口：与前次事件间隔在此内才累积
SENS_GAIN = 0.24                    # 每次窗口内事件累积量 ×S（校准：三部位 20s 重度连击 ~15 轮可达过载）
SENS_CAP = 1.0                      # 敏感化上限（增益最多 ×2.0）
SENS_TAU = 90.0                     # 敏感化衰减 τ（文献锚：中枢感知适应 90-120s，Hollins 1990）
AFTERGLOW_SECONDS = 30 * 60         # overload 后余韵时长（秒）——面板键 vb_afterglow_seconds（300-7200）的常量默认
AFTERGLOW_SENS_MULT = 1.4           # 余韵期敏感度倍数（乘在刺激 S 上）——面板键 vb_afterglow_mult（1.0-2.0）默认
COOLDOWN_SECONDS = 20 * 60          # 余韵后不应期时长（秒）——面板键 vb_cooldown_seconds（300-7200）的常量默认
COOLDOWN_SENS_MULT = 0.6            # 不应期敏感度倍数，结束后回 baseline——面板键 vb_cooldown_mult（0.2-1.0）默认
OVERLOAD_AROUSAL = 0.85             # arousal 达到该值触发 overload（与 profile tiers.overload 对齐）
ACTIVE_AROUSAL_MIN = 0.05           # 部位参与 arousal 归一的活跃下限（静息部位不稀释权重）
OVERLOAD_WARN_MARGIN = 0.10         # building 期 arousal 距 overload 阈值不足此值时注入"快到极限"预警

# ---- 事件解析 ----
ACTION_WORDS = {                    # 动作词 → 强度 S（0-1）；只认词表，不做猜测。profile 可用同名键覆盖/追加
    "碰": 0.4, "摸": 0.5, "拍": 0.5, "揉": 0.6,
    "抓": 0.7, "舔": 0.8, "掐": 0.8, "咬": 0.9,
    # 0.1.2 扩充：轻→重梯度补全
    "蹭": 0.3, "拂": 0.3, "吹": 0.25,
    "抚": 0.4, "拨": 0.4, "擦": 0.4,   # "擦"覆盖"擦过/擦拭"（单字子串命中）；档位同"抚"轻触档
    "挠": 0.5, "搓": 0.6, "亲": 0.6,
    "捏": 0.7, "拽": 0.7, "揪": 0.7,
    "吮": 0.9,
}   # "含"字因"包含/含有"高频歧义于 0.1.7 移除
TOUCH_INTENSITY_WORDS = {"轻": 0.3, "中": 0.6, "重": 1.0}   # /touch 强度词；也接受 0-1 数字
NEGATION_PREFIXES = ("别", "不要", "不准", "不许", "禁止", "莫", "勿", "不可以")   # 动作词前出现即视为拒绝，不触发事件
# 强度修饰词：出现在动作词邻域时乘算强度（查漏补缺——"狠狠揉"≠"轻轻揉"）
INTENSITY_MODIFIERS = {
    "轻轻": 0.55, "轻柔": 0.6, "慢慢": 0.75, "稍": 0.8,
    "用力": 1.3, "狠狠": 1.45, "大力": 1.3, "使劲": 1.35, "死死": 1.4,
}
EVENT_TYPE_DEFAULT = "touch"

# ---- 动作词参数化（0.29.0）----
# action_words 值支持两种形态（向后兼容）：旧 "揉": 0.6 等价 {"揉": {"s": 0.6}}；
# 新形态全键可选：{"s","method","freq_hz","speed_cm_s","depth","temp_c","cooldown_s","type"}
ACTION_WORD_TYPES = ("touch", "pain", "temp", "air", "constrain")   # type 语义类别枚举
ACTION_WORD_METHODS = ("stroke", "press", "rub", "tap", "blow")   # method 可选值（页面"自动"=不落该键，按词判定）
# 事件级参数值域（越界键丢弃、回词默认——与 _action_words 归一化约定一致）
ACTION_WORD_PARAM_LIMITS = {
    "freq_hz": (0.0, 500.0),        # Hz——揉搓 Meissner 带 5-50 / 拍打 Pacinian 带 100-300
    "speed_cm_s": (0.0, 200.0),     # cm/s——抚摸 CT 速度窗 1-10
    "depth": (0.0, 1.0),            # 压深 0-1（按压方式）
    "temp_c": (0.0, 60.0),          # °C——吹气/温度类，32°C 肤温峰
    "cooldown_s": (0.0, 3600.0),    # 词级冷却秒（0=无冷却，旧行为）
}
# type 语义类别增益微调（全部 CONFIG 可调；默认值保证旧词表——全 touch——行为逐位不变）
TYPE_PAIN_LOW_SENS_MAX = 4.0       # sens ≤ 此值视为低敏部位
TYPE_PAIN_LOW_SENS_MULT = 1.2      # 痛类对低敏部位倍率——痛觉广谱受体（nociceptor）全身分布均匀，
                                   # 钝区对触碰迟钝但对痛不迟钝（Derbyshire 2000 系综述的方向性常识）
TYPE_TEMP_THERMAL_COEFF = 0.4      # 温度类受材质热导调制：×(1+系数×thermal)——金属/冰放大温差感
TYPE_TEMP_WARM_COEFF = 0.3         # 温度类受材质保温性放大：×(1+系数×warm_hold)——保温材质让温感停留更久
TYPE_AIR_EXPOSED_MULT = 1.1        # 气流类对 flags 含 exposed 的暴露部位倍率——裸露皮肤气流感受更直接
ACTION_WORD_CD_FACTOR = 0.5        # 词级冷却内命中增益乘数（防复读同一词刷值）

# ---- 注入渲染 ----
RENDER_MAX_CHARS = 400              # 注入块字符上限（含标签）
SHOW_NUMBERS_TO_MODEL = True
RENDER_MIN_AROUSAL = 0.15           # building 期低于该兴奋度不注入（无档位可报，纯噪音）；余韵期不受此门控
RENDER_FOLD_BELOW = 0.05            # 部位数值低于该值且无档位时不渲染（token 纪律）
RENDER_COMPACT = True               # 注入用紧凑形态（一位小数锚点+精简头尾，LLM 对数编码下两位小数无增益）
TREND_EPS = 0.01                    # 趋势判定阈值：窗口增量超过此值显示 ↑/↓，否则 →（借鉴 ST 状态系统）

# ---- 时区与昼夜节律（借鉴 ST 状态系统的时段维度）----
TZ_UTC_OFFSET = None               # None=用容器本地时区；Docker 容器为 UTC 时设 8（北京时间）。昼夜节律与"今日"日切共用

# ---- 筋膜链与体感（医学依据：Myers《Anatomy Trains》肌筋膜经线；链上缺部位自动跳过）----
FASCIA_CHAINS = {
    "后侧链": ["sole", "calf", "thigh", "tail_root", "lower_back", "spine", "nape", "head_top"],   # 浅背线
    "前侧链": ["instep", "shin", "thigh", "belly", "chest", "neck_front"],                          # 浅前线
    "侧链":   ["ankle", "waist", "flank_l", "flank_r", "shoulder", "nape"],                          # 侧线
    "臂链":   ["shoulder", "arm", "elbow_l", "elbow_r", "forearm_l", "forearm_r", "wrist", "palm"],  # 臂线（前后合并）
}
FASCIA_TENSION_TAU_MULT = 1.2       # 链张力>阈值时该链部位衰减 τ 放缓（肌肉紧张让敏感消退变慢）
FASCIA_TENSION_THRESHOLD = 0.35     # 链张力阈值（链上静息部位会稀释加权均值，阈值得留灵敏度）
# 自主神经体感分级（派生自 arousal/部位状态，渲染进注入块）
AUTONOMIC_BREATH = [(0.6, "呼吸急促"), (0.35, "呼吸变浅"), (0.0, "呼吸平稳")]
AUTONOMIC_HEART = [(0.6, "心跳很快"), (0.35, "心跳偏快"), (0.0, "心跳平稳")]
TREMOR_THRESHOLD = 0.4              # 颤抖：链张力或部位 boost 超阈即轻颤

# ---- 动作可行性层（依据：AAOS 关节活动度标准 + 人体测量常识）----
SELF_REACH_HINT = "自身双手可及正面与腿前；颈后中段、背中、骶尾自手不可及（需对方或借物）"   # 注入用固定短行（措辞避开部位名，防折叠断言误触）
POSTURE_DIFF_MIN = 15               # 身高差超过该值（cm）才注入姿态提示
# phase 专属约束追加（overload 后两阶段给模型行为指向——含姿态信息，building 无追加）
PHASE_HINTS = {
    "afterglow": "；余韵未散，此刻格外敏感，四肢发软",
    "cooldown": "；不应期，刺激反应迟钝，全身乏力",
}
CIRCADIAN_ENABLED = True
CIRCADIAN_NIGHT_MULT = 1.1          # 深夜刺激敏感度倍数（乘在 s_eff 上）
CIRCADIAN_NIGHT_WINDOW = (23, 5)    # 夜间窗口（小时）：23:00-次日 05:00，跨午夜
# 面板键 vb_night_window 的默认文案（"起-止"小时，止<=起视为跨午夜）；解析在 decay.parse_night_window
VB_NIGHT_WINDOW_DEFAULT = "23-5"
TIER_LABELS = {"notice": "注意", "react": "反应", "lose": "失控", "overload": "过载"}
TYPE_STALE_SECONDS = 600            # 接触类型显示时效：超过该时长不再渲染"·舔"类标签（防过时信息误导模型）
# 约束行固定文案 + 按档位追加句长约束。措辞纪律（LLM 注入调研）：
# 全正向表述（否定式指令遵循差，DECRIM/MonitorBench）；句长用区间语义不用硬边界（字符级计数最不可靠，46 模型实测）
RENDER_CONSTRAINT_FIXED = "把反应藏进动作里；动作先于台词；状态由系统跟踪，回复只含对话与动作"
# 面板键 vb_constraint_text 的默认即上行（用户改文案后紧凑/完整两形态的固定段都换成自定义文本）
# 面板键 vb_feel_word_ttl 的常量默认沿用 MATERIAL_STALE_SECONDS（600，60-3600）

# 档位 → 表达/语音参数（第 9 节映射表）；接 TTS 时原样输出该 dict 作为参数字典
TIER_EXPRESSION = {
    "notice":   {"max_sentence_len": 30, "speech_rate": 1.0,  "breath_ratio": 0.0, "pauses": 0},
    "react":    {"max_sentence_len": 24, "speech_rate": 0.95, "breath_ratio": 0.1, "pauses": 1},
    "lose":     {"max_sentence_len": 16, "speech_rate": 0.85, "breath_ratio": 0.3, "pauses": 2},
    "overload": {"max_sentence_len": 10, "speech_rate": 0.7,  "breath_ratio": 0.5, "pauses": 3},
}
# 档位句长约束追加进注入"约束"行（区间语义，非硬边界——LLM 数值/字符级计数不可靠）
TIER_SENTENCE_HINT = {
    "notice": "", "react": "；句子短促，二十字上下",
    "lose": "；句子破碎，十字出头", "overload": "；只吐几个词",
}

# ---- LLM 兜底抽取 ----
LLM_FALLBACK_DAILY_LIMIT = 20       # 每日上限（省额度铁律：超限直接跳过，不重试）
LLM_FALLBACK_TIMEOUT_SECONDS = 12
LLM_FALLBACK_PROMPT = (
    '从消息抽取身体接触事件，只输出JSON：{"part":"部位","intensity":0-1,"type":"类型"}；无接触则输出{"none":true}'
)

# ---- 状态存储 ----
STATE_SCHEMA = 1
STATE_DIR_NAME = "state"
STATE_FILE_FMT = "{character}_{session}.json"
BINDINGS_FILE = "bindings.json"     # session→character 会话绑定表（/vbody 指令维护）

# ---- phase 敏感度倍数查表（overload 后两阶段；building 恒 1.0）----
PHASE_SENS_MULT = {"building": 1.0, "afterglow": AFTERGLOW_SENS_MULT, "cooldown": COOLDOWN_SENS_MULT}

# ---- 空间层常量（并入自 manor CONFIG，P0 前缀化防撞） ----
SP_DEFAULT_MOVE_SPEED_MPS = 1.2        # 兜底步速（米/秒）——边缺 seconds 时按几何距离折算
SP_GATE_ROOM_WORDS = ("哪", "哪里", "位置", "过来", "在哪", "在吗")   # 门控④：位置类问题词
SP_INJECT_RECENT_CHANGE_SECONDS = 600  # 门控①：位置/同室状态 10 分钟内变过
SP_MANOR_OVERRIDE_DIR = "manor"        # plugin_data 下用户覆盖层目录（manor.json 用户版）
SP_PLAN_STALE_MINUTES = 90             # PC 日程项超过该时长无更新视为失效
SP_RENDER_MAX_CHARS = 160              # 注入块上限（规范 4.3）
SP_ROOM_ID_RE = r"[a-z][a-z0-9-]*"     # 房间 id 规则（禁下划线——状态文件名前科）
SP_RP_CONFIDENCE_DIRECT = 0.9          # 显式陈述（"我在书房"）
SP_RP_CONFIDENCE_INFER = 0.6           # 上下文推断（她说"过来"后他回复）
SP_RP_CONFIDENCE_MOVE = 0.7            # 文本抽取移动的置信度
SP_RP_OVERRIDE_MINUTES = 120           # RP 覆盖有效期（分钟），过期回落日程/兜底
SP_SESSION_RE = r"[A-Za-z0-9_:\-]+"    # session 白名单（防路径穿越）
SP_STATE_DIR_NAME = "state"
SP_STATE_SCHEMA = 1
SP_TICK_SECONDS = 60                   # 定时推进周期（秒），无消息也照跑
# ---- 空间渲染文案/阈值的面板键默认（0.27.9 下放；解析与消费在 core/render.py）----
SP_ETA_THRESHOLDS_DEFAULT = "15,60"          # 距离档阈值（秒，升序两档：马上就到/还要一会儿/得走一阵）
SP_HEARING_SAME_FLOOR_DEFAULT = "隔着楼层，难闻声息"   # 异层听觉文案（键名沿用面板历史口径）
SP_HEARING_ADJACENT_DEFAULT = "隔壁，可闻动静"         # 隔壁听觉文案

# ---- L2-B 存在桥（锚点 8：空间事件→身体体感联动；强度默认轻微，绝不自动触发过载）----
PRESENCE_BRIDGE_ENABLED = True         # 桥总开关（面板键 presence_bridge_enabled 与此相与，任一 False 即关）
PRESENCE_PROXIMITY_GAIN = 0.05         # 她落位到他所在房间 → 最敏感非占位部位一条 proximity 事件（type=space）的刺激强度
PRESENCE_LEAVE_FALL = 0.03             # 她离开他所在房间 → 该部位体感直接回落量（decay 事件通道只有正向，回落由 bridge 直接施加）
PRESENCE_COOLDOWN_SECONDS = 300        # 同室反复进出不重复计（进出共用一本账）；兼作"刚落位"新鲜窗——久处同室不重计

# ---- 材质触觉层（神经触觉深化：材质×部位×接触方式×频率的医学文献量化；数字锚点与文献 URL 见 data/materials.json _refs）----
# CT 情感触觉 1-10cm/s 慢扫峰/肤温 32°C 峰（Ackerley 2014）、Meissner 5-50Hz、Pacinian 100-300Hz、
# Weinstein 2PD 部位差（sens 即其对数映射）、羊毛 ≥30µm 纤维刺痒阈值——乘数模型见 core/material.py
MATERIAL_ENABLED = True          # 材质层总开关：False 时材质识别/增益/感受词标签全部旁路（回无材质行为）
MATERIAL_MATCH_WINDOW = 10       # 文本抽取：材质词与部位词的就近配对窗口（字符）——跨句/远处的材质不算
MATERIAL_STALE_SECONDS = 600     # 注入渲染：材质感受词显示时效（秒），与 TYPE_STALE_SECONDS 同量级防过时
MATERIAL_GAIN_MIN = 0.4          # 材质乘数下限（刺痒/重压过载感的增益地板）
MATERIAL_GAIN_MAX = 1.8          # 材质乘数上限（CT 峰值慢扫的天花板）

# ---- 材质参数扩展（0.29.0）：四可选参数 + 方式级覆盖，缺省即中性值、现行为逐位不变 ----
# friction 摩擦系数 0-1：织物-皮肤摩擦系数 µ 约 0.5-0.7（Temel 2023 体区测绘的方向锚）——
# 中性值 0.5（缺省即 0.5），rub 增益 ×(0.7+friction×0.6)：0→×0.7 打滑（冰面）、0.5→×1.0 中性、1→×1.3 涩滞（橡胶）
MATERIAL_FRICTION_NEUTRAL = 0.5
MATERIAL_FRICTION_RUB_LO = 0.7
MATERIAL_FRICTION_RUB_SPAN = 0.6
# thickness 厚度 0-1：厚织物吸收高频振动（衬垫减振常识）——tap 增益 ×(1-thickness×0.4)：0→×1.0、1→×0.6
MATERIAL_THICK_TAP_DAMP = 0.4
# warm_hold 保温性 0-1：保温层让皮肤-气流温差衰减变慢（热扩散时间常数随热阻增大，传热学常识）——
# blow 舒适窗宽 ×(1+0.6×warm_hold)；temp 类事件增益 ×(1+TYPE_TEMP_WARM_COEFF×warm_hold)
MATERIAL_WARM_HOLD_WINDOW = 0.6
# pattern_mm 纹理间距 mm：>2mm 为宏观粗纹理（两点辨别阈上限量级，Weinstein 系）——
# 拍打时粗纹理冲击面产生宏观振动、与 Pacinian 频带交互强化：tap 增益 ×(1+0.25×clamp01((mm-2)/10))
MATERIAL_PATTERN_BASE_MM = 2.0
MATERIAL_PATTERN_SPAN_MM = 10.0
MATERIAL_PATTERN_TAP_COEFF = 0.25
# method_gains 材质级方式乘数覆盖（如羽毛 tap 放大、皮革 rub 抑制）：{方式: 0.4-1.8}，缺省 1.0
MATERIAL_METHOD_GAIN_RANGE = (0.4, 1.8)

# ---- 部位级细调参数（0.29.1）：parts[] 可选键，缺省中性 1.0 = 行为逐位不变 ----
# 五键接口契约（键名一字不差）：gain_mult 部位总增益倍率（event_gain 里乘）；
# tolerance_mult 该部位耐受积累倍率；windup_mult 该部位敏感化累积倍率；
# type_mults {type: 倍率} 部位×类别交互（键∈ACTION_WORD_TYPES 五类，如尾根 {"air":1.5}）；
# material_affinity {材质id: 倍率} 部位×材质亲和（如舌尖 {"silk":1.4}）。
# 演算接入 core/decay.py（坏值/越界回中性 1.0），校验 state.validate_parts + web_api 保存链路
PART_GAIN_MULT_RANGE = (0.3, 3.0)          # gain_mult 值域
PART_TOLERANCE_MULT_RANGE = (0.2, 3.0)     # tolerance_mult 值域
PART_WINDUP_MULT_RANGE = (0.3, 3.0)        # windup_mult 值域
PART_TYPE_MULT_RANGE = (0.3, 3.0)          # type_mults 值域（键须为五类 type 之一）
PART_MATERIAL_AFFINITY_RANGE = (0.4, 2.5)  # material_affinity 值域（键=材质 id）

# ---- 心绪编排常量（0.27.0 并入，数据落 plugin_data/mood/） ----
MO_TICK_SECONDS = 60
MO_TRIGGER_TTL_SECONDS = 1800
MO_RULES_FILE = "rules.json"
MO_TRIGGER_FILE = "triggers.json"
MO_LAST_STATE_FILE = "mood_last_state.json"
MO_COOLDOWNS_FILE = "mood_cooldowns.json"
MO_HISTORY_FILE = "mood_history.json"
MO_HISTORY_LIMIT = 100
MO_DATA_SUBDIR = "mood"

# ---- 天气常量（0.27.0 并入 weather_ez） ----
WEATHER_TOOL_DIR_NAME = ""  # no bundled external query executable
WEATHER_DEFAULT_CITY = ""  # no assumed user location
WEATHER_SNAPSHOT_SUBDIR = "weather"
WEATHER_SNAPSHOT_FILE = "weather_last.json"
WEATHER_SNAPSHOT_MAX_CHARS = 500
WEATHER_QUERY_TIMEOUT = 40

# ---- 心绪信号源路径（跨插件只读：洞察/书柜；天气与存在层为内部读取） ----
MO_RAIN_WORDS = ("雨", "rain", "shower", "drizzle", "thunder", "storm")
MO_WEATHER_TEXT_KEYS = ("weather", "condition", "dayText", "text")
# 0.32.0 天气 TTL：快照超过该时长未更新视为过期——read_weather 返回 available=False/is_rain=None
# （过期不当真：rain_care/rain_veranda 不触发、雨天动线不映射室内），stale=True 标注过期形态
MO_WEATHER_TTL_SECONDS = 6 * 3600
# 0.32.0 他位置兜底：he.room 未知而她已落位 → 推断他在她身边（he_room=she_room），
# 该推断的置信度（低于显式陈述 0.9/上下文推断 0.6——只是默认假设）
MO_HE_INFER_CONFIDENCE = 0.5

# ---- 语言调情→身体兴奋（0.30.0：用户核心诉求"两个人语言上调情会让身体兴奋度提高"） ----
# 信号源：PC companions.json 的 users.<QQ>.intent_profile.emotion（用户当前情绪标签），
# 命中亲密情绪集 → is_intimate=True → verbal_arousal 爬升。读法只读+mtime 缓存，绝不写 PC 目录。
VERBAL_AROUSAL_RATE = 0.006           # intimate 时每次调用的爬升量（tick 60s 一轮，~10 分钟聊满一半）
VERBAL_AROUSAL_CAP = 0.35             # verbal 单独撑起的兴奋上限（合成后 ×weight，托底不越界）
VERBAL_AROUSAL_TAU = 900.0            # 非 intimate 时指数衰减 τ（秒）——氛围散去半小时内回落
VERBAL_AROUSAL_WEIGHT = 0.8           # 合成权重：arousal = max(physical, verbal × weight)
VERBAL_AROUSAL_TAU_RANGE = (300.0, 7200.0)   # 面板/参数链值域
VERBAL_AROUSAL_RATE_RANGE = (0.0, 0.05)
VERBAL_AROUSAL_CAP_RANGE = (0.1, 0.6)
VERBAL_AROUSAL_WEIGHT_RANGE = (0.2, 1.5)
VERBAL_RENDER_MIN = 0.1               # verbal 高于此值才渲染"语言撩拨"段（token 纪律）
VERBAL_INTIMATE_EMOTIONS = ("praise", "comfort", "comfort_need",
                            "love", "intimate", "flirty", "happy", "excited",
                            "撒娇", "心动", "喜欢")   # 亲密情绪集（匹配小写化；vb_verbal_emotions 覆盖）
# 0.30.1：前三项是 PC 实际枚举的亲密子集（原 love/flirty 集与 PC 枚举交集空致语言调情恒 0）

# ---- 长期身体记忆（0.30.0 日级适应层：身体会"记住"近一周的触碰模式） ----
ADAPT_ENABLED = True                  # 总开关（面板键 vb_adaptation_enabled 与此相与）
ADAPT_WINDOW_DAYS = 7                 # 统计窗口（天）——history 流水扩容到 200 条供回溯
ADAPT_FREQ_THRESHOLD = 12             # 窗口内直接命中次数 > 此值 → 长效敏感化
ADAPT_SENSITIZE_PER_DAY = 1.01        # 高频部位 +1%/天
ADAPT_DULL_PER_DAY = 0.992            # 零触碰部位（原 sens≥5）-0.8%/天
ADAPT_MULT_RANGE = (0.8, 1.3)         # 长效系数钳制（-20% ~ +30%）
ADAPT_DULL_MIN_SENS = 5.0             # 迟钝化只作用于原本就有感觉的部位（sens≥5）
ADAPT_HISTORY_LIMIT = 200             # history 流水上限（长期记忆的数据面；旧 20 条兼容裁剪）

# ---- 情绪↔身体耦合（0.30.0 双向轻管道） ----
MOOD_COUPLE_ENABLED = True            # 总开关（面板键 vb_mood_coupling_enabled 与此相与）
MOOD_COUPLE_LOW = -0.3                # mood score 低于此 → 低落：全局增益×0.92、overload 阈值+8%（绷着）
MOOD_COUPLE_LOW_GAIN = 0.92
MOOD_COUPLE_LOW_THR = 0.08
MOOD_COUPLE_HIGH = 0.5                # 高于此 → 愉悦：全局增益×1.05
MOOD_COUPLE_HIGH_GAIN = 1.05
MO_BODY_HINT_FILE = "body_mood_hint.json"   # 身体→情绪：overload 进余韵时写 mood/ 下（read_mood 融合）
MO_BODY_HINT_TTL = 7200.0             # warm hint 有效期（2h 内线性衰减）
MO_BODY_HINT_BOOST = 0.15             # 满强度加成（余韵刚结束时最大）

# ---- 持续接触（0.31.0 体验修复一：接触一旦建立就自动延续，不需要每句都重新描述） ----
# 建立：直接事件落账 state.active_contact[pid]（core/persist_contact）；延续：双通道——
# 消息驱动（对话在场每条消息给一次 s×rate 延续增益并刷新续期锚点）+ tick 补充
# （每 tick_every 个 tick 一发、不刷锚点）；否定："别摸耳朵"经 events.extract_stops 清除；
# 过期：距最近一次对话超 max_gap 清除；overload 进余韵时 decay 清全部。
CS_PERSIST_ENABLED = True             # 总开关（面板键 cs_persist_enabled 与此相与）
CS_PERSIST_RATE = 0.4                 # 延续增益 = 原强度 × 此率（每次延续给原强度的 40%）
CS_PERSIST_RATE_RANGE = (0.2, 0.8)
CS_PERSIST_MAX_GAP = 300              # 消息间隔窗（秒）：超出即过期清除（默认 5 分钟）
CS_PERSIST_MAX_GAP_RANGE = (60, 600)
CS_PERSIST_TICK_EVERY = 3             # tick 通道频率：每 N 个 tick 叠一次延续
CS_PERSIST_MSG_MIN_GAP = 20.0         # 消息通道最小间隔（秒）：同一条消息的观察器/注入双推进只计一次增益

# ---- 亲密语境增益加成（0.31.0 体验修复二：她已经有感觉时新刺激更容易被推高） ----
# advance_state 对本轮每个事件（含持续接触延续事件）的 event_gain 结果乘 boost——
# 两条件取最大值不叠乘；无命中回 1.0（行为不变）。倍率面板键 vb_intimacy_boost_l1/l2。
# 0.32.1 死代码清理：INTIMACY_BOOST_SLEEP 同眠档移除（state 白名单封死写入，恒不触发）。
INTIMACY_BOOST_L1 = 1.3               # arousal > 0.15 或 verbal_arousal > 0.05（已经有感觉）
INTIMACY_BOOST_L2 = 1.5               # arousal > 0.35（接近 react，敏感度放大）
INTIMACY_BOOST_L1_RANGE = (1.0, 2.0)
INTIMACY_BOOST_L2_RANGE = (1.0, 2.5)
INTIMACY_BOOST_AROUSAL_L1 = 0.15      # L1 的 arousal 门槛（语义锚，不进面板）
INTIMACY_BOOST_VERBAL_MIN = 0.05      # L1 的 verbal_arousal 门槛
INTIMACY_BOOST_AROUSAL_L2 = 0.35      # L2 的 arousal 门槛

# ---- 主动触碰意图（0.30.0：对话氛围到了她也会想要） ----
PROACTIVE_TOUCH_ENABLED = True        # 总开关（面板键 vb_proactive_touch_enabled 与此相与）
PROACTIVE_TOUCH_AROUSAL = 0.45        # arousal ≥ 此值（且 building）才有主动意愿
PROACTIVE_TOUCH_VERBAL_MIN = 0.15     # verbal_arousal 须 > 此值（对话氛围在场，防纯数值驱动）
PROACTIVE_TOUCH_COOLDOWN = 4 * 3600   # 同一会话意图冷却（state 记 ts）
PROACTIVE_TOUCH_TTL = 1800.0          # 意图短语渲染时效（30 分钟内"她想…"才新鲜）
PROACTIVE_TOUCH_TEMPLATES = (         # 模板池（{part}=最活跃部位 label；渲染尾巴 ≤14 字）
    "想被他碰{part}",
    "想主动蹭他的手",
    "想让他抚摸{part}",
    "想被他多碰碰{part}",
    "盼他把手贴上{part}",
)
