# /// script
# requires-python = ">=3.14"
# ///
"""Print what @vitejs/plugin-vue compiles a .vue file to, in dev and in a production build.

Runs the Vite installed in the project. The dev output comes from the dev server's
transform pipeline with HMR off (the browser also gets HMR code). The build output comes
from an unminified build kept in memory, so dist/ is not touched. --demo creates a
throwaway project (Vue 3.5.44, @vitejs/plugin-vue 6.0.10) whose components reproduce
each case in SKILL.md. Needs node and npm on PATH.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEMO_DEPENDENCIES = {"vue": "3.5.44", "@vitejs/plugin-vue": "6.0.10"}

NODE_DEV = r"""
const { createServer } = await import('vite')
const { files } = JSON.parse(process.env.SFC_TRACE)
const server = await createServer({
  logLevel: 'silent',
  server: { middlewareMode: true, hmr: false, ws: false },
})
const env = server.environments?.client ?? server
for (const file of files) {
  const { code } = await env.transformRequest('/' + file)
  console.log(`===== dev: ${file}\n${code.trim()}\n`)
}
await server.close()
"""

# Rolldown (Vite 8) wraps each module of the final chunk in //#region <path>. Rollup
# (Vite 7) has no markers, so its chunk.modules code is used: renamed like the chunk but
# formatted before the last render pass. A .vue file can span several modules
# (?vue&type=script for lang="ts"); its style modules render no JavaScript.
NODE_BUILD = r"""
const path = await import('node:path')
const { build } = await import('vite')
const { files } = JSON.parse(process.env.SFC_TRACE)
const result = await build({ logLevel: 'silent', build: { write: false, minify: false } })
const chunks = [result].flat().flatMap((r) => r.output).filter((o) => o.type === 'chunk')
const belongs = (id, file) => (id === file || id.startsWith(file + '?')) && !id.includes('type=style')
console.log(`(build ran with NODE_ENV=${process.env.NODE_ENV})\n`)
for (const file of files) {
  const out = []
  for (const chunk of chunks) {
    if (chunk.code.includes('\n//#region ')) {
      let inside = false
      for (const line of chunk.code.split('\n')) {
        if (line.startsWith('//#region ')) inside = belongs(line.slice(10), file)
        else if (line === '//#endregion') inside = false
        else if (inside) out.push(line)
      }
    } else {
      const abs = path.join(process.cwd(), file)
      for (const [id, mod] of Object.entries(chunk.modules)) {
        if (belongs(id, abs)) out.push(mod.code)
      }
    }
  }
  console.log(`===== build: ${file}\n${out.join('\n').trim() || '(not in the bundle)'}\n`)
}
"""

DEMO_FILES = {
    "vite.config.js": """\
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({ plugins: [vue()] })
""",
    "index.html": """\
<!doctype html>
<div id="app"></div>
<script type="module" src="/src/main.js"></script>
""",
    "src/main.js": """\
import { createApp } from 'vue'
import App from './App.vue'

createApp(App).mount('#app')
""",
    "src/App.vue": """\
<script setup>
import Navbar from './components/Navbar.vue'
import Login from './components/Login.vue'
import Counter from './components/Counter.vue'
import Shared from './components/Shared.vue'
import ProfilePage from './pages/profile/index.vue'
</script>

<template>
  <Navbar />
  <Login />
  <Counter />
  <Shared />
  <ProfilePage />
</template>
""",
    "src/components/Navbar.vue": """\
<script setup>
const a = 'Hello from Navbar'
console.log(a)
</script>

<template>
  <nav>{{ a }}</nav>
</template>
""",
    "src/components/Login.vue": """\
<script setup>
const a = 'Hello from Login'
console.log(a)
</script>

<template>
  <form>{{ a }}</form>
</template>
""",
    "src/components/Counter.vue": """\
<script setup>
import { ref } from 'vue'

const label = 'Clicks' // literal: hoisted to module scope
const step = 1 + 1 // expression of literals: hoisted
const limits = { max: 10 } // object: stays in setup()
let renders = 0 // let: stays in setup()
const count = ref(0) // call: stays in setup()
function inc() {
  count.value += step
}
</script>

<template>
  <button @click="inc">{{ label }} {{ count }}/{{ limits.max }} {{ renders }}</button>
</template>

<style scoped>
button { color: red }
</style>
""",
    "src/components/Shared.vue": """\
<script>
let instances = 0 // module scope: one counter shared by every <Shared>
</script>

<script setup>
const title = 'Shared' // not hoisted: a plain <script> block turns hoisting off
instances++
</script>

<template>
  <p>{{ title }} {{ instances }}</p>
</template>
""",
    "src/pages/profile/index.vue": """\
<script setup>
const msg = 'Profile'
</script>

<template>
  <div>{{ msg }}</div>
</template>
""",
}

DEMO_SHOWN = [
    "src/components/Navbar.vue",
    "src/components/Login.vue",
    "src/components/Counter.vue",
    "src/components/Shared.vue",
    "src/pages/profile/index.vue",
]


def run_node(code: str, root: Path, files: list[str]) -> None:
    env = {**os.environ, "SFC_TRACE": json.dumps({"files": files})}
    subprocess.run(
        ["node", "--input-type=module", "-e", code], cwd=root, env=env, check=True
    )


def make_demo(root: Path, vite_version: str) -> None:
    package = {
        "name": "sfc-output-demo",
        "private": True,
        "type": "module",
        "dependencies": {"vue": DEMO_DEPENDENCIES["vue"]},
        "devDependencies": {
            "vite": vite_version,
            "@vitejs/plugin-vue": DEMO_DEPENDENCIES["@vitejs/plugin-vue"],
        },
    }
    (root / "package.json").write_text(json.dumps(package, indent=2))
    for name, text in DEMO_FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    print(f"Installing vite@{vite_version} into {root} ...", file=sys.stderr)
    subprocess.run(
        ["npm", "install", "--no-audit", "--no-fund", "--loglevel=error"],
        cwd=root,
        check=True,
        stdout=subprocess.DEVNULL,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "files", nargs="*", help=".vue files, relative to the Vite root or absolute"
    )
    parser.add_argument(
        "--root", default=".", help="Vite root, where vite.config.* lives (default: .)"
    )
    parser.add_argument("--demo", action="store_true", help="use a throwaway project")
    parser.add_argument(
        "--vite", default="8.3.4", help="Vite version for --demo (default: 8.3.4)"
    )
    stage = parser.add_mutually_exclusive_group()
    stage.add_argument("--dev-only", action="store_true")
    stage.add_argument("--build-only", action="store_true")
    args = parser.parse_args()

    for tool in ("node", "npm"):
        if shutil.which(tool) is None:
            parser.error(f"{tool} is not on PATH")
    if not args.demo and not args.files:
        parser.error("give one or more .vue files, or --demo")

    with tempfile.TemporaryDirectory(prefix="sfc-output-") as tmp:
        if args.demo:
            root = Path(tmp)
            make_demo(root, args.vite)
            files = DEMO_SHOWN
        else:
            root = Path(args.root).resolve()
            files = []
            for name in args.files:
                path = (root / name).resolve()
                if not path.is_relative_to(root):
                    parser.error(f"{name} is outside the Vite root {root}")
                files.append(path.relative_to(root).as_posix())

        if not args.build_only:
            run_node(NODE_DEV, root, files)
        if not args.dev_only:
            run_node(NODE_BUILD, root, files)
    return 0


if __name__ == "__main__":
    sys.exit(main())
