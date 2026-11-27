"""Public administration responses; JSON evidence remains explicitly extensible."""

from __future__ import annotations

from typing import Generic, Literal, TypeVar, get_args, get_origin, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, JsonValue, create_model
from sqlalchemy import JSON

from purrai import models
from purrai.advertisement_schemas import AdvertisementSettings
from purrai.group_menu_schemas import MenuConfig
from purrai.lottery_schemas import LotteryConfig
from purrai.points_schemas import PointsSettings
from purrai.schemas import GroupSettings, RuleInput
from purrai.auto_reply_schemas import AutoReplyInput

JsonObject = dict[str, JsonValue]
T = TypeVar("T")


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


def record(name, model, *, exclude=(), fields=None):
    """Keep database-backed fields authoritative, with public projections explicit."""
    hints, definition = get_type_hints(model), {}
    for column in model.__table__.columns:
        if column.key in exclude or column.info.get("internal"):
            continue
        if isinstance(column.type, JSON):
            annotation = get_args(hints[column.key])[0]
            if column.nullable:
                annotation = next((item for item in get_args(annotation) if item is not type(None)), annotation)
            kind = list[JsonValue] if annotation is list or get_origin(annotation) is list else JsonObject
        else:
            kind = column.type.python_type
        definition[column.key] = (kind | None, ...) if column.nullable else (kind, ...)
    definition.update(fields or {})
    return create_model(name, __base__=Contract, **definition)


class Page(Contract, Generic[T]):
    items: list[T]
    total: int
    page: int | None = None
    size: int | None = None


class Empty(Contract):
    pass


class Ok(Contract):
    ok: bool


class WhitelistRemoval(Ok):
    user_id: int


class User(Contract):
    id: int
    root: bool
    csrf: str
    group_ids: list[int] | None = None
    local_dev: bool = False


class Permissions(Contract):
    ok: bool
    missing: list[str]
    checked_at: float
    permissions: "Permissions | None" = None


Group = record("Group", models.Group, fields={"settings": (GroupSettings, ...),
    "permissions": (Permissions | JsonObject, ...), "setting_overrides": (list[str], ...),
    "previous_ids": (list[int], Field(default_factory=list))})


class Rule(RuleInput):
    model_config = ConfigDict(extra="forbid")
    id: str
    group_id: int | None = None
    version: str | None = None
    inherited: bool | None = None
    inherited_from: str = ""
    customized: bool | None = None
    origin: str | None = None
    learned_at: float | None = None
    sources: list[RuleSource] = Field(default_factory=list)
    kind_label: str | None = None
    matching_label: str | None = None
    scope_label: str | None = None
    consequence: str | None = None
    consequence_short: str | None = None
    hit_count: int | None = None
    last_hit_at: float | None = None
    global_version: str | None = None
    other_sources: int | None = None


class RuleSource(Contract):
    group_id: int
    message_id: int
    origin_group_id: int | None = None
    origin_message_id: int | None = None
    violation_id: str | None = None
    created_at: float
    withdrawn: bool


class ManualReview(Contract):
    outcome: str
    reason: str
    actor_id: int
    reviewed_at: float
    previous_state: str | None = None
    previous_error: str | None = None


Action = record("Action", models.Action, fields={"payload": (JsonObject, ...),
    "can_review_delivery": (bool, False), "manual_review": (ManualReview | None, None),
    "summary": (dict[str, int] | None, None)})
Verification = record("Verification", models.Verification, exclude=("answer_hash", "input_digits", "nonce"),
    fields={"allowed_operations": (list[str], Field(default_factory=list))})
Whitelist = record("Whitelist", models.Whitelist, fields={"version": (str | None, None)})
Violation = record("Violation", models.Violation, fields={"reasons": (list[str], ...),
    "actions": (list[Action], Field(default_factory=list)), "allowed_operations": (list[str], Field(default_factory=list)),
    "permanent_ban": (bool, False), "correction": (JsonObject | None, None),
    "origin_group_id": (int | None, None), "origin_message_id": (int | None, None)})
Member = record("Member", models.Member)
AutoReply = record("AutoReply", models.AutoReply, fields={"version": (str, ...)})


class SettingsCatalog(Contract):
    defaults: GroupSettings
    fields: dict[str, JsonObject]
    numbers: dict[str, str]
    booleans: dict[str, str]
    labels: dict[str, str]


class RuleCatalog(Contract):
    kinds: dict[str, str]
    modes: dict[str, str]
    scopes: dict[str, str]


class Catalog(Contract):
    settings: SettingsCatalog
    states: dict[str, str]
    operations: dict[str, str]
    failure_actions: dict[str, str]
    active_verifications: list[str]
    rules: RuleCatalog


class SharedPolicy(Contract):
    settings: GroupSettings
    rules: list[Rule]


class Inheritance(Contract):
    defaults: GroupSettings
    settings: GroupSettings
    overrides: list[str]


class PackChange(Contract):
    group_id: int
    operation: str
    requested_packs: list[str]
    packs: list[str]
    added: list[str]
    removed: list[str]


class BusinessPolicy(Contract):
    document: JsonObject
    version: str


class Preview(Contract):
    reasons: list[str]
    notes: list[str] = Field(default_factory=list)
    name_reasons: list[str] = Field(default_factory=list)
    mute_reasons: list[str] = Field(default_factory=list)
    ban_reasons: list[str] = Field(default_factory=list)
    hit_rule_ids: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    bio_reasons: list[str] = Field(default_factory=list)
    message_emoji_hit: bool = False
    ai_confirmation_required: bool = False
    kick_member: bool = False
    ban_member: bool = False
    mute_member: bool = False
    delete_message: bool = False
    chat_reasons: list[str] = Field(default_factory=list)
    utility_command: bool = False
    command_candidate: str = ""
    output_reasons: list[str] = Field(default_factory=list)
    advertised_targets: list[str] = Field(default_factory=list)
    reason_categories: list[str] = Field(default_factory=list)


class BusinessPreview(Contract):
    before: Preview
    after: Preview
    changed: bool


class AutoReplyPreview(Contract):
    matched: bool
    rule: AutoReply | AutoReplyDraft | None


class AutoReplyDraft(AutoReplyInput):
    model_config = ConfigDict(extra="forbid")
    version: str


class Emergency(Contract):
    active: bool
    started_at: float | None = None
    until: float | None = None
    reason: str | None = None
    actor: int | str | None = None
    restored_at: float | None = None
    restored_by: int | str | None = None
    restore_reason: str | None = None
    suppressed_until: float | None = None
    raid_enabled: bool | None = None
    base_verification_enabled: bool | None = None


class Dashboard(Contract):
    groups: int
    active_groups: int
    abnormal_groups: int
    pending: int
    passed_today: int
    failed_today: int
    violations_today: int
    failed_actions: int
    advertising_today: int
    spam_today: int
    other_policy_today: int


class Budget(Contract):
    day: str
    group_calls: int
    group_call_limit: int
    group_tokens: int
    group_token_limit: int
    global_calls: int
    global_call_limit: int
    global_tokens: int
    global_token_limit: int
    user_call_limit: int
    user_token_limit: int


class StickerStatus(Contract):
    ready: int
    total: int
    emoji_count: int
    unavailable: list[str]
    error: str


class CommandStatus(Contract):
    enabled: bool
    threshold: int
    minimum_users: int
    window_days: float
    items: list[CommandCount]


class CommandCount(Contract):
    text: str
    count: int
    users: int
    ignored: bool


class ChatStatus(Contract):
    configured: bool
    model: str
    pending: int
    error: str
    budget: Budget | None = None
    stickers: StickerStatus | None = None
    commands: CommandStatus | None = None


class ChatDiagnostic(Contract):
    id: int
    message_id: int
    topic_id: int
    state: str
    direct: bool
    attempts: int
    reason: str
    created_at: float
    available_at: float
    timing: JsonObject | None


class ChatDiagnostics(Page[ChatDiagnostic]):
    usage: list[JsonObject]
    decisions: list[JsonObject]
    turn_retention_hours: int
    usage_retention_days: int


class ArchiveRecording(Contract):
    state: str
    reason: str
    bytes: int | None = None
    limit_bytes: int | None = None
    group_bytes: int | None = None
    group_limit_bytes: int | None = None


class RecordingGap(Contract):
    omitted_at_least: int
    first_skipped_at: float
    last_skipped_at: float
    reason: str
    latest_not_recorded: bool


Revision = record("Revision", models.MessageRevision, fields={"display_text": (str | None, None), "reasons": (list[str], ...)})
ArchivedMessage = record("ArchivedMessage", models.ArchivedMessage, fields={
    "recording_gap": (RecordingGap | None, None), "violation": (Violation | None, None),
    "origin_group_id": (int | None, None), "origin_message_id": (int | None, None),
    "revisions": (list[Revision], Field(default_factory=list)), "actions": (list[Action], Field(default_factory=list))})


class ArchivePage(Page[ArchivedMessage]):
    recording: ArchiveRecording


class GroupQuota(Contract):
    id: int
    title: str
    bytes: int
    limit_bytes: int | None


class ArchiveCapacity(Contract):
    bytes: int
    limit_bytes: int
    group_bytes: int
    group_limit_bytes: int | None
    groups: list[GroupQuota]
    can_edit_quotas: bool


class ArchiveQuota(Ok):
    group_mb: int | None
    global_mb: int | None


class ArchiveExport(Contract):
    id: str
    actor_id: int
    group_id: int
    cutoff: float
    filters: JsonObject
    digest: str
    count: int
    created_at: float
    purged: bool
    purged_at: float | None = None


class ArchivePurge(Ok):
    removed: int
    export_id: str


class ArchiveTrial(Contract):
    id: str
    message_id: int
    text: str
    before: Preview
    after: Preview
    changed: bool
    incomplete: bool
    unresolved_emoji: bool
    unresolved_links: bool


class ArchivePreview(Contract):
    items: list[ArchiveTrial]
    changed_count: int
    notes: list[str]


class MemberDetail(Contract):
    user_id: int
    member: Member | None
    whitelist: Whitelist | None
    current: JsonObject | None
    current_error: str
    checked_at: float | None
    verifications: Page[Verification]
    violations: Page[Violation]
    actions: Page[Action]


class BatchReceipt(Contract):
    batch_id: str
    action_ids: list[str]


class ReleaseMember(Contract):
    id: str
    user_id: int
    user_name: str
    state: str
    generation: str
    nonce: str


class ReleasePreview(Contract):
    items: list[ReleaseMember]
    snapshot: str
    total: int


class CountResult(Ok):
    count: int


class CorrectionPreview(Contract):
    rules: list[Rule]
    global_allowed: bool
    affected_groups: list[JsonObject]
    user_id: int
    excerpt: str


class Correction(Contract):
    actor_id: int
    reason: str
    created_at: float
    scope: str
    disabled_rules: list[str]
    action_ids: list[str]


class Admins(Contract):
    items: list[JsonObject]
    superadmin_ids: list[int]
    access_mode: str


class StatusError(Contract):
    status: int
    detail: str


class ActionStatuses(Contract):
    items: list[Action]
    errors: dict[str, StatusError]


class FailedEvent(Contract):
    id: int
    attempts: int
    error: str


class Retry(Ok):
    event_id: int | None = None
    action_id: str | None = None


class DeliveryResolution(Contract):
    action_id: str
    state: str
    review: ManualReview


class Worker(Contract):
    name: str
    state: str
    error: str
    restarts: int
    retry_at: float | None = None


class ReviewStorage(Contract):
    bytes: int
    limit_bytes: int
    limited: StorageLimit | None
    payload_retention_hours: int
    history_retention_days: int


class StorageLimit(Contract):
    at: float
    count: int


class SourceChecks(Contract):
    delayed: int
    repeated: int
    first_failure: float
    last_failure: float
    next_retry_at: float
    last_success: float


class TelegramMethod(Contract):
    method: str
    purpose: str
    label: str
    calls: int
    failures: int
    consecutive_failures: int
    last_success: float
    last_failure: float
    first_failure: float
    last_logged: float
    seconds: float
    phase: str
    timeout: float | None
    outcome: str
    authentication_failed: bool
    error: str = ""
    retry_at: float = 0


class TelegramHealth(Contract):
    degraded: bool
    problems: list[TelegramMethod]
    warnings: list[TelegramMethod] = Field(default_factory=list)
    methods: list[TelegramMethod]


class SystemStatus(Contract):
    pending_actions: int
    failed_count: int
    failed_actions: list[Action]
    failed_events: list[FailedEvent]
    last_backup: str | None
    archive_status: str
    review_storage: ReviewStorage
    chat_budget: Budget
    bot_configured: bool
    login_configured: bool
    connected: bool
    last_poll: float
    workers: list[Worker]
    runtime_ready: bool
    last_error: str
    poll_error: str
    source_checks: SourceChecks
    telegram_health: TelegramHealth
    poll_stalled: bool


class BackupPlan(Contract):
    enabled: bool
    hour_beijing: int
    interval_days: int
    retention_days: int
    retention_count: int
    max_total_mb: int
    prune_enabled: bool
    exercise_after_backup: bool
    version: str | None = None


class BackupExercise(Contract):
    integrity_ok: bool
    foreign_keys_ok: bool
    revision: str
    compatible: bool
    restored: bool
    counts: dict[str, int]
    checked_at: float
    file: str | None = None


class BackupReceipt(Contract):
    file: str
    created_at: float
    bytes: int
    exercise: BackupExercise | None


class BackupFile(BackupReceipt):
    managed: bool
    revision: str


class BackupPending(Contract):
    file: str
    created_at: float
    state: str
    error: str
    retry_at: float | None


class Backups(Contract):
    plan: BackupPlan
    version: str
    items: list[BackupFile]
    pending: list[BackupPending]
    free_bytes: int
    schedule: JsonObject


class Alert(Contract):
    name: str
    episode: int
    active: bool
    message: str
    changed_at: float
    notified: bool
    details: JsonObject
    severity: str
    resolution: str | None = None


class DiagnosticSummary(Contract):
    id: str
    started_at: float
    finished_at: float | None
    state: str
    context: JsonObject
    complete: bool | None
    capture_error: str | None


class Diagnostics(Page[DiagnosticSummary]):
    enabled: bool
    retention_hours: int
    max_mb: int


class Diagnostic(Contract):
    format: int
    operation: str
    id: str
    started_at: float
    finished_at: float | None = None
    state: str
    context: JsonObject
    complete: bool | None = None
    capture_error: str | None = None
    request: JsonObject | None = None
    response: JsonObject | None = None
    error: JsonObject | None = None


class PointsTrust(Contract):
    earned_points: int
    active_days: int
    level: int


PointsAccount = record("PointsAccount", models.PointsAccount, fields={"rank": (int | None, None),
    "trust": (PointsTrust | None, None), "previous_identity": (JsonObject | None, None)})
PointsEntry = record("PointsEntry", models.PointsEntry)
PointsImport = record("PointsImport", models.PointsImport)


class PointsSummary(Contract):
    accounts: int
    points: int
    pending: int


class PointsSettlement(Contract):
    mode: Literal["disabled", "direct", "model", "local"]
    pending_messages: int
    oldest_at: float | None
    local_window_hours: int


class PointsPage(Page[PointsAccount]):
    summary: PointsSummary
    imports: list[PointsImport]
    settlement: PointsSettlement | None = None


class PointsHistory(Page[PointsEntry]):
    account: PointsAccount


class PointsPolicy(Contract):
    config: PointsSettings
    version: int


class ImportReceipt(Contract):
    batch_key: str
    count: int
    total_points: int
    source: str
    already_imported: bool
    preview: bool | None = None
    id: str | None = None
    group_id: int | None = None
    digest: str | None = None
    actor_id: int | None = None
    created_at: float | None = None


Lottery = record("Lottery", models.Lottery, fields={"config": (LotteryConfig, ...)})
LotteryEntry = record("LotteryEntry", models.LotteryEntry,
    exclude=("lottery_id", "group_id", "request_id", "day"))


class LotteryDelivery(Contract):
    id: str
    state: str
    error: str
    phase: str


class LotteryDetail(Lottery):
    remaining: list[int]
    entries: list[LotteryEntry]
    total_entries: int
    deliveries: list[LotteryDelivery]


class MenuDelivery(Contract):
    id: str
    state: str
    error: str
    operation: str


class Menu(Contract):
    config: MenuConfig
    version: int
    active: bool
    needs_update: bool
    delivery: MenuDelivery | None


AdvertisementSample = record("AdvertisementSample", models.AdvertisementSample,
    fields={"corrected": (bool | None, None), "usable": (bool | None, None)})
AdvertisementReview = record("AdvertisementReview", models.AdvertisementReview)


class Samples(Page[AdvertisementSample]):
    settings: AdvertisementSettings
    version: int
    reviews: dict[str, int]
    model_ready: bool


class AdvertisementPolicy(Contract):
    settings: AdvertisementSettings
    version: int


class SampleMatch(Contract):
    score: float
    coverage: float
    sample_coverage: float
    direct: bool
    sample_id: str
    sample_version: int
    matched: bool
    sample: AdvertisementSample | None = None


class SamplePreview(Contract):
    enabled: bool
    result: SampleMatch | None
    action: str


class AdvertisementImport(Contract):
    count: int
    items: list[JsonObject]
    applied: bool
    scope: str


class AdvertisementRetry(Contract):
    ids: list[str]
