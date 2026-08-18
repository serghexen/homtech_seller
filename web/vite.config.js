import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    // Локальный Vue проксирует /api на локально запущенный FastAPI и не требует CORS-обходов в браузере.
    proxy: {
      '/api': {
        target: process.env.VITE_LOCAL_API_BASE || 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
