import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [vue()],
  // env 文件统一放在项目根目录，与后端 / docker compose 共用一份。
  // vite 默认只读 frontend/ 下的 .env，不做向上查找，所以必须显式指定。
  envDir: fileURLToPath(new URL('..', import.meta.url)),
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    }
  },
  server: {
    port: 5273,
    proxy: {
      // 开发代理：SSE 必须关闭缓冲，否则事件会被攒成一次性返回
      '/api': {
        target: process.env.VITE_PROXY_TARGET || 'http://localhost:8030',
        changeOrigin: true
      }
    }
  },
  build: {
    rollupOptions: {
      output: {
        // vue 系依赖不常变，单独拆 vendor chunk 利于缓存
        manualChunks: {
          vue: ['vue', 'vue-router', 'pinia']
        }
      }
    }
  }
})