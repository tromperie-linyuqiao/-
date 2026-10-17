# 第三方组件与来源说明

本文件记录 0.35.1 已识别的第三方组件。主体采用 [AGPL-3.0-or-later](LICENSE)，第三方原许可及版权独立保留，适用声明见 [NOTICE](NOTICE)。

## pinyin-pro 3.24.2

- 用途：身体档案编辑页将中文名称转为可读的部位标识。
- 本地文件：`pages/status/pinyin-pro.min.js`。
- 上游：[zh-lx/pinyin-pro 3.24.2](https://github.com/zh-lx/pinyin-pro/tree/3.24.2)，[npm 版本信息](https://registry.npmjs.org/pinyin-pro/3.24.2)。
- 许可：MIT，版权声明为 `Copyright (c) 2022-present zh-lx`；完整原文见 [licenses/pinyin-pro-3.24.2-LICENSE.txt](licenses/pinyin-pro-3.24.2-LICENSE.txt)。
- 来源核验：本地压缩文件去除 CDN 说明头后的主体与官方 npm 3.24.2 分发文件逐字节一致；未因本轮整理升级该库。本轮在压缩脚本前附上已核验的 MIT 版权及完整许可注释，原脚本字节完整保留，使浏览器获得脚本副本时也随附声明。

保留该脚本时须同时保留其版权与许可声明。对应原许可的 SHA256 为 `824f6069c5a59b0c1650d0fb9f8d091866c6019eb761c8e658dfc03f001eabd2`。

## Three.js r160 / 0.160.0

- 用途：状态页可选的程序化 3D 专家视图。
- 加载方式：`https://unpkg.com/three@0.160.0/build/three.min.js`；本目录不包含本地打包的 Three.js 库代码。
- 上游：[mrdoob/three.js r160](https://github.com/mrdoob/three.js/tree/r160)。
- 许可：MIT，版权声明为 `Copyright © 2010-2023 three.js authors`；匹配该版本的完整原文见 [licenses/three-r160-LICENSE.txt](licenses/three-r160-LICENSE.txt)。
- 限制：3D 加载依赖外部 CDN。离线、CDN 不可达或 WebGL 不可用时，仍可使用既有 2D 编辑界面；不将 3D 成功加载列为基础安装的前提。

本轮未升级该引用。若未来将库下载并随插件分发，须继续保留其完整版权与许可声明。对应原许可的 SHA256 为 `852e0e8699169bf9f6fdc6bda3e682d078dcbc738b5d33e74df594721bff271d`。

## AstrBot 宿主

本插件使用 AstrBot 的公开插件生命周期、消息、ProviderRequest 与插件页面 bridge API。核对的宿主版本为 [AstrBot v4.26.6](https://github.com/AstrBotDevs/AstrBot/tree/v4.26.6)，其项目声明为 AGPL-3.0-or-later，见 [该版本许可证](https://github.com/AstrBotDevs/AstrBot/blob/v4.26.6/LICENSE) 和 [版本元数据](https://github.com/AstrBotDevs/AstrBot/blob/v4.26.6/pyproject.toml)。

此候选副本没有复制宿主源码或私有宿主补丁。导入宿主 API 不替代对插件主体权利与许可的确认；本文件不据此作出插件衍生作品或许可证适用范围的法律结论。

## 其他依赖与内容

- Python 运行逻辑主要使用标准库及安装环境中的 AstrBot，不内置额外服务凭据。
- `pytest` 是可选开发依赖，由包管理器安装，没有将其源码打包进本插件。本地验收使用的 9.1.1 版本发行元数据声明 `License-Expression: MIT`，源码地址为 [pytest-dev/pytest](https://github.com/pytest-dev/pytest)。此记录不锁定开发依赖版本；允许范围仍见 `requirements-dev.txt`。因未随包分发该库源码，此目录未额外打包其许可全文。
- 研究与资料链接用于说明模型来源，不代表附带文章、图片、数据集或获得相应素材的再分发许可。
- 来源授权尚未明确的天气查询器、城市和气象站数据未纳入本候选版。外部提供者可通过公开 Presence 接口发布天气观测，具体能力与限制见 README。

新增依赖、素材或复制代码前，请记录原始来源、精确版本、许可和分发范围，再更新本文件。

## 历史许可参考

[PolyForm Noncommercial 1.0.0 原文](licenses/PolyForm-Noncommercial-1.0.0.txt)只保留为此前未生效方案的审查参考，不作为本发行主体的许可或非商业附加限制。当前许可为 AGPL-3.0-or-later；分项依赖见 [DEPENDENCIES.md](DEPENDENCIES.md)，决策记录见 [LICENSE_PENDING.md](LICENSE_PENDING.md)。
