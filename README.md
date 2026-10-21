# skills

Knowledge about developer tools that is hard to find in the docs and costly to rediscover: how things actually behave, verified against the tool's source, written so both people and AI agents can use it.

Each skill follows the [Agent Skills](https://agentskills.io) format: a `SKILL.md` with the rules an agent needs, plus `references/` for the deep dives and `scripts/` for runnable helpers.

## Skills

| Skill                                              | Use it when                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | Verified against                                                                                                                          |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| [pre-commit](skills/pre-commit/SKILL.md)           | A hook skips or checks the wrong files, `files`/`exclude`/`types` filters misbehave, `run` and `run -a` disagree                                                                                                                                                                                                                                                                                                                                                                                                                                                               | pre-commit 4.6.2                                                                                                                          |
| [solr](skills/solr/SKILL.md)                       | A doc added to Solr isn't searchable yet (or appears sooner than expected), choosing `autoCommit`/`autoSoftCommit`/`commitWithin`, flaky commit tests, pysolr `always_commit` surprises                                                                                                                                                                                                                                                                                                                                                                                        | Solr 9.10.1, pysolr 3.11.0                                                                                                                |
| [django](skills/django/SKILL.md)                   | Deciding where `atomic()` is needed, lost or un-rolled-back writes, `TransactionManagementError`, jobs running before their rows commit, index migrations on large tables, a migration that prints `OK` every run but never stays applied, a rollback that fails halfway, a forked worker that swaps query results with its parent or ends its session (`server closed the connection unexpectedly`)                                                                                                                                                                           | Django 5.2.17 and 6.1.2, PostgreSQL 16 and 18                                                                                             |
| [postgres](skills/postgres/SKILL.md)               | `current transaction is aborted`, `COMMIT` answered with `ROLLBACK`, sessions stuck idle in transaction, writes lost on close, a `CREATE INDEX CONCURRENTLY` that hangs or leaves an invalid index                                                                                                                                                                                                                                                                                                                                                                             | PostgreSQL 16.15 and 18.6                                                                                                                 |
| [linux](skills/linux/SKILL.md)                     | A script fails with `cannot execute: required file not found`, `bad interpreter`, `Too many levels of symbolic links` or `Exec format error`, `env` shebang options, CRLF shebangs, a script that runs from the terminal but not from cron, a hook or `subprocess`                                                                                                                                                                                                                                                                                                             | Linux 7.0, bash 5.2.21, coreutils 9.4                                                                                                     |
| [python](skills/python/SKILL.md)                   | Who writes `.venv/bin` launchers and why their shebang names the venv's `python`, launchers starting with `#!/bin/sh` and `'''exec'`, `.venv/bin` commands broken after moving a project, relocatable venvs, `multiprocessing.Pool` workers missing the parent's setup under the 3.14 `forkserver` default, `map()` hanging, `Pool is still running`, tasks lost without `join()`                                                                                                                                                                                              | uv 0.13.0, pip 26.2.1, CPython 3.15.0                                                                                                     |
| [mwaa](skills/mwaa/SKILL.md)                       | Where files uploaded to an MWAA bucket land in the container (`/usr/local/airflow/...`), a `plugins.zip`, `requirements.txt` or `startup.sh` change with no effect, `ModuleNotFoundError` after a deploy, `Could not open constraint file`, custom constraints, a startup script that kills the container or loses its exports                                                                                                                                                                                                                                                 | amazon-mwaa-docker-images 3.0.6 image, Airflow 3.0.6, pip 26.2.1                                                                          |
| [django-haystack](skills/django-haystack/SKILL.md) | A drf-haystack response drops a `Meta.fields` entry or shows `null`, stale values or `_exact` keys, a search runs one SQL query per result or too many Solr requests, `sqs[n:]` returns too few results, `update_index -k` hangs on Python 3.14 or fails with `'_thread._local' object has no attribute` or `server closed the connection unexpectedly`, choosing which model fields to put in a `SearchIndex`                                                                                                                                                                 | drf-haystack 1.9.3, django-haystack 3.4.0, DRF 3.18.1, Solr 9.10.1                                                                        |
| [vue](skills/vue/SKILL.md)                         | What a `.vue` file compiles to (`_sfc_main`, `__name`, `__returned__`), whether same-named variables in two components clash, `_sfc_main$2` or `a$1` in a bundle, state shared through a plain `<script>`, `<KeepAlive include>` not matching, changing `data-v` ids or absolute `__file` paths in a build, what re-renders when a `ref` changes and which DOM calls follow, `<!--v-if-->` comments, whether SSR or SSG pages reload on navigation, where a fetch or `onMounted` runs, `Hydration completed but contains mismatches`, Nuxt `_payload.json` and `__NUXT_DATA__` | Vue 3.5.44, @vitejs/plugin-vue 6.0.10, Vite 8.3.4 and 7.3.7, vue-router 5.4.0, Nuxt 4.6.2                                                 |
| [vite](skills/vite/SKILL.md)                       | Which of `.env`, `.env.local`, `.env.[mode]` and `.env.[mode].local` wins for `vite` or `vite build`, an `import.meta.env` value that is `undefined`, cut at `#` or missing a `$` part, `--mode` builds with `PROD` true, `NODE_ENV` in a `.env` file ignored, `.env` changes with no effect                                                                                                                                                                                                                                                                                   | Vite 8.3.4 and 7.3.7, Node 20.19 to 24                                                                                                    |
| [cloudflare](skills/cloudflare/SKILL.md)           | Adding a domain or moving its DNS to Cloudflare, a zone stuck in `Pending Nameserver Update` or turned `Moved`, assigned nameservers that differ from other zones or from the ones preset at the registrar, `SERVFAIL` after switching nameservers (stale DS), the same domain in two accounts, adding a domain failing with 1049, 1099, 1105 or 1097, users still getting old records after the switch, SSH, mail or a leaked origin IP after proxying                                                                                                                        | Cloudflare docs source at `ef83b95` (2026-10-21), API v4 schema via cloudflare-python 5.9.0, live queries to all 899 standard nameservers |

## Install

### Claude Code

```text
/plugin marketplace add DhavalGojiya/skills
/plugin install pre-commit@dhavalgojiya-skills
/plugin install solr@dhavalgojiya-skills
/plugin install django@dhavalgojiya-skills
/plugin install postgres@dhavalgojiya-skills
/plugin install linux@dhavalgojiya-skills
/plugin install python@dhavalgojiya-skills
/plugin install mwaa@dhavalgojiya-skills
/plugin install django-haystack@dhavalgojiya-skills
/plugin install vue@dhavalgojiya-skills
/plugin install vite@dhavalgojiya-skills
/plugin install cloudflare@dhavalgojiya-skills
```

Or from a shell:

```bash
claude plugin marketplace add DhavalGojiya/skills
claude plugin install pre-commit@dhavalgojiya-skills
claude plugin install solr@dhavalgojiya-skills
claude plugin install django@dhavalgojiya-skills
claude plugin install postgres@dhavalgojiya-skills
claude plugin install linux@dhavalgojiya-skills
claude plugin install python@dhavalgojiya-skills
claude plugin install mwaa@dhavalgojiya-skills
claude plugin install django-haystack@dhavalgojiya-skills
claude plugin install vue@dhavalgojiya-skills
claude plugin install vite@dhavalgojiya-skills
claude plugin install cloudflare@dhavalgojiya-skills
```

Each skill is its own plugin, so install only the ones you need.

### Any agent that reads `SKILL.md`

Copy a skill directory into the agent's skills folder, for example for Claude Code without the plugin system:

```bash
git clone https://github.com/DhavalGojiya/skills
cp -r skills/skills/pre-commit ~/.claude/skills/        # all projects
cp -r skills/skills/pre-commit .claude/skills/          # one project
```

### Reading it yourself

Every `references/*.md` file is a standalone article. Start with [How pre-commit selects files for each hook](skills/pre-commit/references/file-selection-order.md).

## Layout

```text
.claude-plugin/marketplace.json   one plugin entry per skill
skills/<tool>/
  SKILL.md                        rules + debug procedure, loaded when the skill triggers
  references/<topic>.md           deep dives, read on demand
  scripts/<helper>.py             runnable helpers
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
