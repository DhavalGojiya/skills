# CSR, SSR and SSG in Vue: First Load, Navigation and Data Fetching

What the browser receives first under client-side rendering, server-side rendering and static generation, what hydration does, why a navbar click does not reload the page in any of them, where a data fetch runs, and what Nuxt adds on top.

Verified against Vue [3.5.44](https://github.com/vuejs/core/tree/v3.5.44), vue-router [5.4.0](https://github.com/vuejs/router/tree/v5.4.0) and Nuxt [4.6.2](https://github.com/nuxt/nuxt/tree/v4.6.2) source. The Vue experiments render a component with `@vue/server-renderer` 3.5.44 on Node 22.22.3 and hydrate the HTML in jsdom 26 in a separate process. The Nuxt experiment runs `nuxt generate` with Nuxt 4.6.2 (Vue 3.5.44, vue-router 5.4.0, Vite 8.3.4) and records the network in headless Chrome 155 with puppeteer-core 24.10.0.

______________________________________________________________________

## Table of Contents

1. [The Three Modes](#1-the-three-modes)
2. [Hydration](#2-hydration)
3. [Navigation After the First Load](#3-navigation-after-the-first-load)
4. [Where a Fetch Runs](#4-where-a-fetch-runs)
5. [Hydration Mismatches](#5-hydration-mismatches)
6. [Nuxt: Payloads, `useFetch`, `<ClientOnly>` and `<NuxtLink>`](#6-nuxt-payloads-usefetch-clientonly-and-nuxtlink)
7. [Common Wrong Assumptions](#7-common-wrong-assumptions)

______________________________________________________________________

## 1. The Three Modes

The modes differ in one thing: the HTML the browser gets for a document request (typing a URL, refreshing, opening a new tab, a crawler).

| Mode | First HTML for `/about`                                   | Made                     | Vue entry                                                                                           |
| ---- | --------------------------------------------------------- | ------------------------ | --------------------------------------------------------------------------------------------------- |
| CSR  | an empty shell, `<div id="app"></div>`, and script tags   | once, by `vite build`    | `createApp(App).mount('#app')`                                                                      |
| SSR  | the page's full HTML, rendered for this request           | per request, on a server | `renderToString(createSSRApp(App))` on the server, `createSSRApp(App).mount('#app')` in the browser |
| SSG  | the page's full HTML, prerendered into `about/index.html` | once, at build time      | the SSR code, run once per route by the build                                                       |

```html
<!-- CSR: what the server sends for every route -->
<!doctype html>
<html>
  <body>
    <div id="app"></div>
    <script type="module" src="/assets/index-a1b2c3.js"></script>
  </body>
</html>
```

```html
<!-- SSR or SSG: what the server sends for /about -->
<!doctype html>
<html>
  <body>
    <div id="app"><!--[--><h1>About Us</h1><p>Loading…</p><!--]--></div>
    <script type="module" src="/assets/entry-a1b2c3.js"></script>
  </body>
</html>
```

The `<!--[-->` and `<!--]-->` comments are fragment markers that `renderToString` writes around a template with several root nodes (and around `v-for` lists), so hydration can find where each fragment starts and ends.

A CSR app with `createWebHistory()` needs the host to answer every unknown path with `index.html` (the SPA fallback); otherwise refreshing on `/about` is a 404, because no `about` file exists. `createWebHashHistory()` does not need it: the part after `#` never reaches the server.

______________________________________________________________________

## 2. Hydration

In SSR and SSG the browser shows the server's HTML at once, before any JavaScript runs. Then the JavaScript loads and Vue hydrates: it runs each component's `setup()` and `render()` once and walks the existing DOM alongside the vnodes, attaching event listeners and storing each existing element in `vnode.el` instead of creating new ones. After hydration the page is an ordinary client-side Vue app.

The experiment rendered this component on the server and hydrated the result in the browser:

```js
// about.mjs
import { ref, onMounted } from 'vue'
export const About = () => ({
  setup() {
    const team = ref(null)
    const where = typeof window === 'undefined' ? 'server' : 'browser'
    console.log(`  setup() runs on the ${where}`)
    onMounted(async () => {
      console.log(`  onMounted runs on the ${where}`)
      team.value = await new Promise(r => setTimeout(() => r(['Asha', 'Ben']), 10))
    })
    return { team }
  },
  template: `<h1>About Us</h1><ul v-if="team"><li v-for="m in team" :key="m">{{ m }}</li></ul><p v-else>Loading…</p>`,
})
```

```text
SERVER
  setup() runs on the server
  HTML sent to the browser: <!--[--><h1>About Us</h1><p>Loading…</p><!--]-->

BROWSER
  setup() runs on the browser
  onMounted runs on the browser
  after hydrate: <!--[--><h1>About Us</h1><p>Loading…</p><!--]-->  (server's <h1> node reused: true)
  after fetch:   <!--[--><h1>About Us</h1><ul><li>Asha</li><li>Ben</li></ul><!--]-->
```

What it shows:

- `setup()` ran twice, once on each side. The same component code produces HTML on the server and hydrates in the browser.
- `onMounted` ran only in the browser. Lifecycle hooks registered during server rendering are dropped, except `onServerPrefetch`:

Source: [`packages/runtime-core/src/apiLifecycle.ts#L72-L78`](https://github.com/vuejs/core/blob/bf5f0dd877dd3c23ce85c2cbc1dd771567e84c8c/packages/runtime-core/src/apiLifecycle.ts#L72-L78)

```ts
    // post-create lifecycle registrations are noops during SSR (except for serverPrefetch)
    if (
      !isInSSRComponentSetup ||
      lifecycle === LifecycleHooks.SERVER_PREFETCH
    ) {
      injectHook(lifecycle, (...args: unknown[]) => hook(...args), target)
    }
```

- The `<h1>` after hydration is the node the server sent. Vue adopted the DOM instead of re-creating it, and the later team list was an ordinary patch.

Because the same `setup()` runs on the server, top-level code in it cannot touch `window`, `document` or `localStorage`. Move that code into `onMounted`, or read the value from a cookie, which the server can see.

______________________________________________________________________

## 3. Navigation After the First Load

Once the app is running, clicking a `<RouterLink>` does not request HTML in any of the three modes. The link cancels the browser's own navigation:

Source: [`packages/router/src/RouterLink.ts#L388-L406`](https://github.com/vuejs/router/blob/596d4816a5d524853dc9785827860ca858e3b799/packages/router/src/RouterLink.ts#L388-L406)

```ts
function guardEvent(e: MouseEvent) {
  // don't redirect with control keys
  if (e.metaKey || e.altKey || e.ctrlKey || e.shiftKey) return
  // don't redirect when preventDefault called
  if (e.defaultPrevented) return
  // don't redirect on right click
  if (e.button !== undefined && e.button !== 0) return
  // don't redirect if `target="_blank"`
  // @ts-expect-error getAttribute does exist
  if (e.currentTarget && e.currentTarget.getAttribute) {
    // @ts-expect-error getAttribute exists
    const target = e.currentTarget.getAttribute('target')
    if (/\b_blank\b/i.test(target)) return
  }
  // this may be a Weex event which doesn't have this method
  if (e.preventDefault) e.preventDefault()

  return true
}
```

and `createWebHistory()` changes the URL without loading a page:

Source: [`packages/router/src/history/html5.ts#L230-L234`](https://github.com/vuejs/router/blob/596d4816a5d524853dc9785827860ca858e3b799/packages/router/src/history/html5.ts#L230-L234)

```ts
    try {
      // BROWSER QUIRK
      // NOTE: Safari throws a SecurityError when calling this function 100 times in 30 seconds
      history[replace ? 'replaceState' : 'pushState'](state, '', url)
      historyState.value = state
```

The router then renders the matched component in the browser. The network only carries what the browser does not have yet: a lazy route's JavaScript chunk and the page's data. Ctrl-click, middle-click and `target="_blank"` fall through to the browser and open a new tab as usual.

```text
Multi-page app (PHP, Django templates, a static site with no client router):
  click ──► GET /about ──► full about.html ──► browser discards the page and loads the new one

Vue app (CSR, SSR or SSG), after the first load:
  click <RouterLink to="/about">
    ├─ preventDefault(), history.pushState('/about')   no reload
    ├─ GET /assets/About-d4e5f6.js                     if not loaded or prefetched yet
    ├─ GET the page's data (JSON)
    └─ About's render() runs in the browser; the navbar and app state stay
```

| Action                                  | CSR                        | SSR                     | SSG                                     |
| --------------------------------------- | -------------------------- | ----------------------- | --------------------------------------- |
| Type `/about`, refresh, open in new tab | shell, JS renders `/about` | server renders `/about` | host serves prebuilt `about/index.html` |
| Click a `<RouterLink to="/about">`      | no reload, JS + data       | no reload, JS + data    | no reload, JS + data                    |
| Click a plain `<a href="/about">`       | full reload                | full reload             | full reload                             |

SSR and SSG change the first load only. The prebuilt `about/index.html` of an SSG build is never downloaded by a click; it exists for document requests. `references/direct-visit-vs-navigation.md` traces every request of a direct visit and of a click for a Vite CSR build and a Nuxt static build, with both build outputs.

Check it in DevTools: Network tab, filter **Doc**, click a nav link. No document request appears. Press F5 and one appears.

______________________________________________________________________

## 4. Where a Fetch Runs

Where a fetch runs depends on where the code is and how the user arrived:

| Code                                                          | Document request (SSR; build time for SSG) | After a client-side click |
| ------------------------------------------------------------- | ------------------------------------------ | ------------------------- |
| Awaited in `onServerPrefetch` or in an `async setup()`        | server                                     | browser                   |
| Inside `onMounted`                                            | browser, after hydration                   | browser                   |
| A call to your own server endpoint that calls the third party | your server, both times                    | your server               |
| Any fetch in a CSR app                                        | browser                                    | browser                   |

The server renderer waits for an `async setup()` and every `onServerPrefetch` before it renders the component's HTML:

Source: [`packages/server-renderer/src/render.ts#L105-L125`](https://github.com/vuejs/core/blob/bf5f0dd877dd3c23ce85c2cbc1dd771567e84c8c/packages/server-renderer/src/render.ts#L105-L125)

```ts
  const res = setupComponent(instance, true /* isSSR */)
  if (__DEV__) popWarningContext()
  const hasAsyncSetup = isPromise(res)
  let prefetches = instance.sp /* LifecycleHooks.SERVER_PREFETCH */
  let p: Promise<SSRBuffer>
  if (hasAsyncSetup || prefetches) {
    p = Promise.resolve(res as Promise<void>)
      .then(() => {
        // instance.sp may be null until an async setup resolves, so evaluate it here
        if (hasAsyncSetup) prefetches = instance.sp
        if (prefetches) {
          try {
            return Promise.all(
              prefetches.map(prefetch => prefetch.call(instance.proxy)),
            ).catch(NOOP)
          } catch {
            // The wrapped lifecycle hook has already reported the error.
          }
        }
      })
      .then(() => renderComponentSubTree(instance, slotScopeId))
```

In the browser an `async setup()` component renders only inside `<Suspense>`; without one, dev prints `setup function returned a promise, but no <Suspense> boundary was found in the parent component tree` ([`component.ts#L911-L922`](https://github.com/vuejs/core/blob/bf5f0dd877dd3c23ce85c2cbc1dd771567e84c8c/packages/runtime-core/src/component.ts#L911-L922)). Nuxt's `<NuxtPage>` renders each page inside `h(Suspense, ...)` ([`pages/runtime/page.ts#L111`](https://github.com/nuxt/nuxt/blob/026e99314c5dfddc63de6d40c977b79c85c88bf8/packages/nuxt/src/pages/runtime/page.ts#L111)), which is why `await useFetch()` works at the top of a Nuxt page.

With a fetch in `onMounted`, a refresh on an SSR page goes:

```text
1. Server runs setup(): team = null. onMounted is dropped. Renders "Loading…".
2. Browser shows the HTML at once: heading and text, then "Loading…".
3. JS loads and Vue hydrates: team is still null in the browser, so its render matches the server HTML.
4. onMounted runs in the browser, fetches and sets team.value; Vue patches "Loading…" into the list.
```

Consequences: crawlers and link previews see "Loading…", not the data; the content shifts when it arrives unless the placeholder reserves the same space; and the request is visible in the browser's Network tab, as in CSR. Fetch on the server what matters for SEO and link previews, and fetch in `onMounted` what is per user or live (notifications, tickers).

A fetch awaited during the server render delays the first byte by that fetch's time. With a slow API, a server-rendered page can show its content later than a CSR page shows its skeleton.

Secrets never belong in browser code. In a Vite app every `import.meta.env.VITE_*` value is inlined into the bundle users download. Call an API that needs a key from a server endpoint.

______________________________________________________________________

## 5. Hydration Mismatches

Hydration expects the browser's first render to produce the same DOM as the server's HTML. The experiment repeated section 2 with a component whose browser render said `Loading!` where the server had sent `Loading…`:

```text
BROWSER (client renders different text)
  setup() runs on the browser
  console.warn: [Vue warn]: Hydration text content mismatch on
  console.error: Hydration completed but contains mismatches.
  onMounted runs on the browser
  after hydrate: <!--[--><h1>About Us</h1><p>Loading!</p><!--]-->  (server's <h1> node reused: true)
```

The warning names the element and is dev-only by default; the error is printed in production too:

Source: [`packages/runtime-core/src/hydration.ts#L490-L500`](https://github.com/vuejs/core/blob/bf5f0dd877dd3c23ce85c2cbc1dd771567e84c8c/packages/runtime-core/src/hydration.ts#L490-L500)

```ts
          textContent !== clientText.replace(/\r\n|\r/g, '\n')
        ) {
          if (!isMismatchAllowed(el, MismatchTypes.TEXT)) {
            ;(__DEV__ || __FEATURE_PROD_HYDRATION_MISMATCH_DETAILS__) &&
              warn(
                `Hydration text content mismatch on`,
                el,
                `\n  - rendered on server: ${textContent}` +
                  `\n  - expected on client: ${clientText}`,
              )
            logMismatchError()
```

Source: [`packages/runtime-core/src/hydration.ts#L60-L66`](https://github.com/vuejs/core/blob/bf5f0dd877dd3c23ce85c2cbc1dd771567e84c8c/packages/runtime-core/src/hydration.ts#L60-L66)

```ts
  if (__TEST__ || hasLoggedMismatchError) {
    return
  }
  // this error should show up in production
  console.error('Hydration completed but contains mismatches.')
  hasLoggedMismatchError = true
}
```

Vue set the text to the browser's value, so the user sees the client version after a flash of the server one. Typical causes are values that differ between the two sides on the first render: `Date.now()`, `Math.random()`, the user's locale or time zone, `window.innerWidth`, and `localStorage` read at the top of `setup()`. Render the same value on both sides first and change it in `onMounted`. For details in a production build, define `__VUE_PROD_HYDRATION_MISMATCH_DETAILS__: true`.

______________________________________________________________________

## 6. Nuxt: Payloads, `useFetch`, `<ClientOnly>` and `<NuxtLink>`

Nuxt builds SSR and SSG on the Vue mechanisms above. Permalinks below are to Nuxt v4.6.2, commit `026e99314c5dfddc63de6d40c977b79c85c88bf8`.

### What `nuxt generate` writes

The experiment app had two pages, and `/about` fetched from a server route:

```vue
<!-- app/pages/about.vue -->
<script setup lang="ts">
const { data: team } = await useFetch('/api/team')
</script>
<template>
  <div>
    <h1>About</h1>
    <ul id="team"><li v-for="name in team" :key="name">{{ name }}</li></ul>
    <NuxtLink to="/">Home</NuxtLink>
  </div>
</template>
```

```ts
// server/api/team.ts
export default defineEventHandler(() => ['Asha', 'Ben'])
```

`nuxt generate` with default options wrote these files, and a `dist` symlink to `.output/public` in the project root:

```text
.output/public/index.html
.output/public/_payload.json
.output/public/about/index.html        HTML with the list rendered: <li>Asha</li><li>Ben</li>
.output/public/about/_payload.json     the useFetch data, as devalue JSON
.output/public/200.html, 404.html
.output/public/_nuxt/*.js              entry, shared and page chunks
.output/public/_nuxt/builds/meta/<buildId>.json   manifest: "prerendered":["/","/about"]
```

`/api/team` itself is not written; the build machine called it while prerendering and stored the result in the payload:

```json
[{"data":1,"prerenderedAt":6},["ShallowReactive",2],{"$ffbcyb7emwc7x":3},[4,5],"Asha","Ben",1792516656721]
```

The HTML carries a reference to it, not the data:

```html
<link rel="preload" as="fetch" crossorigin="anonymous" href="/about/_payload.json?_b=985cad97-b585-4fca-ab07-b6d28f3e77af">
<script type="application/json" data-nuxt-data="nuxt-app" data-ssr="true" id="__NUXT_DATA__" data-src="/about/_payload.json?_b=985cad97-b585-4fca-ab07-b6d28f3e77af">
```

`window.__NUXT__` holds only the runtime config (`window.__NUXT__={};window.__NUXT__.config=...`). It carries the whole payload only with `experimental.renderJsonPayloads: false`.

Whether the first load inlines the data or fetches `_payload.json` is `experimental.payloadExtraction`:

Source: [`packages/schema/src/config/experimental.ts#L113-L129`](https://github.com/nuxt/nuxt/blob/026e99314c5dfddc63de6d40c977b79c85c88bf8/packages/schema/src/config/experimental.ts#L113-L129)

```ts
    /**
     * Extract the data payloads of prerendered and ISR/SWR pages into `_payload.json` files that are reused during client-side navigation.
     *
     * - `'client'`: inline the payload in the HTML for the initial render and extract it to a `_payload.json` file for client-side navigation.
     * - `true`: extract the payload to a `_payload.json` file for both the initial render and client-side navigation.
     * - `false`: disable payload extraction entirely; the payload is always inlined in the HTML.
     *
     * Defaults to `true`, or `'client'` when `future.compatibilityVersion` is `5` or higher. It is forced to `false` when `ssr` is disabled.
     * @see [Payload Extraction documentation](https://nuxt.com/docs/getting-started/prerendering#payload-extraction)
     */
    payloadExtraction: {
      $resolve: async (val, get) => {
        if ((await get('ssr')) === false) { return false }
        if (val === 'client' || typeof val === 'boolean') { return val }
        return (await get('future.compatibilityVersion')) >= 5 ? 'client' as const : true
      },
    },
```

A route rendered per request (plain SSR, no prerender or cache rule) inlines its payload in the `__NUXT_DATA__` script.

### `useFetch` and `useAsyncData`: which side calls the handler

The client decides from the hydration state and the options:

Source: [`packages/nuxt/src/app/composables/asyncData.ts#L553-L554`](https://github.com/nuxt/nuxt/blob/026e99314c5dfddc63de6d40c977b79c85c88bf8/packages/nuxt/src/app/composables/asyncData.ts#L553-L554)

```ts
        if (fetchOnServer && nuxtApp.isHydrating && (asyncData.error.value || (asyncData.data.value !== undefined && (hasServerData || asyncData._initialCachedData !== undefined)))) {
          // 1. Hydration (server: true): no fetch
```

Source: [`packages/nuxt/src/app/composables/asyncData.ts#L559-L570`](https://github.com/nuxt/nuxt/blob/026e99314c5dfddc63de6d40c977b79c85c88bf8/packages/nuxt/src/app/composables/asyncData.ts#L559-L570)

```ts
        } else if (inComponentSetup && ((!isWithinClientOnly && nuxtApp.payload.serverRendered && nuxtApp.isHydrating && (!fetchOnServer || hasServerData)) || opts.lazy) && opts.immediate) {
          // 2. Initial load (server: false): fetch on mounted
          // 3. Initial load or navigation (lazy: true): fetch on mounted
          if (instance) {
            instance._nuxtOnBeforeMountCbs.push(initialFetch)
          } else {
            onBeforeMount(() => { initialFetch() })
          }
        } else if (opts.immediate && asyncData.status.value !== 'success') {
          // 4. Navigation (lazy: false) - or plugin usage: await fetch
          initialFetch()
        }
```

Before calling the handler, the default `getCachedData` returns the hydration payload, or on navigation the data loaded from a prerendered route's `_payload.json`:

Source: [`packages/nuxt/src/app/composables/asyncData.ts#L1070-L1077`](https://github.com/nuxt/nuxt/blob/026e99314c5dfddc63de6d40c977b79c85c88bf8/packages/nuxt/src/app/composables/asyncData.ts#L1070-L1077)

```ts
const getDefaultCachedData: AsyncDataOptions<any>['getCachedData'] = (key, nuxtApp, ctx) => {
  if (nuxtApp.isHydrating) {
    return nuxtApp.payload.data[key]
  }

  if (ctx.cause !== 'refresh:manual' && ctx.cause !== 'refresh:hook') {
    return nuxtApp.static.data[key]
  }
```

| Situation                                             | Who calls the handler (`/api/team` or the third party)                                   |
| ----------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Document request, plain SSR route                     | the server, during the render; the browser reuses the payload                            |
| Document request, prerendered route (`nuxt generate`) | the build machine, once; the browser reads `_payload.json`                               |
| Click to a prerendered route                          | nobody: the data comes from that route's `_payload.json`                                 |
| Click to a plain SSR route                            | the browser, before the new page renders                                                 |
| `server: false`, document request                     | the browser, before mount, after hydration starts; the server HTML has no data           |
| `lazy: true`, no cached data                          | the browser, before mount, instead of awaiting it in `setup()`; render the pending state |

`useFetch('https://third-party/...')` called from a page therefore runs in the browser on some navigations. A request that needs a secret goes through a `server/api/*` route, which always runs on the server.

### `<ClientOnly>`

Source: [`packages/nuxt/src/app/components/client-only.ts#L50-L78`](https://github.com/nuxt/nuxt/blob/026e99314c5dfddc63de6d40c977b79c85c88bf8/packages/nuxt/src/app/components/client-only.ts#L50-L78)

```ts
    const mounted = shallowRef(false)
    onMounted(() => { mounted.value = true })
    // Bail out of checking for pages/layouts as they might be included under `<ClientOnly>` 🤷‍♂️
    if (import.meta.dev) {
      const nuxtApp = useNuxtApp()
      nuxtApp._isNuxtPageUsed = true
      nuxtApp._isNuxtLayoutUsed = true
    }
    const vm = getCurrentInstance()
    if (vm) {
      vm._nuxtClientOnly = true
    }
    provide(clientOnlySymbol, true)
    return () => {
      if (mounted.value) {
        const vnodes = slots.default?.()
        if (vnodes && vnodes.length === 1 && !(vapor && isVaporSlot(slots.default))) {
          return [cloneVNode(vnodes[0]!, attrs)]
        }
        if (import.meta.dev && vapor && isVaporSlot(slots.default) && attrs && Object.keys(attrs).length > 0) {
          renderDiagnostics.NUXT_E4021()
        }
        return vnodes
      }
      const slot = slots.fallback || slots.placeholder
      if (slot) { return h(slot) }
      const fallbackStr = props.fallback || props.placeholder || ''
      const fallbackTag = sanitizeTag(props.fallbackTag || props.placeholderTag, 'span')
      return createElementBlock(fallbackTag, attrs, fallbackStr)
```

`mounted` only becomes `true` in `onMounted`, which never runs on the server, so the server HTML and the first browser render both show the fallback (an empty `<span>` without one), and hydration matches. The default slot replaces it after mount. Use it for components that touch `window` while rendering, such as maps and some chart libraries:

```vue
<template>
  <ClientOnly fallback="Loading map…">
    <LeafletMap />
  </ClientOnly>
</template>
```

### `<NuxtLink>` and prefetching

Source: [`packages/nuxt/src/app/components/nuxt-link.ts#L637-L646`](https://github.com/nuxt/nuxt/blob/026e99314c5dfddc63de6d40c977b79c85c88bf8/packages/nuxt/src/app/components/nuxt-link.ts#L637-L646)

```ts
          onClick: async (event) => {
            if (isExternal.value || hasTarget.value) {
              return
            }

            event.preventDefault()

            try {
              const encodedHref = encodeRoutePath(href.value ?? '')
              return await (props.replace ? router.replace(encodedHref) : router.push(encodedHref))
```

By default a link prefetches its target once it scrolls into view (after the app is ready and the browser is idle), unless the connection has Save-Data on or is 2G:

Source: [`packages/schema/src/config/experimental.ts#L258-L264`](https://github.com/nuxt/nuxt/blob/026e99314c5dfddc63de6d40c977b79c85c88bf8/packages/schema/src/config/experimental.ts#L258-L264)

```ts
      nuxtLink: {
        componentName: 'NuxtLink',
        prefetch: true,
        prefetchOn: {
          visibility: true,
        },
      },
```

`references/direct-visit-vs-navigation.md` section 5 lists the requests with prefetching on and off.

______________________________________________________________________

## 7. Common Wrong Assumptions

| Assumption                                                       | What happens                                                                                                      |
| ---------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| SSR or SSG pages reload on every click                           | Only the first load is server or prebuilt HTML; clicks are client-side navigation (section 3)                     |
| With SSG, a click downloads `about/index.html`                   | The prebuilt HTML serves document requests; a Nuxt click fetches the chunk and `_payload.json` (sections 3, 6)    |
| With SSR, a click asks the server to render `/about`             | The browser renders it from JS and data; the server sees only data and chunk requests (section 3)                 |
| `onMounted` runs on the server during SSR                        | It is dropped there; only `onServerPrefetch` and `async setup()` are awaited (sections 2, 4)                      |
| Hydration re-renders the page from scratch                       | It adopts the server's DOM nodes and attaches listeners (section 2)                                               |
| A fetch in `onMounted` is in the server HTML                     | The server HTML has the placeholder; the browser fetches after hydration (section 4)                              |
| The same fetch always runs in the same place                     | Code awaited in `setup()` runs on the server for a document request and in the browser after a click (section 4)  |
| Nuxt puts the page data in `window.__NUXT__`                     | Nuxt 4 uses `<script type="application/json" id="__NUXT_DATA__">`; `window.__NUXT__` holds the config (section 6) |
| A Nuxt static page has its data inlined in the HTML              | With the default `payloadExtraction: true` the first load fetches `_payload.json` too (section 6)                 |
| `useFetch` calls the API again on every client-side navigation   | Not for prerendered routes: the data comes from their `_payload.json` (section 6)                                 |
| A plain `<a href>` navigates like `<RouterLink>` or `<NuxtLink>` | It is a normal link and reloads the page in every mode (section 3)                                                |
