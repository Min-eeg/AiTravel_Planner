// 类型定义 —— 与后端 app/models/schemas.py 严格对应。
// 后端改结构，这里必须同步改，否则 SSE 解析会在运行时炸。

export interface Location {
  longitude: number
  latitude: number
}

export interface Attraction {
  name: string
  address: string
  location: Location | null
  visit_duration: number
  description: string
  category: string
  ticket_price: number
  image_url?: string | null
  location_corrected: boolean
}

export interface Meal {
  type: 'breakfast' | 'lunch' | 'dinner'
  name: string
  description: string
  estimated_cost: number
}

export interface Hotel {
  name: string
  address: string
  price_range: string
  estimated_cost: number
}

export interface DayPlan {
  date: string
  day_index: number
  title: string
  description: string
  transportation: string
  // 市内交通估算，后端 settle_budget 按预算档位写回
  transportation_cost: number
  hotel: Hotel | null
  attractions: Attraction[]
  meals: Meal[]
}

export interface WeatherInfo {
  date: string
  day_weather: string
  night_weather: string
  day_temp: number
  night_temp: number
  // 降水概率 0-100。比温度更能回答"要不要带伞"，高德不提供时为 null
  precipitation_prob: number | null
}

export interface Budget {
  total_attractions: number
  total_hotels: number
  total_meals: number
  total_transportation: number
  total: number
}

export interface TraceStep {
  stage: string
  name: string
  status: 'running' | 'ok' | 'failed' | 'cached'
  duration_ms: number | null
  detail: string
}

export interface TripForm {
  city: string
  start_date: string
  days: number
  preferences: string[]
  budget_level: string
  pace: string
  free_text: string
}

// ============ SSE 事件载荷 ============

export interface MetaEvent {
  city: string
  start_date: string
  days: number
  request_id: string
  preferences: string[]
}

export interface DayEvent {
  day: DayPlan
}

export interface PatchEvent {
  day_index: number
  attraction_index: number
  location: Location
  corrected: boolean
}

export interface ChartEvent {
  budget: Budget
  weather: WeatherInfo[]
  // settle_budget 写回住宿/交通后的完整逐日数据，前端用于覆盖已渲染的天卡片
  days: DayPlan[]
}

export interface DoneEvent {
  total_duration_ms: number
  days_generated: number
  attractions_count: number
  corrected_count: number
  degraded: boolean
  llm_calls: number
}

export interface ErrorEvent {
  message: string
  stage: string
  degraded: boolean
}

// ============ 行程历史 ============

export interface TripSummary {
  id: number
  city: string
  start_date: string
  days_count: number
  preferences: string[]
  total_budget: number
  degraded: boolean
  created_at: string | null
}

export interface TripDetail {
  summary: TripSummary
  plan: unknown
}

export interface SSEEventMap {
  meta: MetaEvent
  trace: TraceStep
  day: DayEvent
  patch: PatchEvent
  chart: ChartEvent
  done: DoneEvent
  error: ErrorEvent
}