# 模块职责与依赖

PurrAI 仍是单进程 Python 服务与可选 Vue 后台。本次调整保持现有功能、数据库格式和 HTTP 路径，集中解决业务职责混杂、双向依赖、重复实现以及前端共享状态过多的问题。

## 应用入口

`app.py` 和 `runtime.py` 负责组装依赖。`Service`、`Management`、`BotAdmin` 保留原有调用入口，内部只组装或委托具体模块，不再承载数据库查询、处罚决策或面板内容。业务模块不能反向导入这三个入口。

| 入口 | 职责拆分后的实现 |
| --- | --- |
| `service.py` | `member_events.py` 处理成员加入、离开和身份变化；`verification.py` 处理验证输入与期限；`moderation.py` 处理消息审核；`actions.py` 领取并派发任务 |
| `management.py` | 配置、成员处置、规则与白名单、群接入、归档、聊天和系统维护分别由对应的 `*_admin.py` 或 `group_configuration.py` 实现 |
| `bot_admin.py` | `bot_state.py` 保存交互状态；`bot_ui.py` 处理 Telegram 传输；不同业务的 `bot_*_panels.py` 展示面板；输入、命令、按钮路由和回调分别处理 |

`Management` 的 `service` 位置参数和 `chat` 属性只为既有 Python 调用兼容保留。业务实现不持有 `Service`，生产运行时通过构造参数明确传入聊天服务与群访问服务。CLI 批准群时不再创建整套运行服务。

## 后端业务边界

`GroupAccess` 统一群接入和当前 Telegram 管理员身份检查，供管理操作和归档发送共同调用。任务处理器不再创建 `Management` 来检查资格，消除了原来的反向依赖。

`ActionRunner` 管理领取、顺序、失败和重试，将实际操作派发给验证、成员限制、自动处罚及通知处理器。`action_transport.py` 保留网络请求前后的状态重查；`action_execution.py` 保留短事务与外部请求之间的交接。拆分没有把网络请求搬进数据库写锁。

任务的管理员豁免、白名单范围、暂停行为、信任后取消行为及通知分类集中在 `action_policy.py`。不同策略的差异仍明确保留，例如验证码清理独立于成员当前状态，已经完成踢出的任务仍继续解封。

消息审核调用 `message_activity.py` 计算有界的指纹和频率统计，调用 `moderation_records.py` 新增或合并违规证据，再由 `moderation_penalties.py` 按联系人、昵称、正文规则的优先级生成处罚。重复证据不重复产生处罚；新原因或新标志改变处置时仍能升级。发图门槛保持只删消息的独立策略。

成员禁言和恢复共用 `restriction_state.py` 的权限快照、期限及原限制归属比较，各操作仍在原有决策时机重新检查。管理员手动修改限制、重新入群和迟到回执等情况继续由对应业务流程处理。

聊天的观察与调度保留在 `chat.py`。`chat_context.py` 负责可用上下文与已确认回复的记录，`chat_generation.py` 负责请求准备、额度结算和生成结果复用，`chat_delivery.py` 负责文本、贴纸及部分发送成功后的恢复。审核规则和模型输出检查保持独立。

## 共用数据与查询

`management_queries.py` 为 Web 和 Bot 提供同一套记录筛选、分页、结果投影、系统统计和失败记录查询。调用方只决定页大小、筛选条件和展示方式；例如网页人工操作列表可以排除系统操作，Bot 操作记录可以展示全部记录。

`GroupConfiguration.update` 是单项与批量配置修改的共同流程，负责校验、接入重查、事务内合并、冲突检查、重复请求和操作记录。表情包增量修改仍有独立的追加、移除和清空语义。

`pack_names.py` 保存包名解析和默认包列表，不依赖数据库或业务服务；`schemas.py` 的配置校验只依赖该基础模块。ORM 默认配置继续由 `GroupSettings` 生成，不再通过业务执行模块形成循环。`serialization.py` 是 ORM 行转换的唯一实现，`verification_state.py` 是活动验证状态的唯一声明。

## 前端与配置契约

`App.vue` 只持有会话、导航、所选群、共用群列表和本次提交的操作回执。八个 `pages/*Page.vue` 分别持有本页数据、加载状态和筛选；切换群会重新创建相关页面。群设置和规则编辑弹窗独立管理草稿与错误，不再共用一个任意类型的表单对象。

`useRecords` 共用记录分页和北京时间筛选；`usePreview` 共用规则预览与处置说明；`useTask` 共用请求状态和会话过期处理。`ActionTracker` 的回执独立于当前页面，查询失败后仍保留已受理的任务并继续刷新。

Web 从需要登录的 `GET /api/catalog` 读取配置默认值、取值范围、活动验证状态、状态名称和操作名称。Bot 与该接口共用后端字段描述，数值范围来自同一份 `GroupSettings` schema。前端不再维护第二套业务默认值和范围。

修改 `GroupSettings` 或管理目录后，在仓库根运行：

```powershell
& 'D:\Tools\venvs\PurrAI\Scripts\python.exe' scripts/sync_admin_contract.py --write
& 'D:\Tools\venvs\PurrAI\Scripts\python.exe' scripts/sync_admin_contract.py --check
```

该脚本生成 `frontend/src/groupSettings.ts` 和浏览器测试的 `frontend/e2e/fixtures/catalog.json`。后者只用于模拟 API，产品运行时读取真实接口。生成的 TypeScript 只声明字段类型，不重复保存配置值。

## 结构与行为检查

`tests/test_architecture_design.py` 检查包含函数内导入的生产模块依赖无环、业务模块不反向依赖入口、配置契约同步、两端共用查询投影、配置流程复用和违规证据升级。既有验证、处罚、重启、并发、归档、OIDC、聊天及贴纸测试继续验证实际行为。

前端类型检查、单元与浏览器测试覆盖拆分后的页面和弹窗，包括后端配置目录变更后默认值与范围直接生效。生产构建仍使用既有 `npm run build`。测试均在 Windows 与模拟接口环境执行；本次未部署或操作真实 Telegram 群。
