<!-- readme-header:start -->

<p align="center">
  <img src="./docs/assets/logo.svg" width="88" alt="PurrAI">
</p>

<h1 align="center">PurrAI</h1>

<p align="center">
  <strong>部署在服务器上的 Telegram 群管理机器人：入群验证、防广告、消息归档与可选 AI 聊天。</strong>
</p>

<p align="center">
  <strong>中文</strong> · <a href="./README.en.md">English</a> · <a href="./README.ja.md">日本語</a> | <a href="./BOT_GUIDE.md">文档</a> | <a href="./CONTRIBUTING.md">贡献</a> | <a href="https://github.com/CheshireMew/PurrAI/issues">反馈</a>
</p>

<p align="center">
  <a href="https://x.com/0xCheshire" title="X"><img src="https://img.shields.io/badge/X-%400xCheshire-000000?logo=x&amp;logoColor=white" alt="X：@0xCheshire"></a>
  <a href="https://t.me/CheshireBTC" title="Telegram"><img src="https://img.shields.io/badge/Telegram-CheshireBTC-26A5E4?logo=telegram&amp;logoColor=white" alt="Telegram：CheshireBTC"></a>
  <a href="https://blog.blacknico.com/" title="Blog"><img src="https://img.shields.io/badge/Blog-blog.blacknico.com-2E7D32?logo=rss&amp;logoColor=white" alt="博客：blog.blacknico.com"></a>
  <a href="https://blacknico.com/" title="Homepage"><img src="https://img.shields.io/badge/Home-blacknico.com-1F6FEB?logo=googlechrome&amp;logoColor=white" alt="个人主页：blacknico.com"></a>
</p>

<!-- readme-header:end -->

## 给自己的群配一个常驻管理员

PurrAI 部署在服务器上，为所有者批准的 Telegram 群提供入群验证、防广告、成员管理、消息归档和可选 AI 聊天。日常操作直接在机器人的私聊面板和群命令中完成；Python 服务持续运行，网页后台按需启用。

| 你要做什么 | 使用入口与结果 |
| --- | --- |
| 验证新成员 | 成员看图，从六个完整答案中点击正确的六位数字，通过后恢复发言；也可在私聊验证后批准入群申请 |
| 管理广告与刷屏 | 逐群维护昵称、正文、域名、邀请链接及引流账号规则，支持谐音和忽略干扰；查看命中原因与实际处理结果 |
| 处理群成员 | 私聊 `/admin` 选群，或在群里回复成员消息发送 `/mute`、`/ban`、`/release` |
| 查找消息与已删广告 | 在“消息归档与导出”查看收到的原文、编辑版本和处理结果，导出 JSONL |
| 让机器人参与聊天 | 配置模型 API，在“群聊天设置”开启；支持叫昵称、@、回复、主动参与及贴纸 |

[部署到服务器](#部署到服务器) · [接入第一个群](#接入第一个群) · [Bot 使用说明](BOT_GUIDE.md) · [运维与开发](docs/OPERATIONS.md)

## 部署到服务器

主部署方式是 **Linux + systemd + Python 3.12**，由单个服务进程通过 Telegram 长轮询工作。只使用 Bot 时，准备 Bot Token 即可接通 Telegram，群服务还需所有者批准并启用；网页、OIDC、HTTPS 域名和 Node.js 都是启用网页时才需要的部分。

以下按仓库的 [systemd 模板](deploy/purrai.service)使用 `/opt/purrai`、`/etc/purrai.env` 和 `/var/lib/purrai`。已有部署沿用自己的路径与账号，并同步调整模板。Windows 本地开发见[运维与开发说明](docs/OPERATIONS.md#windows-本地开发)。

### 1. 准备源码与依赖

在服务器准备 Python 3.12，将你有权限取得的源码放到 `/opt/purrai`，确保运行账号可读取源码、执行虚拟环境。在该目录安装：

```bash
cd /opt/purrai
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e . --no-deps
```

### 2. 配置机器人与数据目录

首次部署创建服务账号和配置文件；已有账号或 `/etc/purrai.env` 时跳过对应创建步骤，原地修改配置，保留已有值。

```bash
sudo useradd --system --user-group --home-dir /var/lib/purrai --shell /usr/sbin/nologin purrai
sudo install -d -o purrai -g purrai /var/lib/purrai /var/lib/purrai/backups
sudo install -o root -g purrai -m 0640 .env.example /etc/purrai.env
sudoedit /etc/purrai.env
```

在配置文件中填写 BotFather 提供的 Token，并确认数据库、备份路径与下面一致。其余参数见 [.env.example](.env.example)。

```dotenv
PURRAI_BOT_TOKEN=your_bot_token
PURRAI_DATABASE_URL=sqlite+aiosqlite:////var/lib/purrai/purrai.db
PURRAI_BACKUP_DIR=/var/lib/purrai/backups
PURRAI_SUPERADMIN_IDS=[]
```

超级管理员可稍后配置：服务运行后私聊机器人发送 `/id`，将返回的数字填入 `PURRAI_SUPERADMIN_IDS=[123456789]` 并重启服务，即可在 Bot 面板批准群接入。用户名不能代替数字 ID；也可停服后用 [CLI 批准群](docs/OPERATIONS.md#批准或撤销群接入)。

### 3. 初始化并启动常驻服务

手动 CLI 默认读取当前目录的 `.env`；systemd 使用 `/etc/purrai.env`。因此迁移时显式传入与服务相同的数据路径：

```bash
sudo -u purrai env \
  PURRAI_DATABASE_URL=sqlite+aiosqlite:////var/lib/purrai/purrai.db \
  PURRAI_BACKUP_DIR=/var/lib/purrai/backups \
  /opt/purrai/.venv/bin/purrai init-db
sudo install -m 0644 deploy/purrai.service /etc/systemd/system/purrai.service
sudo systemctl daemon-reload
sudo systemctl enable --now purrai
sudo systemctl status purrai
```

服务连接 Telegram 后，在服务器检查 `http://127.0.0.1:8765/health/ready`；返回 200 表示数据库、后台任务和机器人连接就绪。查看日志使用 `journalctl -u purrai -n 100 --no-pager`。更新配置后执行 `sudo systemctl restart purrai`。

## 接入第一个群

1. 将机器人加入测试超级群，设为管理员，授予删除消息、限制和封禁成员权限。同一个 Bot Token 保持一个长轮询实例；已有 Webhook 时先核实原用途再人工移除。
2. 所有者在私聊 `/admin` 的接入授权中批准该群，或停服后用 CLI 批准。加入群、被设为管理员及升级旧库都不会自动获得接入许可。
3. 本群群主或管理员私聊机器人发送 `/admin`，选群后点击 **“检查并启用群保护”**。配置广告规则、白名单和验证策略，先在测试群走通验证、超时、人工放行及广告处理。

能管理哪些群取决于 Telegram 当前管理员身份；群主和群管理员自动免验证及全部自动限制。超级管理员负责群接入和系统维护。完整按钮、命令与规则设置见 [Bot 使用说明](BOT_GUIDE.md)。

## 默认会怎样处理

| 场景 | 默认行为 |
| --- | --- |
| 入群验证 | 180 秒内最多答错 3 次；答错换题，截止时间不延长；选项随题保存，重启后仍可使用 |
| 验证失败 | 永久禁言，成员留在群里；可逐群改为踢出并允许重加。申请入群失败则拒绝申请，重试间隔 10 分钟 |
| 昵称或用户名违规 | 入群命中永久禁言；发言时命中还会删除当条消息 |
| 正文、链接或刷屏违规 | 默认删除消息；关键词规则可逐条选择删消息并永久禁言；关键词、域名和引流账号名单初始为空 |
| 明确广告与外群链接 | 明确交易招揽广告、确认的外群链接及外部私密邀请：删消息并永久封禁；本群和允许名单排除，举报引用单独处理 |
| 分享联系人名片 | 普通成员一律删除消息并永久封禁，不限入群时长；禁止重新加入 |
| 新成员观察期 | 默认 3 天且检查前 3 条消息；链接、@账号或群组、用户及频道转发命中删消息；钱包及代币合约地址独立对所有普通成员拦截 |
| 新成员发图门槛 | 有可靠入群记录的成员，发满 3 条不同纯文字或入群满 15 天解除；仅触发门槛时删消息，不警告、禁言或踢人 |
| 名字旁状态表情 | 默认拦截 `bygbb666`、`GsHeChaba`、`TGVIP_16`；入群命中永久封禁，群内发言命中永久禁言；各群名单可维护 |
| 管理员手动解禁 | 自动加入本群白名单，取消排队处罚，免后续验证及自动处理 |

“等待处理”表示已受理，只有 Telegram 确认后才显示“已完成”。其他管理员施加的限制不会被自动覆盖；已删除消息无法原地恢复。验证码用于增加自动化账号的进入成本，不能保证阻止 OCR 或人工代答。

## 消息归档与 AI 聊天

**消息归档默认开启**，与群保护开关独立。获准群在容量和版本上限内保存新消息、编辑版本及机器人删除前的广告原文；图片、视频只保存文件标识和元数据。私聊 `/admin` → 选群 → **“消息归档与导出”**，可查询并导出 JSONL。数据范围、容量限制和导出说明见 [MESSAGE_ARCHIVE.md](MESSAGE_ARCHIVE.md)。

**AI 聊天按群开启**。在服务器配置 `PURRAI_CHAT_API_KEY`，按需设置 `PURRAI_CHAT_API_BASE` 和 `PURRAI_CHAT_MODEL`，重启后进入“群聊天设置”调整昵称、性格和参与方式。默认模型与参数以 [.env.example](.env.example)为准；每日全局、群和个人预算用尽时暂停 AI 请求，验证和防广告继续工作。

聊天使用最近的文字与回复关系，各群、各话题分别保留上下文；开启后，相应内容会发送给配置的模型厂商。已有模型历史不会因后续编辑或删除而回写。聊天不下载媒体，不执行管理操作。更多行为见 [Bot 使用说明](BOT_GUIDE.md)，额度与存储见[运维说明](docs/OPERATIONS.md#ai-聊天与存储额度)。

## 网页与日常维护

| 需要 | 去哪里 |
| --- | --- |
| 启用可选网页 | [构建前端、配置 HTTPS 与 Telegram OIDC](docs/OPERATIONS.md#可选网页)；前端构建使用 Node.js 24 |
| 备份、升级或恢复 | [运维流程](docs/OPERATIONS.md#备份升级与恢复)；使用 SQLite 一致性备份，升级前停服迁移 |
| 检查运行、失败任务和实际状态 | [运行检查](docs/OPERATIONS.md#运行检查与任务状态)，以及 Bot / 网页“系统状态”和“操作记录” |
| 修改代码或本地调试 | [贡献说明](CONTRIBUTING.md)与 [Windows 本地开发](docs/OPERATIONS.md#windows-本地开发) |
| 核对设计与验证边界 | [实施计划](IMPLEMENTATION_PLAN.md) · [验证记录](VALIDATION.md) |

后端使用 aiogram 3、FastAPI、SQLAlchemy 与 SQLite；网页使用 Vue 3、Element Plus。源码在 `src/purrai` 与 `frontend/src`，数据库迁移在 `migrations`，服务器模板在 `deploy`。

机器人只能处理 Telegram 实际交付的更新，无法补抓入群前的历史或列出所有现有成员，也无法保证获知其他人手动删除的普通群消息。自动化测试使用模拟 Telegram / OIDC / 后台接口；服务器部署与真实群行为需在对应环境确认。

## 许可与第三方资源

仓库目前未指定项目代码许可证；第三方字体继续遵循随附许可。验证码使用 [Noto Sans](https://github.com/notofonts/latin-greek-cyrillic)，文件位于 `src/purrai/assets/NotoSans.ttf`，版权归 The Noto Project Authors，按 [SIL Open Font License 1.1](src/purrai/assets/OFL.txt)分发。
