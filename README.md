# PurrAI

PurrAI 是自用的 Telegram 多群管理机器人，提供入群图片验证和基础防广告。**主要通过 Telegram Bot 的私聊面板和群内命令管理**，网页后台保留为可选入口。机器人采用 aiogram 3，服务端采用 FastAPI，网页采用 Vue 3 和 Element Plus。

## 已实现的功能

- 新成员入群后禁言，通过图片下方数字键盘输入六位验证码；支持超时、重试、退群重进及管理员处理。
- 数字可连续输入，无需等待圆点逐位刷新；按键先保存，再合并刷新。进度刷新或按钮回应请求缓慢时不阻塞后续数字与提交。
- 昵称、用户名与正文分别检查；支持普通关键词、去除表情和分隔符、拼音谐音，以及昵称＋正文组合规则。
- 域名、群邀请链接、@引流账号、新成员前几条消息、近似刷屏和连续发送检测；支持编辑消息、媒体说明和隐藏链接。
- 入群及每次发消息检查最新昵称和用户名，命中则删除当条消息并永久禁言；正文单独违规仍只删消息。管理员手动解禁后自动加入本群白名单，取消排队处罚并免后续自动处理。
- 本群未发够 3 条不同的纯文字消息前，图片、贴纸、动图及纯表情整条删除，说明文字一并删除；仅命中文字门槛时不警告、不禁言、不踢人；同时存在昵称违规按永久禁言规则处理。独立保存文字资格，支持逐群修改门槛或关闭。
- 名字旁状态表情包黑名单：默认加入 `bygbb666` 和 `GsHeChaba`，按官方包标识查询，命中永久禁言；Bot 与网页只发送新链接即可追加、自动去重，支持单项移除和独立清空。
- 可选申请入群验证：在私聊完成验证码后批准入群；可生成需要审核的入群链接，直进入群仍有群内验证兜底。
- 私聊机器人发送 `/admin` 即可选群、检查并启用保护、配置验证和防广告、维护规则与白名单、查看权限说明、处置成员及查看结果。
- 群里可回复成员消息发送 `/mute`、`/ban`、`/release` 等命令；实际执行后由机器人回执，重复提交不会重复处罚。
- 自动识别 Telegram 群主与群管理员，只能管理本群；查询和修改均核实当前身份；可选网页使用 Telegram OIDC 登录。
- 当前群主和管理员免验证及全部自动限制，昵称、状态表情、文字或媒体违规均不自动处理；执行排队处罚前重查身份，提升后取消旧任务，撤销管理员后恢复普通规则。
- 群配置、验证记录、规则预览、白名单、违规记录、失败任务重试和一致性备份。
- SQLite 持久化收件、任务去重、断线重试和重启恢复。只允许一个服务进程持有同一数据库。
- 可选大模型群聊天：填写厂商的 OpenAI 兼容 API，逐群设置昵称、性格和主动参与；保留最近多人对话及回复关系，同群不同话题分开，聊天请求独立处理。

Bot 的配置和使用步骤见 [BOT_GUIDE.md](BOT_GUIDE.md)。完整设计、默认规则和验收范围见 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)，测试情况见 [VALIDATION.md](VALIDATION.md)。

## Windows 启动

运行服务需要 Python 3.12；构建可选网页需要 Node.js 24。当前机器使用 `D:\Tools\Python312` 与 `D:\Tools\NodeJS`。

```powershell
Set-Location -LiteralPath '<项目或验证目录>
& 'D:\Tools\Python312\python.exe' -m venv 'D:\Tools\venvs\PurrAI'
$env:PIP_CACHE_DIR = 'D:\Tools\pip-cache'
$env:TEMP = 'D:\Tools\Temp'
$env:TMP = 'D:\Tools\Temp'
& 'D:\Tools\venvs\PurrAI\Scripts\python.exe' -m pip install -r requirements.lock
& 'D:\Tools\venvs\PurrAI\Scripts\python.exe' -m pip install -e . --no-deps
Copy-Item -LiteralPath '.env.example' -Destination '.env'
```

已存在的 `.env` 应原地编辑，不覆盖其中的运行配置。只用 Telegram 管理时，填写 Bot Token 后即可启动；还需要由所有者明确批准要服务的群。不需要 OIDC、HTTPS 域名或前端构建：

```powershell
& 'D:\Tools\venvs\PurrAI\Scripts\python.exe' -m purrai.cli init-db
& 'D:\Tools\venvs\PurrAI\Scripts\python.exe' -m purrai.cli doctor
& 'D:\Tools\venvs\PurrAI\Scripts\python.exe' -m purrai.cli run
```

服务运行后，群主或群管理员私聊机器人发送 `/start` 或 `/admin`。发送 `/id` 即可查看自己的数字用户 ID，无需管理权限。服务器仍须保持服务运行，机器人才能执行。需要使用网页时，再构建前端并配置 Telegram OIDC：

```powershell
Set-Location -LiteralPath '<项目或验证目录>
$env:npm_config_cache = 'D:\Tools\npm-cache'
& 'D:\Tools\NodeJS\npm.cmd' ci
& 'D:\Tools\NodeJS\npm.cmd' run build
Set-Location -LiteralPath '<项目或验证目录>
& 'D:\Tools\venvs\PurrAI\Scripts\python.exe' -m purrai.cli init-db
& 'D:\Tools\venvs\PurrAI\Scripts\python.exe' -m purrai.cli doctor
& 'D:\Tools\venvs\PurrAI\Scripts\python.exe' -m purrai.cli run
```

访问 `http://127.0.0.1:8765`。也可以运行 `scripts/start.ps1`，它会先执行迁移，再启动服务。前端本地开发用 `npm run dev`，API 转发到 8765。

执行 `purrai init-db` 后，未配置 Bot Token 时服务可以启动并展示已构建的登录页面，但不会连接 Telegram。启动会核对当前迁移版本、必需表和字段；结构不兼容时拒绝启动并提示先迁移。OIDC 未配置只影响网页登录，Bot 管理仍可用；不提供假登录。`/health/live` 表示进程存活；`/health/ready` 还要求数据库兼容、后台任务存活、Bot Token 已配置且机器人已连接，否则返回 503，并分别提供 `database_ready`、`runtime_ready` 等状态。

## 实际接入 Telegram

1. 在 BotFather 创建机器人，设置 `PURRAI_BOT_TOKEN`。
2. 超级管理员可暂不配置。服务启动后私聊 Bot 发送 `/id`，它会回复你的数字 ID；需要全局维护时再设置 `PURRAI_SUPERADMIN_IDS=[该数字]` 并重启。不要填写 `@用户名`，支持多个超级管理员。
3. 将机器人加入测试超级群，授予删除消息、限制和封禁成员权限。保持长轮询模式；如果此 Bot 已配置 Webhook，程序会提示，须先核实原用途再人工移除。
4. 所有者批准该群接入：配置超级管理员后在 `/admin` 面板批准，或者停服后使用下面的本地 CLI。加入群和迁移旧库均不会自动获得授权。
5. 获准群的群主或管理员私聊机器人发送 `/admin`，选群后点击“检查并启用群保护”，维护配置和规则；本群其他管理员自动获得管理权限。用测试账号核实验证和防广告行为后再日常使用。

需要网页时再为其准备 HTTPS 域名，配置 `PURRAI_PUBLIC_URL`、`PURRAI_COOKIE_SECURE=true`，在 BotFather 设置允许登录和回调地址、OIDC Client ID / Client Secret，以及至少 32 字符的 `PURRAI_SESSION_SECRET`。网页登录与 Bot 面板使用相同的授权和操作记录。

官方参考：[Telegram 登录](https://core.telegram.org/bots/telegram-login)、[Bot API](https://core.telegram.org/bots/api)、[aiogram](https://docs.aiogram.dev/)。

## 自用和朋友群的接入与额度

只有所有者明确批准的群才能使用验证、防广告、消息归档及 AI 聊天。陌生群把 Bot 拉进去或自行授予管理员，都不能启用服务或使用模型 Key；程序只保留最多 50 个待审批群的名称和 ID，不保存这些群的消息。升级前已有的群也需要批准。批准和撤销可在所有者的 Bot 面板或网页操作；普通群管理员只能管理获准的本群，不能给其他群开通服务。

没有配置超级管理员时，可以在服务停止后通过本地 CLI 操作，不需要 Telegram 网络连接：

```powershell
& 'D:\Tools\venvs\PurrAI\Scripts\python.exe' -m purrai.cli groups
& 'D:\Tools\venvs\PurrAI\Scripts\python.exe' -m purrai.cli groups --approve=-1001234567890
# 撤销同一个群：把 --approve 换成 --revoke。
```

撤销会停止新增处理、取消待处理处罚和聊天任务、清理验证码提示；已经在 Telegram 生效的禁言不会被自动解除。批准只授予接入权限，首次仍须由群管理员启用群保护，AI 聊天另有开关。

AI 聊天采用保守的每日预算，同时检查以下三层限制，任何一层不足就暂停本次 AI 请求，验证和防广告继续运行：

| 范围 | 每日模型请求上限 | 每日 Token 预算 |
|---|---:|---:|
| 全部获准群合计 | 500 | 1,000,000 |
| 每个群（合并全部话题） | 200 | 400,000 |
| 每位成员（合并全部群） | 60 | 100,000 |

按北京时间零点重置，用量写入数据库，重启不会清空。请求前先预留本次输入与输出预算，厂商返回有效用量后按实际值结算；选择沉默的调用、失败重试也占请求次数，没有可靠用量的失败或中断保留预留额。长上下文可能提前耗尽 Token 预算。单人实际模型调用至少相隔 5 秒，同群至少 2 秒，输出默认最多 256 tokens，厂商附加参数不能绕过服务端输出上限。Bot 群聊天面板和网页群设置显示本群用量，系统状态显示全局用量。

这些值由所有者在服务器环境配置中调整并重启，群管理员不能提高限额，配置名称见 `.env.example`。Token 预算是本地资源控制，不等于精确的人民币费用上限；如果厂商支持，给此项目单独使用一个 Key 并在厂商后台设置消费上限，作为最后一道费用控制。当前不引入付费账号或计费系统。

私聊控制、网页请求及匿名登录都有进程内限流；登录发现文档有缓存，未完成登录记录有数量上限。默认仅监听本机 `127.0.0.1`，不需要网页时无需开放公网入口。需要网页时，部署模板另外提供 Nginx 请求、连接和请求体限制，修改后应在实际服务器执行 `nginx -t` 再应用。

为避免消息归档占满存储，主 SQLite 文件和 WAL 合计达到 512 MB 后停止新增归档，每条消息最多保存 20 个版本；旧数据保留，可在系统状态查看暂停原因。这是归档停止阈值，其他业务记录仍可能使数据库增长，不是整个服务器的磁盘配额。相关阈值也由所有者配置。

## 默认规则

验证码默认 180 秒有效，最多答错 3 次。直接入群后的失败处理可逐群选择：**永久禁言（默认，留在群里）**或**踢出群聊（允许重新加入）**。永久禁言没有到期时间，可由管理员人工放行或重新出题；不会在 600 秒后自动解除。申请模式的成员尚未进群，失败只拒绝申请，并限制 10 分钟内再次申请，可由管理员提前解除间隔。

昵称或用户名命中规则时，发言删除当条消息并**永久禁言**，入群命中直接永久禁言；正文、链接、联系人或刷屏单独命中仍只删除消息。管理员通过 Bot、网页或 Telegram 群设置手动解除禁言后，该用户自动加入本群白名单，免入群验证及全部自动处罚。关键词、域名及引流账号名单初始为空，不内置特定行业的广告词表。

新成员观察期为验证通过后 10 分钟，且前 3 条消息也继续检查，需两个条件都结束才退出观察。默认限制链接、联系人及未在允许名单的 @提及。相同内容 30 秒内 5 条、近似内容 60 秒内 4 条、任意消息 10 秒内 8 条会触发删除。近似程度默认 85%，最短 12 字；媒体相册按一组计算连续发送，编辑和重复交付不增加条数。

昵称规则支持包含、完整、忽略表情及分隔符、拼音谐音匹配。规则只对管理员配置的词组生效，不做通用语义识别；头像、图片中文字和二维码不在本次范围。设置方法见 BOT_GUIDE。

验证码仅用于提高自动化广告账号的进入成本，不保证无法被 OCR 或人工代答突破。入群后禁言也存在 Telegram 通知到达前的抢发窗口；系统清理已收到的待验证成员消息，后续广告继续交给规则检测。

Bot 与网页显示“等待处理”代表操作已受理，只有 Telegram 调用确认成功后才显示“已完成”。其他管理员另行施加的限制不会被 PurrAI 自动覆盖。已删除的群消息无法原地恢复。

## 测试

```powershell
Set-Location -LiteralPath '<项目或验证目录>
$env:TEMP = 'D:\Tools\Temp'
$env:TMP = 'D:\Tools\Temp'
& 'D:\Tools\venvs\PurrAI\Scripts\python.exe' -m pytest -q
& 'D:\Tools\venvs\PurrAI\Scripts\ruff.exe' check src tests
Set-Location -LiteralPath '<项目或验证目录>
& 'D:\Tools\NodeJS\npm.cmd' test
& 'D:\Tools\NodeJS\npm.cmd' run build
$env:PLAYWRIGHT_BROWSERS_PATH = 'D:\Tools\playwright-browsers'
# 当前机器复用已经安装的 Chromium；其他机器配置相应浏览器路径。
$env:PURRAI_BROWSER_EXECUTABLE = 'D:\Tools\playwright-browsers\chromium-1228\chrome-win64\chrome.exe'
& 'D:\Tools\NodeJS\npm.cmd' run test:e2e
```

后端模拟 Telegram 和 OIDC；浏览器测试模拟后台接口。两者都不操作真实群。浏览器测试每次使用独立输出目录，截图位于前端 `test-results`，不作为产品默认数据。

## 数据备份与恢复

Bot 面板或网页的“系统状态”，以及 `purrai backup`，均使用 SQLite 在线备份接口生成一致性备份，不直接复制活跃数据库文件。

恢复时先停止服务，执行 `purrai restore 备份文件完整路径`。恢复命令取得与服务相同的进程锁，先保留当前数据库备份，再检查并恢复指定备份，最后执行 `purrai init-db` 并启动。不会自动删除旧备份。

后台会话、验证进度和任务均在数据库中。备份应与服务器配置分开保管；Bot Token 与 OIDC 密钥仅在环境配置中，不进入版本控制。

普通执行任务先保存意图，再释放数据库写锁等待 Telegram，返回后核对原任务和成员状态。管理员重新出题、放行或成员重新入群后，迟到结果不会覆盖新决定；迟到的旧验证图片会按消息 ID 排队清理。手动禁言和封禁先保存归属及期限，响应丢失或进程中断后可核对实际限制，避免重复处罚；已到期的限制不会在重试时重新施加。验证码按键只等待同群同成员的相关成员事件及本群机器人权限事件，不会因其他群或成员的失败事件停住。

网页保存群设置时只提交实际修改的字段，不覆盖 Bot 同时修改的其他字段。同一字段发生冲突时明确提示重新打开设置，保留当前编辑草稿。API 的 `PATCH /api/groups/{id}/settings` 接受局部 `settings`，可以同时提供这些字段打开时的 `baseline` 值进行冲突校验，冲突返回 409。后台错误记录及日志会保留原因、代码位置和任务、更新或聊天编号；配置中的密钥在记录前遮去，方便按同一编号定位问题。

## 大模型群聊天（可选）

默认接入 DeepSeek 的 `deepseek-flash`，地址为 `https://api.deepseek.com`，使用非思考模式和 JSON 输出，单次输出上限 256 tokens。只需在已有 `.env` 的 `PURRAI_CHAT_API_KEY` 填入自己的 DeepSeek Key；没有 Key 时不会调用模型。选择依据与参数见 [DeepSeek 模型和价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)及[思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)。实际费用取决于输入、输出 token 用量，输出上限并非每次固定消耗。

需要换厂商时修改 `PURRAI_CHAT_API_BASE` 和 `PURRAI_CHAT_MODEL`。地址按厂商文档填写，支持带版本路径的 Base URL 或完整 `/chat/completions` 地址。通过项目现有 HTTPX 调用，不增加模型 SDK 或服务。仅对 DeepSeek 官方接口自动补入非思考和 JSON 参数，不向其他厂商发送这些专用默认值；`PURRAI_CHAT_API_EXTRA` 可显式覆盖厂商参数，但不能提高服务端输出上限。群聊上下文条数和接话间隔保持原有设置。

更新后先停止旧服务，运行 `purrai init-db` 升级数据库，再启动服务。升级前会自动备份。私聊 Bot 发送 `/admin` → 选群 → **群聊天设置**，开启聊天并设置昵称、性格、主动参与和上下文条数。网页“群设置”提供相同选项。API 未配置时验证、防广告和管理照常工作；不会调用模型。

群友可以 @机器人、叫昵称或回复它；刚聊过的同一位群友在 90 秒内可以继续对话，但回复其他成员不会自动视作续聊。机器人先判断这句话在对谁说、是否与自己有关、是否需要补充，再决定是否回复；无关闲聊和简单附和默认旁听。判断和生成使用同一次模型请求。连续约 1.5 秒内的消息合并处理，同话题发言至少间隔 8 秒；主动检查默认最多每群 90 秒一次，不同话题共用冷却，刚说过话也不会立即在别的话题插嘴。关闭主动参与后仍可直接呼叫和自然续聊。文字与表情分开发送：先发纯文字，再单独发匹配贴纸，或只发其中一种；贴纸不可用时不会把 emoji 塞回正文，限流重试不重复发送已成功的文字。

默认使用本群、本话题最近 40 条、6 小时内的文字上下文；最多保存最近 80 条上下文及刚发出的回复，包含昵称、发送者 ID、时间、引用和机器人自己的发言。聊天上下文与完整消息归档各自开关；归档关闭时仍可聊天。“重新开始聊天上下文”会让后续聊天忽略旧上下文，归档继续保留。只理解文字和媒体说明，不下载媒体，也没有长时记忆或知识库。

新消息在第一次发给模型前，会根据当前记录筛选，排除已识别的广告、待删除或已删除的消息、待验证成员、命令、入退群和置顶通知，以及其他机器人或机器人转发内容。只接纳 PurrAI 已成功发送的正常聊天回复，验证码与管理通知不占聊天上下文。新增引用只从同群同话题、干净的已记录消息读取，原文已经在上下文中时不重复发送；不直接使用收到的嵌套引用原文或旧引用缓存。过滤先于最近消息条数限制，脏消息不挤占正常聊天的名额，也不增加模型调用。

已经发给模型的内容保存当时的版本，后续编辑、删除或判为广告不会再改写这段历史；新内容按顺序追加。系统提示词保持固定，本轮回复对象与唤醒方式放在请求末尾，尽量复用 DeepSeek 缓存前缀；重启也保留这份历史。仅在上下文达到条数/长度上限时按批次缩短、会话满 6 小时或管理员重新开始时轮换历史，不为删除旧消息反复生成回复。实际缓存命中由厂商决定，规则见 [DeepSeek 上下文缓存](https://api-docs.deepseek.com/guides/kv_cache/)。

PurrAI 自己处理或已有记录的删除，可以在首次发送前排除；Telegram 普通群聊没有通用的消息删除更新，因此其他管理员或群友手动删除且未通知机器人的消息，无法保证自动识别。依据见 [Telegram Update 类型](https://core.telegram.org/bots/api#update)。

两个独立聊天任务并发处理不同会话，同一会话按顺序回复；模型请求不占用数据库写锁。API 超时或限流有受限重试，重启会恢复尚未过时的任务；超过两分钟不再补发旧话题。发送结果不确定时不会自动重复发送。API 配置和最近问题可在群聊天面板查看，详细行为与限制见 [BOT_GUIDE.md](BOT_GUIDE.md)。

## Linux 原生部署

服务器部署材料在 `deploy`，实际部署前核实现有目录、域名、HTTPS 反向代理与服务账号。本项目不自动更改现有服务器。

1. 将源码放到 `/opt/purrai`，使用 Python 3.12 在该目录创建 `.venv`，按锁文件安装依赖，再执行 `pip install -e . --no-deps`。
2. 前端在本机完成构建，将 `frontend/dist` 与源码一同同步到服务器；不需要打包成安装程序。
3. 创建 `purrai` 服务账号，使其可读取程序并写入 `/var/lib/purrai`。将配置保存到 `/etc/purrai.env`。
4. 设置 `PURRAI_DATABASE_URL=sqlite+aiosqlite:////var/lib/purrai/purrai.db`、`PURRAI_BACKUP_DIR=/var/lib/purrai/backups`，填写 Bot Token，需要全局维护时再配置超级管理员 ID；仅在启用网页时填写 OIDC。
5. 在同样环境中运行 `purrai init-db`，安装 `deploy/purrai.service`。服务固定监听 `127.0.0.1:8765`。
6. 启动服务并检查健康接口及测试群行为。如需网页，参考 `deploy/nginx.conf.example` 接入现有 HTTPS 反向代理并核对登录。

升级顺序：生成备份 → 停止服务 → 更新代码与前端资源 → 执行迁移 → 启动 → 检查任务恢复与测试群。出现不兼容升级时停服恢复代码和备份；迁移不提供自动删表降级。

## 数据与运行限制

按最新需求，默认记录获准群在容量和版本上限内的新消息及编辑版本，包括 PurrAI 删除广告前的完整正文、说明、链接实体、发送者、媒体标识、命中原因和实际删除结果。归档先于事件确认和处罚落库；普通管理员与白名单消息也记录。暂停群保护不会暂停归档，关闭记录请用单独的 `archive_enabled` 开关；关闭不会清除已有记录。群管理员只能访问本群归档，超级管理员可访问所有群。记录自动清理默认关闭。

私聊 Bot 发送 `/admin` → 选择群 → **消息归档与导出**，可以查询、查看编辑版本和导出 JSONL；网页提供相同归档入口，并支持按类型和删除状态筛选及导出全部匹配消息。Bot 单次上限 1000 条、20 MB，超出时明确提示，不截断。图片和视频只保存文件标识及元数据，不自动下载原文件；旧违规记录只能迁入已有摘录。详细范围和导出格式见 [消息归档说明](MESSAGE_ARCHIVE.md)。

处理成功后会清空临时收件的原始消息，归档在独立表中保存。刷屏检测仍使用摘要，不依赖归档是否开启。

机器人不能列出所有现有群成员或恢复 Telegram 从未交付的入群事件。Telegram 更新有服务端保留时限；服务器长期离线后，管理员应检查真实群状态，不能把本地任务恢复等同于补齐所有历史事件。

字体来自 Google Fonts 的 Noto Sans，按随附 `src/purrai/assets/OFL.txt` 分发。
