import type * as Api from './adminApi'
import type { GroupSettings } from './groupSettings'
export type { GroupSettings } from './groupSettings'

export type User = Pick<Api.User, 'id' | 'root' | 'csrf' | 'local_dev'>
export type Group = Pick<Api.Group, 'id' | 'title' | 'type' | 'status' | 'approved' | 'enabled' | 'settings' | 'previous_ids'> & { setting_overrides?: string[] }
export type Rule = Omit<Api.Rule, 'id' | 'version' | 'kind' | 'mode' | 'scope' | 'penalty'> & { id?: string; version?: string; kind: string; mode: string; scope: string; penalty: string } & Required<Pick<Api.Rule, 'value' | 'context_value' | 'enabled'>>
export type SharedPolicy = Omit<Api.SharedPolicy, 'rules'> & { rules: Rule[] }
export type Whitelist = Pick<Api.Whitelist, 'user_id' | 'verification' | 'moderation' | 'note'> & { version?: string }
export type Verification = Pick<Api.Verification, 'id' | 'user_id' | 'user_name' | 'state' | 'deadline' | 'attempts' | 'failure_action' | 'error'> & { allowed_operations: NonNullable<Api.Verification['allowed_operations']> }
export type Operation = Pick<Api.Action, 'id' | 'kind' | 'state' | 'created_at' | 'actor_id' | 'reason' | 'error' | 'can_review_delivery' | 'manual_review'> & Partial<Pick<Api.Action, 'user_id'>> & { payload?: Api.Action['payload'] & { invite_link?: string } }
export type Violation = Pick<Api.Violation, 'id' | 'user_id' | 'created_at' | 'sender_name' | 'sender_chat' | 'excerpt' | 'reasons' | 'warning_active' | 'profile_violation' | 'rule_mute'> & { allowed_operations: NonNullable<Api.Violation['allowed_operations']>; permanent_ban: NonNullable<Api.Violation['permanent_ban']>; actions: Operation[]; correction?: Pick<Api.Correction, 'reason' | 'actor_id'> | null }
export type Budget = Pick<Api.Budget, 'group_calls' | 'group_call_limit' | 'group_tokens' | 'group_token_limit' | 'global_calls' | 'global_call_limit' | 'global_tokens' | 'global_token_limit'>
export type WorkerStatus = Pick<Api.Worker, 'name' | 'state' | 'error' | 'restarts'>
export type StickerStatus = Api.StickerStatus
export type ChatStatus = Api.ChatStatus
export type TelegramProblem = Pick<Api.TelegramMethod, 'method' | 'label' | 'seconds' | 'consecutive_failures' | 'last_failure' | 'purpose'>
export type SystemStatus = Api.SystemStatus
export type Page<T> = Pick<Api.Page_Action_, 'total'> & { items: T[]; page: number; size: number }
export type ArchiveRecording = Api.ArchiveRecording
export type ArchivedMessage = Pick<Api.ArchivedMessage, 'id' | 'message_id' | 'sender_id' | 'sender_name' | 'username' | 'sender_type' | 'message_type' | 'text' | 'caption' | 'sent_at' | 'source' | 'revision_count' | 'original_observed' | 'recording_gap' | 'deletion_state' | 'deletion_error' | 'origin_group_id' | 'origin_message_id'> & { violation?: Pick<Api.Violation, 'reasons'> | null }
export type ArchivedDetail = ArchivedMessage & { revisions: Api.Revision[] }
export type ArchivePage = Page<ArchivedMessage> & Pick<Api.ArchivePage, 'recording'>
export type PreviewInput = Required<Pick<Api.PreviewInput, 'text' | 'sender_name' | 'newcomer' | 'contact' | 'forwarded' | 'repeats' | 'similar' | 'burst'>> & Pick<Api.PreviewInput, 'sender_bio' | 'message_emoji_packs'>
export type PreviewResult = Api.Preview
export interface FieldSchema { minimum?: number; maximum?: number; minLength?: number; maxLength?: number; type: string }
export type AdminCatalog = Omit<Api.Catalog, 'settings' | 'rules'> & { settings: Pick<Api.SettingsCatalog, 'defaults' | 'numbers' | 'booleans'> & Partial<Pick<Api.SettingsCatalog, 'labels'>> & { fields: Record<keyof GroupSettings, FieldSchema> }; rules?: Api.RuleCatalog }
