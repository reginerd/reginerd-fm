import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../output/player',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/now-playing': 'http://localhost:8090',
      '/art': 'http://localhost:8090',
      '/stream': 'http://localhost:8090',
      '/vote': 'http://localhost:8090',
    },
  },
})
