import { defineConfig } from 'vite'

// Vite lib build → a single ESM bundle (dist/index.mjs) the host loads via
// ContributedPage. React / react-dom / the app SDK are resolved at RUNTIME from
// window.__personalclaw_modules (the host provides them), so they are externals —
// keeping the bundle tiny and sharing the host's single React instance.
//
// EXTERNAL IS NOT A WISH LIST. The host resolves a bare specifier only if
// `installAppSdk()` put it in `window.__personalclaw_modules` AND `resolvableAppSpecs()`
// lists it. Anything else is left bare, and the blob `import()` of the rewritten bundle
// THROWS — the page does not mount at all, it errors. Two specifiers here are NOT
// provided by the host, and neither can simply be un-externalled (this bundle installs
// no `react`, so vite cannot resolve them to bundle them either):
//
//   · `react/jsx-runtime` — vite 8 defaults TSX to the AUTOMATIC runtime, which imports
//     it. MEASURED on origin/main, before this migration, by building the bundle and
//     reading its imports: growth's page could not mount, because the host has no
//     `react/jsx-runtime` entry. `minutes` escaped only by still being on vite 6, whose
//     default is the classic transform. So the transform is pinned below rather than left
//     to a vite default that has already changed under this app once.
//   · `lucide-react` — the host comment claims it is provided; it appears in
//     `resolvableAppSpecs` but has NO entry in the module map, so `appModuleShimUrl`
//     returns null and the specifier stays bare. Left external and unused: importing it
//     would break the mount the same way.
export default defineConfig({
  // Classic transform → `React.createElement` (React is imported at the top of
  // index.tsx), so the bundle's only bare imports are ones the host actually resolves.
  // Drop this once the host provides `react/jsx-runtime` to contributed bundles.
  esbuild: { jsx: 'transform' },
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
