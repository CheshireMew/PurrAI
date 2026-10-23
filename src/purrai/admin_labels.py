"""Administrator labels and configuration field descriptions."""

STATE = {
    "unapproved": "等待所有者批准",
    "pending": "等待处理",
    "active": "保护中",
    "paused": "已暂停",
    "error": "需要处理",
    "muting": "正在禁言",
    "releasing": "等待恢复发言",
    "removing": "正在移出",
    "failing": "正在执行失败处理",
    "passed": "已通过",
    "failed": "失败",
    "succeeded": "已完成",
    "cancelled": "已取消",
    "left": "已退群",
    "superseded": "已换新验证",
}


OPERATIONS = {
    "release": "放行",
    "reissue": "重新出题",
    "ban": "临时封禁",
    "mute": "临时禁言",
    "unmute": "解除 PurrAI 禁言",
    "unban": "解除 PurrAI 封禁",
    "revoke_warning": "撤销警告",
    "manual_mute": "手动禁言",
    "manual_ban": "手动封禁",
    "challenge": "重新出题",
    "settings": "修改配置",
    "rule": "修改规则",
    "whitelist": "白名单",
    "activate": "启用保护",
    "verify_mute": "验证禁言",
    "moderation_mute": "违规禁言",
    "delete": "删除消息",
    "warn": "群内通知",
    "progress": "更新验证输入",
    "batch_release": "批量放行",
    "admin_notify": "执行结果回执",
    "admin": "管理员授权",
    "backup": "生成备份",
    "retry": "重试任务",
    "retry_event": "重试事件",
    "profile_mute": "昵称命中永久禁言",
    "emoji_status_check": "检查名字旁状态表情",
    "emoji_status_mute": "状态表情命中永久禁言",
    "contact_kick": "分享联系人踢出群聊",
    "contact_ban": "分享联系人永久封禁",
    "emoji_status_ban": "状态表情命中永久封禁",
    "rule_mute": "正文广告规则永久禁言",
    "join_link": "生成申请入群链接",
    "verification_failure": "验证失败处理",
    "archive_export": "导出消息归档",
}


BOOLEANS = {
    "archive_enabled": "记录群消息归档",
    "join_requests": "申请入群先验证",
    "verification_enabled": "图片验证",
    "newcomer_links": "新成员限制链接",
    "newcomer_mentions": "新成员限制 @账号及群组",
    "newcomer_forwards": "新成员限制用户及频道转发",
    "newcomer_wallets": "新成员限制加密货币地址",
    "repeat_enabled": "重复刷屏检测",
    "block_invites": "群邀请链接拦截",
    "similar_enabled": "近似内容刷屏检测",
    "burst_enabled": "连续发送检测",
}


NUMBERS = {
    "verification_timeout": "验证时限（秒）",
    "max_attempts": "最多错误次数",
    "failure_ban_seconds": "手动封禁及申请重试间隔（秒）",
    "newcomer_seconds": "新成员限制期（秒）",
    "newcomer_messages": "前几条消息继续观察（0 关闭）",
    "media_min_text_messages": "发图及表情前独立文字条数（0 关闭）",
    "repeat_window": "重复检测窗口（秒）",
    "repeat_count": "重复消息触发条数",
    "similar_window": "近似内容窗口（秒）",
    "similar_count": "近似内容触发条数",
    "similar_threshold": "近似程度（百分比）",
    "similar_min_length": "近似检测最短长度",
    "burst_window": "连续发送窗口（秒）",
    "burst_count": "连续发送触发条数",
    "mute_seconds": "手动禁言（秒）",
}


KINDS = {
    "keyword": "关键词",
    "block_domain": "禁止域名",
    "allow_domain": "允许域名",
    "block_mention": "禁止引流账号",
    "allow_mention": "允许提及账号",
}


MODES = {"contains": "包含", "exact": "完整", "compact": "忽略表情和分隔符", "pinyin": "谐音"}


SCOPES = {
    "message": "正文",
    "name": "昵称及用户名",
    "either": "昵称或正文",
    "name_and_message": "昵称＋正文组合",
}


CHAT_FIELDS = {
    "chat_name": "聊天昵称",
    "chat_persona": "性格与说话习惯",
    "chat_ignored_texts": "聊天忽略口令（每行一条，完整匹配，- 清空）",
    "chat_history": "最近消息条数",
    "chat_interval": "主动接话检查间隔（秒）",
    "chat_sticker_packs": "聊天贴纸包（addstickers 链接或短名称，多个用空格或换行分隔，- 清空）",
    "chat_sticker_interval": "聊天贴纸最短间隔（秒）",
    "chat_antifraud_packs": "防诈贴纸包（addstickers 链接或短名称，多个用空格或换行分隔，- 清空）",
    "chat_antifraud_interval": "防诈提醒最短间隔（秒）",
}


CHAT_BUTTON_LABELS = {
    "chat_name": "聊天昵称",
    "chat_persona": "性格与说话习惯",
    "chat_ignored_texts": "聊天忽略口令",
    "chat_history": "最近消息条数",
    "chat_interval": "主动接话检查间隔",
    "chat_sticker_packs": "聊天贴纸包",
    "chat_sticker_interval": "聊天贴纸最短间隔",
    "chat_antifraud_packs": "防诈贴纸包",
    "chat_antifraud_interval": "防诈提醒最短间隔",
}


NAVIGATION_ACTIONS = {
    "groups",
    "group",
    "access_panel",
    "settings",
    "archive",
    "archive_detail",
    "list",
    "detail",
    "members",
    "emoji_packs",
    "admins",
    "system",
    "failures",
    "prompt",
    "confirm",
    "emoji_packs_clear_confirm",
    "permissions",
}
