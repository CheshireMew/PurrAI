<!-- readme-header:start -->

<p align="center">
  <img src="./docs/assets/logo.svg" width="88" alt="PurrAI">
</p>

<h1 align="center">PurrAI</h1>

<p align="center">
  <strong>A server-hosted Telegram bot for join verification, moderation, message archives, and optional AI chat.</strong>
</p>

<p align="center">
  <a href="./README.md">中文</a> · <strong>English</strong> · <a href="./README.ja.md">日本語</a> | <a href="./BOT_GUIDE.md">文档</a> | <a href="./CONTRIBUTING.md">贡献</a> | <a href="https://github.com/CheshireMew/PurrAI/issues">反馈</a>
</p>

<p align="center">
  <a href="https://x.com/0xCheshire" title="X"><img src="https://img.shields.io/badge/X-%400xCheshire-000000?logo=x&amp;logoColor=white" alt="X：@0xCheshire"></a>
  <a href="https://t.me/CheshireBTC" title="Telegram"><img src="https://img.shields.io/badge/Telegram-CheshireBTC-26A5E4?logo=telegram&amp;logoColor=white" alt="Telegram：CheshireBTC"></a>
  <a href="https://blog.blacknico.com/" title="Blog"><img src="https://img.shields.io/badge/Blog-blog.blacknico.com-2E7D32?logo=rss&amp;logoColor=white" alt="博客：blog.blacknico.com"></a>
  <a href="https://blacknico.com/" title="Homepage"><img src="https://img.shields.io/badge/Home-blacknico.com-1F6FEB?logo=googlechrome&amp;logoColor=white" alt="个人主页：blacknico.com"></a>
</p>

<!-- readme-header:end -->

## A resident admin for your own groups

PurrAI runs on a server and provides join verification, anti-spam rules, member management, message archives, and optional AI chat for Telegram groups approved by the owner. Manage it through the bot's private panel and group commands while the Python service stays running. The web console is optional.

| Task | Entry point and result |
| --- | --- |
| Verify new members | Match the six-digit image with one of six complete answers to regain speaking access; join requests can be verified in private before approval |
| Handle ads and flooding | Configure name, text, domain, invitation-link, and promotional-account rules per group, including pinyin and noise-tolerant matching; inspect matches and actual outcomes |
| Manage members | Send `/admin` privately, or reply to a member in the group with `/mute`, `/ban`, or `/release` |
| Find messages and deleted ads | Open the message archive to inspect received text, edits, and outcomes, and export JSONL |
| Include the bot in conversations | Configure the model API and enable group chat; use its nickname, mentions, replies, optional proactive participation, and stickers |

Deployment is described below. Detailed [Bot instructions](BOT_GUIDE.md) and [operations and development notes](docs/OPERATIONS.md) are currently in Chinese, as is the admin interface.

## Server deployment

The main deployment uses **Linux, systemd, and Python 3.12**, with a single service process receiving Telegram updates through long polling. Bot-only operation needs a Bot Token, followed by owner approval and protection activation for each group. Node.js, a web build, OIDC, and a public HTTPS domain are needed only for the optional web console.

The [systemd template](deploy/purrai.service) uses `/opt/purrai`, `/etc/purrai.env`, and `/var/lib/purrai`. For an existing installation, retain its paths and account and adjust the template consistently. Windows is a local development option, covered in [operations notes](docs/OPERATIONS.md#windows-本地开发).

### 1. Prepare source and dependencies

Install Python 3.12 on the server and place an authorized copy of the source in `/opt/purrai`. The service account must be able to read the source and execute the virtual environment. Install from that directory:

```bash
cd /opt/purrai
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e . --no-deps
```

### 2. Configure the bot and data directories

For a new installation, create the service account and configuration file below. Skip creation of an existing account or configuration file; edit existing configuration in place.

```bash
sudo useradd --system --user-group --home-dir /var/lib/purrai --shell /usr/sbin/nologin purrai
sudo install -d -o purrai -g purrai /var/lib/purrai /var/lib/purrai/backups
sudo install -o root -g purrai -m 0640 .env.example /etc/purrai.env
sudoedit /etc/purrai.env
```

Enter the Token from BotFather and use the same database and backup paths shown here. Other options are documented in [.env.example](.env.example).

```dotenv
PURRAI_BOT_TOKEN=your_bot_token
PURRAI_DATABASE_URL=sqlite+aiosqlite:////var/lib/purrai/purrai.db
PURRAI_BACKUP_DIR=/var/lib/purrai/backups
PURRAI_SUPERADMIN_IDS=[]
```

You can configure a superadmin later: send `/id` privately to the running bot, put the returned number in `PURRAI_SUPERADMIN_IDS=[123456789]`, and restart the service. This enables group approval through the bot panel. A username cannot replace the numeric ID. Alternatively, stop the service and [approve groups through the CLI](docs/OPERATIONS.md#批准或撤销群接入).

### 3. Initialize and start the service

Manual CLI commands read `.env` from the current directory by default; systemd loads `/etc/purrai.env`. Pass the service's data paths explicitly when migrating:

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

Once the bot connects to Telegram, check `http://127.0.0.1:8765/health/ready` on the server. HTTP 200 means the database, background tasks, and bot connection are ready. Use `journalctl -u purrai -n 100 --no-pager` for recent logs and `sudo systemctl restart purrai` after configuration changes.

## Connect your first group

1. Add the bot to a test supergroup as an administrator with permission to delete messages, restrict members, and ban members. Keep one long-polling instance per Bot Token; investigate any existing webhook before removing it manually.
2. Approve the group through the owner's private `/admin` panel or the CLI while the service is stopped. Adding the bot, granting admin permissions, or migrating an old database does not approve access.
3. As the group owner or an administrator, privately send `/admin`, select the group, and choose **“检查并启用群保护”** (check and enable protection). Configure rules, the whitelist, and verification; check successful verification, timeouts, manual release, and ad handling in the test group first.

Management access follows the current Telegram administrator identity. Group owners and administrators are automatically exempt from verification and all automatic restrictions. Superadmins manage group admission and system maintenance. See the [Bot guide](BOT_GUIDE.md) for all controls and commands.

## Default behavior

| Situation | Default result |
| --- | --- |
| Join verification | 180 seconds and up to 3 wrong answers; a wrong answer creates a new challenge without extending the deadline; saved choices survive restarts |
| Failed verification | Permanent mute while remaining in the group; configurable to removal with rejoining allowed. Failed join requests are declined with a 10-minute retry interval |
| Name or username violation | Permanent mute on joining; a violating message is also deleted when the member speaks |
| Text, link, or flooding violation | Deletes the message by default; individual keyword rules can also permanently mute the sender; rule lists start empty |
| Clear solicitations and external group links | Deletes and permanently bans confirmed group links, external private invites and explicit trading solicitations; excludes known own-group and allowed links, with separate report handling |
| Sharing a contact card | Deletes the message and permanently bans ordinary members regardless of membership age; rejoining is prohibited |
| New-member observation | Three days and the first three messages; links, @accounts or groups, user/channel forwards and common cryptocurrency addresses are deleted |
| New-member media gate | For members with a reliable join record: lifted after 3 distinct text-only messages or 15 days in the group; this gate alone only deletes the message |
| Profile status emoji | The `bygbb666` and `GsHeChaba` packs are blocked by default with a permanent ban; the list is editable |
| Manual unmute by an administrator | Adds the member to the group's whitelist, cancels queued penalties, and exempts future verification and automatic handling |

“Pending” means accepted, while “completed” requires Telegram confirmation. PurrAI does not automatically override restrictions imposed by other administrators. Deleted messages cannot be restored in place. The image challenge raises the cost of automated entry; it cannot guarantee resistance to OCR or human assistance.

## Message archives and AI chat

**Archiving is enabled by default**, independently of group protection. Within capacity and revision limits, approved groups retain new messages, edits, and ad text received before deletion. Media retain file identifiers and metadata only. Open `/admin` → group → **“消息归档与导出”** to search and export JSONL. See [MESSAGE_ARCHIVE.md](MESSAGE_ARCHIVE.md) for scope, limits, and format.

**AI chat is enabled per group.** Set `PURRAI_CHAT_API_KEY` on the server, adjust `PURRAI_CHAT_API_BASE` and `PURRAI_CHAT_MODEL` as needed, restart, and configure nickname, personality, and participation under group chat settings. Model defaults come from [.env.example](.env.example). Global, group, or member budget limits pause AI requests while verification and moderation continue.

Chat uses recent text and reply relationships, with separate context for each group and topic. Enabling it sends relevant content to the configured model provider. Previously sent model history is not rewritten after later edits or deletion. Chat does not download media or execute administrative actions. See the [Bot guide](BOT_GUIDE.md) and [budget and storage notes](docs/OPERATIONS.md#ai-聊天与存储额度).

## Web console and maintenance

| Need | Reference |
| --- | --- |
| Optional web console | [Build the frontend and configure HTTPS and Telegram OIDC](docs/OPERATIONS.md#可选网页); building requires Node.js 24 |
| Backup, upgrade, or restore | [Operations procedure](docs/OPERATIONS.md#备份升级与恢复); use consistent SQLite backups and stop the service for migrations |
| Runtime state and failed tasks | [Runtime checks](docs/OPERATIONS.md#运行检查与任务状态), system status, and operation records |
| Code changes or local debugging | [Contribution guide](CONTRIBUTING.md) and [Windows development](docs/OPERATIONS.md#windows-本地开发) |
| Design and validation boundaries | [Implementation plan](IMPLEMENTATION_PLAN.md) and [validation record](VALIDATION.md) |

The backend uses aiogram 3, FastAPI, SQLAlchemy, and SQLite; the console uses Vue 3 and Element Plus. Source lives in `src/purrai` and `frontend/src`, migrations in `migrations`, and server templates in `deploy`.

The bot can process only updates delivered by Telegram. It cannot retrieve pre-join history, enumerate every existing member, or reliably learn about ordinary group messages manually deleted by others. Automated tests simulate Telegram, OIDC, or backend APIs; server deployment and real group behavior require checks in those environments.

## Licensing and third-party resources

No project code license has been designated. Third-party fonts retain their own license. Verification images use [Noto Sans](https://github.com/notofonts/latin-greek-cyrillic) at `src/purrai/assets/NotoSans.ttf`, copyright The Noto Project Authors, distributed under the included [SIL Open Font License 1.1](src/purrai/assets/OFL.txt).
