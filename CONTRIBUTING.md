# 贡献指南

请在 [Issues](https://github.com/tromperie-linyuqiao/-/issues) 报告问题或讨论范围，再提交便于审核的修改。本项目主体采用 [AGPL-3.0-or-later](LICENSE)，第三方声明保持原许可；贡献者应确认自己有权提交相应代码、文档和素材。

## 本地开发

```bash
git clone https://github.com/tromperie-linyuqiao/-.git astrbot_plugin_virtual_body
cd astrbot_plugin_virtual_body
python -m venv .venv
```

在该虚拟环境中安装 `requirements-dev.txt`，运行 `python -m pytest -q -p no:cacheprovider` 与 `node --test tests/ui.test.cjs`。Windows/Linux 的完整命令见 [TESTING.md](TESTING.md)。目录名应为 `astrbot_plugin_virtual_body`，测试会按这一插件包名导入；CI 也显式使用该目录。

## 修改约定

- 保留现有插件 ID、公开 API、配置和数据兼容性。新增配置提供安全默认值，必要的迁移显式启用。
- 状态携带真实作用域与观测时刻；未知、暂停、离线和过期不能互相冒充。等待异步来源后重新核对实例与授权。
- Context 只在当前请求临时附加，遵守预算和重复过滤；不要把缓存背景持久塞入人格或历史，也不虚构真实 token 节省比例。
- UI 使用官方页面桥，失败保留用户编辑并给反馈；不要加入私人 Token、独立鉴权或隐式跨插件文件读取。
- 运行数据存放在插件数据目录，禁止将日志、数据库、用户账号、聊天、私服配置、密钥和环境目录提交到仓库。
- 新依赖、素材或移植代码说明精确来源、许可和分发方式，保留原版权通知，不凭“AI生成”或“找到链接”替代实际来源核对。

按变更选择相关回归，说明实际执行环境、结果及未覆盖范围。不要为制造通过而删断言、跳过关键场景或吞异常。真实手机、模型与 Dashboard 验收和受控测试分别报告；更新用户可见行为时同步教程和变更记录。
