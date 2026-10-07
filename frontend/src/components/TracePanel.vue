<script setup lang="ts">
// Agent 执行轨迹面板：把 SSE 的 trace 事件可视化。
//
// 目的有两个：
// 1. 工程价值 —— 让"Agent 到底在干什么"可观测，这是生产级 AI 应用的必要能力
// 2. 演示价值 —— 生成过程中让用户看到进度，而不是一个空转的 loading

import { computed } from 'vue'
import type { TraceStep } from '@/types'

const props = defineProps<{
  traces: TraceStep[]
  /** 历史行程不走 Agent，空态文案要说清"本来就没有轨迹"，而不是"等待执行" */
  history?: boolean
}>()

const iconMap: Record<string, string> = {
  running: '◌',
  ok: '✓',
  failed: '✕',
  cached: '⚡'
}

const sorted = computed(() =>
  [...props.traces].sort((a, b) => (a.duration_ms ?? 0) - (b.duration_ms ?? 0))
)
</script>

<template>
  <div class="trace-panel">
    <div class="trace-head">
      <span class="title">Agent 执行轨迹</span>
      <span class="count">{{ traces.length }} 步</span>
    </div>

    <ul v-if="sorted.length" class="trace-list">
      <li v-for="(t, i) in sorted" :key="i" class="trace-item" :class="t.status">
        <span class="icon">{{ iconMap[t.status] || '·' }}</span>
        <div class="body">
          <div class="name-row">
            <span class="name">{{ t.name }}</span>
            <span v-if="t.duration_ms !== null" class="duration">
              {{ t.duration_ms }}ms
            </span>
          </div>
          <div v-if="t.detail" class="detail">{{ t.detail }}</div>
        </div>
      </li>
    </ul>

    <p v-else class="empty">
      {{ history ? '历史行程 · 不经过 Agent 生成，无执行轨迹' : '等待 Agent 执行…' }}
    </p>
  </div>
</template>

<style scoped>
.trace-panel {
  font-size: 13px;
}
.trace-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--color-border-tertiary, rgba(0, 0, 0, 0.12));
}
.title {
  font-weight: 500;
  font-size: 14px;
}
.count {
  font-size: 12px;
  color: #888780;
}
.trace-list {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 320px;
  overflow-y: auto;
}
.trace-item {
  display: flex;
  gap: 10px;
  padding: 9px 0;
  border-bottom: 1px solid var(--color-border-tertiary, rgba(0, 0, 0, 0.07));
}
.icon {
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  background: #F1EFE8;
  color: #5F5E5A;
}
.trace-item.ok .icon {
  background: #E1F5EE;
  color: #0F6E56;
}
.trace-item.failed .icon {
  background: #FCEBEB;
  color: #A32D2D;
}
.body {
  flex: 1;
  min-width: 0;
}
.name-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}
.name {
  font-weight: 500;
}
.duration {
  flex-shrink: 0;
  font-size: 11px;
  color: #888780;
  font-variant-numeric: tabular-nums;
}
.detail {
  margin-top: 3px;
  font-size: 12px;
  color: #888780;
  line-height: 1.5;
  word-break: break-word;
}
.empty {
  margin: 18px 0;
  text-align: center;
  color: #888780;
  font-size: 13px;
}
</style>