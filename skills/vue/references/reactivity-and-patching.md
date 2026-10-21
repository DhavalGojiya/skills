# How a Vue Component Updates the Page

What runs in the browser when a `ref` changes: the template compiled to a render function, the `ref` that records who read it, the component that re-renders, and the patch that turns the difference into a few DOM calls.

Verified against Vue [3.5.44](https://github.com/vuejs/core/tree/v3.5.44) source. Every "Source:" link points at the commit the tag names, with line numbers. Compiler output comes from `@vue/compiler-dom` and `@vue/compiler-sfc` 3.5.44; DOM traces come from Vue 3.5.44 running in jsdom 26 with `Node.prototype` methods wrapped to log each call, in dev and with `NODE_ENV=production`, and match what `scripts/reactivity_playground.html` shows in a browser.

______________________________________________________________________

## Table of Contents

1. [The Whole Path in One Picture](#1-the-whole-path-in-one-picture)
2. [The Template Becomes a Render Function](#2-the-template-becomes-a-render-function)
3. [`ref` Records Its Readers and Re-runs Them](#3-ref-records-its-readers-and-re-runs-them)
4. [What Re-renders: the Whole Component, Once per Tick](#4-what-re-renders-the-whole-component-once-per-tick)
5. [The Patch: Old Tree, New Tree, a Few DOM Calls](#5-the-patch-old-tree-new-tree-a-few-dom-calls)
6. [Which State Is Shared](#6-which-state-is-shared)
7. [The Playground](#7-the-playground)
8. [Common Wrong Assumptions](#8-common-wrong-assumptions)

______________________________________________________________________

## 1. The Whole Path in One Picture

```text
BUILD TIME (@vitejs/plugin-vue, @vue/compiler-sfc)
  <template> with v-if, {{ }}, :prop     ──►  render(): plain JS returning vnodes
                                               (no template compiler ships to the browser)

IN THE BROWSER (Vue runtime chunk + your compiled components)
  your code:  isAdmin.value = true
                 │
                 ▼  ref setter: value changed? → dep.trigger()
  every effect that READ isAdmin during its last run is marked dirty
                 │
                 ▼  scheduler: queueJob(component update), flushed in a microtask
  the component's whole render() runs again → new vnode tree
                 │
                 ▼  patch(oldTree from memory, newTree)
  only the differences become DOM calls:
      parent.removeChild(<!--v-if-->)
      a.textContent = "Admin"
      parent.insertBefore(a, <span class="user">)
```

The template describes the page for every possible value of the state. Your code only changes state. Vue writes the DOM update code for you, at build time (the render function) and at runtime (the patch).

______________________________________________________________________

## 2. The Template Becomes a Render Function

The browser never reads `.vue` files or template syntax. `@vitejs/plugin-vue` compiles each template into a JavaScript function. This template:

```vue
<template>
  <nav>
    <a href="/home">Home</a>
    <a href="/about">About</a>
    <a v-if="isAdmin" href="/admin">Admin</a>
    <span>{{ userName }}</span>
  </nav>
</template>
```

compiles to (`compile(template, { mode: 'module', hoistStatic: true })` from `@vue/compiler-dom`):

```js
const _hoisted_1 = {
  key: 0,
  href: "/admin"
}

export function render(_ctx, _cache) {
  return (_openBlock(), _createElementBlock("nav", null, [
    _cache[0] || (_cache[0] = _createElementVNode("a", { href: "/home" }, "Home", -1 /* CACHED */)),
    _cache[1] || (_cache[1] = _createElementVNode("a", { href: "/about" }, "About", -1 /* CACHED */)),
    (_ctx.isAdmin)
      ? (_openBlock(), _createElementBlock("a", _hoisted_1, "Admin"))
      : _createCommentVNode("v-if", true),
    _createElementVNode("span", null, _toDisplayString(_ctx.userName), 1 /* TEXT */)
  ]))
}
```

What to read in it:

| Template                              | Compiled                                                           | Meaning                                                                               |
| ------------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------- |
| `v-if="isAdmin"` / no `v-else`        | `(_ctx.isAdmin) ? ... : _createCommentVNode("v-if", true)`         | `v-if` is a ternary; the false branch is a comment placeholder                        |
| Static `<a>Home</a>`                  | `_cache[0] \|\| (_cache[0] = ...)`, flag `-1 /* CACHED */`         | Created on the first render, reused afterwards, skipped by the patch and by hydration |
| `{{ userName }}`                      | `_toDisplayString(_ctx.userName)`, flag `1 /* TEXT */`             | The patch only compares this element's text                                           |
| `href="/admin"` on the `v-if`         | `_hoisted_1`, a module-level object                                | Static props objects are hoisted; static vnodes are cached in `_cache` (Vue 3.5)      |
| `_openBlock()` / `createElementBlock` | a block that collects its dynamic descendants in `dynamicChildren` | The patch walks that flat list instead of the whole tree                              |

The `-1` flag:

Source: [`packages/shared/src/patchFlags.ts#L111-L115`](https://github.com/vuejs/core/blob/bf5f0dd877dd3c23ce85c2cbc1dd771567e84c8c/packages/shared/src/patchFlags.ts#L111-L115)

```ts
  /**
   * Indicates a cached static vnode. This is also a hint for hydration to skip
   * the entire sub tree since static content never needs to be updated.
   */
  CACHED = -1,
```

### The comment text exists only in dev

Source: [`packages/compiler-core/src/transforms/vIf.ts#L225-L233`](https://github.com/vuejs/core/blob/bf5f0dd877dd3c23ce85c2cbc1dd771567e84c8c/packages/compiler-core/src/transforms/vIf.ts#L225-L233)

```ts
    return createConditionalExpression(
      branch.condition,
      createChildrenCodegenNode(branch, keyIndex, context),
      // make sure to pass in asBlock: true so that the comment node call
      // closes the current block.
      createCallExpression(context.helper(CREATE_COMMENT), [
        __DEV__ ? '"v-if"' : '""',
        'true',
      ]),
```

A false `v-if` is `<!--v-if-->` in the dev server and `<!---->` in a production build. Both are real DOM comment nodes, visible in DevTools Elements.

### A directive value is a JavaScript expression

The quotes in `v-if="isAdmin"` are HTML attribute syntax. The text inside is compiled as JavaScript, so it names a variable:

```vue
<template>
  <div class="team">plain attribute: the string "team"</div>
  <div :class="team">bound with ":": the variable team</div>
  <div v-if="team && team.length > 0">directive: any JS expression</div>
  <div v-if="user.role === 'admin'">single quotes inside for a JS string</div>
</template>
```

Only expressions work (they are dropped into a ternary or an argument list), so `v-if="const x = 1"` fails to compile; put logic in a `computed`. In `<script setup>` the compiler resolves the name against the file's own bindings. The same `<script setup>` compiled both ways (`compileScript(descriptor, { inlineTemplate })` from `@vue/compiler-sfc`):

```vue
<script setup>
import { ref } from 'vue'
const team = ref(null)
</script>

<template>
  <ul v-if="team"><li v-for="m in team" :key="m">{{ m }}</li></ul>
  <p v-else>Loading team…</p>
</template>
```

| Build                                 | How the template reads `team`                                                                                                      |
| ------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| Dev server (`inlineTemplate: false`)  | `setup()` returns `__returned__ = { team, ref }`; a separate render function reads `$setup.team`, which the instance proxy unwraps |
| `vite build` (`inlineTemplate: true`) | the render function is a closure inside `setup()` and reads `team.value` directly                                                  |

```js
// vite build shape, from compileScript with inlineTemplate: true
setup(__props) {

const team = ref(null)

return (_ctx, _cache) => {
  return (team.value)
    ? (_openBlock(), _createElementBlock("ul", _hoisted_1, [
        (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(team.value, (m) => {
          return (_openBlock(), _createElementBlock("li", { key: m }, _toDisplayString(m), 1 /* TEXT */))
        }), 128 /* KEYED_FRAGMENT */))
      ]))
    : (_openBlock(), _createElementBlock("p", _hoisted_2, "Loading team…"))
}
}
```

That is why the template writes `team` and the script writes `team.value`: the compiler adds the `.value`. Reading `.value` inside the render is also what subscribes the component to `team` (section 3).

A name the component does not define is not a compile error. It reads as `undefined` (so the `v-if` block never shows), and in dev Vue warns:

Source: [`packages/runtime-core/src/componentPublicInstance.ts#L557-L561`](https://github.com/vuejs/core/blob/bf5f0dd877dd3c23ce85c2cbc1dd771567e84c8c/packages/runtime-core/src/componentPublicInstance.ts#L557-L561)

```ts
      } else if (instance === currentRenderingInstance) {
        warn(
          `Property ${JSON.stringify(key)} was accessed during render ` +
            `but is not defined on instance.`,
        )
```

______________________________________________________________________

## 3. `ref` Records Its Readers and Re-runs Them

A `ref` is an object with a getter and a setter. The getter records whoever is running ("track"); the setter re-runs them ("trigger"). This standalone version has the same shape and runs with `node mini-reactivity.mjs`:

```js
let activeEffect = null // the effect that is running right now

function ref(initial) {
  const subscribers = new Set() // Vue calls this list the ref's `dep`
  let value = initial
  return {
    get value() {
      if (activeEffect) subscribers.add(activeEffect) // track: remember the reader
      return value
    },
    set value(next) {
      if (Object.is(next, value)) return // same value: nobody re-runs
      value = next
      subscribers.forEach(run => run()) // trigger: re-run every reader
    },
  }
}

function effect(fn) {
  const run = () => {
    activeEffect = run
    fn()
    activeEffect = null
  }
  run()
}

const team = ref(null)
effect(() => console.log('Header render:', team.value ? team.value.length : 0))
effect(() => console.log('Footer render')) // never reads team
team.value = ['Asha', 'Ben']
team.value = team.value // same array: no re-render
console.log(team.value.length) // read outside any effect: subscribes nothing
```

Output:

```text
Header render: 0
Footer render
Header render: 2
2
```

```text
effect(Header) runs   activeEffect = Header, reads team.value → team's list: { Header }
effect(Footer) runs   activeEffect = Footer, never reads team → team's list: { Header }
team.value = [...]    setter → Header re-runs; Footer is not on the list
team.value = same     Object.is → no change → nothing runs
```

`activeEffect` is a global because `team.value` takes no argument: the getter cannot be told who is reading, so the runner announces itself before it runs. JavaScript runs one effect at a time, so the global is always the right one.

### The real getter and setter

Source: [`packages/reactivity/src/ref.ts#L129-L164`](https://github.com/vuejs/core/blob/bf5f0dd877dd3c23ce85c2cbc1dd771567e84c8c/packages/reactivity/src/ref.ts#L129-L164)

```ts
  get value() {
    if (__DEV__) {
      this.dep.track({
        target: this,
        type: TrackOpTypes.GET,
        key: 'value',
      })
    } else {
      this.dep.track()
    }
    return this._value
  }

  set value(newValue) {
    const oldValue = this._rawValue
    const useDirectValue =
      this[ReactiveFlags.IS_SHALLOW] ||
      isShallow(newValue) ||
      isReadonly(newValue)
    newValue = useDirectValue ? newValue : toRaw(newValue)
    if (hasChanged(newValue, oldValue)) {
      this._rawValue = newValue
      this._value = useDirectValue ? newValue : toReactive(newValue)
      if (__DEV__) {
        this.dep.trigger({
          target: this,
          type: TriggerOpTypes.SET,
          key: 'value',
          newValue,
          oldValue,
        })
      } else {
        this.dep.trigger()
      }
    }
  }
```

`track()` does nothing unless an effect is running, which is why a read in `onMounted` or a click handler subscribes nothing:

Source: [`packages/reactivity/src/dep.ts#L108-L115`](https://github.com/vuejs/core/blob/bf5f0dd877dd3c23ce85c2cbc1dd771567e84c8c/packages/reactivity/src/dep.ts#L108-L115)

```ts
  track(debugInfo?: DebuggerEventExtraInfo): Link | undefined {
    if (!activeSub || !shouldTrack || activeSub === this.computed) {
      return
    }

    let link = this.activeLink
    if (link === undefined || link.sub !== activeSub) {
      link = this.activeLink = new Link(activeSub, this)
```

"Changed" means `Object.is`, so assigning the same primitive or the same object reference triggers nothing:

Source: [`packages/shared/src/general.ts#L143-L144`](https://github.com/vuejs/core/blob/bf5f0dd877dd3c23ce85c2cbc1dd771567e84c8c/packages/shared/src/general.ts#L143-L144)

```ts
export const hasChanged = (value: any, oldValue: any): boolean =>
  !Object.is(value, oldValue)
```

Two consequences:

- `team = data` (without `.value`) replaces the local variable. The setter never runs, nothing is triggered, and the page never updates.
- A plain `let isAdmin = false` has no getter or setter, so no render subscribes to it and changing it changes nothing on screen.

Subscriptions are rebuilt on every run. If a render reads `loading` only inside a `v-else` branch, then once the `v-if` branch shows, that render no longer depends on `loading`. `computed`, `watch` and `watchEffect` are effects too and subscribe the same way.

______________________________________________________________________

## 4. What Re-renders: the Whole Component, Once per Tick

Each component instance gets one effect that wraps its render, and that effect's scheduler queues a job instead of running at once:

Source: [`packages/runtime-core/src/renderer.ts#L1628-L1635`](https://github.com/vuejs/core/blob/bf5f0dd877dd3c23ce85c2cbc1dd771567e84c8c/packages/runtime-core/src/renderer.ts#L1628-L1635)

```ts
    const effect = (instance.effect = new ReactiveEffect(componentUpdateFn))
    instance.scope.off()

    const update = (instance.update = effect.run.bind(effect))
    const job: SchedulerJob = (instance.job = effect.runIfDirty.bind(effect))
    job.i = instance
    job.id = instance.uid
    effect.scheduler = () => queueJob(job)
```

The queue is flushed once, in a microtask after the current code finishes:

Source: [`packages/runtime-core/src/scheduler.ts#L120-L124`](https://github.com/vuejs/core/blob/bf5f0dd877dd3c23ce85c2cbc1dd771567e84c8c/packages/runtime-core/src/scheduler.ts#L120-L124)

```ts
function queueFlush() {
  if (!currentFlushPromise) {
    currentFlushPromise = resolvedPromise.then(flushJobs)
  }
}
```

So the unit of re-rendering is the component, and the unit of time is the tick. Given:

```vue
<!-- Navbar.vue: reads isAdmin and userName -->
<template>
  <nav>
    <a href="/home">Home</a>
    <a v-if="isAdmin" href="/admin">Admin</a>
    <UserMenu :name="userName" />
  </nav>
</template>
```

```vue
<!-- App.vue -->
<template>
  <Navbar />
  <Footer />  <!-- reads neither ref -->
</template>
```

the traced runs counted re-renders with each component's `updated` hook:

| Change                                           | Navbar re-renders          | UserMenu re-renders              | Footer re-renders |
| ------------------------------------------------ | -------------------------- | -------------------------------- | ----------------- |
| `isAdmin.value = true`                           | once, the whole `render()` | no: its `name` prop is unchanged | no                |
| `isAdmin.value = true` again (same value)        | no (`Object.is`)           | no                               | no                |
| `isAdmin` set three times in one synchronous run | once                       | no                               | no                |
| `userName.value = 'Ben'`                         | once                       | once: its `name` prop changed    | no                |

A child is skipped when the props the compiler marked dynamic are unchanged:

Source: [`packages/runtime-core/src/componentRenderUtils.ts#L407-L418`](https://github.com/vuejs/core/blob/bf5f0dd877dd3c23ce85c2cbc1dd771567e84c8c/packages/runtime-core/src/componentRenderUtils.ts#L407-L418)

```ts
    } else if (patchFlag & PatchFlags.PROPS) {
      const dynamicProps = nextVNode.dynamicProps!
      for (let i = 0; i < dynamicProps.length; i++) {
        const key = dynamicProps[i]
        if (
          hasPropValueChanged(nextProps!, prevProps!, key) &&
          !isEmitListener(emits, key)
        ) {
          return true
        }
      }
    }
```

Re-running a 1000-line template's `render()` because one line reads `isAdmin` is cheap: cached static vnodes are reused, and the patch visits only the block's `dynamicChildren`. When one part of a large component changes very often (a timer, a live ticker), move that part into a child component so only the child re-renders.

The DOM is updated after the flush, not when you assign. `await nextTick()` waits for that flush, for example before measuring an element that a `v-if` just added.

______________________________________________________________________

## 5. The Patch: Old Tree, New Tree, a Few DOM Calls

Vue keeps the previous vnode tree in memory as `instance.subTree` and compares it with the new one. It never reads the real DOM to find out what is there:

Source: [`packages/runtime-core/src/renderer.ts#L1564-L1584`](https://github.com/vuejs/core/blob/bf5f0dd877dd3c23ce85c2cbc1dd771567e84c8c/packages/runtime-core/src/renderer.ts#L1564-L1584)

```ts
        const nextTree = renderComponentRoot(instance)
        if (__DEV__) {
          endMeasure(instance, `render`)
        }
        const prevTree = instance.subTree
        instance.subTree = nextTree

        if (__DEV__) {
          startMeasure(instance, `patch`)
        }
        patch(
          prevTree,
          nextTree,
          // parent may have changed if it's in a teleport
          hostParentNode(prevTree.el!)!,
          // anchor may have changed if it's in a fragment
          getNextHostNode(prevTree),
          instance,
          parentSuspense,
          namespace,
        )
```

The newest render is the source of truth, and the old tree is only used to work out the difference. Editing Vue-managed DOM by hand (`querySelector(...).remove()`) makes the page disagree with `subTree`, and the next patch acts on a tree that is no longer there. Change a `ref` instead.

### `v-if` switching: remove the old node, insert at its anchor

When the old and new vnodes in a slot have different types (comment versus `<a>`), the patch saves the node that follows the old one, unmounts the old one and mounts the new one before that saved node:

Source: [`packages/runtime-core/src/renderer.ts#L400-L404`](https://github.com/vuejs/core/blob/bf5f0dd877dd3c23ce85c2cbc1dd771567e84c8c/packages/runtime-core/src/renderer.ts#L400-L404)

```ts
    if (n1 && !isSameVNodeType(n1, n2)) {
      anchor = getNextHostNode(n1)
      unmount(n1, parentComponent, parentSuspense, true)
      n1 = null
    }
```

`anchor` is the old node's `nextSibling` (`getNextHostNode`), saved before the unmount because a removed node has no siblings. The placeholder comment is what gives a hidden `v-if` a position in the DOM to anchor on.

The DOM calls themselves are thin wrappers in `runtime-dom`:

Source: [`packages/runtime-dom/src/nodeOps.ts#L45-L54`](https://github.com/vuejs/core/blob/bf5f0dd877dd3c23ce85c2cbc1dd771567e84c8c/packages/runtime-dom/src/nodeOps.ts#L45-L54)

```ts
  insert: (child, parent, anchor) => {
    parent.insertBefore(child, anchor || null)
  },

  remove: child => {
    const parent = child.parentNode
    if (parent) {
      parent.removeChild(child)
    }
  },
```

Source: [`packages/runtime-dom/src/nodeOps.ts#L75-L83`](https://github.com/vuejs/core/blob/bf5f0dd877dd3c23ce85c2cbc1dd771567e84c8c/packages/runtime-dom/src/nodeOps.ts#L75-L83)

```ts
  createComment: text => doc.createComment(text),

  setText: (node, text) => {
    node.nodeValue = text
  },

  setElementText: (el, text) => {
    el.textContent = text
  },
```

`insertBefore(child, null)` appends, so a placeholder that was the last child needs no special case.

### Traced DOM calls

Starting DOM (dev):

```html
<nav><a href="/home">Home</a><!--v-if--><span class="user">Asha</span></nav><footer>Footer</footer>
```

| Change                   | DOM calls Vue made, in order                                                                                                                      |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `isAdmin.value = true`   | `removeChild(<!--v-if-->)`, `textContent = "Admin"` (on the new, detached `<a>`), `insertBefore(<a href="/admin">Admin</a>, <span class="user">)` |
| `isAdmin.value = false`  | `removeChild(<a href="/admin">Admin</a>)`, `insertBefore(<!--v-if-->, <span class="user">)`                                                       |
| `userName.value = 'Ben'` | `textContent = "Ben"` on the `<span>`                                                                                                             |
| Same value again         | none                                                                                                                                              |
| Production build         | the same calls with `<!---->` in place of `<!--v-if-->`                                                                                           |

Other bindings, traced the same way:

| Binding change                               | DOM call                                                                    |
| -------------------------------------------- | --------------------------------------------------------------------------- |
| `<span>count {{ n }} here</span>`, `n` 1 → 2 | `textContent = "count 2 here"` on that span                                 |
| `:class="cls"`, `'a'` → `'b'`                | `className = "b"`                                                           |
| `:href="href"` on `<a>`                      | `el.href = "/y"` (a DOM property, because `href in el`; not `setAttribute`) |
| `v-show` false                               | `style.display = "none"`; the element stays                                 |
| `v-if` false                                 | the element is removed; a comment takes its place                           |
| `v-html="html"`                              | `innerHTML = "<i>2</i>"`, the only binding that sets `innerHTML`            |

`textContent` is the normal way text is updated, but only on the node whose text changed. Nothing re-assigns a parent's `innerHTML` to update a page.

`v-if` versus `v-show` follows from this. `v-if` creates and destroys the element (and runs a child component's `setup()` and `onMounted` again each time it appears); `v-show` keeps it and toggles `style.display`. Use `v-show` for things that toggle often, and `v-if` for things that rarely appear, so the hidden part is never built.

Lists use the same anchor logic to insert and move rows, and match old and new rows by `:key`. Without a stable key, Vue patches rows in place by position, which can leave one row's input state on another row.

______________________________________________________________________

## 6. Which State Is Shared

Vue tracks the `ref` object, not the variable name. Two variables share state only when they hold the same object:

| Where the `ref()` call is                                  | How many objects          | Who re-renders when it changes       |
| ---------------------------------------------------------- | ------------------------- | ------------------------------------ |
| `<script setup>` of `Card.vue`                             | one per `<Card>` instance | only that instance                   |
| Three files that each declare `const isAdmin = ref(false)` | three, unrelated          | only the file's own instances        |
| Top level of a plain `.js` module                          | one for the app           | every component whose render read it |
| Inside an exported `useThing()` function                   | one per call              | only the caller                      |
| Pinia store state                                          | one per store for the app | every component whose render read it |

```js
// composables/useDialog.js
import { ref } from 'vue'

const isOpen = ref(false) // module level: every caller gets this same object

export function useDialog() {
  const draft = ref('') // inside the function: a new object per call
  return { isOpen, draft }
}
```

ES modules are evaluated once, so a module-level `ref` is one object however many files import it. In a browser that is one per tab. On an SSR server the module is shared by every request, so a module-level `ref` set during one user's render is visible to the next user's render; keep request state in `setup()`, or in Pinia with a new `createPinia()` for each request's app.

Why same-named variables in different `.vue` files never clash, and which `<script setup>` constants are hoisted to module scope, is in `references/sfc-compilation.md` sections 1, 4 and 7.

______________________________________________________________________

## 7. The Playground

`scripts/reactivity_playground.html` is one HTML file that loads Vue 3.5.44 from unpkg. Open it in a browser (`xdg-open scripts/reactivity_playground.html` or `open` on macOS). It wraps the DOM methods Vue calls before Vue loads, and for each button shows:

1. The DOM calls the last change made, the live HTML with its `<!--v-if-->` comment, and re-render counts for a navbar, its child and a footer.
2. `v-if` and `v-show` side by side in the live HTML.
3. Three instances of one component, each with its own `isAdmin`, next to one module-level `sharedAdmin`.
4. A `ref(null)` that renders "Loading team…" until a timer sets it.

______________________________________________________________________

## 8. Common Wrong Assumptions

| Assumption                                              | What happens                                                                                                        |
| ------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| The browser runs the template                           | It runs `render()`, compiled at build time; the template compiler is not shipped (section 2)                        |
| `v-if="team"` is a string                               | It is the JavaScript expression `team`, compiled against the file's bindings (section 2)                            |
| Only the template line that uses a ref re-runs          | The component's whole `render()` re-runs; the patch then changes only the differing nodes (sections 4 and 5)        |
| Every component re-renders when any state changes       | Only components whose last render read that ref, and children whose dynamic props changed (section 4)               |
| Each assignment re-renders immediately                  | Jobs are queued and flushed once per microtask; use `await nextTick()` to wait for the DOM (section 4)              |
| Vue diffs against the real DOM                          | It diffs against the previous vnode tree kept in `instance.subTree` (section 5)                                     |
| A hidden `v-if` leaves nothing in the DOM               | It leaves a comment, `<!--v-if-->` in dev and `<!---->` in production, used as the insertion anchor (sections 2, 5) |
| Updates rebuild the page with `innerHTML`               | Only `v-html` sets `innerHTML`; text changes set `textContent` or `nodeValue` on one node (section 5)               |
| Bound attributes always use `setAttribute`              | A key that exists as a DOM property (`href`, `value`) is assigned as a property (section 5)                         |
| Static template parts are hoisted out of `render()`     | In Vue 3.5 static vnodes are cached in `_cache` on first render; only static props objects are hoisted (section 2)  |
| Same variable name in two components means shared state | Each `ref()` call makes its own object; share by importing one object (section 6)                                   |
