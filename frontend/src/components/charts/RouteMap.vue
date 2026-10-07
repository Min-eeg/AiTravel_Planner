<script setup lang="ts">
// 行程路线图：高德地图 JS API + 景点标记 + 逐日连线。
//
// 实现要点（对照高德 JS API 文档，踩过的坑都标在注释里）：
// 1. position 必须是 [经度, 纬度]——与直觉相反，写成 [纬度, 经度] 会定位到南半球某处
// 2. setFitView(markers) 要传标记数组，否则视野不会贴合数据，底图看起来是空的
// 3. 不要设置自定义 mapStyle，JS API 的样式 ID 与 Web 端不通用，错误 ID 会导致瓦片不加载
// 4. viewMode 用 '2D'：瓦片是 <img> 且支持 CORS，便于后续导出图片
//
// 组件卸载时必须 destroy()，否则切页会残留 canvas 与事件监听。

import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import AMapLoader from '@amap/amap-jsapi-loader'

interface RoutePoint {
  name: string
  lng: number
  lat: number
  description?: string
  imageUrl?: string | null
  visitDuration?: number
  ticketPrice?: number
}

interface RouteDay {
  name: string
  dayIndex: number
  points: RoutePoint[]
}

const props = defineProps<{ routes: RouteDay[] }>()

const container = ref<HTMLDivElement>()
const status = ref<'loading' | 'ready' | 'failed'>('loading')

let AMap: any = null
let map: any = null
let loaded = false

/** 加载高德 SDK，同一页面只加载一次 */
async function loadAmap(): Promise<boolean> {
  if (loaded && AMap) return true

  const key = import.meta.env.VITE_AMAP_WEB_KEY
  if (!key) {
    console.warn('[RouteMap] 未配置 VITE_AMAP_WEB_KEY')
    return false
  }

  try {
    AMap = await AMapLoader.load({
      key,
      version: '2.0',
      plugins: ['AMap.Marker', 'AMap.Polyline', 'AMap.InfoWindow', 'AMap.CircleMarker']
    })
    loaded = true
    return true
  } catch (err) {
    console.error('[RouteMap] 高德地图加载失败', err)
    return false
  }
}

async function initMap() {
  if (!container.value) return
  status.value = 'loading'

  const ok = await loadAmap()
  if (!ok || !container.value) {
    status.value = 'failed'
    return
  }

  map = new AMap.Map(container.value, {
    zoom: 12,
    center: [120.155, 30.274], // 默认杭州；下方 setFitView 会自动修正
    viewMode: '2D'
  })

  map.on('complete', () => {
    status.value = 'ready'
    draw()
  })

  // 兜底：8 秒后无论瓦片是否加载完都渲染标记，避免卡在"加载中"
  setTimeout(() => {
    if (status.value === 'loading') {
      status.value = 'ready'
      draw()
    }
  }, 8000)
}

function draw() {
  if (!map || !AMap) return
  map.clearMap()

  const spots: Array<{
    name: string
    lng: number
    lat: number
    description: string
    dayName: string
    dayIndex: number
    imageUrl: string | null
    visitDuration: number
    ticketPrice: number
    /** 全局连续序号：第1天 1,2；第2天 3,4（与逐日行程列表一致） */
    seq: number
    /** 天内序号，用于 zIndex 排序（每天首站置顶） */
    daySeq: number
  }> = []

  let global = 0
  for (const day of props.routes) {
    let daySeq = 0
    for (const p of day.points) {
      daySeq += 1
      global += 1
      spots.push({
        name: p.name,
        lng: p.lng,
        lat: p.lat,
        description: p.description || '',
        dayName: day.name,
        dayIndex: day.dayIndex,
        imageUrl: p.imageUrl || null,
        visitDuration: p.visitDuration ?? 0,
        ticketPrice: p.ticketPrice ?? 0,
        seq: global,
        daySeq
      })
    }
  }

  if (!spots.length) return

  const markers: any[] = spots.map((s) => {
    const color = dayColor(s.dayIndex)
    // 有图：照片缩略图 + 序号徽章（旅行 App 通用样式）；
    // 无图：退回彩色序号圆点，保证标记始终可辨
    const content = s.imageUrl
      ? `<div class="rm-marker-photo" style="--dot:${color}">
           <img src="${s.imageUrl}" alt="${escapeHtml(s.name)}"
                referrerpolicy="no-referrer" loading="lazy" />
           <span class="rm-marker-badge">${s.seq}</span>
         </div>`
      : `<div class="rm-marker" style="--dot:${color}">${s.seq}</div>`

    const marker = new AMap.Marker({
      // 注意：高德的 position 是 [经度, 纬度]
      position: [s.lng, s.lat],
      title: s.name,
      // 同一天的首站置顶，避免被后续标记压住看不清序号
      zIndex: 100 - s.daySeq,
      anchor: 'bottom-center', // 照片气泡立在景点正上方，底部尖端指向真实位置
      // 全局连续序号：第1天 1,2；第2天 3,4
      content
    })

    const infoWindow = new AMap.InfoWindow({
      offset: new AMap.Pixel(0, -14),
      maxWidth: 280,
      content: `
        <div class="rm-info">
          ${
            s.imageUrl
              ? `<img class="rm-info-img" src="${s.imageUrl}" alt="${escapeHtml(s.name)}"
                   referrerpolicy="no-referrer" loading="lazy" />`
              : ''
          }
          <div class="rm-info-body">
            <div class="rm-info-name">
              <span class="rm-info-seq" style="--dot:${color}">${s.seq}</span>${escapeHtml(s.name)}
            </div>
            <div class="rm-info-day">${escapeHtml(s.dayName)}</div>
            ${
              s.visitDuration
                ? `<div class="rm-info-meta">建议游览 ${s.visitDuration} 分钟${
                    s.ticketPrice ? ` · 门票 ¥${s.ticketPrice}` : ''
                  }</div>`
                : ''
            }
            ${s.description ? `<div class="rm-info-desc">${escapeHtml(s.description)}</div>` : ''}
          </div>
        </div>`
    })

    marker.on('click', () => infoWindow.open(map, [s.lng, s.lat]))
    return marker
  })

  map.add(markers)

  // 自动缩放到所有标记范围—— 必须传 markers 数组，否则视野不对
  map.setFitView(markers, false, [70, 70, 100, 70], 16)

  // 绘制线路：每天一条独立折线，不跨天连
  for (const day of props.routes) {
    if (day.points.length < 2) continue
    const color = dayColor(day.dayIndex)

    // 先画一条半透明粗底线做「halo」，让线路在浅色地图上更醒目
    map.add(
      new AMap.Polyline({
        path: day.points.map((p) => [p.lng, p.lat]),
        strokeColor: '#ffffff',
        strokeWeight: 9,
        strokeOpacity: 0.9,
        strokeStyle: 'solid',
        lineJoin: 'round',
        lineCap: 'round',
        zIndex: 50
      })
    )

    map.add(
      new AMap.Polyline({
        path: day.points.map((p) => [p.lng, p.lat]),
        strokeColor: color,
        // 主线 5.5px：配合下方白色 halo 在浅色底图上足够醒目
        strokeWeight: 5.5,
        strokeOpacity: 0.95,
        strokeStyle: 'solid',
        lineJoin: 'round',
        lineCap: 'round',
        showDir: true, // 行进方向箭头
        zIndex: 51
      })
    )

    // 每天的起点额外标一个圆环，和序号区分开
    const first = day.points[0]
    map.add(
      new AMap.CircleMarker({
        center: [first.lng, first.lat],
        radius: 15,
        strokeColor: color,
        strokeWeight: 2,
        strokeOpacity: 0.9,
        fillColor: '#ffffff',
        fillOpacity: 0.95,
        zIndex: 49
      })
    )
  }
}

/** 每天一个固定颜色，与 ECharts 图表配色保持一致 */
function dayColor(dayIndex: number): string {
  const palette = ['#378ADD', '#1D9E75', '#D4537E', '#BA7517', '#7F77DD', '#639922']
  return palette[dayIndex % palette.length]
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c] as string)
  )
}

onMounted(async () => {
  await initMap()
  if (status.value === 'ready') draw()
})

onBeforeUnmount(() => {
  // 不销毁会残留 canvas 与事件监听，切页后内存持续增长
  map?.destroy?.()
  map = null
  AMap = null
  loaded = false
})

watch(
  () => props.routes,
  () => {
    if (status.value === 'ready') draw()
  },
  { deep: true }
)
</script>

<template>
  <div class="rm-wrap">
    <div ref="container" class="rm-canvas" />
    <div v-if="status === 'loading'" class="rm-tip">地图加载中…</div>
    <div v-else-if="status === 'failed'" class="rm-tip error">
      地图加载失败，请检查 frontend/.env 中的 VITE_AMAP_WEB_KEY
    </div>
  </div>
</template>

<style scoped>
.rm-wrap {
  position: relative;
  width: 100%;
  /* 高德初始化时会读取容器尺寸算中心点，高度为 0 会导致定位异常，
     因此给足最小高度 */
  min-height: 400px;
  height: 400px;
  border-radius: 8px;
  overflow: hidden;
}
.rm-canvas {
  width: 100%;
  height: 100%;
}
.rm-tip {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  color: #888780;
  background: rgba(255, 255, 255, 0.7);
  pointer-events: none;
}
.rm-tip.error {
  color: #791f1f;
  background: rgba(252, 235, 235, 0.92);
}
</style>

<style>
/* 高德 Marker / InfoWindow 的内容渲染在组件作用域之外，需用全局样式 */
.rm-marker {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: var(--dot, #378add);
  /* 白色描边 + 阴影，让标记在浅色地图和线路之上都清晰可辨 */
  border: 2.5px solid #fff;
  box-shadow: 0 1px 5px rgba(0, 0, 0, 0.35);
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 12px;
  font-weight: 600;
  line-height: 1;
  cursor: pointer;
}
/* 照片气泡标记：照片 + 底部尖端 + 右上角序号徽章 */
.rm-marker-photo {
  position: relative;
  width: 46px;
  height: 52px;
  cursor: pointer;
}
.rm-marker-photo img {
  display: block;
  width: 46px;
  height: 46px;
  border-radius: 10px;
  /* 描边用当天颜色，与线路颜色呼应，分得清照片属于哪天 */
  border: 2.5px solid var(--dot, #378add);
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.3);
  object-fit: cover;
  background: #f1efe8; /* 加载中/加载失败的底色 */
}
/* 底部尖端，指向景点真实坐标 */
.rm-marker-photo::after {
  content: '';
  position: absolute;
  left: 50%;
  bottom: 1px;
  transform: translateX(-50%);
  border: 6px solid transparent;
  border-top-color: var(--dot, #378add);
  border-bottom-width: 0;
}
/* 序号徽章：白底描边，压在照片右上角，保证在任何照片上都可读 */
.rm-marker-badge {
  position: absolute;
  top: -7px;
  right: -7px;
  min-width: 19px;
  height: 19px;
  padding: 0 4px;
  border-radius: 10px;
  background: var(--dot, #378add);
  border: 2px solid #fff;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.35);
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 10.5px;
  font-weight: 600;
  line-height: 1;
  box-sizing: border-box;
}
.rm-info {
  padding: 10px 12px;
  font-size: 13px;
  color: #2c2c2a;
  width: 250px;
}
/* 图片在上、文字在下，横向排布在280px 宽的信息窗里容易拥挤 */
.rm-info-img {
  display: block;
  width: 100%;
  height: 130px;
  object-fit: cover;
  border-radius: 6px;
  margin-bottom: 8px;
  background: #f1efe8;
}
.rm-info-name {
  font-weight: 500;
  margin-bottom: 5px;
  display: flex;
  align-items: baseline;
  gap: 6px;
  line-height: 1.4;
}
.rm-info-seq {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 17px;
  height: 17px;
  border-radius: 50%;
  background: var(--dot, #378add);
  color: #fff;
  font-size: 10px;
  font-weight: 500;
}
.rm-info-day {
  font-size: 12px;
  color: #888780;
}
.rm-info-meta {
  margin-top: 4px;
  font-size: 12px;
  color: #5f5e5a;
}
.rm-info-desc {
  margin-top: 6px;
  font-size: 12px;
  color: #5f5e5a;
  line-height: 1.5;
}
</style>