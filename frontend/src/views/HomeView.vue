<script setup lang="ts">
// 行程规划表单页。
//
// 交互要点：free_text 是"自然语言输入"，后端会把它解析成结构化约束
// （节奏/预算/兴趣点），这是演示"LLM 做意图理解"的入口。

import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useTripStore } from '@/stores/trip'

const router = useRouter()
const store = useTripStore()

const CITY_PRESETS = ['北京', '上海', '杭州', '成都', '长沙', '西安', '广州', '厦门']
const PREFERENCE_PRESETS = [
  '历史文化', '博物馆', '美食', '自然风光', '亲子', '小众', '夜景', '购物', '咖啡馆'
]

const form = ref({
  city: '杭州',
  start_date: new Date().toISOString().slice(0, 10),
  days: 3,
  preferences: ['历史文化', '美食'] as string[],
  budget_level: '中等',
  pace: '适中',
  free_text: ''
})

const submitting = computed(() => store.loading)

function togglePreference(tag: string) {
  const idx = form.value.preferences.indexOf(tag)
  if (idx === -1) {
    form.value.preferences.push(tag)
  } else {
    form.value.preferences.splice(idx, 1)
  }
}

function submit() {
  if (!form.value.city.trim()) return
  store.generate({ ...form.value })
  router.push('/result')
}
</script>

<template>
  <div class="home-page">
    <div class="hero">
      <h1 class="title">途策智游</h1>
      <p class="subtitle">填一张表，我帮你做旅游规划</p>
    </div>

    <form class="form-card" @submit.prevent="submit">
      <!-- 城市 -->
      <section class="field">
        <label class="label">目的地</label>
        <input
          v-model="form.city"
          type="text"
          placeholder="例如：杭州"
          class="input"
          required
        />
        <div class="presets">
          <button
            v-for="c in CITY_PRESETS"
            :key="c"
            type="button"
            class="chip"
            :class="{ active: form.city === c }"
            @click="form.city = c"
          >
            {{ c }}
          </button>
        </div>
      </section>

      <!-- 日期与天数 -->
      <section class="field-row">
        <div class="field">
          <label class="label">出发日期</label>
          <input v-model="form.start_date" type="date" class="input" />
        </div>
        <div class="field">
          <label class="label">天数</label>
          <input
            v-model.number="form.days"
            type="number"
            min="1"
            max="15"
            class="input"
          />
        </div>
      </section>

      <!-- 偏好 -->
      <section class="field">
        <label class="label">兴趣偏好（可多选）</label>
        <div class="presets">
          <button
            v-for="p in PREFERENCE_PRESETS"
            :key="p"
            type="button"
            class="chip"
            :class="{ active: form.preferences.includes(p) }"
            @click="togglePreference(p)"
          >
            {{ p }}
          </button>
        </div>
      </section>

      <!-- 预算与节奏 -->
      <section class="field-row">
        <div class="field">
          <label class="label">预算档位</label>
          <div class="presets">
            <button
              v-for="b in ['经济', '中等', '宽松']"
              :key="b"
              type="button"
              class="chip"
              :class="{ active: form.budget_level === b }"
              @click="form.budget_level = b"
            >
              {{ b }}
            </button>
          </div>
        </div>
        <div class="field">
          <label class="label">行程节奏</label>
          <div class="presets">
            <button
              v-for="p in ['紧凑', '适中', '悠闲']"
              :key="p"
              type="button"
              class="chip"
              :class="{ active: form.pace === p }"
              @click="form.pace = p"
            >
              {{ p }}
            </button>
          </div>
        </div>
      </section>

      <!-- 自然语言 -->
      <section class="field">
        <label class="label">补充要求（自然语言）</label>
        <textarea
          v-model="form.free_text"
          rows="3"
          class="input textarea"
          placeholder="例如：学生党穷游，不想早起暴走，想多看博物馆，晚上要有本地夜市"
        ></textarea>
      </section>

      <button type="submit" class="submit" :disabled="submitting">
        {{ submitting ? '生成中…' : '开始规划' }}
      </button>
    </form>
  </div>
</template>

<style scoped>
.home-page {
  max-width: 680px;
  margin: 0 auto;
  padding: 48px 24px 80px;
}
.hero {
  text-align: center;
  margin-bottom: 28px;
}
.title {
  margin: 0;
  font-size: 30px;
  font-weight: 500;
  letter-spacing: 0.02em;
}
.subtitle {
  margin: 10px 0 0;
  font-size: 13.5px;
  color: #888780;
  line-height: 1.6;
}
.form-card {
  padding: 26px 28px;
  border-radius: 14px;
  border: 1px solid var(--color-border-tertiary, rgba(0, 0, 0, 0.1));
  background: var(--color-background-primary, #fff);
}
.field {
  margin-bottom: 22px;
  flex: 1;
}
.field-row {
  display: flex;
  gap: 18px;
  margin-bottom: 22px;
  flex-wrap: wrap;
}
.label {
  display: block;
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 500;
}
.input {
  width: 100%;
  padding: 9px 12px;
  font-size: 14px;
  font-family: inherit;
  color: inherit;
  border: 1px solid var(--color-border-secondary, rgba(0, 0, 0, 0.16));
  border-radius: 8px;
  background: transparent;
  box-sizing: border-box;
}
.input:focus {
  outline: none;
  border-color: #378ADD;
}
.textarea {
  resize: vertical;
  line-height: 1.6;
}
.presets {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  margin-top: 8px;
}
.chip {
  padding: 5px 12px;
  font-size: 12.5px;
  font-family: inherit;
  color: inherit;
  border: 1px solid var(--color-border-secondary, rgba(0, 0, 0, 0.16));
  border-radius: 14px;
  background: transparent;
  cursor: pointer;
  transition: all 0.15s ease;
}
.chip:hover {
  border-color: #378ADD;
}
.chip.active {
  background: #E6F1FB;
  border-color: #378ADD;
  color: #0C447C;
}
.submit {
  width: 100%;
  padding: 12px;
  font-size: 15px;
  font-family: inherit;
  color: #fff;
  background: #378ADD;
  border: none;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.15s ease;
}
.submit:hover:not(:disabled) {
  background: #185FA5;
}
.submit:disabled {
  background: #B5D4F4;
  cursor: not-allowed;
}
</style>