import { defineConfig } from 'vite'

// Vite lib build → a single ESM bundle (bundle/index.mjs) the host loads via
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
//   · `react/jsx-runtime` — emitted by the AUTOMATIC JSX runtime, which is vite 8's default
//     for TSX. `growth` hit it first: its page stopped rendering entirely ("Failed to resolve
//     module specifier react/jsx-runtime", measured in a browser on unmodified sources; fixed
//     since). This bundle is on vite 8 too, so the CLASSIC transform PINNED below is what
//     keeps that import out of it.
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
    // "bundle/index.mjs" as ui/bundle/index.mjs — so build INTO ui/bundle (not the app root).
    // The bundle is COMMITTED: an install copies it as it is and never runs npm, so a user
    // needs no Node to see this page. After editing src/, run `npm ci && npm run build` here
    // and commit the result; CI rebuilds it and fails when the committed file differs.
    outDir: 'bundle',
    emptyOutDir: true,
    rollupOptions: {
      external: ['react', 'react-dom', 'react-dom/client', 'react/jsx-runtime', '@personalclaw/app-sdk', '@personalclaw/app-sdk/ui', 'lucide-react'],
    },
  },
})
