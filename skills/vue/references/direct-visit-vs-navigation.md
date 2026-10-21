# Direct Visit Versus Navbar Click: `/about` Traced in CSR and Nuxt SSG

What happens when a user opens `/about` directly (address bar, refresh, new tab, a shared link, a crawler) compared with clicking an About link in the navbar of an app that is already open, in a Vite client-side app and in a Nuxt static build. Includes what each build writes to disk and every request the browser made.

Verified with Vite 8.3.4, @vitejs/plugin-vue 6.0.10, Vue 3.5.44 and vue-router 5.4.0 for the CSR build, and Nuxt 4.6.2 (Vue 3.5.44, vue-router 5.4.0, Vite 8.3.4) `nuxt generate` for the static build, each served by a static file server and loaded in headless Chrome 155 with puppeteer-core 24.10.0 on Node 22.22.3. The source behind the click handling, hydration and Nuxt payloads is quoted in `references/rendering-modes.md`.

______________________________________________________________________

## Table of Contents

1. [The Short Answer](#1-the-short-answer)
2. [The Test App](#2-the-test-app)
3. [What Each Build Writes](#3-what-each-build-writes)
4. [Every Request, Traced](#4-every-request-traced)
5. [With Nuxt Link Prefetching On](#5-with-nuxt-link-prefetching-on)
6. [Why a Direct Visit Can Be a 404](#6-why-a-direct-visit-can-be-a-404)
7. [Check It Yourself](#7-check-it-yourself)
8. [Common Wrong Assumptions](#8-common-wrong-assumptions)

______________________________________________________________________

## 1. The Short Answer

```text
DIRECT VISIT  (address bar, refresh, new tab, crawler)        no app is running yet
  browser ──► GET /about (a document request) ──► server or CDN answers with HTML
    CSR:  the same empty shell as every route; JS then builds the page and calls the API
    SSR:  HTML rendered for this request, content included; JS then hydrates it
    SSG:  the prebuilt about/index.html, content included; JS then hydrates it

NAVBAR CLICK  (<RouterLink> / <NuxtLink>, app already running)
  link calls preventDefault() and history.pushState('/about')   no reload, no HTML
    browser ──► GET the About route chunk (unless already loaded or prefetched)
    browser ──► GET the data: the API (CSR, SSR) or about/_payload.json (Nuxt SSG)
    About's render() runs in the browser; the navbar and app state stay
```

| Action                                  | CSR                        | SSR                     | SSG                                     |
| --------------------------------------- | -------------------------- | ----------------------- | --------------------------------------- |
| Type `/about`, refresh, open in new tab | shell, JS renders `/about` | server renders `/about` | host serves prebuilt `about/index.html` |
| Click a `<RouterLink to="/about">`      | no reload, JS + data       | no reload, JS + data    | no reload, JS + data                    |
| Click a plain `<a href="/about">`       | full reload                | full reload             | full reload                             |

The rendering mode only decides what the first HTML contains. Every click after that is client-side navigation in all three.

______________________________________________________________________

## 2. The Test App

Two builds of the same two pages, Home and About, with a navbar of links. The About page shows a team list.

The CSR build is a Vite app with lazy routes, where About fetches the team in `onMounted`:

```js
// src/main.js
import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: () => import('./Home.vue') },
    { path: '/about', component: () => import('./About.vue') },
  ],
})
createApp(App).use(router).mount('#app')
```

```vue
<!-- src/App.vue -->
<template>
  <nav>
    <RouterLink id="home-link" to="/">Home</RouterLink>
    <RouterLink id="about-link" to="/about">About</RouterLink>
  </nav>
  <RouterView />
</template>
```

```vue
<!-- src/About.vue -->
<script setup>
import { ref, onMounted } from 'vue'
const team = ref(null)
onMounted(async () => {
  team.value = await (await fetch('/api/team.json')).json()
})
</script>

<template>
  <h1>About</h1>
  <ul v-if="team" id="team"><li v-for="m in team" :key="m">{{ m }}</li></ul>
  <p v-else>Loading team…</p>
</template>
```

The SSG build is the Nuxt app of `references/rendering-modes.md` section 6, where About runs `await useFetch('/api/team')` against a server route that returns the same list.

______________________________________________________________________

## 3. What Each Build Writes

```text
CSR: vite build → dist/
  dist/index.html                  the only HTML file: the empty shell, served for every route
  dist/assets/index-Dl737BAP.js    Vue, vue-router and App.vue (89 kB)
  dist/assets/Home-Cod2t5JD.js     lazy route chunk
  dist/assets/About-DS-MfZ3K.js    lazy route chunk
  dist/api/team.json               copied as-is from public/
```

```text
Nuxt SSG: nuxt generate → .output/public/   (plus a dist symlink to it in the project root)
  index.html                       prerendered Home
  _payload.json                    Home's data
  about/index.html                 prerendered About, list already in it: <li>Asha</li><li>Ben</li>
  about/_payload.json              About's useFetch data
  200.html, 404.html               fallback pages
  _nuxt/*.js                       entry, shared and page chunks
  _nuxt/builds/meta/<buildId>.json manifest: "prerendered":["/","/about"]
```

The CSR `dist/` has no `about` folder at all: `/about` exists only inside the router. The Nuxt output has one folder per prerendered route, each with its HTML and its data.

______________________________________________________________________

## 4. Every Request, Traced

Each build was served by a static file server, and headless Chrome recorded every request. A "direct visit" opens `http://localhost/about` in a new tab; a "click" loads `/` first, then clicks the About link. Link prefetching was disabled (`IntersectionObserver` stubbed) so the click column shows what the click itself fetches; with prefetching on, Nuxt loads those files earlier (section 5).

| Scenario                           | Requests, in order                                                                                                   | HTML the server sent                     | Document loads in `performance` |
| ---------------------------------- | -------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- | ------------------------------- |
| CSR, direct visit, SPA fallback    | document `/about` (200, the shell), `assets/index-*.js`, `assets/About-*.js`, fetch `/api/team.json`                 | empty `<div id="app"></div>`             | `/about`                        |
| CSR, direct visit, no SPA fallback | document `/about` (**404**)                                                                                          | the host's 404 page; the app never loads | `/about`                        |
| CSR, click from `/`                | `assets/About-*.js`, fetch `/api/team.json`                                                                          | none                                     | `/` only                        |
| Nuxt SSG, direct visit             | document `/about` (200, `about/index.html`), entry and page chunks, `/about/_payload.json` (preload), build manifest | full page with the team list             | `/about`                        |
| Nuxt SSG, click from `/`           | `_nuxt/<About chunk>.js`, fetch `/about/_payload.json`                                                               | none                                     | `/` only                        |

What the table shows:

- A direct visit is the only case with a document request. CSR gets the same shell for every path and builds the page in the browser, including the API call. SSG gets `about/index.html` with the content already in it, so the list is visible before any JavaScript runs.
- Without the SPA fallback, the CSR direct visit is a 404: `dist/` has no `about` file, and the router never gets to run. With `createWebHistory()` the host must answer unknown paths with `index.html`.
- A click never downloads HTML. CSR fetches the route chunk and calls the API; Nuxt fetches the route chunk and `about/_payload.json`, and never calls `/api/team`, which ran once at build time.
- `performance.getEntriesByType('navigation')` still lists only `/` after a click: the browser did not load a new document.

______________________________________________________________________

## 5. With Nuxt Link Prefetching On

By default a `<NuxtLink>` prefetches its target's chunk and `_payload.json` once the link is visible, so the click itself has almost nothing left to fetch. Loading `/` with prefetching on, then clicking the About link:

| Moment                                                    | Requests                                                                                                                                   |
| --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Loading `/` (link visible)                                | document `/`, `/_payload.json`, entry and shared chunks, then prefetches: the About page chunk, the build manifest, `/about/_payload.json` |
| Click, after the prefetch                                 | `/about/_payload.json` from the browser cache; no document, no new JS, no `/api/team`                                                      |
| Click, prefetch disabled (`IntersectionObserver` stubbed) | `/_nuxt/<About chunk>.js` and `/about/_payload.json`; no document, no `/api/team`                                                          |

In both runs the URL became `/about`, the list rendered, and the browser's navigation history still held only the first document load.

______________________________________________________________________

## 6. Why a Direct Visit Can Be a 404

With `createWebHistory()`, URLs look like `/about`, and a direct visit sends that path to the host. A static host looks for an `about` file, finds none in a CSR `dist/`, and answers 404 before any JavaScript runs. The fix is on the host: answer every unknown path with `index.html` (the SPA fallback), and the router renders `/about` from the URL. For example, on Cloudflare Workers static assets:

```toml
# wrangler.toml
[assets]
directory = "dist"
not_found_handling = "single-page-application"
```

`vite` and `vite preview` already answer unknown paths with `index.html` (with the default `appType: 'spa'`; `vite preview` returned it even for `/missing.js`), so the 404 shows up only on the real host. `createWebHashHistory()` avoids the problem, because a URL like `/#/about` always requests `/`: the part after `#` never reaches the server. A Nuxt static build has a real `about/index.html` for every prerendered route, so those need no fallback.

______________________________________________________________________

## 7. Check It Yourself

1. Open DevTools, Network tab, filter **Doc**.
2. Click a navbar link. No row appears: no document was requested.
3. Press F5. One row appears: the direct visit.
4. In the Console, list the documents this tab has loaded. After any number of clicks it still shows only the first one:
   ```js
   performance.getEntriesByType('navigation').map(e => e.name)
   ```
5. Filter **Fetch/XHR** and **JS** and click again to see what a click does fetch: the route chunk and the data.

A row under **Doc** after a click means a full page load: a plain `<a href>`, a click with Ctrl, Shift, Alt or Meta held, or `target="_blank"`.

______________________________________________________________________

## 8. Common Wrong Assumptions

| Assumption                                                        | What happens                                                                                             |
| ----------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| SSG means every click loads that page's prebuilt HTML             | A click fetches the route chunk and `_payload.json`; the HTML is only for direct visits (section 4)      |
| SSR means the server renders every page the user clicks to        | The server renders the first document; clicks render in the browser (section 1)                          |
| A CSR app has one HTML file per route in `dist/`                  | It has one `index.html` for all routes (section 3)                                                       |
| If clicking to `/about` works, refreshing `/about` works          | Only with the host's SPA fallback; otherwise the refresh is a 404 (sections 4, 6)                        |
| A direct visit and a click run the same requests                  | Only a direct visit requests a document; a click fetches the chunk and data (section 4)                  |
| A Nuxt static page calls its API when the user clicks to it       | It reads `about/_payload.json`; the API ran once at build time (section 4)                               |
| A direct visit to a Nuxt static page has all its data in the HTML | The content is rendered in the HTML, but the data is fetched from `_payload.json` by default (section 4) |
