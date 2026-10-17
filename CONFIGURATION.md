# 配置说明

适用版本：0.35.1 完整源码发行。首次安装步骤见 [INSTALL.md](INSTALL.md)；本文不表示已经完成你的平台与模型实测。

首次使用先完成 [INSTALL.md](INSTALL.md)。本页只列普通使用和排障需要的真实设置；其余模拟参数保持默认，具体类型与提示以 `_conf_schema.json` 为准。

## 配置分为三处

| 位置 | 内容 | 生效方式 |
| --- | --- | --- |
| AstrBot 的插件配置 | 开关、默认角色、加载目录、注入预算和时区 | 保存后重新加载插件，尤其是角色、目录和服务开关 |
| 运行目录中的 JSON 覆盖层 | 角色白名单、完整角色模板、空间与材质等 | 手工修改前停用插件，改后重新加载；保留备份 |
| 五个扩展页面 | 已加载角色的可编辑参数、空间、材质和规则 | 按页面相应保存按钮提交；成功后重新读取核对 |

运行目录由 `StarTools.get_data_dir("astrbot_plugin_virtual_body")` 决定，常见为 `data/plugin_data/astrbot_plugin_virtual_body/`，实际位置以宿主挂载为准。源码里的 `data/` 是内置模板，不是运行目录。

扩展页总览中的配置摘要主要用于查看，不是所有设置都能在那里编辑。现有身体页也不编辑角色 `id`、`name` 或 `whitelist`。

## 最小配置

```json
{
  "vb_enabled": true,
  "sp_enabled": true,
  "mo_enabled": true,
  "vb_default_character": "example",
  "vb_enabled_profiles": "example",
  "vb_inject_position": "user",
  "vb_llm_fallback_enabled": false,
  "presence_layer_enabled": true,
  "presence_context_enabled": true,
  "presence_context_max_chars": 420,
  "presence_repeat_seconds": 300
}
```

这是常用项示例，不是替换宿主整份配置文件的指令。`vb_enabled_profiles` 的实际默认值为空字符串，示例主动限定为 `example`。使用时仍必须在运行角色文件中填写真实授权用户 ID；上面的插件配置不包含白名单。

## 角色与授权

| 设置 | 类型 / 默认 | 用途 |
| --- | --- | --- |
| `vb_default_character` | 字符串 / `example` | 未绑定会话使用的角色 ID，须与已加载 profile 的 `id` 相同 |
| `vb_enabled_profiles` | 字符串 / `""` | 留空加载全部；或填 `example,my_role` 这样的逗号分隔 ID，不能填 JSON 数组 |
| `vb_profiles_dir` | 字符串 / `""` | 可选额外模板目录的绝对路径；一般留空，用运行目录的 `profiles/` 即可 |

角色加载优先级从高到低为：

```text
运行目录/profiles/
→ vb_profiles_dir 指定目录
→ 源码/data/profiles/
```

同 ID 文件是整份覆盖。内置 `example` 的白名单为空，表示无人获授权。最稳妥的入门方式是复制完整 `data/profiles/example.json` 到运行目录 `profiles/example.json`，只改自己的 `whitelist`。仅有肩部的 `examples/profile.example.json` 用于理解结构，不会自动补回其他部位。

### 白名单、默认角色和会话绑定

- 正常私聊的角色优先取已有 `/vbody` 绑定，无有效绑定才取默认角色，再检查该角色白名单。
- `/vbody 目标ID` 的入口先检查**默认角色白名单**，再检查**目标角色白名单**。目标授权不替代默认角色授权；未授权时通常静默返回。
- 空间功能叠加 `manor.whitelist`：为空时跟随角色白名单；非空时取交集。只改空间白名单不能绕过角色白名单。
- 这里填写平台发送者 ID 字符串。插件私聊白名单、AstrBot 管理后台登录、外部设备所有权是不同权限，不能互相替代。

自定义空间白名单时，从源码 `data/manor.json` 复制完整文件到运行目录 `manor/manor.json`，保留 `id: "manor"` 及房间结构，修改顶层 `whitelist` 后重新加载。普通入门使用可保持空间白名单为空。

## 模块和注入开关

| 键 | 类型 / 默认 | 实际作用 |
| --- | --- | --- |
| `vb_enabled` | 布尔 / `true` | 身体推进、接触处理、身体指令及身体注入；不是全部模块的总开关 |
| `vb_inject_enabled` | 布尔 / `true` | 是否向本轮 LLM 请求附加身体背景 |
| `sp_enabled` | 布尔 / `true` | 空间状态、空间指令、推进与注入 |
| `sp_inject_enabled` | 布尔 / `true` | 空间模块开启时，是否按位置门控注入空间背景 |
| `mo_enabled` | 布尔 / `true` | 心绪规则评估；不负责自己定时向用户发消息 |
| `presence_layer_enabled` | 布尔 / `true` | 统一状态接口；关闭后外部来源不能继续把该服务当作可用接口 |
| `presence_context_enabled` | 布尔 / `true` | 是否自动附加环境背景；服务开启时仍保留按需查询 |
| `presence_bridge_enabled` | 布尔 / `true` | 空间落位/离开触发身体微量联动，不是手机桥服务开关 |

只关闭 `vb_enabled` 不会自动关闭空间和心绪；只关闭 `presence_context_enabled` 不会删除身体/空间背景。需要停用整个插件时使用 AstrBot 的插件停用操作。

## 上下文与额外调用

| 键 | 类型 / 默认 | 说明 |
| --- | --- | --- |
| `vb_inject_position` | 字符串 / `user` | `user` 使用本轮临时 `TextPart`；兼容值 `system` 将身体/空间块追加到本轮系统提示尾部 |
| `vb_render_max_chars` | 整数 / `400` | 身体块预算；角色 `render.max_chars` 非空时优先 |
| `vb_show_numbers` | 布尔 / `true` | 身体背景向模型提供数值；角色 `render.show_numbers_to_model` 为布尔时优先 |
| `sp_render_max_chars` | 整数 / `160` | 空间块预算，与身体和环境块分开 |
| `presence_context_max_chars` | 整数 / `420` | 环境块预算，范围 160–1200，错误或越界回 420 |
| `presence_repeat_seconds` | 整数 / `300` | 相同重要背景最小重述间隔，范围 30–3600 秒，错误或越界回 300 |
| `vb_llm_fallback_enabled` | 布尔 / `false` | 规则未命中时可选额外模型事件抽取；默认关闭 |
| `vb_llm_fallback_daily_limit` | 整数 / `20` | 兜底抽取每天的调用上限，失败也占用已预留额度 |

这些预算单位是**字符**，不是计费 Token。它们各管一个背景块，不是整个聊天请求的总额度。默认路径不替换人格或原问题；没有有效事实或没有足够空间时可能不附加背景。

保持 `vb_inject_position=user`。宿主缺少临时内容 API 时，默认路径跳过注入，不改成把状态拼入原问题和长期历史。`system` 是显式兼容选项，不是解决所有无注入问题的通用办法。

环境层对普通背景按相关问题读取，对重要变化按变化与重述间隔处理。歌曲进度刷新不应每轮重述；切歌、暂停、失效和撤权应更新本轮事实。同一请求重试只保留自身的一份环境块。

## 时间与持续接触

| 键 | 类型 / 默认 | 说明 |
| --- | --- | --- |
| `vb_tick_seconds` | 整数 / `60` | 定时推进间隔，实际至少 15 秒；不会因此定时主动发消息 |
| `vb_tz_utc_offset` | 整数 / `-1` | `-1` 使用宿主/容器本地时区；固定 UTC+8 可设 `8`，允许偏移范围 -13～14，`-1` 保留作跟随值 |
| `sp_tz_utc_offset` | 整数 / `-1` | `-1` 跟随身体层时区；其他合法值单独设置空间时区 |
| `cs_persist_enabled` | 布尔 / `true` | 接触建立后，在规定的对话间隔内续接；明确停止语会清除相应持续接触 |
| `cs_persist_rate` | 浮点 / `0.4` | 延续率，范围 0.2–0.8 |
| `cs_persist_max_gap` | 整数 / `300` | 无对话后的过期窗，范围 60–600 秒 |

时间来自宿主，不是手机 GPS 或用户作息侦测。时区设为 `8` 也不会提供真实天气或自动生成主动陪伴消息。

## 覆盖层与保存

| 运行目录内路径 | 内容 |
| --- | --- |
| `profiles/<角色ID>.json` | 完整角色参数与白名单覆盖 |
| `profiles/<角色ID>.names.json` | 部位名称/别名独立覆盖层 |
| `state/` | 身体状态、空间状态及会话绑定 |
| `manor/manor.json` | 房间与空间结构覆盖 |
| `materials.json` | 页面保存的材质库覆盖 |
| `objects.json` | `/put`、`/find` 与空间页共用的物品账本 |
| `mood/` | 规则覆盖、冷却、触发历史及相关运行快照 |

身体页“导出 JSON”不等于备份整个插件运行目录；恢复时还要保留命名层、空间、物品、规则和宿主配置。物品账本是这份插件运行目录共用的数据，不应作为每用户私有物品库使用。

手工改白名单后重新加载；页面参数保存成功则由对应接口热载。页面中导入、弹窗确认、完成编辑和落盘保存不是同一步，应按成功提示核对。

## 不应当成新能力的兼容选项

`sp_pc_plan_enabled`、`sp_llm_fallback_enabled`、`vb_verbal_arousal_enabled` 等保留键不代表本候选附带第三方日程、空间模型抽取或语言情绪采集器。没有对应来源时，切开关不会生成真实观测，也不会让所有旧心绪规则自动消费新 provider。

`sp_migrate_legacy_manor=false` 是默认值。开启会读取旧庄园的空间/物品文件并写迁移标记，属于有副作用的一次性兼容操作；先备份，详见 [INSTALL.md](INSTALL.md)。

音乐、天气、设备等来源的采集频率、所有权验证、API Key 与传输配置归来源插件管理，不填入本插件不存在的配置项。统一接入契约见 [PRESENCE_API.md](PRESENCE_API.md)。
