import { defineConfig } from 'vite'

// Vite lib build → a single ESM bundle (dist/index.mjs) the host loads via
// ContributedPage. React / react-dom / the app SDK / lucide are resolved at RUNTIME
// from window.__personalclaw_modules (the host provides them), so they are externals —
// keeping the bundle tiny and sharing the host's single React instance.
export default defineConfig({
  build: {
    lib: { entry: 'src/index.tsx', formats: ['es'], fileName: () => 'index.mjs' },
    // The host serves app UI assets from <app>/ui/, resolving the manifest entry
    // "dist/index.mjs" as ui/dist/index.mjs — so build INTO ui/dist (not the app root).
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      external: ['react', 'react-dom', 'react-dom/client', 'react/jsx-runtime', '@personalclaw/app-sdk', '@personalclaw/app-sdk/ui', 'lucide-react'],
    },
  },
})
