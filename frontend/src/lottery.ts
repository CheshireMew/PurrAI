export interface LotteryPrize { name: string; kind: 'points' | 'manual' | 'none'; points: number; quantity: number; weight: number }
export interface LotteryConfig {
  title: string; description: string; mode: 'raffle' | 'instant'; entry_cost: number; min_balance: number;
  active_window_days: number; min_active_days: number; draw_mode: 'manual' | 'time' | 'participants';
  ends_at: number; participant_target: number; min_participants: number; daily_limit: number; prizes: LotteryPrize[];
}
export interface Lottery {
  id: string; group_id: number; config: LotteryConfig; state: string; version: number;
  participant_count: number; draw_count: number; created_at: number; opened_at: number; finished_at: number;
}
export interface LotteryEntry {
  id: string; user_id: number; display_name: string; fee: number; state: string;
  prize_index: number | null; prize_label: string; reward_points: number; fulfilled: boolean; created_at: number;
}
export interface LotteryDetail extends Lottery {
  entries: LotteryEntry[]; total_entries: number; remaining: number[];
  deliveries: { id: string; state: string; error: string; phase: string }[];
}
export const lotteryStates: Record<string, string> = { draft: '草稿', open: '进行中', drawn: '已开奖', closed: '已结束', cancelled: '已取消' }
export const entryStates: Record<string, string> = { joined: '已报名', won: '已中奖', lost: '未中奖', refunded: '已退款', refund_pending: '待退款' }
export function newLottery(): LotteryConfig {
  return { title: '', description: '', mode: 'raffle', entry_cost: 0, min_balance: 0,
    active_window_days: 7, min_active_days: 0, draw_mode: 'manual', ends_at: 0,
    participant_target: 10, min_participants: 1, daily_limit: 1,
    prizes: [{ name: '', kind: 'manual', points: 0, quantity: 1, weight: 1 }] }
}
export function beijingInput(timestamp: number) {
  return timestamp ? new Date((timestamp + 8 * 3600) * 1000).toISOString().slice(0, 16).replace('T', ' ') : ''
}
