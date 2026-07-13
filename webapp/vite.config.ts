import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { vouchProxy } from './plugins/vouch-proxy'
import { claudeBridge } from './plugins/claude-bridge'

export default defineConfig({
  plugins: [react(), tailwindcss(), vouchProxy(), claudeBridge()],
})
