---
name: vue
description: >-
  What a Vue single-file component (.vue) compiles to with @vitejs/plugin-vue and @vue/compiler-sfc, in the dev server and in a vite build, and how Rolldown (Vite 8) or Rollup (Vite 7) name it in the bundle. Use when asking what <script setup> becomes, whether two components with the same top-level variable names clash, why compiled or bundled code shows _sfc_main, _sfc_main$2, a$1, Counter_default, _export_sfc or __returned__, which <script setup> variables are hoisted to module scope and which run per instance, why state in a plain <script> block is shared by every instance, why a component is named "index", why <KeepAlive include> does not match a component, why scoped data-v ids differ between dev and build, why a production bundle contains absolute paths in __file, or how to see the compiled output of one component.
---

# Vue single-file components: what a .vue file compiles to

Verified against Vue [3.5.44](https://github.com/vuejs/core/tree/v3.5.44) (`packages/compiler-sfc/src/compileScript.ts`, `packages/runtime-core/src/component.ts`, `packages/runtime-core/src/components/KeepAlive.ts`) and @vitejs/plugin-vue [6.0.10](https://github.com/vitejs/vite-plugin-vue/tree/plugin-vue@6.0.10) (`src/script.ts`, `src/main.ts`, `src/index.ts`, `src/utils/descriptorCache.ts`) source, and by compiling and building components with Vite 8.3.4 (Rolldown 1.2.14) and Vite 7.3.7 (Rollup 4.64.10) on Node 22.22.3. Line-level source links are in `references/sfc-compilation.md`.

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

## Debug procedure

Run the shell commands from the Vite root (where `vite.config.*` lives). Replace `src/components/Navbar.vue` and `Navbar` with your component.

1. See what the dev server sends for one component, with `vite` running. The URL path is the file path from the Vite root. Look for hoisted consts above `const _sfc_main` and the bindings in `__returned__` (rules 3 and 5).
   ```bash
   curl -s http://localhost:5173/src/components/Navbar.vue
   ```
2. See the production code for one component. The script builds in memory and leaves `dist/` alone; the manual route writes an unminified build and searches it.
   ```bash
   uv run scripts/show_sfc_output.py --root path/to/app --build-only src/components/Navbar.vue
   ```
   ```bash
   npx vite build --minify false --outDir dist-unminified
   grep -n -B3 -A10 '__name: "Navbar"' dist-unminified/assets/*.js
   ```
3. Find a component in a minified bundle (Rolldown quotes the name with backticks):
   ```bash
   grep -o '__name:.Navbar.' dist/assets/*.js
   ```
4. Check a build for absolute source paths. Any output means it was built without `NODE_ENV=production` (rule 13); check `echo $NODE_ENV` and `NODE_ENV` in the `.env` files.
   ```bash
   grep -o '__file.\{0,4\}/[^"`]*' dist/assets/*.js
   ```
5. A value shared between instances: look for it in a plain `<script>` block (rule 7).
6. `<KeepAlive>` not caching a component: compare each `include` entry with the component's `name` or file name, character for character, with no spaces after commas (rule 14).

## Script

`scripts/show_sfc_output.py` prints a component's dev output (HMR code left out) and its production code from an unminified in-memory build, using the Vite installed in the project. `--demo` builds a throwaway project that reproduces rules 1 to 11, and `--vite 7.3.7` repeats it with Rollup. Needs `node` and `npm`.

```bash
uv run scripts/show_sfc_output.py --demo
uv run scripts/show_sfc_output.py --root path/to/app src/components/Navbar.vue
```

## References

- `references/sfc-compilation.md`: each step from `.vue` file to bundle with the quoted compiler-sfc, plugin-vue and runtime-core source and the script's dev and build output, a Vite 8 versus Vite 7 naming table, and a table of common wrong assumptions. Read it when a rule above does not explain the code you see, or to cite the source.
