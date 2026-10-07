// 行程状态管理：把 SSE 事件流映射为可响应的 UI 状态。
//
// 设计要点：
// 1. 事件到达即写入，Vue 响应式自动驱动增量渲染——这是流式体验的核心
// 2. 图表数据（budget/weather）与行程数据解耦存放，
//    收到 chart 事件才填充，避免图表在半成品数据上反复重绘

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { streamTrip } from '@/services/stream'
import type {
  Budget,
  DayPlan,
  DoneEvent,
  TraceStep,
  TripForm,
  WeatherInfo
} from '@/types'

export const useTripStore = defineStore('trip', () => {
  // ===== 状态 =====
  const loading = ref(false)
  const meta = ref<{ city: string; start_date: string; days: number; preferences?: string[] } | null>(null)
  const days = ref<DayPlan[]>([])
  const traces = ref<TraceStep[]>([])
  const budget = ref<Budget | null>(null)
  const weather = ref<WeatherInfo[]>([])
  const summary = ref<DoneEvent | null>(null)
  const error = ref<string>('')
  const degraded = ref(false)
  // 当前展示的行程是否来自历史库（不走 Agent，无执行轨迹）
  const fromHistory = ref(false)

  let abort: (() => void) | null = null

  // ===== 计算属性 =====
  const finished = computed(() => summary.value !== null)
  const generatedDays = computed(() => days.value.length)
  const totalAttractions = computed(() =>
    days.value.reduce((sum, d) => sum + d.attractions.length, 0)
  )

  /** 路线坐标序列，供地图与 ECharts 使用：每天一条，含景点名称 */
  const routeSeries = computed(() =>
    days.value.map((d) => ({
      name: `第${d.day_index + 1}天${d.title ? ` · ${d.title}` : ''}`,
      dayIndex: d.day_index,
      color: d.day_index,
      points: d.attractions
        .filter((a) => a.location)
        .map((a) => ({
          name: a.name,
          lng: a.location!.longitude,
          lat: a.location!.latitude,
          description: a.description,
          imageUrl: a.image_url ?? null,
          visitDuration: a.visit_duration,
          ticketPrice: a.ticket_price
        }))
    }))
  )

  /** 每日开销明细，供堆叠柱状图使用 */
  const costBreakdown = computed(() =>
    days.value.map((d) => ({
      day: `第${d.day_index + 1}天`,
      attraction: d.attractions.reduce((s, a) => s + (a.ticket_price || 0), 0),
      meal: d.meals.reduce((s, m) => s + (m.estimated_cost || 0), 0)
    }))
  )

  // ===== 动作 =====

  function reset() {
    loading.value = false
    meta.value = null
    days.value = []
    traces.value = []
    budget.value = null
    weather.value = []
    summary.value = null
    error.value = ''
    degraded.value = false
    fromHistory.value = false
  }

  function generate(form: TripForm) {
    reset()
    loading.value = true

    abort = streamTrip(form, {
      onEvent(event, payload) {
        switch (event) {
          case 'meta':
            meta.value = payload as typeof meta.value
            break

          case 'trace':
            traces.value.push(payload as TraceStep)
            break

          case 'day': {
            // 增量渲染的核心：一天到达就插进列表，不等 done
            const day = (payload as { day: DayPlan }).day
            const existing = days.value.findIndex((d) => d.day_index === day.day_index)
            if (existing === -1) {
              days.value.push(day)
              days.value.sort((a, b) => a.day_index - b.day_index)
            } else {
              days.value[existing] = day
            }
            break
          }

          case 'patch': {
            // 坐标修正：不重发整天，只改一个点，避免打断用户阅读
            const p = payload as {
              day_index: number
              attraction_index: number
              location: { longitude: number; latitude: number }
            }
            const day = days.value.find((d) => d.day_index === p.day_index)
            if (day && day.attractions[p.attraction_index]) {
              day.attractions[p.attraction_index].location = p.location
              day.attractions[p.attraction_index].location_corrected = true
            }
            break
          }

          case 'chart': {
            const c = payload as {
              budget: Budget
              weather: WeatherInfo[]
              days?: DayPlan[]
            }
            budget.value = c.budget
            weather.value = c.weather
            // chart 在 settle_budget 之后发出，days 含逐日住宿/交通；
            // 覆盖流式阶段先到的 day 事件（当时结算字段还没算出来）
            if (c.days?.length) days.value = c.days
            break
          }

          case 'done': {
            const d = payload as DoneEvent
            summary.value = d
            degraded.value = d.degraded
            loading.value = false
            break
          }

          case 'error': {
            const e = payload as { message: string; degraded: boolean }
            error.value = e.message
            degraded.value = true
            // 不置 loading=false：降级行程还在继续下发，等 done 再收尾
            break
          }
        }
      },
      onError(err) {
        error.value = err.message
        loading.value = false
      },
      onClose() {
        loading.value = false
      }
    })
  }

  function cancel() {
    abort?.()
    abort = null
    loading.value = false
  }

  /**
   * 从历史记录载入一份行程（不经SSE 重新生成）。
   *
   * 复用结果页的渲染逻辑：历史页点「查看」时灌入 store 并跳转 /result，
   * 与刚生成完的行程体验一致，不需要写第二套渲染代码。
   */
  function loadFromPlan(plan: unknown) {
    reset()
    const p = plan as {
      city?: string
      start_date?: string
      days?: DayPlan[]
      weather?: WeatherInfo[]
      budget?: Budget
    }
    const dayList = p.days ?? []

    meta.value = {
      city: p.city ?? '',
      start_date: p.start_date ?? '',
      days: dayList.length,
      preferences: (plan as { preferences?: string[] }).preferences ?? []
    }
    days.value = dayList
    weather.value = p.weather ?? []
    if (p.budget) {
      budget.value = p.budget
    }
    fromHistory.value = true
    // 标记为已结束，避免结果页显示"生成中"
    summary.value = {
      total_duration_ms: 0,
      days_generated: dayList.length,
      attractions_count: dayList.reduce((s, d) => s + d.attractions.length, 0),
      corrected_count: 0,
      degraded: false,
      llm_calls: 0
    }
  }

  return {
    loading,
    meta,
    days,
    traces,
    budget,
    weather,
    summary,
    error,
    degraded,
    finished,
    generatedDays,
    totalAttractions,
    routeSeries,
    costBreakdown,
    reset,
    generate,
    fromHistory,
    cancel,
    loadFromPlan
  }
})