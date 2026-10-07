<script setup lang="ts">
// 预算构成分解卡：横向条形列表，取代原来的 ECharts 环形图。
//
// 为什么不用饼图/环形图：
// 环形图要用 300px 高度才能放 4 个数字，且"住宿 57%"这类占比
// 用一根条 + 金额反而更直观（D3 社区的共识：类别 ≤5 个时条形优于饼图）。
// 纯 CSS 实现，无图表库依赖，风格与天气卡片完全一致。
//
// 排序：按金额降序，一眼看出"钱主要花在哪"。

import { computed } from 'vue'
import type { Budget } from '@/types'

const props = defineProps<{ budget?: Budget }>()

interface Row {
  label: string
  color: string
  amount: number
  percent: number
}

const rows = computed<Row[]>(() => {
  const b = props.budget
  if (!b) return []

  const items: Array<[string, string, number]> = [
    ['住宿', '#1D9E75', b.total_hotels],
    ['餐饮', '#EF9F27', b.total_meals],
    ['交通', '#7F77DD', b.total_transportation],
    ['门票', '#378ADD', b.total_attractions]
  ]

  const total = b.total || items.reduce((s, [, , v]) => s + v, 0)
  if (total <= 0) return []

  return items
    .map(([label, color, amount]) => ({
      label,
      color,
      amount,
      percent: (amount / total) * 100
    }))
    .sort((x, y) => y.amount - x.amount)
})

const totalText = computed(() => {
  const t = props.budget?.total ?? 0
  return `¥${Math.round(t).toLocaleString()}`
})
</script>

<template>
  <div class="bk">
    <div class="bk-head">
      <span class="bk-label">预算构成</span>
      <span class="bk-total">{{ totalText }}</span>
    </div>

    <div v-if="rows.length" class="bk-rows">
      <div v-for="r in rows" :key="r.label" class="bk-row">
        <span class="bk-name">{{ r.label }}</span>
        <div class="bk-track">
          <div
            class="bk-bar"
            :style="{ width: `${Math.max(r.percent, 1.5)}%`, background: r.color }"
          />
        </div>
        <span class="bk-pct">{{ r.percent.toFixed(0) }}%</span>
        <span class="bk-amount">¥{{ Math.round(r.amount).toLocaleString() }}</span>
      </div>
    </div>
    <p v-else class="bk-empty">暂无预算数据</p>
  </div>
</template>

<style scoped>
.bk {
  padding: 12px 14px;
  border-radius: 10px;
  background: var(--color-background-secondary, #FAFBFD);
  border: 1px solid rgba(0, 0, 0, 0.08);
}
.bk-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 10px;
}
.bk-label {
  font-size: 12.5px;
  font-weight: 500;
  color: #5F5E5A;
}
.bk-total {
  font-size: 15px;
  font-weight: 600;
  color: var(--color-text-primary, #2C2C2A);
  font-variant-numeric: tabular-nums;
}
.bk-rows {
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.bk-row {
  display: grid;
  grid-template-columns: 34px 1fr 34px 62px;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #5F5E5A;
}
.bk-name {
  color: var(--color-text-primary, #2C2C2A);
}
.bk-track {
  height: 8px;
  border-radius: 4px;
  background: rgba(0, 0, 0, 0.06);
  overflow: hidden;
}
.bk-bar {
  height: 100%;
  border-radius: 4px;
  transition: width 0.5s ease;
}
.bk-pct {
  text-align: right;
  color: #888780;
  font-variant-numeric: tabular-nums;
}
.bk-amount {
  text-align: right;
  color: var(--color-text-primary, #2C2C2A);
  font-variant-numeric: tabular-nums;
}
.bk-empty {
  margin: 0;
  font-size: 12px;
  color: #888780;
}
</style>