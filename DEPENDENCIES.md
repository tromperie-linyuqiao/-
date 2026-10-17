# 依赖、来源与许可范围

本发行主体采用 AGPL-3.0-or-later；依赖与第三方组件保留原许可，不随之重新授权。原许可与通知见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 运行、页面及测试基础

| 组件 | 版本/范围 | 使用与分发方式 | 来源及许可 | 当前发行许可的处理 |
| --- | --- | --- | --- | --- |
| Python | 测试目标3.12 | 外部运行时及标准库，不随插件打包 | [CPython](https://github.com/python/cpython)，[PSF及附带通知](https://docs.python.org/3.12/license.html) | 保留独立许可，不给运行时加非商业限制 |
| AstrBot | 声明 `>=4.26.6,<5`，实际核验4.26.6 | 同进程导入API、继承Star、注册钩子并修改ProviderRequest；宿主不打包 | [AstrBot v4.26.6](https://github.com/AstrBotDevs/AstrBot/tree/v4.26.6)，[AGPL-3.0-or-later](https://github.com/AstrBotDevs/AstrBot/blob/v4.26.6/LICENSE) | 主体提供 AGPL-3.0-or-later 授权，不附加非商业限制；宿主独立遵守其原条款 |
| pinyin-pro | 3.24.2 | 压缩脚本随包，附完整MIT注释；原脚本字节保留 | [上游版本](https://github.com/zh-lx/pinyin-pro/tree/3.24.2)，MIT | 保留[原许可](licenses/pinyin-pro-3.24.2-LICENSE.txt)及版权；不撤销其已有权利 |
| Three.js | r160 / 0.160.0 | 可选3D视图固定CDN引用，不内置源码 | [上游r160](https://github.com/mrdoob/three.js/tree/r160)，MIT | 保留[原许可](licenses/three-r160-LICENSE.txt)；以后打包须再核对清单 |
| pytest | `>=8,<10` | 仅开发测试，包管理器安装，不随插件分发 | [pytest](https://github.com/pytest-dev/pytest)，MIT | 不给工具重新许可；实际解析版本见下表 |
| Node.js | 页面测试需20+ | 使用内置test/vm模块，不分发运行时 | [Node.js](https://github.com/nodejs/node)，项目MIT及第三方通知 | 生产普通功能无需Node，测试运行时另行许可 |

无额外Python生产依赖包，故没有虚构需要额外安装的运行依赖。AstrBot自身的完整传递依赖由宿主维护，没有随本插件打包；本插件不改变这些依赖各自的原许可。

## 已验证干净开发环境的解析快照

2026-10-17，新venv仅从 `requirements-dev.txt` 经官方PyPI安装；Python3.12.10、Node24.18.0。下列版本是该次解析快照，不将浮动版本范围冒充锁定文件。许可证信息来自所装发行包元数据及附带许可；这些包均未打入插件源码包。

| 包 | 实际版本 | 许可记录 | 上游 |
| --- | --- | --- | --- |
| pytest | 9.1.1 | MIT | [pytest-dev/pytest](https://github.com/pytest-dev/pytest) |
| iniconfig | 2.3.1 | MIT | [pytest-dev/iniconfig](https://github.com/pytest-dev/iniconfig) |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | [pypa/packaging](https://github.com/pypa/packaging) |
| pluggy | 1.7.0 | MIT | [pytest-dev/pluggy](https://github.com/pytest-dev/pluggy) |
| Pygments | 2.21.0 | BSD-2-Clause | [pygments/pygments](https://github.com/pygments/pygments) |
| colorama | 0.4.6 | 包内三条款原文核对为BSD-3-Clause；原元数据许可字段为空，不伪造字段 | [tartley/colorama](https://github.com/tartley/colorama) |
| pip | 25.0.1 | MIT；venv自带安装工具，不是插件运行依赖 | [pypa/pip](https://github.com/pypa/pip) |

MIT、BSD和Apache组件保留各自原许可，不因主体采用AGPL而被重新授权。若未来改为打包或复制其源码，须另行满足对应声明、通知等条件，不能仅引用此表。

## GitHub测试工作流

Action只用于测试，未包含发布步骤，不随插件分发Action源码。以下固定提交的上游LICENSE已独立获取核对：

| Action | 固定提交 | 原许可 |
| --- | --- | --- |
| actions/checkout | d23441a48e516b6c34aea4fa41551a30e30af803 | [MIT](https://github.com/actions/checkout/blob/d23441a48e516b6c34aea4fa41551a30e30af803/LICENSE) |
| actions/setup-python | ece7cb06caefa5fff74198d8649806c4678c61a1 | [MIT](https://github.com/actions/setup-python/blob/ece7cb06caefa5fff74198d8649806c4678c61a1/LICENSE) |
| actions/setup-node | 249970729cb0ef3589644e2896645e5dc5ba9c38 | [MIT](https://github.com/actions/setup-node/blob/249970729cb0ef3589644e2896645e5dc5ba9c38/LICENSE) |

本清单不是主体版权授权书。维护者已确认自研部分及庄园的委托 GLM 编写来源；现有素材、词表、参数及引用已复核，未发现新的第三方复用阳性。可访问的研究链接本身仍不等于素材授权。来源不明的天气代码和数据未分发。
