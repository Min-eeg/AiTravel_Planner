<script lang="ts">
// 普通 script 块：仅用于声明组件选项名称（配合 <script setup> 使用）
export default { name: 'App' }
</script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <RouterLink to="/" class="brand">
        <!-- 品牌图标：折线路径 + 三个节点，呼应「逐日规划」的核心能力 -->
        <svg
          class="brand-mark"
          viewBox="0 0 24 24"
          fill="none"
          xmlns="http://www.w3.org/2000/svg"
          aria-hidden="true"
        >
          <path
            d="M5 19c0-3 3-3.5 4.5-5S11 9 11 6.5C11 4.5 12.5 3 15 3"
            stroke="url(#brand-gradient)"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
          <circle cx="5" cy="19" r="2" fill="url(#brand-gradient)" />
          <circle cx="11" cy="10.5" r="1.8" fill="url(#brand-gradient)" />
          <circle cx="15" cy="3" r="2" fill="url(#brand-gradient)" />
          <defs>
            <linearGradient id="brand-gradient" x1="4" y1="20" x2="16" y2="2">
              <stop stop-color="#378ADD" />
              <stop offset="1" stop-color="#1D9E75" />
            </linearGradient>
          </defs>
        </svg>
        <span class="brand-text">途策智游</span>
      </RouterLink>
      <nav class="nav">
        <RouterLink to="/">规划</RouterLink>
        <RouterLink to="/result">行程结果</RouterLink>
        <RouterLink to="/history">我的行程</RouterLink>
      </nav>
    </header>

    <main class="app-main">
      <RouterView />
    </main>
  </div>
</template>

<style scoped>
.app-shell {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}
.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 24px;
  border-bottom: 1px solid var(--color-border-tertiary, rgba(0, 0, 0, 0.1));
}
.brand {
  display: flex;
  align-items: center;
  gap: 9px;
  text-decoration: none;
  color: inherit;
}
.brand-mark {
  width: 22px;
  height: 22px;
  flex-shrink: 0;
}
.brand-text {
  font-size: 16px;
  font-weight: 500;
}
.nav {
  display: flex;
  gap: 20px;
}
.nav a {
  font-size: 13px;
  text-decoration: none;
  color: #888780;
  padding-bottom: 2px;
  border-bottom: 2px solid transparent;
}
.nav a.router-link-active {
  color: #0C447C;
  border-bottom-color: #378ADD;
}
.app-main {
  flex: 1;
}
</style>