# 虚拟存在层 · VirtualBody

为 AstrBot 角色维护身体、接触、庄园空间、材质和心绪状态，并将有效的环境信息整合为简短的本轮上下文。

[![Tests](https://github.com/tromperie-linyuqiao/-/actions/workflows/tests.yml/badge.svg)](https://github.com/tromperie-linyuqiao/-/actions/workflows/tests.yml)

**版本：0.35.1 · Python：3.12 · AstrBot：`>=4.26.6,<5.0.0` · 许可：[AGPL-3.0-or-later](LICENSE)**

English: A virtual body, space and presence-context layer for AstrBot. External perception providers are optional; role expressions remain controlled by the existing persona and model.

## 文档导航

- [安装与首次使用](INSTALL.md)：从零安装、白名单、首次验收和迁移。
- [配置说明](CONFIGURATION.md)：真实设置、角色覆盖层和多角色授权。
- [排障指南](TROUBLESHOOTING.md)：加载失败、无响应、保存、环境状态和回退。
- [Provider 接入 API](PRESENCE_API.md)：其他插件如何注册、更新与读取状态。
- [测试方法与覆盖范围](TESTING.md)、[变更记录](CHANGELOG.md)、[贡献指南](CONTRIBUTING.md)。

## 功能

| 模块 | 实际能力 |
| --- | --- |
| 身体与接触 | 部位参数、事件解析、阶段与衰减、持续接触和显式停止 |
| 空间 | 房间、位置、移动耗时、物品、场景词与空间上下文 |
| 材质 | 材质/接触方式参数、中文名称转换、页面编辑与运行覆盖层 |
| 心绪 | 基于可用信号的规则与模拟状态；不独立发送主动消息 |
| Presence | 来源注册、状态更新/读取、作用域、TTL、优先级、变化检测与冲突处理 |
| 上下文 | 重要变化及相关环境背景、重复过滤、字符预算、同请求重试更新和临时历史过滤 |
| 五个管理页 | 总览、身体、空间、材质、心绪，使用 AstrBot 官方页面桥 |

音乐、天气、设备和其他观测需要外部提供者。本仓库**不附手机 APK、曲库、其他陪伴插件、图片生成系统或详细天气查询器**。没有来源时保持未知，不把模拟状态当成真实用户活动；`weather_detail` 兼容入口会明确返回未配置。

## 快速开始

1. 在 AstrBot 插件页选择 **安装插件 → 从链接安装**，填写：

   ```text
   https://github.com/tromperie-linyuqiao/-.git
   ```

   也可从 [v0.35.1 源码 ZIP](https://github.com/tromperie-linyuqiao/-/archive/refs/tags/v0.35.1.zip) 下载后通过文件安装。仓库名 `-` 不影响 AstrBot 4.26.6 按元数据安装成 `astrbot_plugin_virtual_body`。

2. 插件初次加载后，停用插件，将源码内完整的 `data/profiles/example.json` 复制到运行目录的 `profiles/example.json`。默认运行目录为 `data/plugin_data/astrbot_plugin_virtual_body/`，实际位置由宿主数据挂载决定。
3. 只修改运行角色文件中的 `whitelist`，填入需要授权的平台用户 ID 字符串。默认角色保持 `example`，保存并重新加载插件。
4. 在已授权用户的私聊中依次发送 `/vbody example`、`/vbstatus`、`/map`，再从插件详情打开管理页面。

**空角色白名单表示无人获授权。** 不改白名单时没有聊天效果是预期行为。不要直接修改仓库内模板来保存私人账号。`examples/profile.example.json` 只是肩部最小示例；需要完整身体时按上面步骤复制完整模板。

多角色的 `/vbody 目标ID` 先检查**配置默认角色**白名单，再检查目标角色白名单；空间还会叠加 `manor.whitelist`（空表跟随角色，非空取交集）。完整步骤见 [安装教程](INSTALL.md)。

普通使用没有额外 Python 运行依赖，也不需要 Node.js。自带 `data/` 是必要模板，应保留；运行数据、凭据和会话不属于源码发行内容。

## 指令和管理页

以下使用默认 `/` 指令前缀，以授权私聊为前提：

| 指令 | 用途 |
| --- | --- |
| `/vbody`、`/vbody example` | 查看或切换角色 |
| `/vbstatus` | 查看身体状态 |
| `/touch 肩膀 轻` | 对已配置部位施加显式事件 |
| `/map` | 查看房间 |
| `/where`、`/where 工作区` | 查看或设置自己的位置 |
| `/hermove 工作区` | 移动角色 |
| `/put 笔记本 工作区`、`/find 笔记本` | 记录或查询物品 |
| `/spacestatus` | 查看空间诊断 |

在 AstrBot 插件详情中选择“总览 / 身体 / 空间 / 材质 / 心绪”。不要直接打开 HTML 文件；页面需要 `AstrBotPluginPage`。身体页编辑部位与参数，不用于修改角色 ID、白名单或新建未加载角色。配置保存、页面保存、手工角色文件的区别见 [配置说明](CONFIGURATION.md)。

## 架构与外部接入

```text
私聊事件 → 身体 / 接触 / 空间 / 心绪 → 自有状态存储
                                              │
感知 provider → context.presence_layer → 授权 / TTL / 变化筛选
                                              │
                       临时 Context → 原有 AstrBot LLM 请求
```

插件增加上下文，不替换现有人格。环境块默认最多 420 **字符**，不是精确 token 上限；身体、空间和环境各有原有门控，环境块不重复身体与空间。可选模型事件抽取默认关闭，开启后可能产生额外模型调用。

其他插件通过公开 API v1 注册来源、提交带观测时间的 JSON 状态，并以真实事件取得授权 scope；不需要导入本插件内部实现或读取其他插件文件。完整合同及示例见 [PRESENCE_API.md](PRESENCE_API.md)。注册一个新域不会自动驱动所有旧心绪规则。

## 验证和已知限制

- 业务及受控宿主测试：782 项 Python、12 项 Node 通过；官方 AstrBot 4.26.6 隔离 SDK：54 项检查通过。
- 五页已做真实 Chromium 桌面/手机布局和关键交互验证，页面桥/业务数据使用受控替身。
- 实际集成版本仅 4.26.6；声明的兼容范围不等于每个版本都已验证。
- 手机音乐、真实平台收发、付费模型、真实角色表现和用户自己的 Dashboard 登录不在上述验收范围。
- 可选 Three.js 3D 依赖 CDN/WebGL，完整 3D 兼容未实测；基础 2D 不以它为前提。
- 没有真实计费比较，不保证特定 token 节省比例。

远端 CI 的实时结果见上方 Tests；本地及官方 SDK 的执行环境和边界见 [TESTING.md](TESTING.md)。源码及模块审查见 [来源对比](docs/SOURCE_REVIEW.md)、[模块审查](docs/MODULE_REVIEW.md)和[发行状态](docs/RELEASE_STATUS.md)。

## 更新与迁移

更新前备份插件配置和运行数据，不用通用模板覆盖自己的角色。旧庄园数据迁移默认关闭；确需迁移时显式开启 `sp_migrate_legacy_manor`，先备份并核对权限。它只复制未存在的目标并写迁移标记，不删除旧文件或覆盖已有目标；详见安装教程。

插件目录与 `name` 为 `astrbot_plugin_virtual_body`，历史 `author: menglimi` 因现有运行身份兼容保留，运行 ID 仍是 `menglimi/astrbot_plugin_virtual_body`。这不是上游账号对本发行版的背书。维护账户为 [tromperie-linyuqiao](https://github.com/tromperie-linyuqiao)，来源与身份说明见 [SOURCE_AND_LICENSE.md](SOURCE_AND_LICENSE.md)。

## 来源、许可与反馈

主体源码、测试与自有文档采用 **GNU Affero General Public License v3.0 or later（AGPL-3.0-or-later）**，完整条款见 [LICENSE](LICENSE)。这允许商业使用，分发与适用的远程网络交互须履行相应源码和通知义务；没有额外非商业限制。第三方组件及许可文本保留各自权利，见 [NOTICE](NOTICE) 和 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

本插件沿用既有 VirtualBody 与维护者委托 GLM 开发的空间模块。早期围绕 [Private Companion](https://github.com/menglimi/astrbot_plugin_private_companion) 陪伴生态使用，感谢其生态和功能思路；在审查的六个 Companion 固定版本中未发现整体换壳或主要实现直接复用。

问题反馈请使用 [Issues](https://github.com/tromperie-linyuqiao/-/issues)，附版本、部署方式、复现步骤与脱敏日志。贡献前阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。本仓库公开发布不等于已经提交或获 AstrBot 插件市场收录。
