<script setup lang="ts">
// 行程历史页：展示已保存的行程，支持查看与删除。

import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { deleteTrip, fetchTripDetail, fetchTrips } from '@/services/history'
import { useTripStore } from '@/stores/trip'
import type { TripSummary } from '@/types'

const router = useRouter()
const store = useTripStore()

const trips = ref<TripSummary[]>([])
const loading = ref(false)
const errorMsg = ref('')

async function load() {
  loading.value = true
  errorMsg.value = ''
  try {
    trips.value = await fetchTrips()
  } catch (err) {
    errorMsg.value = (err as Error).message || '加载失败'
  } finally {
    loading.value = false
  }
}

async function open(trip: TripSummary) {
  try {
    const detail = await fetchTripDetail(trip.id)
    // 把历史行程灌进 store，复用结果页的渲染逻辑
    store.reset()
    store.loadFromPlan(detail.plan as never)
    router.push('/result')
  } catch (err) {
    errorMsg.value = (err as Error).message || '打开失败'
  }
}

async function remove(trip: TripSummary) {
  if (!confirm(`确定删除「${trip.city} ${trip.start_date}」的行程吗？`)) return
  try {
    await deleteTrip(trip.id)
    trips.value = trips.value.filter((t) => t.id !== trip.id)
  } catch (err) {
    errorMsg.value = (err as Error).message || '删除失败'
  }
}

function fmtDate(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso.slice(0, 10) : d.toLocaleDateString('zh-CN')
}

onMounted(load)
</script>

<template>
  <div class="history-page">
    <header class="head">
      <h1 class="title">我的行程</h1>
      <button class="refresh" :disabled="loading" @click="load">
        {{ loading ? '加载中…' : '刷新' }}
      </button>
    </header>

    <p v-if="errorMsg" class="error">{{ errorMsg }}</p>

    <div v-if="trips.length" class="trip-list">
      <article v-for="t in trips" :key="t.id" class="trip-card">
        <div class="main">
          <div class="title-row">
            <span class="city">{{ t.city }}</span>
            <span class="days">{{ t.days_count }} 天</span>
            <span v-if="t.degraded" class="tag-degraded" title="该行程由降级兜底链路生成">
              模板
            </span>
          </div>
          <div class="meta">
            <span>{{ t.start_date }}</span>
            <span v-if="t.total_budget > 0">预算 ¥{{ t.total_budget }}</span>
            <span>{{ fmtDate(t.created_at) }} 保存</span>
          </div>
          <div v-if="t.preferences.length" class="prefs">
            <span v-for="p in t.preferences" :key="p" class="pref">{{ p }}</span>
          </div>
        </div>
        <div class="actions">
          <button class="btn-primary" @click="open(t)">查看</button>
          <button class="btn-danger" @click="remove(t)">删除</button>
        </div>
      </article>
    </div>

    <div v-else-if="!loading" class="empty">
      <p>还没有保存的行程</p>
      <RouterLink to="/" class="link">去规划一段 →</RouterLink>
    </div>
  </div>
</template>

<style scoped>
.history-page {
  max-width: 860px;
  margin: 0 auto;
  padding: 28px 24px 60px;
}
.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
}
.title {
  margin: 0;
  font-size: 20px;
  font-weight: 500;
}
.refresh {
  padding: 6px 14px;
  font-size: 13px;
  font-family: inherit;
  color: inherit;
  background: transparent;
  border: 1px solid var(--color-border-secondary, rgba(0, 0, 0, 0.18));
  border-radius: 6px;
  cursor: pointer;
}
.refresh:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.error {
  padding: 10px 14px;
  margin-bottom: 14px;
  border-radius: 8px;
  background: #FCEBEB;
  color: #791F1F;
  font-size: 13px;
}
.trip-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.trip-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 16px 18px;
  border-radius: 12px;
  border: 1px solid var(--color-border-tertiary, rgba(0, 0, 0, 0.1));
  background: var(--color-background-primary, #fff);
}
.main {
  flex: 1;
  min-width: 0;
}
.title-row {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.city {
  font-size: 16px;
  font-weight: 500;
}
.days {
  font-size: 12px;
  color: #888780;
}
.tag-degraded {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  background: #FAEEDA;
  color: #633806;
}
.meta {
  display: flex;
  gap: 14px;
  margin-top: 6px;
  font-size: 12px;
  color: #888780;
  flex-wrap: wrap;
}
.prefs {
  display: flex;
  gap: 6px;
  margin-top: 8px;
  flex-wrap: wrap;
}
.pref {
  font-size: 11px;
  padding: 1px 7px;
  border-radius: 4px;
  background: rgba(127, 127, 127, 0.1);
  color: #5F5E5A;
}
.actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}
.btn-primary,
.btn-danger {
  padding: 6px 14px;
  font-size: 13px;
  font-family: inherit;
  border-radius: 6px;
  cursor: pointer;
  border: 1px solid transparent;
}
.btn-primary {
  background: #378ADD;
  color: #fff;
}
.btn-primary:hover {
  background: #185FA5;
}
.btn-danger {
  background: transparent;
  border-color: var(--color-border-secondary, rgba(0, 0, 0, 0.18));
  color: #A32D2D;
}
.btn-danger:hover {
  background: #FCEBEB;
}
.empty {
  text-align: center;
  padding: 60px 0;
  color: #888780;
}
.empty p {
  margin: 0 0 10px;
  font-size: 14px;
}
.link {
  font-size: 14px;
  color: #185FA5;
  text-decoration: none;
}
</style>