# How Vite Loads .env Files

How Vite turns a command and a mode into a set of `.env` files, merges and parses them, and decides what reaches `import.meta.env`, `vite.config.*` and `NODE_ENV`.

Verified against Vite [8.3.4](https://github.com/vitejs/vite/tree/v8.3.4) source. Every "Source:" link points at the commit the tag names, with line numbers. The outputs come from `scripts/explain_env.py --demo` and from builds of a test project on Node 22.22.3. Vite 7.3.7 parses with dotenv 17 and dotenv-expand 12 instead of `util.parseEnv` and dotenv-expand 13; its `loadEnv` gave the same values for every case in this file.

______________________________________________________________________

## Table of Contents

01. [The Mode of Each Command](#1-the-mode-of-each-command)
02. [Which Files Are Read](#2-which-files-are-read)
03. [Merge Order](#3-merge-order)
04. [Parsing: `#`, Quotes and `$`](#4-parsing--quotes-and-)
05. [What Reaches Client Code](#5-what-reaches-client-code)
06. [When a Change Takes Effect](#6-when-a-change-takes-effect)
07. [`vite.config` and `process.env`](#7-viteconfig-and-processenv)
08. [Mode Versus `NODE_ENV`](#8-mode-versus-node_env)
09. [The Demo](#9-the-demo)
10. [Common Wrong Assumptions](#10-common-wrong-assumptions)

______________________________________________________________________

## 1. The Mode of Each Command

`vite` with no command starts the dev server; `serve` and `dev` are aliases:

Source: [`packages/vite/src/node/cli.ts#L203-L205`](https://github.com/vitejs/vite/blob/b6c20f6a8126d8561891e5fb9368e8fc9da37538/packages/vite/src/node/cli.ts#L203-L205)

```ts
  .command('[root]', 'start dev server') // default command
  .alias('serve') // the command is called 'serve' in Vite's API
  .alias('dev') // alias to align with the script name
```

The dev server resolves its config with the defaults of `resolveConfig`, mode `development` and `NODE_ENV` `development`:

Source: [`packages/vite/src/node/server/index.ts#L530-L532`](https://github.com/vitejs/vite/blob/b6c20f6a8126d8561891e5fb9368e8fc9da37538/packages/vite/src/node/server/index.ts#L530-L532)

```ts
  const config = isResolvedConfig(inlineConfig)
    ? inlineConfig
    : await resolveConfig(inlineConfig, 'serve')
```

`vite build` and `vite preview` pass `production` for both:

Source: [`packages/vite/src/node/build.ts#L586-L591`](https://github.com/vitejs/vite/blob/b6c20f6a8126d8561891e5fb9368e8fc9da37538/packages/vite/src/node/build.ts#L586-L591)

```ts
  return resolveConfig(
    inlineConfig,
    'build',
    'production',
    'production',
    false,
```

Source: [`packages/vite/src/node/preview.ts#L138-L144`](https://github.com/vitejs/vite/blob/b6c20f6a8126d8561891e5fb9368e8fc9da37538/packages/vite/src/node/preview.ts#L138-L144)

```ts
  const config = await resolveConfig(
    inlineConfig,
    'serve',
    'production',
    'production',
    true,
  )
```

`--mode` overrides the default:

Source: [`packages/vite/src/node/config.ts#L1479`](https://github.com/vitejs/vite/blob/b6c20f6a8126d8561891e5fb9368e8fc9da37538/packages/vite/src/node/config.ts#L1479)

```ts
  let mode = inlineConfig.mode || defaultMode
```

______________________________________________________________________

## 2. Which Files Are Read

The mode turns into exactly four file names in `envDir` (the Vite root unless `envDir` is set; `envDir: false` reads none):

Source: [`packages/vite/src/node/env.ts#L13-L27`](https://github.com/vitejs/vite/blob/b6c20f6a8126d8561891e5fb9368e8fc9da37538/packages/vite/src/node/env.ts#L13-L27)

```ts
export function getEnvFilesForMode(
  mode: string,
  envDir: string | false,
): string[] {
  if (envDir !== false) {
    return [
      /** default file */ `.env`,
      /** local file */ `.env.local`,
      /** mode file */ `.env.${mode}`,
      /** mode local file */ `.env.${mode}.local`,
    ].map((file) => normalizePath(path.join(envDir, file)))
  }

  return []
}
```

No other file is consulted. In dev the list is `.env`, `.env.local`, `.env.development`, `.env.development.local`; `.env.production` is not on it, so its contents cannot affect the dev server however it is written. `local` is reserved:

Source: [`packages/vite/src/node/env.ts#L41-L46`](https://github.com/vitejs/vite/blob/b6c20f6a8126d8561891e5fb9368e8fc9da37538/packages/vite/src/node/env.ts#L41-L46)

```ts
  if (mode === 'local') {
    throw new Error(
      `"local" cannot be used as a mode name because it conflicts with ` +
        `the .local postfix for .env files.`,
    )
  }
```

______________________________________________________________________

## 3. Merge Order

The four files are read in list order and flattened into one object. `Object.fromEntries` keeps the last value for a repeated key, so a later file overrides an earlier one key by key, and a missing file contributes nothing:

Source: [`packages/vite/src/node/env.ts#L53-L62`](https://github.com/vitejs/vite/blob/b6c20f6a8126d8561891e5fb9368e8fc9da37538/packages/vite/src/node/env.ts#L53-L62)

```ts
  const parsed = Object.fromEntries(
    envFiles.flatMap((filePath) => {
      const stat = tryStatSync(filePath)
      // Support FIFOs (named pipes) for apps like 1Password
      if (!stat || (!stat.isFile() && !stat.isFIFO())) return []

      const parsedEnv = parseEnv(fs.readFileSync(filePath, 'utf-8'))
      return Object.entries(parsedEnv as Record<string, string>)
    }),
  )
```

After the files, every prefixed variable already in `process.env` is copied over the result, which is why a shell or CI variable beats every file:

Source: [`packages/vite/src/node/env.ts#L96-L102`](https://github.com/vitejs/vite/blob/b6c20f6a8126d8561891e5fb9368e8fc9da37538/packages/vite/src/node/env.ts#L96-L102)

```ts
  // check if there are actual env variables starting with VITE_*
  // these are typically provided inline and should be prioritized
  for (const key in process.env) {
    if (prefixes.some((prefix) => key.startsWith(prefix))) {
      env[key] = process.env[key]!
    }
  }
```

The full order, highest first:

```text
1. process.env (shell, CI, inline VAR=x vite ...)
2. .env.<mode>.local
3. .env.<mode>
4. .env.local
5. .env
```

With `VITE_API_URL` set in each layer, `loadEnv` returned:

| Files present                                                                         | Mode          | `VITE_API_URL` from      |
| ------------------------------------------------------------------------------------- | ------------- | ------------------------ |
| `.env`, `.env.production`                                                             | `development` | `.env`                   |
| `.env`, `.env.production`                                                             | `production`  | `.env.production`        |
| `.env`, `.env.local`, `.env.development`, `.env.development.local`, `.env.production` | `development` | `.env.development.local` |
| same, without `.env.development.local`                                                | `development` | `.env.development`       |
| `.env`, `.env.local`, `.env.production`                                               | `development` | `.env.local`             |
| `.env`, `.env.local`, `.env.production`                                               | `production`  | `.env.production`        |
| `.env`, `.env.local`, `.env.development`, `.env.development.local`                    | `staging`     | `.env.local`             |
| any, plus `VITE_API_URL` in the shell                                                 | any           | the shell                |

______________________________________________________________________

## 4. Parsing: `#`, Quotes and `$`

Each file goes through Node's built-in `util.parseEnv` (section 3), then the merged object goes through `dotenv-expand`:

Source: [`packages/vite/src/node/env.ts#L78-L81`](https://github.com/vitejs/vite/blob/b6c20f6a8126d8561891e5fb9368e8fc9da37538/packages/vite/src/node/env.ts#L78-L81)

```ts
  // let environment variables use each other. make a copy of `process.env` so that `dotenv-expand`
  // doesn't re-assign the expanded values to the global `process.env`.
  const processEnv = { ...process.env } as DotenvPopulateInput
  expand({ parsed, processEnv })
```

`util.parseEnv` ends an unquoted value at the first `#`, with or without a space before it, and strips the quotes from a quoted value. Node 20.19.0, 22.22.3 and 24.22.0 gave the same results:

| Line in the file  | Value       |
| ----------------- | ----------- |
| `A=a#b`           | `"a"`       |
| `B=a #b`          | `"a"`       |
| `C=#ff0000`       | `""`        |
| `D="#ff0000"`     | `"#ff0000"` |
| `F="x" # comment` | `"x"`       |

`dotenv-expand` then replaces `$NAME` and `${NAME}` with the variable's value from the merged files or `process.env`, and with nothing when the name is unknown. The quotes are already gone by then, so quoting does not protect a `$`:

| Line in the file                   | Value                          |
| ---------------------------------- | ------------------------------ |
| `VITE_PW=abc$def`                  | `"abc"`                        |
| `VITE_PW='abc$def'`                | `"abc"`                        |
| `VITE_PW=abc\$def`                 | `"abc$def"`                    |
| `VITE_URL=https://${VITE_HOST}/v1` | `"https://api.example.com/v1"` |

Values are never converted: `VITE_FLAG=false` is `"false"`, `VITE_N=3` is `"3"`, `VITE_EMPTY=` is `""`.

______________________________________________________________________

## 5. What Reaches Client Code

Only prefixed keys from the files are kept:

Source: [`packages/vite/src/node/env.ts#L83-L88`](https://github.com/vitejs/vite/blob/b6c20f6a8126d8561891e5fb9368e8fc9da37538/packages/vite/src/node/env.ts#L83-L88)

```ts
  // only keys that start with prefix are exposed to client
  for (const [key, value] of Object.entries(parsed)) {
    if (prefixes.some((prefix) => key.startsWith(prefix))) {
      env[key] = value
    }
  }
```

In a build, each key becomes a compile-time replacement, and any other `import.meta.env.*` becomes `undefined`:

Source: [`packages/vite/src/node/plugins/define.ts#L41-L46`](https://github.com/vitejs/vite/blob/b6c20f6a8126d8561891e5fb9368e8fc9da37538/packages/vite/src/node/plugins/define.ts#L41-L46)

```ts
    if (isBundled) {
      for (const key in config.env) {
        const val = JSON.stringify(config.env[key])
        importMetaKeys[`import.meta.env.${key}`] = val
        importMetaEnvKeys[key] = val
      }
```

Source: [`packages/vite/src/node/plugins/define.ts#L127-L128`](https://github.com/vitejs/vite/blob/b6c20f6a8126d8561891e5fb9368e8fc9da37538/packages/vite/src/node/plugins/define.ts#L127-L128)

```ts
            define['import.meta.env'] = importMetaEnvVal
            define['import.meta.env.*'] = 'undefined'
```

With `.env` holding `VITE_API_URL=https://api.example.com`, `DB_PASSWORD=secret`, `VITE_COLOR=#ff0000` and `VITE_COLOR_Q="#ff0000"`, and `.env.production` holding `VITE_API_URL=https://prod.example.com`, this source:

```js
console.log('API', import.meta.env.VITE_API_URL)
console.log('SECRET', import.meta.env.DB_PASSWORD)
console.log('MODE', import.meta.env.MODE, 'PROD', import.meta.env.PROD, 'DEV', import.meta.env.DEV)
```

builds to:

```js
console.log("API", "https://prod.example.com");
console.log("SECRET", void 0);
console.log("MODE", "production", "PROD", true, "DEV", false);
```

`import.meta.env[name]` or a bare `import.meta.env` cannot be replaced key by key, so the whole object (`BASE_URL`, `DEV`, `MODE`, `PROD`, `SSR` and every prefixed variable) is inlined there.

In dev nothing is replaced. The dev server prepends the object to each module that mentions `import.meta.env`:

Source: [`packages/vite/src/node/plugins/importAnalysis.ts#L236-L251`](https://github.com/vitejs/vite/blob/b6c20f6a8126d8561891e5fb9368e8fc9da37538/packages/vite/src/node/plugins/importAnalysis.ts#L236-L251)

```ts
      for (const key in config.env) {
        importMetaEnvKeys[key] = JSON.stringify(config.env[key])
      }
      for (const key in config.define) {
        // non-import.meta.env.* is handled in `clientInjection` plugin
        if (key.startsWith('import.meta.env.')) {
          userDefineEnv[key.slice(16)] = config.define[key]
        }
      }
      const env = `import.meta.env = ${serializeDefine({
        ...importMetaEnvKeys,
        SSR: '__vite_ssr__',
        ...userDefineEnv,
      })};`
      _ssrEnv = env.replace('__vite_ssr__', 'true')
      _env = env.replace('__vite_ssr__', 'false')
```

which the browser receives as:

```js
import.meta.env = {"BASE_URL": "/", "DEV": true, "MODE": "development", "PROD": false, "SSR": false, "VITE_API_URL": "https://api.example.com", "VITE_COLOR": "", "VITE_COLOR_Q": "#ff0000"};console.log('API', import.meta.env.VITE_API_URL)
```

______________________________________________________________________

## 6. When a Change Takes Effect

The dev server watches the mode's four files and restarts when one changes:

Source: [`packages/vite/src/node/server/hmr.ts#L431-L434`](https://github.com/vitejs/vite/blob/b6c20f6a8126d8561891e5fb9368e8fc9da37538/packages/vite/src/node/server/hmr.ts#L431-L434)

```ts
  const isEnv =
    config.envDir !== false &&
    getEnvFilesForMode(config.mode, config.envDir).includes(file)
  if (isConfig || isConfigDependency || isEnv) {
```

Editing `.env.production` while `vite` runs does nothing, since that file is not in the dev list. A build has the values compiled in (section 5): a `.env` edit after `vite build` needs another build, and `vite preview` only serves `dist/`.

______________________________________________________________________

## 7. `vite.config` and `process.env`

Vite evaluates the config file before it knows `envDir` and before it calls `loadEnv`, and `loadEnv` copies expanded values into a private object rather than `process.env` (section 4). A config that prints both:

```js
import { defineConfig, loadEnv } from 'vite'
export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  console.error(`[config] command=${command} mode=${mode} NODE_ENV=${process.env.NODE_ENV} process.env.VITE_API_URL=${process.env.VITE_API_URL} loadEnv.VITE_API_URL=${env.VITE_API_URL} loadEnv.DB_PASSWORD=${env.DB_PASSWORD}`)
  return { build: { minify: false } }
})
```

prints for `vite build`:

```text
[config] command=build mode=production NODE_ENV=production process.env.VITE_API_URL=undefined loadEnv.VITE_API_URL=https://prod.example.com loadEnv.DB_PASSWORD=secret
```

The third argument is the prefix list; `''` matches every key, which includes the whole process environment, so use the result for config values and never pass it to `define` wholesale.

______________________________________________________________________

## 8. Mode Versus `NODE_ENV`

`NODE_ENV` gets the command's default only when nothing set it before Vite started:

Source: [`packages/vite/src/node/config.ts#L1488-L1495`](https://github.com/vitejs/vite/blob/b6c20f6a8126d8561891e5fb9368e8fc9da37538/packages/vite/src/node/config.ts#L1488-L1495)

```ts
  const isNodeEnvSet = !!process.env.NODE_ENV
  const packageCache: PackageCache = new Map()

  // some dependencies e.g. @vue/compiler-* relies on NODE_ENV for getting
  // production-specific behavior, so set it early on
  if (!isNodeEnvSet) {
    process.env.NODE_ENV = defaultNodeEnv
  }
```

A `NODE_ENV` line in an env file is first stored as `VITE_USER_NODE_ENV`:

Source: [`packages/vite/src/node/env.ts#L66-L69`](https://github.com/vitejs/vite/blob/b6c20f6a8126d8561891e5fb9368e8fc9da37538/packages/vite/src/node/env.ts#L66-L69)

```ts
  // test NODE_ENV override before expand as otherwise process.env.NODE_ENV would override this
  if (parsed.NODE_ENV && process.env.VITE_USER_NODE_ENV === undefined) {
    process.env.VITE_USER_NODE_ENV = parsed.NODE_ENV
  }
```

That happens before the loop in section 3 copies prefixed `process.env` keys, so `VITE_USER_NODE_ENV` itself lands in `import.meta.env`; a build with `NODE_ENV=development` in `.env.staging` compiles `console.log(import.meta.env.VITE_USER_NODE_ENV)` to `console.log("development")`. Then `resolveConfig` honours only `development`, and only if the shell did not set `NODE_ENV`:

Source: [`packages/vite/src/node/config.ts#L1817-L1836`](https://github.com/vitejs/vite/blob/b6c20f6a8126d8561891e5fb9368e8fc9da37538/packages/vite/src/node/config.ts#L1817-L1836)

```ts
  const userEnv = loadEnv(mode, envDir, resolveEnvPrefix(config))

  // Note it is possible for user to have a custom mode, e.g. `staging` where
  // development-like behavior is expected. This is indicated by NODE_ENV=development
  // loaded from `.staging.env` and set by us as VITE_USER_NODE_ENV
  const userNodeEnv = process.env.VITE_USER_NODE_ENV
  if (!isNodeEnvSet && userNodeEnv) {
    if (userNodeEnv === 'development') {
      process.env.NODE_ENV = 'development'
    } else {
      // NODE_ENV=production is not supported as it could break HMR in dev for frameworks like Vue
      logger.warn(
        `NODE_ENV=${userNodeEnv} is not supported in the .env file. ` +
          `Only NODE_ENV=development is supported to create a development build of your project. ` +
          `If you need to set process.env.NODE_ENV, you can set it in the Vite config instead.`,
      )
    }
  }

  const isProduction = process.env.NODE_ENV === 'production'
```

`import.meta.env.DEV` and `PROD` follow `NODE_ENV`; `MODE` follows the mode. Real builds of one project:

| Command                                                               | `MODE`        | `NODE_ENV`    | `PROD`  | `DEV`   |
| --------------------------------------------------------------------- | ------------- | ------------- | ------- | ------- |
| `vite`                                                                | `development` | `development` | `false` | `true`  |
| `vite build`                                                          | `production`  | `production`  | `true`  | `false` |
| `vite build --mode staging`                                           | `staging`     | `production`  | `true`  | `false` |
| `vite build --mode development`                                       | `development` | `production`  | `true`  | `false` |
| `NODE_ENV=development vite build`                                     | `production`  | `development` | `false` | `true`  |
| `vite build --mode staging`, `NODE_ENV=development` in `.env.staging` | `staging`     | `development` | `false` | `true`  |

The config function runs before `loadEnv`, so in the last row it still sees `process.env.NODE_ENV` as `production`; the switch to `development` happens after it returns. Plugins that read `config.isProduction` (Vue's, for example) see `false` and produce development output.

______________________________________________________________________

## 9. The Demo

`uv run scripts/explain_env.py --demo` writes these files into a throwaway project:

```text
# .env
VITE_API_URL=https://api.example.com
VITE_COLOR=#ff0000
VITE_TOKEN=abc$def
DB_PASSWORD=secret

# .env.development
VITE_API_URL=http://localhost:3000

# .env.production
VITE_API_URL=https://prod.example.com

# .env.staging
VITE_API_URL=https://staging.example.com
NODE_ENV=development
```

and reports four commands. The dev server:

```text
$ vite
  mode 'development', envDir /tmp/vite-env-0xzcftet
  NODE_ENV=development (set by Vite for this command) -> DEV=true, PROD=false

  Env files for this mode, lowest to highest priority (shell beats all):
    .env                         read
    .env.local                   not found
    .env.development             read
    .env.development.local       not found

  import.meta.env:
    BASE_URL       "/"                              Vite
    DEV            true                             Vite
    MODE           "development"                    Vite
    PROD           false                            Vite
    VITE_API_URL   "http://localhost:3000"          .env.development (beats .env)
    VITE_COLOR     ""                               .env
    VITE_TOKEN     "abc"                            .env

  Not exposed to the client (no VITE_ prefix):
    DB_PASSWORD    .env

  Check these values:
    .env:2 VITE_COLOR=#ff0000 reads as "": an unquoted # starts a comment; write VITE_COLOR="#ff0000"
    .env:3 VITE_TOKEN=abc$def reads as "abc": $ expands variables, even inside quotes; escape it as \$
```

A staging build (warnings left out):

```text
$ vite build --mode staging
  mode 'staging', envDir /tmp/vite-env-0xzcftet
  NODE_ENV=development (from .env.staging) -> DEV=true, PROD=false

  Env files for this mode, lowest to highest priority (shell beats all):
    .env                         read
    .env.local                   not found
    .env.staging                 read
    .env.staging.local           not found

  import.meta.env:
    BASE_URL             "/"                              Vite
    DEV                  true                             Vite
    MODE                 "staging"                        Vite
    PROD                 false                            Vite
    VITE_API_URL         "https://staging.example.com"    .env.staging (beats .env)
    VITE_COLOR           ""                               .env
    VITE_TOKEN           "abc"                            .env
    VITE_USER_NODE_ENV   "development"                    Vite, copied from NODE_ENV in .env.staging

  Not exposed to the client (no VITE_ prefix):
    DB_PASSWORD          .env
```

A build with the variable also set in the shell:

```text
$ VITE_API_URL=https://shell.example.com vite build
  ...
    VITE_API_URL   "https://shell.example.com"      shell (beats .env, .env.production)
```

______________________________________________________________________

## 10. Common Wrong Assumptions

| Assumption                                                       | What happens                                                                                                                                                      |
| ---------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The dev server reads `.env.production` but gives it low priority | It never opens it; dev reads the four `development` files only (section 2)                                                                                        |
| `.env.development` is always the highest priority in dev         | `.env.development.local` and shell variables beat it (section 3)                                                                                                  |
| `.env.local` beats everything                                    | `.env.<mode>` and `.env.<mode>.local` beat it (section 3)                                                                                                         |
| Quoting a value protects `$` and `#`                             | Quotes protect `#`; `$` still expands unless written `\$` (section 4)                                                                                             |
| `VITE_FLAG=false` is a boolean                                   | It is the string `"false"`, which is truthy (section 4)                                                                                                           |
| `process.env.VITE_X` works in `vite.config.*`                    | It holds only shell variables; call `loadEnv` (section 7)                                                                                                         |
| Editing `.env` updates a built app                               | Values are compiled into the bundle; rebuild (sections 5 and 6)                                                                                                   |
| `--mode development` makes a development build                   | It changes `MODE` and the files read; `NODE_ENV` and `PROD` stay production (section 8)                                                                           |
| `NODE_ENV=production` in `.env.production` is needed or works    | Vite warns and ignores it; builds are already production (section 8)                                                                                              |
| Everything in `.env` stays on the build machine                  | Every `VITE_` value the code reads is compiled into the bundle, all of them where `import.meta.env` is read whole; never prefix a secret with `VITE_` (section 5) |
