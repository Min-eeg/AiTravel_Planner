// 行程历史 API。
//
// 用原生 fetch 而非 axios：本项目只有 4 个历史接口，
// 为此引入 axios 不划算（axios 约 13KB gzip）。
// SSE 那边本来就得手写 fetch + ReadableStream，统一用 fetch 更一致。

import type { TripDetail, TripSummary } from '@/types'

const BASE = import.meta.env.VITE_API_BASE_URL || ''

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers }
  })
  if (!resp.ok) {
    // 尽量提取后端的错误信息，便于定位 404/400 的原因
    let detail = `HTTP ${resp.status}`
    try {
      const body = await resp.json()
      detail = body.detail ?? detail
    } catch {
      // 响应体不是 JSON 时沿用状态码
    }
    throw new Error(detail)
  }
  return (await resp.json()) as T
}

/** 保存行程到历史。重复保存时后端返回已有记录。 */
export function saveTrip(
  plan: unknown,
  preferences: string[] = []
): Promise<{ trip_id: number; duplicate: boolean; message: string }> {
  return request('/api/trips', {
    method: 'POST',
    body: JSON.stringify({ plan, preferences })
  })
}

/** 历史列表。 */
export async function fetchTrips(): Promise<TripSummary[]> {
  const data = await request<{ items: TripSummary[] }>('/api/trips')
  return data.items ?? []
}

/** 行程详情。 */
export function fetchTripDetail(id: number): Promise<TripDetail> {
  return request(`/api/trips/${id}`)
}

/** 删除行程。 */
export function deleteTrip(id: number): Promise<{ success: boolean }> {
  return request(`/api/trips/${id}`, { method: 'DELETE' })
}