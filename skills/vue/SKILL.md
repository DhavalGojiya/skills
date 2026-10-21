---
name: vue
description: >-
  How Vue 3 compiles and renders: what a .vue file becomes with @vitejs/plugin-vue and how Rolldown or Rollup name it, templates as render functions, ref tracking, re-renders and DOM patching, and CSR versus SSR and SSG with hydration and Nuxt payloads. Use when asking what <script setup> or a template compiles to, whether same-named variables in two components clash, why a bundle shows _sfc_main$2, a$1, _export_sfc or __returned__, which state is per instance or shared, why <KeepAlive include> does not match, why data-v ids or __file paths differ in a build, what re-renders when a ref changes, why a component does not update, why the DOM has <!--v-if--> comments, whether SSR or SSG pages reload on navigation, where a fetch or onMounted runs, what _payload.json or __NUXT_DATA__ holds, or on "Hydration completed but contains mismatches".
---

# Vue: what a .vue file compiles to, and how it renders

Verified against Vue [3.5.44](https://github.com/vuejs/core/tree/v3.5.44) (`packages/compiler-sfc/src/compileScript.ts`, `packages/runtime-core/src/component.ts`, `packages/runtime-core/src/components/KeepAlive.ts`) and @vitejs/plugin-vue [6.0.10](https://github.com/vitejs/vite-plugin-vue/tree/plugin-vue@6.0.10) (`src/script.ts`, `src/main.ts`, `src/index.ts`, `src/utils/descriptorCache.ts`) source, and by compiling and building components with Vite 8.3.4 (Rolldown 1.2.14) and Vite 7.3.7 (Rollup 4.64.10) on Node 22.22.3. The reactivity and rendering rules are checked against Vue 3.5.44 (`packages/reactivity/src/ref.ts`, `packages/runtime-core/src/renderer.ts`, `packages/runtime-core/src/hydration.ts`), vue-router [5.4.0](https://github.com/vuejs/router/tree/v5.4.0) and Nuxt [4.6.2](https://github.com/nuxt/nuxt/tree/v4.6.2) source, by tracing DOM calls and server rendering in jsdom 26, and by a `nuxt generate` build loaded in headless Chrome. Line-level source links are in the files under `references/`.

```text
Navbar.vue                         one .vue file = one ES module
  │  @vue/compiler-sfc compileScript (genDefaultAs: '_sfc_main') + compileTemplate,
  │  assembled by @vitejs/plugin-vue
  ▼
const a = 'Hello from Navbar'      literal const: hoisted to module scope
const _sfc_main = {                the same name in every .vue file
  __name: 'Navbar',                the only trace of the file name
  setup() { ... }                  every other binding: once per instance
}
export default _sfc_main           or _export_sfc(_sfc_main, [render, __scopeId, __file])
  │
  ├─ vite (dev)   served alone at /src/components/Navbar.vue; module scope keeps files apart
  └─ vite build   all modules hoisted into one chunk; clashing top-level names get $N:
                  a$1, _sfc_main$5
```

## Rules

### What one .vue file becomes

1. Expect `_sfc_main` as the component object in every compiled `.vue` file. plugin-vue passes `genDefaultAs: '_sfc_main'`; nothing in the compiled code is named after the file except the `__name` string.
2. `__name` is the file name without its extension: `Navbar.vue` gives `'Navbar'`, `pages/profile/index.vue` gives `'index'`. `defineOptions({ name: 'ProfilePage' })` adds `name`, which wins over `__name` wherever Vue looks a name up.
3. In dev, the template is a separate `_sfc_render` function and `setup()` returns `__returned__`, an object of every top-level binding (a `let` as a getter/setter pair) that the template reads as `$setup.x`. In a production build the template is inlined: `setup()` returns the render function, which reads the bindings as closure variables.
4. `export default _export_sfc(_sfc_main, [...])` attaches `render`, `__scopeId` (scoped styles), `__cssModules` and `__file`. A production build of a component with no scoped styles or CSS modules has nothing to attach and emits `export default _sfc_main`.

### Where top-level variables live

5. A `<script setup>` `const` whose initializer is built only from literals (`'x'`, `42`, `-1`, `true`, `null`, `'a' + 'b'`, `` `plain` ``) is hoisted out of `setup()` to module scope and evaluated once. Everything else (`let`, objects, arrays, `ref()`, any call, functions) runs inside `setup()`, once per component instance.
6. A plain `<script>` block in the same file turns that hoisting off for the whole file.
7. Code in a plain `<script>` block runs once, at module scope, so `let count = 0` there is one counter shared by every instance. Keep per-instance state in `<script setup>`.
8. Do not rename variables to avoid clashes between components: same-named top-level variables in different `.vue` files are separate in dev (one module per file) and renamed by the bundler in a build.

### Names in a built bundle

09. Expect `_sfc_main$5` and `a$1` in unminified build output: the bundler appends `$N` to clashing top-level names in its own processing order, unrelated to file or import names. Minification shortens every name; the `__name` strings survive (`` __name:`Navbar` `` in Rolldown's minified output).
10. A default export that needs its own variable (through `_export_sfc`, or `defineComponent` for `lang="ts"`) is named by the bundler. Rolldown (Vite 8) names it after the file: `Counter_default`, `Login_default`, `profile_default` for `profile/index.vue`. Rollup (Vite 7) names it after the importing file's local name: `import Xyz from './Hoist.vue'` gives `const Xyz`.
11. Find a component in a build by its `__name`, or in unminified Vite 8 output by its `//#region src/components/Navbar.vue` marker, never by its import name.

### Dev and build differences that leak into output

12. The scoped style id `data-v-<hash>` hashes the root-relative path in dev and the path plus the file's content in a production build, so the build id changes with every edit and never equals the dev id. Set `vue({ features: { componentIdGenerator: 'filepath' } })` for ids that depend on the path only.
13. Build with `NODE_ENV=production` (the `vite build` default). With any other `NODE_ENV` (`NODE_ENV=development vite build`, a shell export, or `NODE_ENV=development` in `.env.<mode>`), plugin-vue compiles every component in its dev shape and writes each component's absolute source path on the build machine into `__file`. `features.prodDevtools` or `__VUE_PROD_DEVTOOLS__` also keeps templates separate and keeps `__file`, as the base name.

### Component names at runtime

14. `<KeepAlive include>` and `exclude` compare `name`, else `__name`, exactly and case-sensitively; a string is split on `,` without trimming. Every `index.vue` is `index`, and `include="Navbar, Login"` never matches `Login` (the second entry is `" Login"`). Write `include="Navbar,Login"` or an array, and name `index.vue` components with `defineOptions`.

### Templates, reactivity and re-renders

15. Read a directive value (`v-if="team"`, `:href="url"`, `@click="save"`) as a JavaScript expression against the component's own bindings, never a string. The template is compiled to a render function at build time, which adds the `.value` for you; a `v-if` is a ternary in it.
16. Change state through `.value`. `team = data` replaces the local variable and updates nothing, a plain `let` is never tracked, and assigning a value `Object.is`-equal to the current one triggers nothing.
17. Expect the whole `render()` of each component that read a ref during its last render to re-run, once per tick however many times the ref was assigned. `await nextTick()` before reading the DOM that change produced.
18. A child component re-renders only when its dynamic props or slots changed. Move a part that changes often (a timer, a live ticker) into its own component so its parent does not re-render with it.
19. Change a ref instead of editing Vue-managed DOM: the patch compares the new vnode tree with the previous one kept in `instance.subTree`, never with the page, and turns the difference into single calls (`insertBefore`, `removeChild`, `textContent`, `className`, a DOM property such as `el.href`). Only `v-html` sets `innerHTML`.
20. A false `v-if` renders a comment node (`<!--v-if-->` in dev, `<!---->` in production) that marks where the element will be inserted. `v-show` keeps the element and sets `style.display = "none"`; use it for elements that toggle often.

### CSR, SSR, SSG and hydration

21. Treat SSR and SSG as first-load strategies. After hydration, a `<RouterLink>` or `<NuxtLink>` click calls `preventDefault()` and `history.pushState()` and fetches only JS chunks and data, never the page's HTML, in every mode. A plain `<a href>` reloads the page in every mode.
22. Keep `window`, `document` and `localStorage` out of the top level of `setup()` in an SSR or SSG app, because `setup()` also runs on the server. Put that code in `onMounted`, which is dropped during server rendering; only `onServerPrefetch` and an `async setup()` are awaited there.
23. Make the browser's first render match the server HTML. A value that differs between the two (`Date.now()`, `Math.random()`, locale, `window` sizes, `localStorage`) logs `Hydration completed but contains mismatches.` and the page switches to the browser's value.
24. Call any API that needs a secret from a server endpoint. A fetch in `onMounted`, a fetch awaited in `setup()` after a client-side click, and every `import.meta.env.VITE_*` value run or ship in the browser.
25. In a Nuxt 4 `nuxt generate` build, look for page data in `<route>/_payload.json`, referenced by `data-src` on `<script type="application/json" id="__NUXT_DATA__">`, not in `window.__NUXT__` (runtime config only). With the default `experimental.payloadExtraction: true` the first load fetches it too, and a click to a prerendered route reads it without calling the `useFetch` handler.
26. Serve `index.html` for every unknown path when a CSR app uses `createWebHistory()`. `vite build` writes one `index.html` and no `about/` folder, so without that fallback a direct visit or refresh on `/about` is the host's 404 and the router never runs. `createWebHashHistory()` needs no fallback.

## Debug procedure

Run the shell commands from the Vite root (where `vite.config.*` lives). Replace `src/components/Navbar.vue` and `Navbar` with your component.

01. See what the dev server sends for one component, with `vite` running. The URL path is the file path from the Vite root. Look for hoisted consts above `const _sfc_main` and the bindings in `__returned__` (rules 3 and 5).
    ```bash
    curl -s http://localhost:5173/src/components/Navbar.vue
    ```
02. See the production code for one component. The script builds in memory and leaves `dist/` alone; the manual route writes an unminified build and searches it.
    ```bash
    uv run scripts/show_sfc_output.py --root path/to/app --build-only src/components/Navbar.vue
    ```
    ```bash
    npx vite build --minify false --outDir dist-unminified
    grep -n -B3 -A10 '__name: "Navbar"' dist-unminified/assets/*.js
    ```
03. Find a component in a minified bundle (Rolldown quotes the name with backticks):
    ```bash
    grep -o '__name:.Navbar.' dist/assets/*.js
    ```
04. Check a build for absolute source paths. Any output means it was built without `NODE_ENV=production` (rule 13); check `echo $NODE_ENV` and `NODE_ENV` in the `.env` files.
    ```bash
    grep -o '__file.\{0,4\}/[^"`]*' dist/assets/*.js
    ```
05. A value shared between instances: look for it in a plain `<script>` block (rule 7).
06. `<KeepAlive>` not caching a component: compare each `include` entry with the component's `name` or file name, character for character, with no spaces after commas (rule 14).
07. A component does not update after a change: look in the dev console for `[Vue warn]: Property "teem" was accessed during render but is not defined on instance.` (a misspelt name), then check the assignment goes through `.value` and changes the value (rule 16).
08. See which DOM calls a change makes and which components re-render: open `scripts/reactivity_playground.html` in a browser. In your own app, add `onUpdated(() => console.count('Navbar'))` to the component.
09. Check whether a click reloads the page: DevTools, Network, filter **Doc**, click the link. A new row means a document request: a plain `<a>`, a modifier key, or `target="_blank"` (rule 21).
10. A hydration mismatch: reproduce it in dev, where `[Vue warn]: Hydration text content mismatch on` names the element with the server and client values. A production build prints only the summary unless Vite `define` sets `__VUE_PROD_HYDRATION_MISMATCH_DETAILS__: true` (rule 23).
11. Find where a Nuxt static page's data lives. A `data-src` attribute means the data is in that `_payload.json`, not in the HTML (rule 25).
    ```bash
    grep -o '<script type="application/json"[^>]*>' .output/public/about/index.html
    ```

## Script

`scripts/show_sfc_output.py` prints a component's dev output (HMR code left out) and its production code from an unminified in-memory build, using the Vite installed in the project. `--demo` builds a throwaway project that reproduces rules 1 to 11, and `--vite 7.3.7` repeats it with Rollup. Needs `node` and `npm`.

```bash
uv run scripts/show_sfc_output.py --demo
uv run scripts/show_sfc_output.py --root path/to/app src/components/Navbar.vue
```

`scripts/reactivity_playground.html` is a single page that loads Vue 3.5.44 from unpkg and, for each button, shows the DOM calls Vue made, the live HTML with its `<!--v-if-->` comments, and re-render counts (rules 15 to 20). Open it in a browser:

```bash
xdg-open scripts/reactivity_playground.html
```

## References

- `references/sfc-compilation.md`: each step from `.vue` file to bundle with the quoted compiler-sfc, plugin-vue and runtime-core source and the script's dev and build output, a Vite 8 versus Vite 7 naming table, and a table of common wrong assumptions. Read it when a rule above does not explain the code you see, or to cite the source.
- `references/reactivity-and-patching.md`: a template's compiled render function, a runnable 20-line `ref` with track and trigger, the quoted `ref`, scheduler and patch source, traced DOM calls and re-render counts for a `v-if` navbar, which state is shared, and a table of wrong assumptions. Read it for rules 15 to 20 or when an update does more or less than expected.
- `references/rendering-modes.md`: the first HTML under CSR, SSR and SSG, a server render and hydration traced step by step, `<RouterLink>` click handling, a table of where a fetch runs, a hydration mismatch, and Nuxt's `_payload.json`, `useFetch`, `<ClientOnly>` and `<NuxtLink>` prefetch with the network trace of a `nuxt generate` build. Read it for rules 21 to 25.
- `references/direct-visit-vs-navigation.md`: opening `/about` directly versus clicking the navbar link, for a Vite CSR build and a Nuxt `nuxt generate` build: the test app, what each build writes (`dist/` versus `.output/public` with `about/index.html` and `about/_payload.json`), every request traced in Chrome, the 404 without an SPA fallback, and DevTools checks. Read it for rules 21 and 26, or when a refresh or a shared link behaves differently from a click.
