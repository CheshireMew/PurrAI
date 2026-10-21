# 参与 PurrAI

PurrAI 面向自用和朋友群，由服务器上的单个 Python 服务运行。修改前请先阅读 [README](README.md)、[Bot 使用说明](BOT_GUIDE.md)和相关模块；[实施计划](IMPLEMENTATION_PLAN.md)记录设计范围，[验证记录](VALIDATION.md)记录已有检查及未验证边界。

## 提交问题与修改

通过仓库的 [Issues](https://github.com/CheshireMew/PurrAI/issues)描述问题，附上触发步骤、预期结果和实际结果。日志只保留定位所需片段，遮去 Bot Token、API Key、登录密钥及真实成员信息。仓库目前为私有，反馈入口需要相应访问权限。

修改应围绕一个明确结果，说明对群管理员和成员的实际影响。新增持久化字段时提供 Alembic 迁移；群配置应同时核对 Bot 面板、网页和默认值；调整处罚、任务恢复或验证码时覆盖旧按钮、重复事件和重启后的行为。AI 聊天与群管理分别处理，不让模型回复直接执行管理动作。

模块分工与依赖方向见[架构说明](docs/ARCHITECTURE.md)。修改群配置字段或管理目录后，运行 `python scripts/sync_admin_contract.py --write` 同步 TypeScript 类型与浏览器测试目录，再用 `--check` 核对；网页运行时直接使用后端配置目录。

## 本地检查

环境准备和命令见[运维与开发说明](docs/OPERATIONS.md)。按改动运行相关后端测试、前端测试或构建，并在提交说明中列出实际命令和结果。模拟 Telegram、OIDC 或后台接口的测试只证明模拟环境行为；真实群验收请使用专门测试群。

仅修改 README 时，检查三种语言的命令与功能边界一致、相对链接可达。项目标志由网页组件生成，修改 `CatMark.vue` 或主色后运行：

```powershell
python scripts/readme_logo.py --write
python scripts/readme_logo.py --check
```

## 提交前

不要提交 `.env`、数据库、备份、归档导出、依赖目录和测试产物。说明数据库兼容性、必要的配置变化及仍未验证的部分。项目尚未指定代码许可证；贡献前请与维护者确认使用和授权安排，现有第三方资源遵循各自随附许可。
