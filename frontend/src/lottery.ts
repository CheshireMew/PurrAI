import type * as Api from './adminApi'
export type LotteryPrize = Required<Api.LotteryPrize>
export type LotteryConfig = Omit<Required<Api.LotteryConfig>, 'prizes'> & { prizes: LotteryPrize[] }
export type Lottery = Omit<Api.Lottery, 'config'> & { config: LotteryConfig }
export type LotteryEntry = Api.LotteryEntry
export type LotteryDetail = Omit<Api.LotteryDetail, 'config'> & { config: LotteryConfig }
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
