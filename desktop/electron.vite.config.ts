import { defineConfig } from 'electron-vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'node:path'

function csp() {
  return {
    name: 'inject-csp',
    transformIndexHtml: {
      order: 'pre' as const,
      handler(html: string, ctx: { server?: unknown }) {
        const dev = !!ctx.server
        const policy = dev
          ? "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self' ws://localhost:* http://localhost:*"
          : "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'"
        return html.replace(/<head>/, `<head>\n<meta http-equiv="Content-Security-Policy" content="${policy}">`)
      },
    },
  }
}

export default defineConfig({
  main: {
    build: {
      outDir: 'out/main',
      lib: { entry: resolve('src/main/index.ts'), formats: ['cjs'] },
      rollupOptions: { output: { entryFileNames: '[name].cjs' } },
    },
  },
  preload: {
    build: {
      outDir: 'out/preload',
      lib: { entry: resolve('src/preload/index.ts'), formats: ['cjs'] },
      rollupOptions: { output: { entryFileNames: '[name].cjs' } },
    },
  },
  renderer: {
    root: 'src/renderer',
    build: {
      outDir: 'out/renderer',
      rollupOptions: { input: resolve('src/renderer/index.html') },
    },
    plugins: [react(), csp()],
  },
})
