<script setup lang="ts">
import { ref, computed } from 'vue'
import { useDashboardStore } from '../stores/dashboard'
import { Receipt, Search } from 'lucide-vue-next'

const store = useDashboardStore()
const filter = ref<'all' | 'active' | 'closed'>('all')
const keyword = ref('')

const trades = computed(() => {
  const all: any[] = store.data?.trades || []
  return all.filter((t) => {
    if (filter.value === 'active' && t.status !== 'holding') return false
    if (filter.value === 'closed' && t.status === 'holding') return false
    if (keyword.value) {
      const q = keyword.value.toLowerCase()
      const matchInst = (t.inst || '').toLowerCase().includes(q)
      const matchStrat = (t.strategy || '').toLowerCase().includes(q)
      const matchReason = (t.exit_reason || '').toLowerCase().includes(q)
      if (!matchInst && !matchStrat && !matchReason) return false
    }
    return true
  })
})

const holdingCount = computed(() => (store.data?.trades || []).filter((t: any) => t.status === 'holding').length)
const closedCount = computed(() => (store.data?.trades || []).filter((t: any) => t.status !== 'holding').length)

function num(v: any): number {
  return Number(v) || 0
}

function getPnl(t: any): number {
  return Number(t.pnl ?? t.net_pnl ?? t.gross_pnl ?? 0)
}

function getRoi(t: any): number {
  return Number(t.roi_pct ?? t.roi ?? 0)
}

function formatPx(v: any): string {
  if (v == null || v === '') return '--'
  const n = Number(v)
  if (isNaN(n)) return String(v)
  return n >= 100 ? n.toFixed(2) : n >= 1 ? n.toFixed(4) : n.toFixed(6)
}

function clean(v: any, fallback = '--'): string {
  return v || fallback
}
</script>

<template>
  <div class="space-y-3.5">
    <!-- Header -->
    <div
      class="rounded-xl border p-4 sm:p-5 flex flex-wrap items-center justify-between gap-3 shadow-xs transition-colors"
      style="background-color: var(--bg-card); border-color: var(--border-subtle);"
    >
      <div class="flex items-center space-x-3">
        <div
          class="w-9 h-9 rounded-lg flex items-center justify-center border shrink-0"
          style="background-color: var(--bg-card-subtle); border-color: var(--border-medium); color: var(--text-main);"
        >
          <Receipt class="w-4 h-4" />
        </div>
        <div>
          <h2 class="text-xs sm:text-sm font-black font-mono uppercase tracking-wide" style="color: var(--text-main);">
            完整成交台账与生命周期履历
          </h2>
          <p class="text-xs font-mono mt-0.5" style="color: var(--text-muted);">
            真实撮合成交记录，已扣除交易所手续费与资金费率净额
          </p>
        </div>
      </div>
      <div class="text-xs font-mono" style="color: var(--text-muted);">
        持仓 <strong style="color: var(--color-brand);">{{ holdingCount }}</strong> · 已平仓 <strong style="color: var(--text-main);">{{ closedCount }}</strong>
      </div>
    </div>

    <!-- Filter bar -->
    <div
      class="rounded-xl border p-2.5 sm:p-3 flex flex-wrap items-center justify-between gap-3 shadow-xs transition-colors"
      style="background-color: var(--bg-card); border-color: var(--border-subtle);"
    >
      <div class="flex rounded-lg border p-0.5 font-mono text-xs" style="background-color: var(--bg-badge); border-color: var(--border-subtle);">
        <button
          @click="filter = 'all'"
          class="px-3 py-1.5 rounded-md cursor-pointer transition font-medium"
          :style="filter === 'all'
            ? { backgroundColor: 'var(--bg-card)', color: 'var(--text-main)', borderColor: 'var(--border-medium)', boxShadow: 'var(--shadow-card)' }
            : { color: 'var(--text-muted)' }"
          :class="filter === 'all' ? 'border font-bold' : ''"
        >
          全部
        </button>
        <button
          @click="filter = 'active'"
          class="px-3 py-1.5 rounded-md cursor-pointer transition font-medium"
          :style="filter === 'active'
            ? { backgroundColor: 'var(--bg-card)', color: 'var(--text-main)', borderColor: 'var(--border-medium)', boxShadow: 'var(--shadow-card)' }
            : { color: 'var(--text-muted)' }"
          :class="filter === 'active' ? 'border font-bold' : ''"
        >
          持仓中
        </button>
        <button
          @click="filter = 'closed'"
          class="px-3 py-1.5 rounded-md cursor-pointer transition font-medium"
          :style="filter === 'closed'
            ? { backgroundColor: 'var(--bg-card)', color: 'var(--text-main)', borderColor: 'var(--border-medium)', boxShadow: 'var(--shadow-card)' }
            : { color: 'var(--text-muted)' }"
          :class="filter === 'closed' ? 'border font-bold' : ''"
        >
          已平仓
        </button>
      </div>

      <div
        class="flex items-center space-x-1.5 flex-1 min-w-[180px] max-w-[320px] rounded-lg border px-3 py-1.5"
        style="background-color: var(--bg-input); border-color: var(--border-subtle);"
      >
        <Search class="w-3.5 h-3.5 shrink-0" style="color: var(--text-faint);" />
        <input
          v-model="keyword"
          placeholder="搜索币种 / 策略 / 平仓原因..."
          class="flex-1 bg-transparent text-xs font-mono outline-none min-w-0"
          style="color: var(--text-main);"
        />
      </div>
    </div>

    <!-- Trades Table with Fixed Max-Height (No infinite page stretching) -->
    <div
      class="rounded-xl border shadow-xs transition-colors overflow-hidden flex flex-col"
      style="background-color: var(--bg-card); border-color: var(--border-subtle);"
    >
      <div v-if="trades.length === 0" class="py-12 text-center text-xs font-mono" style="color: var(--text-muted);">
        无匹配交易台账记录
      </div>
      <div v-else class="overflow-x-auto overflow-y-auto max-h-[580px]">
        <table class="w-full text-left text-xs font-mono whitespace-nowrap">
          <thead class="sticky top-0 z-10" style="background-color: var(--bg-card);">
            <tr class="border-b text-[11px] uppercase tracking-wider" style="border-color: var(--border-subtle); color: var(--text-muted);">
              <th class="py-3 px-4 font-bold">标的 / 方向</th>
              <th class="py-3 px-3 font-bold">策略来源</th>
              <th class="py-3 px-3 font-bold">保证金</th>
              <th class="py-3 px-3 font-bold">开仓价 / 时间</th>
              <th class="py-3 px-3 font-bold">平仓价 / 时间</th>
              <th class="py-3 px-3 text-right font-bold">净盈亏 / ROI</th>
              <th class="py-3 px-3 text-center font-bold">时长</th>
              <th class="py-3 px-4 font-bold">状态 / 平仓原因</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="(t, idx) in trades"
              :key="t.id || idx"
              class="border-b last:border-b-0 transition-colors hover:bg-[var(--bg-card-hover)]"
              style="border-color: var(--border-subtle);"
            >
              <td class="py-3 px-4">
                <span class="font-bold text-sm" style="color: var(--text-main);">{{ t.inst }}</span>
                <span
                  class="ml-1.5 px-1.5 py-0.5 rounded text-[10px] font-bold border"
                  :style="{
                    backgroundColor: t.side === '多' ? 'var(--color-up-bg)' : 'var(--color-down-bg)',
                    borderColor: t.side === '多' ? 'var(--color-up-border)' : 'var(--color-down-border)',
                    color: t.side === '多' ? 'var(--color-up)' : 'var(--color-down)'
                  }"
                >
                  {{ t.side }} {{ t.lever || '3x' }}
                </span>
              </td>
              <td class="py-3 px-3">
                <div class="flex flex-col space-y-0.5">
                  <span
                    class="px-2 py-0.5 rounded border text-[11px] w-fit"
                    style="background-color: var(--bg-badge); border-color: var(--border-subtle); color: var(--text-muted);"
                  >
                    {{ clean(t.strategy, '观望') }}
                  </span>
                  <span
                    v-if="t.policy_version || t.policy_hash"
                    class="text-[9px] font-mono px-1 py-0.2 rounded border w-fit text-cyan-400 border-cyan-500/30 bg-cyan-500/10"
                    :title="t.policy_version || t.policy_hash"
                  >
                    #{{ (t.policy_hash || (t.policy_version || '').split('@')[1] || '').substring(0, 8) }}
                  </span>
                </div>
              </td>
              <td class="py-3 px-3 font-bold num-tabular" style="color: var(--text-main);">
                {{ t.margin ? num(t.margin).toFixed(1) + ' U' : '--' }}
              </td>
              <td class="py-3 px-3">
                <span class="num-tabular" style="color: var(--text-main);">{{ formatPx(t.open_px) }}</span>
                <span class="text-[10px] ml-1 num-tabular" style="color: var(--text-faint);">({{ (t.open_time || '--').substring(5, 19) }})</span>
              </td>
              <td class="py-3 px-3">
                <span class="num-tabular" :style="{ color: t.status === 'holding' ? 'var(--color-brand)' : 'var(--text-main)' }">
                  {{ t.status === 'holding' ? '盯盘中' : formatPx(t.close_px) }}
                </span>
                <span class="text-[10px] ml-1 num-tabular" style="color: var(--text-faint);">
                  ({{ t.status === 'holding' ? '--' : (t.close_time || '--').substring(5, 19) }})
                </span>
              </td>
              <td class="py-3 px-3 text-right">
                <span
                  class="font-bold text-sm num-tabular"
                  :style="{ color: getPnl(t) >= 0 ? 'var(--color-up)' : 'var(--color-down)' }"
                >
                  {{ getPnl(t) >= 0 ? '+' : '' }}{{ getPnl(t).toFixed(2) }} U
                </span>
                <span
                  class="text-[10px] ml-1 num-tabular"
                  :style="{ color: getRoi(t) >= 0 ? 'var(--color-up)' : 'var(--color-down)' }"
                >
                  ({{ getRoi(t) >= 0 ? '+' : '' }}{{ getRoi(t).toFixed(1) }}%)
                </span>
              </td>
              <td class="py-3 px-3 text-center num-tabular" style="color: var(--text-muted);">
                {{ clean(t.hold_duration, '--') }}
              </td>
              <td class="py-3 px-4 text-xs" style="color: var(--text-muted);">
                <span
                  class="px-2 py-0.5 rounded text-[10px] font-bold border mr-1"
                  :style="{
                    backgroundColor: t.status === 'holding' ? 'var(--color-brand-bg)' : 'var(--bg-badge)',
                    borderColor: t.status === 'holding' ? 'var(--color-brand-border)' : 'var(--border-subtle)',
                    color: t.status === 'holding' ? 'var(--color-brand)' : 'var(--text-muted)'
                  }"
                >
                  {{ t.status === 'holding' ? '在途' : '已平' }}
                </span>
                <span>{{ clean(t.exit_reason, '持仓中') }}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- Table Footer Summary -->
      <div
        class="px-4 py-2.5 border-t flex items-center justify-between text-[11px] font-mono shrink-0"
        style="border-color: var(--border-subtle); background-color: var(--bg-card-subtle); color: var(--text-faint);"
      >
        <span>已载入 {{ trades.length }} 笔真实撮合成交记录</span>
        <span class="hidden sm:inline">OKX 交易所实盘历史履历</span>
      </div>
    </div>
  </div>
</template>
