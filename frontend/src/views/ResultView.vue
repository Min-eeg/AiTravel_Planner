<script setup lang="ts">
// 行程结果页：流式渲染的主舞台。
//
// 布局刻意做成"左中右三栏 + 下方行程流"：
//   左：执行轨迹（Agent 在干什么）
//   中：图表（预算/天气/路线）
//   右：汇总
//   下：逐日行程卡片（随 SSE day 事件一张张出现）
//
// 这样安排是因为流式场景下用户注意力是动态的：
// 图表在数据到达时刷新，行程卡片在生成时不断追加，两者互不干扰。

import { computed, ref } from 'vue'
import { useTripStore } from '@/stores/trip'
import { saveTrip } from '@/services/history'
import type { DayPlan } from '@/types'
import BudgetBreakdown from '@/components/BudgetBreakdown.vue'
import RouteMap from '@/components/charts/RouteMap.vue'
import WeatherCards from '@/components/WeatherCards.vue'
import TracePanel from '@/components/TracePanel.vue'

const store = useTripStore()

const saving = ref(false)
const saveMsg = ref('')

/** 保存行程到历史。重复内容后端会去重。 */
async function handleSave() {
  if (!store.days.length) return
  saving.value = true
  saveMsg.value = ''
  try {
    const res = await saveTrip(
      {
        city: store.meta?.city,
        start_date: store.meta?.start_date,
        days: store.days,
        weather: store.weather,
        budget: store.budget
      },
      store.meta?.preferences ?? []
    )
    saveMsg.value = res.duplicate ? '已保存过' : '保存成功'
  } catch (err) {
    saveMsg.value = (err as Error).message || '保存失败'
  } finally {
    saving.value = false
  }
}

const progress = computed(() => {
  if (!store.meta) return 0
  return Math.min(100, Math.round((store.generatedDays / store.meta.days) * 100))
})

/**
 * 景点全局序号：第1天 1、2，第2天 3、4……
 * 与地图标记上的序号一致，用户看列表时能直接对照地图上的数字。
 * 键为 `${day_index}-${当天内下标}`。
 */
const globalIndex = computed(() => {
  const result: Record<string, number> = {}
  let seq = 0
  for (const day of store.days) {
    for (let i = 0; i < day.attractions.length; i += 1) {
      seq += 1
      result[`${day.day_index}-${i}`] = seq
    }
  }
  return result
})

/** 从未发起过生成（无会话元数据）且不在生成中 —— 结果页整体空态 */
const isEmpty = computed(() => !store.meta && !store.loading && !store.days.length)

const mealLabel: Record<string, string> = {
  breakfast: '早餐',
  lunch: '午餐',
  dinner: '晚餐'
}

/** 按天索引取当天天气，用于行程卡片上的出行提示 */
function weatherOf(dayIndex: number) {
  return store.weather[dayIndex] ?? null
}

function fmtDur(ms: number | null): string {
  if (ms === null) return '—'
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`
}

/** 当天总花费 = 门票 + 三餐 + 住宿 + 交通（后端已写回逐日），天头部徽章用 */
function dayCost(day: DayPlan): number {
  const tickets = day.attractions.reduce((s, a) => s + (a.ticket_price || 0), 0)
  const meals = day.meals.reduce((s, m) => s + (m.estimated_cost || 0), 0)
  const hotel = day.hotel?.estimated_cost ?? 0
  return Math.round(tickets + meals + hotel + (day.transportation_cost || 0))
}
</script>

<template>
  <div class="result-page">
    <!-- 空态：打开页面但从未生成过行程，不给一堆空面板 -->
    <div v-if="isEmpty" class="empty-state">
      <div class="empty-icon">🗺️</div>
      <h2 class="empty-title">还未生成行程</h2>
      <RouterLink to="/" class="empty-btn">去规划 →</RouterLink>
    </div>

    <template v-else>
    <!-- 顶部状态条 -->
    <header class="status-bar">
      <div class="status-left">
        <span class="city">{{ store.meta?.city || '规划中' }}</span>
        <span v-if="store.meta" class="range">
          {{ store.meta.start_date }} 起 · 共 {{ store.meta.days }} 天
        </span>
      </div>

      <div class="status-right">
        <div v-if="store.loading" class="progress-wrap">
          <div class="progress-track">
            <div class="progress-fill" :style="{ width: `${progress}%` }" />
          </div>
          <span class="progress-text">
            已生成 {{ store.generatedDays }}/{{ store.meta?.days ?? 0 }} 天
          </span>
        </div>
        <button v-if="store.loading" class="btn-ghost" @click="store.cancel()">
          停止生成
        </button>
        <template v-else-if="store.days.length">
          <span v-if="saveMsg" class="save-msg">{{ saveMsg }}</span>
          <button class="btn-ghost" :disabled="saving" @click="handleSave">
            {{ saving ? '保存中…' : '保存行程' }}
          </button>
        </template>
      </div>
    </header>

    <!-- 降级提示：AI 调用失败但仍返回了可用行程 -->
    <div v-if="store.degraded" class="alert">
      <strong>已降级为模板行程</strong>
      <span v-if="store.error">：{{ store.error }}</span>
      <span v-else>AI 生成未完整完成，以下为保底方案。</span>
    </div>

    <!-- 主体三栏 -->
    <section class="grid">
      <aside class="panel trace-col">
        <TracePanel :traces="store.traces" :history="store.fromHistory" />
      </aside>

      <div class="panel chart-col">
        <!-- 天气卡片行：直接给行动建议，取代无趋势的折线图 -->
        <WeatherCards :weather="store.weather" />

        <BudgetBreakdown :budget="store.budget ?? undefined" />

        <h3 class="panel-title mt">路线分布</h3>
        <RouteMap :routes="store.routeSeries" />
      </div>

      <aside class="panel summary-col">
        <h3 class="panel-title">生成汇总</h3>
        <dl v-if="store.summary" class="summary-list">
          <div class="row">
            <dt>总耗时</dt>
            <dd>{{ fmtDur(store.summary.total_duration_ms) }}</dd>
          </div>
          <div class="row">
            <dt>生成天数</dt>
            <dd>{{ store.summary.days_generated }}</dd>
          </div>
          <div class="row">
            <dt>景点总数</dt>
            <dd>{{ store.summary.attractions_count }}</dd>
          </div>
          <div class="row">
            <dt>坐标校正</dt>
            <dd>{{ store.summary.corrected_count }}</dd>
          </div>
          <div class="row">
            <dt>LLM 调用</dt>
            <dd>{{ store.summary.llm_calls }} 次</dd>
          </div>
          <div v-if="store.budget" class="row total">
            <dt>预算合计</dt>
            <dd>¥{{ store.budget.total.toFixed(0) }}</dd>
          </div>
        </dl>
        <p v-else class="empty">生成完成后显示统计数据…</p>
      </aside>
    </section>

    <!-- 行程流：day 事件到达即追加 -->
    <section class="itinerary">
      <h2 class="section-title">
        逐日行程
        <span v-if="store.loading" class="streaming-tag">生成中…</span>
      </h2>

      <div v-if="store.days.length" class="day-list">
        <article
          v-for="day in store.days"
          :key="day.day_index"
          class="day-card"
        >
          <header class="day-head">
            <div class="day-badge">第 {{ day.day_index + 1 }} 天</div>
            <div class="day-meta">
              <h3 class="day-title">{{ day.title || day.date }}</h3>
              <span class="day-date">{{ day.date }}</span>
            </div>
            <!-- 当天花费与天气：钱数和带伞提示都在天头部，扫一眼即得 -->
            <div class="day-side">
              <span class="day-cost" :title="'门票+三餐合计'">¥{{ dayCost(day) }}</span>
              <div v-if="weatherOf(day.day_index)" class="day-weather">
                <span class="w-icon">{{ weatherOf(day.day_index)!.day_weather }}</span>
                <span class="w-temp">
                  {{ weatherOf(day.day_index)!.day_temp }}° / {{ weatherOf(day.day_index)!.night_temp }}°
                </span>
                <span
                  v-if="(weatherOf(day.day_index)!.precipitation_prob ?? 0) >= 30"
                  class="w-rain"
                  title="降水概率较高，建议带伞"
                >
                  降水 {{ weatherOf(day.day_index)!.precipitation_prob }}%
                </span>
              </div>
            </div>
          </header>

          <p v-if="day.description" class="day-desc">{{ day.description }}</p>

          <div class="attractions">
            <div
              v-for="(a, i) in day.attractions"
              :key="i"
              class="attraction"
            >
              <div class="idx">{{ globalIndex[`${day.day_index}-${i}`] }}</div>
              <!-- 景点真实照片，来自高德 POI 接口；无图时不占位 -->
              <div
                v-if="a.image_url"
                class="a-thumb"
                :style="{ backgroundImage: `url(${a.image_url})` }"
                :title="a.name"
              />
              <div class="a-body">
                <div class="a-head">
                  <span class="a-name">{{ a.name }}</span>
                  <span v-if="a.ticket_price > 0" class="a-ticket">
                    ¥{{ a.ticket_price }}
                  </span>
                  <!-- 标记哪些坐标被真实 POI 覆盖过，体现"不信任 LLM 输出" -->
                  <span v-if="a.location_corrected" class="a-corrected" title="坐标已用真实 POI 数据校正">
                    已校准
                  </span>
                </div>
                <p v-if="a.description" class="a-desc">{{ a.description }}</p>
                <span v-if="a.category" class="a-cat">{{ a.category }}</span>
              </div>
            </div>
          </div>

          <div class="meals">
            <span
              v-for="m in day.meals"
              :key="m.type"
              class="meal"
            >
              <span class="meal-type">{{ mealLabel[m.type] || m.type }}</span>
              <span class="meal-name">{{ m.name }}</span>
              <span v-if="m.estimated_cost > 0" class="meal-cost">
                ¥{{ m.estimated_cost }}
              </span>
            </span>
          </div>

          <!-- 住宿/交通明细：金额有来源（settle_budget 按档位写回），不编造酒店名 -->
          <div class="extras">
            <div class="extra-row">
              <span class="extra-label">🚕 市内交通</span>
              <span class="extra-note">打车 + 地铁估算</span>
              <span class="extra-amount">¥{{ day.transportation_cost }}</span>
            </div>
            <div v-if="day.hotel" class="extra-row">
              <span class="extra-label">🏨 住宿</span>
              <span class="extra-note">{{ day.hotel.name }} · {{ day.hotel.price_range }}</span>
              <span class="extra-amount">¥{{ day.hotel.estimated_cost }}</span>
            </div>
            <div v-else-if="day.day_index > 0" class="extra-row muted">
              <span class="extra-label">🧳 返程日</span>
              <span class="extra-note">行程结束，无住宿安排</span>
            </div>
          </div>
        </article>

        <!-- 占位卡片：提示还有几天在生成中，避免页面看起来已经结束 -->
        <div
          v-if="store.loading && store.generatedDays < (store.meta?.days ?? 0)"
          class="day-card skeleton"
        >
          <div class="skeleton-line w40" />
          <div class="skeleton-line w80" />
          <div class="skeleton-line w60" />
          <span class="skeleton-tip">
            正在生成第 {{ store.generatedDays + 1 }} 天…
          </span>
        </div>
      </div>

      <p v-else-if="store.loading" class="empty">正在规划行程，请稍候…</p>
      <p v-else class="empty">尚未生成行程</p>
    </section>
    </template>
  </div>
</template>

<style scoped>
.result-page {
  max-width: 1400px;
  margin: 0 auto;
  padding: 20px 24px 60px;
}
.empty-state {
  max-width: 420px;
  margin: 90px auto 0;
  padding: 44px 32px;
  text-align: center;
  border-radius: 14px;
  border: 1px dashed var(--color-border-secondary, rgba(0, 0, 0, 0.18));
  background: var(--color-background-secondary, #FAFBFD);
}
.empty-icon {
  font-size: 40px;
  line-height: 1;
}
.empty-title {
  margin: 16px 0 24px;
  font-size: 18px;
  font-weight: 500;
  color: var(--color-text-primary, #2C2C2A);
}
.empty-btn {
  display: inline-block;
  padding: 9px 22px;
  font-size: 13.5px;
  text-decoration: none;
  border-radius: 8px;
  background: #378ADD;
  color: #fff;
}
.empty-btn:hover {
  background: #185FA5;
}
.status-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--color-border-tertiary, rgba(0, 0, 0, 0.12));
  flex-wrap: wrap;
}
.status-left {
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.city {
  font-size: 20px;
  font-weight: 500;
}
.range {
  font-size: 13px;
  color: #888780;
}
.status-right {
  display: flex;
  align-items: center;
  gap: 12px;
}
.progress-wrap {
  display: flex;
  align-items: center;
  gap: 10px;
}
.progress-track {
  width: 180px;
  height: 5px;
  border-radius: 3px;
  background: rgba(0, 0, 0, 0.08);
  overflow: hidden;
}
.progress-fill {
  height: 100%;
  background: #378ADD;
  border-radius: 3px;
  transition: width 0.4s ease;
}
.progress-text {
  font-size: 12px;
  color: #888780;
  font-variant-numeric: tabular-nums;
}
.btn-ghost {
  padding: 6px 14px;
  font-size: 13px;
  border: 1px solid var(--color-border-secondary, rgba(0, 0, 0, 0.2));
  background: transparent;
  border-radius: 6px;
  cursor: pointer;
  color: inherit;
}
.btn-ghost:hover {
  background: rgba(0, 0, 0, 0.04);
}
.btn-ghost:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.save-msg {
  font-size: 12px;
  color: #0F6E56;
}
.alert {
  margin-top: 14px;
  padding: 10px 14px;
  border-radius: 8px;
  background: #FAEEDA;
  border: 1px solid rgba(186, 117, 23, 0.3);
  font-size: 13px;
  color: #633806;
  line-height: 1.6;
}
.grid {
  display: grid;
  grid-template-columns: 260px 1fr 260px;
  gap: 16px;
  margin-top: 18px;
  align-items: start;
}
@media (max-width: 1100px) {
  .grid {
    grid-template-columns: 1fr;
  }
}
.panel {
  padding: 16px;
  border-radius: 12px;
  border: 1px solid var(--color-border-tertiary, rgba(0, 0, 0, 0.1));
  background: var(--color-background-secondary, rgba(127, 127, 127, 0.04));
}
.panel-title {
  margin: 0 0 12px;
  font-size: 14px;
  font-weight: 500;
}
.panel-title.mt {
  margin-top: 24px;
}
.summary-list {
  margin: 0;
}
.row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  padding: 8px 0;
  border-bottom: 1px solid var(--color-border-tertiary, rgba(0, 0, 0, 0.07));
  font-size: 13px;
}
.row dt {
  color: #888780;
}
.row dd {
  margin: 0;
  font-variant-numeric: tabular-nums;
}
.row.total dt,
.row.total dd {
  font-weight: 500;
  color: #2C2C2A;
}
.itinerary {
  margin-top: 28px;
}
.section-title {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 16px;
  font-weight: 500;
  margin: 0 0 14px;
}
.streaming-tag {
  font-size: 12px;
  font-weight: 400;
  color: #185FA5;
  padding: 2px 8px;
  border-radius: 10px;
  background: #E6F1FB;
}
.day-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.day-card {
  padding: 18px 20px;
  border-radius: 12px;
  border: 1px solid var(--color-border-tertiary, rgba(0, 0, 0, 0.1));
  background: var(--color-background-primary, #fff);
  animation: slide-in 0.35s ease;
}
@keyframes slide-in {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}
@media (prefers-reduced-motion: reduce) {
  .day-card {
    animation: none;
  }
}
.day-head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
}
.day-badge {
  flex-shrink: 0;
  padding: 4px 10px;
  font-size: 12px;
  border-radius: 6px;
  background: #E6F1FB;
  color: #0C447C;
}
.day-meta {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}
.day-title {
  margin: 0;
  font-size: 15px;
  font-weight: 500;
}
.day-date {
  font-size: 12px;
  color: #888780;
  font-variant-numeric: tabular-nums;
}
.day-side {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 12px;
}
.day-cost {
  padding: 3px 10px;
  border-radius: 8px;
  background: #FDF3E7;
  color: #8A5A0B;
  font-size: 13px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  cursor: default;
}
.day-weather {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #5F5E5A;
}
.w-icon {
  padding: 2px 8px;
  border-radius: 6px;
  background: rgba(127, 127, 127, 0.1);
}
.w-temp {
  font-variant-numeric: tabular-nums;
}
.w-rain {
  padding: 2px 8px;
  border-radius: 6px;
  background: #E6F1FB;
  color: #0C447C;
}
.day-desc {
  margin: 0 0 14px;
  font-size: 13px;
  color: #5F5E5A;
  line-height: 1.65;
}
.attractions {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.attraction {
  display: flex;
  gap: 10px;
}
.a-thumb {
  flex-shrink: 0;
  width: 68px;
  height: 68px;
  border-radius: 8px;
  background-size: cover;
  background-position: center;
  background-color: var(--color-background-secondary, #f1efe8);
  cursor: pointer;
}
.idx {
  flex-shrink: 0;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: #f1efe8;
  color: #444441;
  font-size: 11px;
  font-weight: 500;
  display: flex;
  align-items: center;
  justify-content: center;
  /* 与缩略图（68px）和标题首行对齐 */
  margin-top: 2px;
}
.a-body {
  flex: 1;
  min-width: 0;
}
.a-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}
.a-name {
  font-weight: 500;
  font-size: 14px;
}
.a-ticket {
  font-size: 12px;
  color: #BA7517;
  font-variant-numeric: tabular-nums;
}
.a-corrected {
  font-size: 11px;
  color: #0F6E56;
  padding: 1px 6px;
  border-radius: 4px;
  background: #E1F5EE;
}
.a-desc {
  margin: 3px 0 0;
  font-size: 13px;
  color: #5F5E5A;
  line-height: 1.6;
}
.a-cat {
  display: inline-block;
  margin-top: 4px;
  font-size: 11px;
  color: #888780;
  padding: 1px 6px;
  border: 1px solid var(--color-border-tertiary, rgba(0, 0, 0, 0.12));
  border-radius: 4px;
}
.meals {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--color-border-tertiary, rgba(0, 0, 0, 0.07));
}
.meal {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  font-size: 12px;
  padding: 4px 10px;
  border-radius: 6px;
  background: rgba(127, 127, 127, 0.07);
}
.meal-type {
  color: #888780;
}
.meal-cost {
  color: #BA7517;
}
/* 住宿/交通明细：label + 说明 + 金额三列，金额右对齐便于纵向对比 */
.extras {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--color-border-tertiary, rgba(0, 0, 0, 0.08));
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.extra-row {
  display: flex;
  align-items: baseline;
  gap: 10px;
  font-size: 12.5px;
  color: var(--color-text-primary, #2C2C2A);
}
.extra-label {
  flex-shrink: 0;
}
.extra-note {
  color: #888780;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}
.extra-amount {
  flex-shrink: 0;
  font-weight: 500;
  color: #BA7517;
  font-variant-numeric: tabular-nums;
}
.extra-row.muted .extra-label,
.extra-row.muted .extra-note {
  color: #A9A79E;
}
.skeleton {
  border-style: dashed;
  background: transparent;
}
.skeleton-line {
  height: 10px;
  border-radius: 5px;
  background: rgba(127, 127, 127, 0.12);
  margin-bottom: 10px;
  animation: pulse 1.4s ease-in-out infinite;
}
.skeleton-line.w40 { width: 40%; }
.skeleton-line.w80 { width: 80%; }
.skeleton-line.w60 { width: 60%; }
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.45; }
}
@media (prefers-reduced-motion: reduce) {
  .skeleton-line { animation: none; }
}
.skeleton-tip {
  display: block;
  margin-top: 8px;
  font-size: 12px;
  color: #888780;
}
.empty {
  text-align: center;
  color: #888780;
  font-size: 13px;
  padding: 30px 0;
}
</style>