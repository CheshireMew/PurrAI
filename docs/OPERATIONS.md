# PurrAI 运维与开发说明

[返回 README](../README.md) · [Bot 使用说明](../BOT_GUIDE.md) · [消息归档](../MESSAGE_ARCHIVE.md)

PurrAI 以 Linux 服务器上的单进程服务常驻运行，通过 Telegram 长轮询接收事件。日常管理使用 Bot；网页是可选入口。本文承载服务器维护与本地开发细节，首次部署从 [README](../README.md) 开始。

## 服务器路径与配置

仓库提供的 [systemd 模板](../deploy/purrai.service)使用以下路径。已有服务器使用其他目录或服务账号时，需同步调整模板；不要覆盖已有配置。

| 内容 | 模板位置 |
| --- | --- |
| 源码与工作目录 | `/opt/purrai` |
| Python 虚拟环境 | `/opt/purrai/.venv` |
| 服务配置 | `/etc/purrai.env` |
| 数据库与备份 | `/var/lib/purrai/purrai.db`、`/var/lib/purrai/backups` |
| 监听地址 | `127.0.0.1:8765` |
| 运行账号 | `purrai` |

systemd 从 `/etc/purrai.env` 加载环境；手动 CLI 默认读取当前目录的 `.env`。维护命令必须显式指向服务的数据库与备份目录，避免误操作默认的 `./data`。以下命令在 `/opt/purrai` 执行：

```bash
sudo -u purrai env \
  PURRAI_DATABASE_URL=sqlite+aiosqlite:////var/lib/purrai/purrai.db \
  PURRAI_BACKUP_DIR=/var/lib/purrai/backups \
  /opt/purrai/.venv/bin/purrai backup
```

需要其他服务器配置的命令还应传入对应环境变量。配置字段和默认值见 [.env.example](../.env.example)；修改服务配置后重启生效。不要启动多个进程或多个 worker 共享同一数据库。

## 批准或撤销群接入

配置超级管理员后，优先在 Bot 的 `/admin` 面板完成。没有超级管理员时，先停止服务，再使用 CLI：

```bash
sudo systemctl stop purrai
sudo -u purrai env \
  PURRAI_DATABASE_URL=sqlite+aiosqlite:////var/lib/purrai/purrai.db \
  /opt/purrai/.venv/bin/purrai groups
sudo -u purrai env \
  PURRAI_DATABASE_URL=sqlite+aiosqlite:////var/lib/purrai/purrai.db \
  /opt/purrai/.venv/bin/purrai groups --approve=-1001234567890
sudo systemctl start purrai
```

示例群 ID 需替换为列表中的真实 ID；撤销用 `--revoke`。批准后仍须由群管理员启用群保护，AI 聊天另行开启。撤销会取消待处理处罚和聊天任务、清理验证提示；已经在 Telegram 生效的禁言需人工解除。

## 备份、升级与恢复

`purrai backup` 和 Bot / 网页“系统状态”使用 SQLite 在线备份接口，生成一致性备份。不要直接复制仍在写入的数据库文件。备份默认保留，配置中的密钥不在数据库备份内，应分别保管。

升级顺序：生成备份 → 停止服务 → 更新源码及已启用的前端资源 → 按锁文件更新依赖 → 执行 `init-db` → 启动服务 → 检查健康状态与测试群。服务器迁移命令与首次部署相同，须传入相同数据库、备份目录；`init-db` 在已有版本需要升级时先自动备份。

恢复前停止服务，在上面的 `env` 命令中把 `backup` 换成 `restore /绝对路径/备份文件`。恢复会先保留当前数据库，检查备份完整性，再写回；之后运行 `init-db` 并启动。回退不兼容版本时同时恢复相应源码与备份，迁移不提供自动删表降级。

## 运行检查与任务状态

- `systemctl status purrai` 查看服务状态，`journalctl -u purrai -n 100 --no-pager` 查看最近日志。
- `/health/live` 只表示进程存活；`/health/ready` 还要求数据库兼容、后台任务正常、Bot Token 已配置且机器人已连接，否则返回 503。
- 启动会核对迁移版本、表和字段；不兼容时先停服执行迁移。缺少 OIDC 只影响网页，未构建前端不影响 Bot。
- Bot / 网页的“等待处理”只表示已受理；Telegram 确认后才显示“已完成”。失败与重试可在操作记录、系统状态查看。

执行任务先持久化意图，网络请求期间不占数据库写锁，返回后重新核对成员和任务状态。重新出题、放行、重新入群后的迟到结果不会覆盖新决定；重复事件不会重复处罚。聊天任务超过两分钟不再补发旧话题，发送结果不确定时不会自动重复发送。

群事件按群保持顺序，私聊管理按管理员保持顺序，处罚和验证按成员保持顺序；不同群和成员可并行推进。管理私聊、通知与导出使用独立的有界处理通道，慢请求不会独占验证和处罚队列。状态仍保存在数据库中，重启后继续处理待执行任务。旧记录的解除操作只作用于对应入群批次和原限制，原限制被替换时会拒绝或取消旧操作。

网页批量放行返回 `batch_id` 和原有的 `action_ids`。使用 `GET /api/actions/{batch_id}` 一次查询整批成功、等待、失败、取消的数量，无须逐条查询。状态查询暂时失败不会丢失提交回执；具体成员的结果仍可在操作记录查看。升级需先停服执行 `purrai init-db`，0016 迁移会为已有批次补齐任务关联并建立查询索引。

重新出验证码会重置错误次数和上一轮失败结果，放行及失败任务按当前题目状态分别去重。0017 迁移会恢复能够确认由旧去重标识造成的卡住任务，保留当前已有的任务及失败回执。暂停群保护后，自动处置在资料查询返回和发起处罚前检查最新开关；已经发给 Telegram 的移出仍完成后续解封。管理员操作在写入群数据前重新检查接入状态，撤销接入后的迟到请求无法重新启用群保护。

AI 聊天按群和话题选择可执行任务，某个话题积压超过 100 条请求或正在等待模型，不会遮住其他群的话题。网页验证与违规记录的时间筛选固定使用北京时间，归档详情忽略已经关闭或被新消息替代的请求结果。

网页保存群设置只提交修改字段。同一字段冲突时保留草稿并提示重新打开；API `PATCH /api/groups/{id}/settings` 可携带 `baseline`，冲突返回 409。日志保留任务、更新或聊天编号与代码位置，输出前遮去配置中的密钥。

## 可选网页

前端使用 Vue 3、Element Plus 与 Vite，构建需要 Node.js 24。在构建机器的仓库根执行：

```bash
cd frontend
npm ci
npm run build
```

将生成的 `frontend/dist` 与对应源码一起放到服务器。配置 HTTPS 正式地址、`PURRAI_COOKIE_SECURE=true`、OIDC Client ID / Client Secret 及至少 32 字符的 `PURRAI_SESSION_SECRET`，并在 BotFather 配置相应登录与回调地址。

[Nginx 示例](../deploy/nginx.conf.example)需要替换域名、证书路径，并合入现有反向代理；限流区域放在 `http` 层。应用前在服务器执行 `nginx -t`。Bot 和网页共享群授权、配置及操作记录。

## AI 聊天与存储额度

模型地址、模型名及 Key 由服务器操作员配置；群管理员只能管理本群开关、昵称、性格、上下文和贴纸。默认值由 [config.py](../src/purrai/config.py) 与 [.env.example](../.env.example)定义，具体聊天行为见 [Bot 使用说明](../BOT_GUIDE.md)。

| 每日范围 | 模型请求 | Token 预算 |
| --- | ---: | ---: |
| 所有获准群合计 | 2,500 | 5,000,000 |
| 每群（合并话题） | 1,000 | 2,000,000 |
| 每位成员（合并群） | 300 | 500,000 |

按北京时间零点重置，用量持久保存。请求前预留输入输出预算，有可靠用量时结算；沉默、失败和重试也占请求次数，没有可靠用量的失败保留预留额。任一额度不足就暂停该次 AI 请求，验证和防广告继续工作。更换模型不会自动调整额度；Token 预算不等于厂商的精确费用上限。

消息归档默认开启，与群保护、聊天上下文分别控制。数据库主文件和 WAL 合计达到 512 MB 后暂停新增归档，每条消息最多 20 个版本，旧记录继续保留；这不是整个数据库或磁盘的硬上限。原始媒体不下载。Bot 单次归档导出最多 1,000 条、20 MB，网页可导出全部匹配记录，格式与边界见 [MESSAGE_ARCHIVE.md](../MESSAGE_ARCHIVE.md)。

辅助审核记录另有保留边界：每群每成员保留最早 100 条不同的有效文字贡献，足够支持所有可配置的发图门槛，重新入群仍保留贡献。消息指纹保留最多一小时内的最近 500 条（按群、成员及入群批次），另保留当前入群批次前 100 次发言及其相册的指纹，以便旧消息编辑时沿用首次发言的位置；过期记录每 30 秒清理。0017 迁移收紧已有辅助记录并建立时间窗口和相册查询索引，不清理消息归档、违规历史或操作回执。升级前按上面的流程停服并运行 `purrai init-db`，该命令会先备份已有数据库。

## Windows 本地开发

Windows 用于本地开发和检查；生产部署使用上面的服务器入口。需要 Python 3.12；仅开发网页时需要 Node.js 24。以下使用 `D:\Tools` 作为环境与缓存示例根，在仓库根运行；已有环境可复用，其他位置需相应修改命令。

```powershell
py -3.12 -m venv 'D:\Tools\venvs\PurrAI'
$Python = 'D:\Tools\venvs\PurrAI\Scripts\python.exe'
New-Item -ItemType Directory -Force -Path 'D:\Tools\Temp' | Out-Null
$env:PIP_CACHE_DIR = 'D:\Tools\pip-cache'
$env:TEMP = 'D:\Tools\Temp'
$env:TMP = 'D:\Tools\Temp'
& $Python -m pip install -r requirements.lock
& $Python -m pip install -e . --no-deps
```

首次准备配置时将 `.env.example` 复制为 `.env`；已有 `.env` 原地修改，避免覆盖。`init-db` 创建或迁移本地数据库，`doctor` 检查配置，`run` 启动本地服务。只做本地检查可以不填写 Bot Token，此时不会连接 Telegram，也不会通过就绪检查。

```powershell
& $Python -m purrai.cli init-db
& $Python -m purrai.cli doctor
& $Python -m purrai.cli run
```

[scripts/start.ps1](../scripts/start.ps1)固定读取上面的虚拟环境路径，先迁移再启动。网页构建后本地地址为 `http://127.0.0.1:8765`；前端开发模式在 `frontend` 运行 `npm run dev`，API 转发至 8765。不要同时用生产 Bot Token 启动另一份长轮询实例。

## 开发检查

在已有开发环境安装 `pip install -e ".[dev]"`；按改动选择相关测试。后端入口如下，执行单个文件时在 `pytest` 后传入具体路径：

```powershell
& $Python -m pytest -q
& $Python -m ruff check src tests
```

前端在 `frontend` 中使用 `npm test`、`npm run build` 和 `npm run test:e2e`。浏览器检查需要已安装的兼容 Chromium；可通过 `PURRAI_BROWSER_EXECUTABLE` 指定其绝对路径，通过 `PLAYWRIGHT_BROWSERS_PATH` 指定浏览器根。缓存可用 `npm_config_cache` 放到 `D:\Tools\npm-cache`。

后端测试模拟 Telegram 和 OIDC，前端浏览器测试模拟后台接口，均不操作真实群。浏览器输出位于 `frontend/test-results` 的独立运行目录。既有验证范围与结果见 [VALIDATION.md](../VALIDATION.md)，本地测试通过不代表服务器或真实 Telegram 群已经验收。

README 的 SVG 标志由 `scripts/readme_logo.py` 从 `CatMark.vue` 与项目主色生成；用 `--write` 更新，`--check` 核对同步。
