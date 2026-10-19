---
name: vite
description: >-
  How Vite picks a mode and loads .env files into import.meta.env for vite (dev), vite build, vite preview and --mode. Use when an import.meta.env value is not the expected one, when asking which of .env, .env.local, .env.[mode] and .env.[mode].local wins or whether .env.production is read in dev, when a value is undefined in the browser (no VITE_ prefix) or in vite.config (process.env lacks .env values), when a value is cut at "#" or loses the part after a "$", when "vite build --mode staging" or "--mode development" still gives PROD true, when NODE_ENV in a .env file is ignored or Vite warns "NODE_ENV=production is not supported in the .env file", when a changed .env has no effect after a build or in vite preview, when VITE_FLAG=false behaves as true, or when VITE_USER_NODE_ENV appears in import.meta.env.
---

# Vite modes and env files

Verified against Vite [8.3.4](https://github.com/vitejs/vite/tree/v8.3.4) source (`packages/vite/src/node/env.ts`, `config.ts`, `cli.ts`, `build.ts`, `preview.ts`, `plugins/define.ts`, `plugins/importAnalysis.ts`, `server/hmr.ts`), and by running `loadEnv`, the dev server and builds with `scripts/explain_env.py` on Node 22.22.3. Vite 7.3.7, which parses with dotenv 17 instead of Node's `util.parseEnv`, gave the same value in every case, and `util.parseEnv` parsed the same on Node 20.19.0, 22.22.3 and 24.22.0. Line-level source links are in `references/env-files.md`.

```text
command                         mode          env files read, lowest -> highest priority
vite, vite dev, vite serve      development   .env  .env.local  .env.development  .env.development.local
vite build                      production    .env  .env.local  .env.production   .env.production.local
vite build --mode staging       staging       .env  .env.local  .env.staging      .env.staging.local
vite preview                    production    serves dist/ as built; env files change nothing
                                              a variable already in the shell beats every file
```

## Rules

### Which value wins

1. The mode alone decides which files are read: `vite` (also `vite dev`, `vite serve`) runs in `development`, `vite build` and `vite preview` in `production`, and `--mode <name>` replaces either. Vite reads only `.env`, `.env.local`, `.env.<mode>` and `.env.<mode>.local` in `envDir` (default: the Vite root). `.env.production` is never opened in dev.
2. Per key, the later file wins: `.env.<mode>.local` beats `.env.<mode>`, which beats `.env.local`, which beats `.env`. A key missing from a higher file falls back to a lower one, so `.env` holds the defaults shared by every mode.
3. A variable already in the process environment (`VITE_API_URL=... vite build`, a CI variable, an `export` in the shell) beats every file.
4. A mode without its own file is not an error: `--mode staging` with no `.env.staging` uses `.env` and `.env.local`. `local` cannot be a mode; `loadEnv` throws because it clashes with the `.local` suffix.

### What reaches client code

5. Only keys starting with `envPrefix` (default `VITE_`) reach `import.meta.env`. Any other key in a `.env` file is `undefined` in client code (`void 0` in the bundle). Treat every `VITE_` value as public: each one the code reads is written into the JavaScript the browser downloads.
6. A build replaces each `import.meta.env.VITE_X` with its value as a string literal, so a changed `.env` file needs a rebuild, and `vite preview` shows what the last build baked in. `import.meta.env` used whole or with a computed key (`import.meta.env[name]`) inlines the full object of all variables at that spot.
7. The dev server prepends the full `import.meta.env` object to every module that uses it, and restarts itself when one of the mode's four env files changes.
8. Every value is a string. `VITE_FLAG=false` is the truthy string `"false"` and `VITE_N=3` is `"3"`; compare with `=== 'true'` or convert.

### How a line is parsed

09. An unquoted `#` starts a comment, even in the middle of a value: `VITE_COLOR=#ff0000` gives `""` and `VITE_KEY=ab#cd` gives `"ab"`. Quote such values: `VITE_COLOR="#ff0000"`.
10. `$NAME` and `${NAME}` expand to other variables from the env files or the shell, and an unknown name expands to nothing: `VITE_TOKEN=abc$def` gives `"abc"`. Single and double quotes do not stop it; escape the dollar: `VITE_TOKEN=abc\$def`.

### vite.config and NODE_ENV

11. `vite.config.*` runs before Vite loads the env files, so `process.env.VITE_X` there holds only what the shell set. Load them explicitly: `defineConfig(({ mode }) => { const env = loadEnv(mode, process.cwd(), '') ... })`. The `''` prefix returns keys without `VITE_` too.
12. Mode and `NODE_ENV` are separate. `vite build --mode development` and `vite build --mode staging` still run with `NODE_ENV=production`, so `import.meta.env.PROD` is `true` and `DEV` is `false`; only `MODE` changes. Get a development build with `NODE_ENV=development vite build`, or with `NODE_ENV=development` in `.env.<mode>`.
13. A `.env` file can set `NODE_ENV` only to `development`, and only when the shell has not set it. Any other value prints `NODE_ENV=production is not supported in the .env file. Only NODE_ENV=development is supported to create a development build of your project.` and is ignored.
14. A `NODE_ENV` read from a `.env` file also reaches client code as `import.meta.env.VITE_USER_NODE_ENV`: Vite stores it in `process.env` under that name, and the `VITE_` prefix exposes it.

## Debug procedure

Run these from the Vite root (where `vite.config.*` lives).

1. Print the files Vite reads for a command and mode, the file or shell variable each value comes from, keys hidden by the prefix, and values changed by `#` or `$` (rules 1 to 10, 13, 14):
   ```bash
   uv run scripts/explain_env.py --root path/to/app --command build --mode staging
   ```
2. Check the process environment, which beats every file (rule 3). In CI, check the job's variables too.
   ```bash
   env | grep -E '^(VITE_|NODE_ENV=)'
   ```
3. Watch Vite load the files: the debug log lists the four paths it tries and the values it keeps.
   ```bash
   DEBUG=vite:env npx vite build
   ```
4. Check what a build baked in by searching the bundle for the value you expected; an old value means the build predates the `.env` change (rule 6).
   ```bash
   grep -o 'https://api\.example\.com[^"`]*' dist/assets/*.js
   ```
5. A value `undefined` only in `vite.config.*`: use `loadEnv` (rule 11). `PROD` true in a "development" build: check `NODE_ENV`, not the mode (rule 12).

## Script

`scripts/explain_env.py` resolves the project's config with its own Vite, so `envDir`, `envPrefix`, the mode and `NODE_ENV` match a real run. `--demo` runs `vite`, `vite build`, `vite build --mode staging` and a build with `VITE_API_URL` set in the shell against a throwaway project. Needs `node` and `npm`.

```bash
uv run scripts/explain_env.py --demo
uv run scripts/explain_env.py --root path/to/app --command serve
```

## References

- `references/env-files.md`: the quoted Vite source for each step (mode per command, file list, merge order, `util.parseEnv` and `dotenv-expand`, the prefix filter, build-time replacement and dev injection, `NODE_ENV` handling), a mode and `NODE_ENV` table from real builds, the `--demo` output, and a table of common wrong assumptions. Read it when a rule above does not explain a value, or to cite the source.
