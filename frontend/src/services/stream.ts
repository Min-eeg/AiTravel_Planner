// SSE 客户端。
//
// 为什么不用原生 EventSource：EventSource 只支持 GET，无法携带请求体
// （本项目需要 POST 传 TripForm）。因此用 fetch + ReadableStream 手动解析 SSE 帧。
//
// 另一个选择是 @microsoft/fetch-event-source，它封装了重连与解析，
// 但对"一次性流式生成"这个场景，手写约 60 行的解析器更直白，也少一个依赖。

import type { SSEEventMap } from '@/types'

export type SSEHandler = (event: keyof SSEEventMap, payload: unknown) => void

export interface StreamOptions {
  onEvent: SSEHandler
  onError?: (err: Error) => void
  onClose?: () => void
}

/**
 * 发起一次流式请求并逐事件回调。
 * 返回 abort 函数，组件卸载时调用以中断请求。
 */
export function streamTrip(
  form: unknown,
  options: StreamOptions
): () => void {
  const controller = new AbortController()

  ;(async () => {
    try {
      const resp = await fetch('/api/trip/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(form),
        signal: controller.signal
      })

      if (!resp.ok || !resp.body) {
        throw new Error(`请求失败：HTTP ${resp.status}`)
      }

      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      // 循环读取并按 SSE 协议切帧
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })

        // SSE 以空行（\n\n）分隔一帧
        let sepIndex: number
        while ((sepIndex = buffer.indexOf('\n\n')) !== -1) {
          const raw = buffer.slice(0, sepIndex)
          buffer = buffer.slice(sepIndex + 2)

          const parsed = parseFrame(raw)
          if (parsed) {
            // payload 保持 unknown：具体类型由 store 在 switch 中收窄，
            // 这样新增事件类型时无需改动 SSE 解析器
            options.onEvent(parsed.event, parsed.data)
          }
        }
      }

      options.onClose?.()
    } catch (err) {
      // 主动 abort 不算错误
      if ((err as Error).name === 'AbortError') {
        options.onClose?.()
        return
      }
      options.onError?.(err as Error)
    }
  })()

  return () => controller.abort()
}

interface ParsedFrame {
  event: keyof SSEEventMap
  data: unknown
}

/** 解析单个 SSE 帧：event 行 + 多行 data。 */
function parseFrame(raw: string): ParsedFrame | null {
  let event = ''
  const dataLines: string[] = []

  for (const line of raw.split('\n')) {
    if (line.startsWith('event: ')) {
      event = line.slice(7).trim()
    } else if (line.startsWith('data: ')) {
      // 多行 data 需要拼接（后端 JSON 含换行时会拆成多行）
      dataLines.push(line.slice(6))
    }
  }

  if (!event || dataLines.length === 0) return null

  try {
    return { event: event as keyof SSEEventMap, data: JSON.parse(dataLines.join('\n')) }
  } catch {
    return null
  }
}