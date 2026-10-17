# 公开代码结构与实现对比

审查日期：2026-10-18。对象为虚拟存在层 0.35.1 独立候选及下列六个公开仓库的固定提交。本页是审查结论，不分发双方业务源码，不代表相关作者对本项目背书。

## 技术判断

**当前候选不属于所查六个插件的整体换名套壳：它们的模块职责、核心状态模型及主要调用链不同。** 已执行的目录、入口、继承关系、状态/持久化及实现片段比对，没有发现直接复用其主要实现的证据。

这是一项针对所列源码版本的工程判断，不是对所有历史、未公开版本或短小通用代码的绝对原创证明。

## 固定比较版本

2026-10-18 再次核对六个 `main` HEAD，均与此前实施比对的固定版本一致。

| 公开仓库 | 固定提交 |
| --- | --- |
| [Private Companion](https://github.com/menglimi/astrbot_plugin_private_companion) | `1cdce640db3a0f50b2e1055315ba7cd83d30018b` |
| [Memory Companion](https://github.com/menglimi/astrbot_plugin_memory_companion) | `e40a83005b0a2e0817e5680c7d14398c9ab49790` |
| [Reality Companion](https://github.com/menglimi/astrbot_plugin_reality_companion) | `7bf630a5f685143739dece9ea618d6f03f392d76` |
| [Together Companion](https://github.com/menglimi/astrbot_plugin_together_companion) | `79dea555f5da9456027c95ee9f52b25a1d90e9a5` |
| [Screen Companion](https://github.com/menglimi/astrbot_plugin_screen_companion) | `dda3c07c66d63b5879855dbc3f15f264a5fb0682` |
| [Live Stream Companion](https://github.com/menglimi/astrbot_plugin_live_stream_companion) | `280fbab46bb28f3fdc894a3aca3c06aa2fa46370` |

## 核心结构差异

| 对象 | 主要组织与数据 | 与虚拟存在层的关系 |
| --- | --- | --- |
| 虚拟存在层 | `VirtualBodyPlugin` 协调身体事件、衰减、材质、空间和心绪；独立状态存储及 Presence 注册/筛选/上下文模块 | 核心是角色与会话状态演化及环境上下文 |
| Private Companion | 多组 Mixin、领域模块、主动调度与多后端存储管理 | 陪伴生态协作；候选没有其继承树、存储管理体系和完整消息调度链 |
| Memory Companion | Service、记忆记录、实体、检索及 SQLite 存储 | 记忆模型与候选身体/空间/provider 状态不同 |
| Reality Companion | 手机网关、设备鉴权/动作、提醒及设备请求状态 | 候选接收规范化状态，不包含其设备网关与动作实现 |
| Together Companion | 房间票据、WebSocket、媒体会话、服务器及隧道 | 候选不是音视频房间协作系统 |
| Screen Companion | 远程帧请求、截图/录屏、活动观察与记忆 | 候选不包含该采集与分析流程 |
| Live Stream Companion | 直播平台输入、字幕、口型、Live2D 与桥接进程 | 与候选身体/空间状态模型和处理链不同 |

共同的 `main.py`、`metadata.yaml`、`_conf_schema.json`、`Star`、初始化/卸载钩子、请求事件及 `AstrBotPluginPage` 调用，属于 [AstrBot 插件规范](https://docs.astrbot.app/dev/star/plugin-new.html)或常见工程构件。同名文件、接口协议和生态适配不能直接认定为复制实现。

## 实施过的比对与边界

- 公开实现比对覆盖七个固定源码快照、1152 个代码文件实例（含重复版本，非唯一文件数）；本轮最新 HEAD 未变化，继续复用该实现比对结果。
- 长片段以连续五行规范化代码且至少 160 字符筛查；较长 Python 函数另做 AST 同一性检查。未检出超过所设阈值的实现候选。
- 补充对照部署代码的词元窗口、归一化函数、长模板，以及 40 个页面的 375 个文件对，未检出过阈的共享实现块。部署旧版本与当前公开版本分别记录，没有混用。
- 本轮重新获取完整六仓目录元数据，并检查六个入口及 24 个关键模型/存储/运行文件的结构，未把函数名称本身当成调用证明。
- 短 helper、标准模板、复杂重写、未公开版本及全部历史不在上述结论的完整覆盖范围内；测试使用了方法正负对照，没有把所有相似结果直接删去。

候选仍沿用旧虚拟存在层与历史并入的空间代码，以及既有页面和数据模板。2026-10-18，维护者确认自研部分委托 GLM 编写，庄园模块完全由其委托编写，并要求完成审查发布。此说明已作为来源证据记录；本次模块复核未发现需据此否定分发权的第三方复用阳性，不再将缺少庄园初代源码单独列为阻断。第三方组件保留原许可，维护者随后选择的主体 AGPL-3.0-or-later 授权见根目录 [来源与许可](../SOURCE_AND_LICENSE.md)。

## 对发布的意义

可以结束“是否与这六个插件同构或整体换壳”的技术排查；当前没有证据支持该指控。维护者已确认委托开发来源并随后选择 AGPL-3.0-or-later；这两项更新见根目录来源和许可记录，无需无限重复同一份代码查重。
