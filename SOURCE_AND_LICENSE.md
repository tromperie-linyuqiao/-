# 来源、运行身份与许可

## 来源和修改范围

维护者确认：本项目的自研部分由其委托 GLM 编写，庄园模块完全由其委托 GLM 编写。此次独立发行沿用旧 VirtualBody 及历史合入的空间、页面、预设和模板，不把整理与修复声称为从零原创。

对 [menglimi](https://github.com/menglimi) 六个相关 Companion 公开仓库的固定版本，已比较目录、数据模型、实际调用、持久化、长实现片段和页面资源；未发现整体换壳或主要实现直接复制的证据。共同 AstrBot API、文件名和生态接口不是复制证明。具体范围、固定提交与局限见 [来源审查](docs/SOURCE_REVIEW.md)。

早期本插件围绕 [Private Companion](https://github.com/menglimi/astrbot_plugin_private_companion) 生态使用，感谢其生态和功能思路。当前公开版不包含其业务实现或私人文件读取，不要求该插件存在。

本次整理的改动包括：已验证的生命周期与并发清理、上下文重试/去重、存储和页面保存问题修复；独立配置及中性模板；隔离私有鉴权与数据耦合；接口、安装、测试与许可文档。生产运行代码以审定版本为基础，发行时新增真实仓库元数据和许可证，不替换架构。

| 内容 | 来源和处理 |
| --- | --- |
| 身体、接触、衰减、空间、材质、心绪、Presence、接口和测试 | 既有 VB 与已记录的维护者委托开发；未发现所查 Companion 实现复用阳性 |
| 页面、自有 SVG、58 预设、12 材质和13项资料引用 | 继承旧版并做有限公开来源比对；未发现资产或长正文/数据库复制阳性，不把资料链接等同授权书 |
| pinyin-pro 3.24.2 | 已识别并内置的 MIT 组件；完整版权和许可保留 |
| 可选 Three.js 0.160.0 | 固定远程引用，保留 MIT 通知，不分发其本地源码 |
| AstrBot | 外部 AGPL-3.0-or-later 宿主；未捆绑宿主源码或私有补丁 |
| 旧详细天气查询器及其数据 | 来源未确认，已隔离，不随发行 |

## 运行身份与维护账户

公开仓库和维护账户为 [tromperie-linyuqiao/-](https://github.com/tromperie-linyuqiao/-)。`name` 与目录为 `astrbot_plugin_virtual_body`；历史 `author: menglimi` 暂为现有运行身份兼容保留。AstrBot 4.26.6 按 `author/name` 生成运行 ID，所以它仍是 `menglimi/astrbot_plugin_virtual_body`。

保留字段不表示该公开账号是本次发行的唯一权利人、授权者、维护者或背书者。更正运行身份需同步验证插件页面、权限和停用配置迁移，不能当作纯显示调整；本次没有变更 ID、内部调用或 import 路径。

## 当前许可

2026-10-18，维护者明确授权按 **AGPL-3.0-or-later** 发布完整主体代码。完整 GNU 条款见 [LICENSE](LICENSE)，发行声明见 [NOTICE](NOTICE)。允许商业使用，不附加非商业限制。第三方组件及许可原文独立保留其原版权和权利，分项见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 与 [DEPENDENCIES.md](DEPENDENCIES.md)。

根 LICENSE 为 SPDX 官方固定版本收录的标准 GNU AGPLv3 完整文本，未拼接 AstrBot 或其他项目的应用声明，也未改写许可条款；“or later”适用声明由本发行的 NOTICE/README 明确给出。

此前 PolyForm 只是未生效的拟定方案。为保留审查记录，原官方参考文本仍在 `licenses/`，不作为当前主体的替代许可或附加限制。决策沿革见 [LICENSE_PENDING.md](LICENSE_PENDING.md)。
