# 安装验证与测试边界

本文件提供 0.35.1 的可复现检查步骤，主体许可为 [AGPL-3.0-or-later](LICENSE)。测试通过、真实宿主集成、真实账号验收和远端发布分别记录，不混为一项。

## 本地业务与页面脚本测试

先用 `git clone https://github.com/tromperie-linyuqiao/-.git astrbot_plugin_virtual_body` 克隆并进入该目录，再使用 Python 3.12 和 Node.js 20+，新建不共享系统包的虚拟环境。仅需安装 `requirements-dev.txt`；没有 npm 依赖，也不需要把 AstrBot 整个源码复制进本仓库。

```bash
python -m venv .venv
```

Linux / macOS：

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q -p no:cacheprovider
node --test tests/ui.test.cjs
```

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
node --test tests/ui.test.cjs
```

Python 测试包含模拟 AstrBot API 的测试边界，覆盖状态、规则、配置、生命周期、并发、临时上下文和异常路径。Node 测试执行页面内联脚本，但 DOM 和页面桥仍是受控替身；不能据此报告真实后台登录、浏览器排版或模型回复已通过。

## 在隔离 AstrBot 中验证

在无生产数据的 AstrBot v4.26.6 / Python 3.12 测试实例中，只安装本插件，依照 [README](README.md)设置通用角色白名单。不要复制生产 Cookie、Token、模型密钥、会话或数据库来完成测试。

| 检查 | 操作和预期 |
| --- | --- |
| 正常加载 | 启用插件；日志无初始化错误，五个扩展页存在；记录实际版本 |
| 首次使用 | 未授权私聊不修改或注入状态；授权用户可使用 `/vbody example` 和 `/vbstatus` |
| Provider | 按 [PRESENCE_API.md](PRESENCE_API.md)注册合成来源，以真实测试事件取得 scope，更新、读取并等待 TTL 过期 |
| 多来源与隔离 | 两个来源发布不同域；检查来源优先级、会话范围、注销后降级和异常来源不阻断其他来源 |
| 音乐 | 发布播放、切歌、暂停和未知观测；确认上下文更新，不把暂停当未知，也不引用过期曲目 |
| LLM 请求组装 | 在模型请求边界捕获 ProviderRequest，检查最新片段、预算、同请求重试去重，以及临时内容的历史过滤 |
| 页面保存 | 从宿主打开扩展页，保存配置后重新读取和刷新；检查失败提示、移动端、草稿与慢请求 |
| 卸载重载 | 连续初始化、卸载再加载；不残留任务、不重复注册、不删除现有配置和覆盖层 |
| 数据迁移 | 默认关闭旧庄园迁移；明确授权并备份后才测试显式迁移，检查已有目标不被覆盖 |

Provider 示例是接入合同，不是内置手机采集器。未安装外部来源时音乐、天气、设备保持未知，普通聊天应继续。

## 当前候选的已执行验证

2026-10-17 对本候选所基于的合规快照，在新建虚拟环境和新的单插件副本中执行：

- Python 3.12.10：782 项通过，0 失败、0 错误、0 跳过。
- Node.js 24.18.0：12 项通过；另对 pinyin-pro 执行实际导出和拼音转换烟雾检查。
- 单独的真实 AstrBot v4.26.6 SDK 隔离验证：54 项检查通过，覆盖真实 API、初始化、状态链、请求组装、临时内容历史过滤、页面后端请求、配置持久化及卸载重载。

SDK 使用官方 v4.26.6 源码与固定运行镜像依赖、空运行数据、禁用网络；模型和消息发送边界禁止真实调用。它验证了真实宿主 API 集成，但不是完整 Dashboard 登录流程，也没有执行付费模型或向用户发消息。

本次发行调整文档、许可证、真实 repo 元数据与 CI 的检出目录，并将 CONFIG 顶部版本说明和未使用的 CHANGELOG 说明字符串改为实际发行状态；业务实现、模拟参数、页面、数据和测试不变。相对 SDK 基准的 schema 只有两条帮助文案变化，配置键/类型/默认值不变。CI 显式检出到 `astrbot_plugin_virtual_body`，避免仓库名 `-` 让测试找不到包。发行记录分别保留打包检查、实际测试与旧 SDK 结果，不把此前 SDK 冒充又完整重跑。

## 未验证项

- 实际手机音乐采集、QQ 收发、角色自然表现与真实模型计费。
- 用户自己的 Dashboard 登录、平台适配器和全部浏览器 / Android WebView 组合。
- 声明兼容范围内每个 AstrBot 4.x 版本。
- 远端 CI 的最新结果以 [Actions](https://github.com/tromperie-linyuqiao/-/actions/workflows/tests.yml) 为准；自动测试不等于某位用户已经在自己的生产宿主安装验收。

## 报告问题

记录插件与宿主版本、入口、复现步骤、预期和实际行为、有关测试结果。日志必须隐藏密码、Token、Cookie、平台用户 ID、私人服务器地址和真实聊天内容。不要为了通过测试删除断言、跳过关键场景或吞掉异常。
