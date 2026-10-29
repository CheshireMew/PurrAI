import type { GroupSettings } from './groupSettings'
export type { GroupSettings } from './groupSettings'

export interface User { id: number; root: boolean; csrf: string }
export interface Group { id: number; title: string; type: string; status: string; approved: boolean; enabled: boolean; settings: GroupSettings }
export interface Rule { id?: string; inherited?: boolean; inherited_from?: string; kind: string; value: string; mode: string; scope: string; context_value: string; penalty: string; enabled: boolean }
export interface SharedPolicy { settings: GroupSettings; rules: Rule[] }
export interface Whitelist { user_id: number; verification: boolean; moderation: boolean; note: string }
export interface Verification { allowed_operations: string[]; id: string; user_id: number; user_name: string; state: string; deadline: number; attempts: number; failure_action: string; error: string }
export interface Operation { id: string; kind: string; state: string; created_at: number; actor_id: number | null; reason: string; error: string }
export interface Violation { allowed_operations: string[]; permanent_ban: boolean; id: string; user_id: number; created_at: number; sender_name: string; sender_chat: boolean; excerpt: string; reasons: string[]; actions: Operation[]; warning_active: boolean; profile_violation: boolean; rule_mute: boolean }
export interface Budget { group_calls: number; group_call_limit: number; group_tokens: number; group_token_limit: number; global_calls: number; global_call_limit: number; global_tokens: number; global_token_limit: number }
export interface StickerStatus { ready: number; total: number; emoji_count: number; unavailable: string[]; error: string }
export interface ChatStatus { configured: boolean; model: string; pending: number; error: string; budget?: Budget; stickers?: StickerStatus; commands?: { enabled: boolean; threshold: number; minimum_users: number; window_days: number; items: { text: string; count: number; users: number; ignored: boolean }[] } }
export interface SystemStatus { connected: boolean; bot_configured: boolean; login_configured: boolean; pending_actions: number; last_poll: number; last_backup: string | null; last_error: string; archive_status: string; failed_actions: Operation[]; failed_events: {id: number; error: string}[]; chat_budget?: Budget }
export interface Page<T> { items: T[]; total: number; page: number; size: number }
export interface ArchiveRecording { state: string; reason: string }
export interface ArchivedMessage {
  id: string; message_id: number; sender_id: number | null; sender_name: string; username: string;
  sender_type: string; message_type: string; text: string; caption: string; sent_at: number;
  source: string; revision_count: number; original_observed: boolean;
  deletion_state: string; deletion_error: string; violation?: { reasons: string[] } | null;
}
export interface ArchivedDetail extends ArchivedMessage {
  revisions: { id: number; is_edit: boolean; event_at: number; reasons: string[];
    payload: Record<string, unknown> & { text?: string; caption?: string } }[]
}
export interface ArchivePage extends Page<ArchivedMessage> { recording: ArchiveRecording }
export interface PreviewInput { text: string; sender_name: string; newcomer: boolean; contact: boolean; forwarded: boolean; repeats: number; similar: number; burst: number }
export interface PreviewResult { reasons: string[]; notes?: string[]; kick_member?: boolean; ban_member?: boolean; mute_member?: boolean; delete_message?: boolean }
export interface FieldSchema { minimum?: number; maximum?: number; minLength?: number; maxLength?: number; type: string }
export interface AdminCatalog { settings: {defaults: GroupSettings; fields: Record<keyof GroupSettings, FieldSchema>; numbers: Record<string, string>; booleans: Record<string, string>}; states: Record<string, string>; operations: Record<string, string>; failure_actions: Record<string, string>; active_verifications: string[] }
