import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const LIVE_API_BASE = 'https://yvhb48sthk.execute-api.us-west-2.amazonaws.com'
const developmentRouteBridge = {
  '/air-quality/v1/summary': '/metrics',
  '/air-quality/v1/timeseries': '/series',
  '/air-quality/v1/observations/export': '/silver-download',
  '/air-quality/v1/observations': '/silver-records',
}

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [
    tailwindcss(),
    react()
  ],
  server: {
    proxy: Object.fromEntries(
      Object.entries(developmentRouteBridge).map(([path, legacyPath]) => [path, {
        target: LIVE_API_BASE,
        changeOrigin: true,
        rewrite: (requestPath) => requestPath.replace(path, legacyPath),
      }]),
    ),
  },
})
