<script setup lang="ts">
// 每日天气卡片行。
//
// 为什么用卡片而不是折线图：
// 行程通常只有 2-5 天，3 个点的折线没有任何趋势可言（实测 24→25→24 是平的），
// 降水为 0 时折线贴底纯属噪音。天气预报类产品也从不用折线展示几天的天气。
//
// 卡片的核心价值：每张卡直接给出行动建议（带伞 / 加外套 / 防晒），
// 这是折线图给不了的。判断规则故意保持简单，宁可保守也不要误导。

import { computed } from 'vue'
import type { WeatherInfo } from '@/types'

const props = defineProps<{ weather: WeatherInfo[] }>()

interface Card {
  date: string
  icon: string
  desc: string
  dayTemp: number
  nightTemp: number
  tip: string
  /** true = 需要提醒（带伞等），用高亮样式 */
  alert: boolean
}

function iconOf(desc: string): string {
  if (desc.includes('雷')) return '⛈️'
  if (desc.includes('雨')) return '🌧️'
  if (desc.includes('雪')) return '❄️'
  if (desc.includes('雾') || desc.includes('霾')) return '🌫️'
  if (desc.includes('阴')) return '☁️'
  if (desc.includes('多云')) return '⛅'
  return '☀️'
}

function buildCard(w: WeatherInfo): Card {
  const diff = w.day_temp - w.night_temp
  const rainy = w.day_weather.includes('雨') || w.day_weather.includes('雪')
  const prob = w.precipitation_prob

  // 降水提示：优先用概率；数据源没给概率时（如高德）按天气现象兜底
  let rainTip: string
  let alert = false
  if (prob !== null && prob >= 30) {
    rainTip = `降水 ${prob}% · 记得带伞`
    alert = true
  } else if (prob === null && rainy) {
    rainTip = '有雨雪 · 记得带伞'
    alert = true
  } else if (prob !== null) {
    rainTip = prob <= 10 ? '降水 0% · 无需带伞' : `降水 ${prob}%`
  } else {
    rainTip = '无明显降水'
  }

  // 穿衣/行程建议：一条就够，宁可笼统也不要编造精确感受
  let tip: string
  if (w.day_temp >= 32) tip = '炎热，注意防晒补水'
  else if (w.day_temp <= 5) tip = '寒冷，注意保暖'
  else if (diff >= 10) tip = '昼夜温差大，早晚加外套'
  else if (rainy) tip = '有降水，建议室内景点优先'
  else if (w.day_weather.includes('阴') || w.day_weather.includes('多云')) tip = '体感舒适，适合出行'
  else tip = '适合户外与步行'

  return {
    date: w.date.slice(5), // 2026-10-07 → 10-07，卡片上不需要年份
    icon: iconOf(w.day_weather),
    desc: w.day_weather,
    dayTemp: Math.round(w.day_temp),
    nightTemp: Math.round(w.night_temp),
    tip: `${rainTip} · ${tip}`.replace(' · ', diff >= 10 && rainTip ? ' · ' : ' · '),
    alert
  }
}

const cards = computed<Card[]>(() => props.weather.slice(0, 5).map(buildCard))
</script>

<template>
  <div v-if="cards.length" class="wx-row" :class="{ 'single': cards.length === 1 }">
    <div v-for="c in cards" :key="c.date" class="wx-card" :class="{ alert: c.alert }">
      <div class="wx-top">
        <span class="wx-icon">{{ c.icon }}</span>
        <span class="wx-desc">{{ c.desc }}</span>
        <span class="wx-date">{{ c.date }}</span>
      </div>
      <div class="wx-temp">
        {{ c.dayTemp }}°<small>/{{ c.nightTemp }}°</small>
      </div>
      <div class="wx-tip" :class="{ warn: c.alert }">{{ c.tip }}</div>
    </div>
  </div>
</template>

<style scoped>
.wx-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(108px, 1fr));
  gap: 8px;
}
.wx-card {
  padding: 10px 11px;
  border-radius: 10px;
  background: var(--color-background-secondary, #FAFBFD);
  border: 1px solid rgba(55, 138, 221, 0.16);
  min-width: 0;
}
.wx-card.alert {
  border-color: rgba(186, 117, 23, 0.35);
  background: #FDFAF3;
}
.wx-top {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  font-weight: 500;
  color: var(--color-text-primary, #2C2C2A);
}
.wx-icon {
  font-size: 14px;
  line-height: 1;
}
.wx-desc {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.wx-date {
  margin-left: auto;
  font-size: 10.5px;
  color: #888780;
  font-weight: 400;
  font-variant-numeric: tabular-nums;
}
.wx-temp {
  margin-top: 6px;
  font-size: 21px;
  font-weight: 500;
  color: var(--color-text-primary, #2C2C2A);
  font-variant-numeric: tabular-nums;
  line-height: 1.1;
}
.wx-temp small {
  font-size: 12px;
  color: #888780;
  font-weight: 400;
}
.wx-tip {
  margin-top: 6px;
  font-size: 11px;
  color: #5F5E5A;
  line-height: 1.45;
}
.wx-tip.warn {
  color: #8A5A0B;
}
</style>