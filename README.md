# skills

Knowledge about developer tools that is hard to find in the docs and costly to rediscover: how things actually behave, verified against the tool's source, written so both people and AI agents can use it.

Each skill follows the [Agent Skills](https://agentskills.io) format: a `SKILL.md` with the rules an agent needs, plus `references/` for the deep dives and `scripts/` for runnable helpers.

## Skills

| Skill                                                | Use it when                                                                                                                                                                                                                                                              | Verified against                              |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------- |
| [pre-commit](skills/pre-commit/SKILL.md)             | A hook skips or checks the wrong files, `files`/`exclude`/`types` filters misbehave, `run` and `run -a` disagree                                                                                                                                                         | pre-commit 4.6.2                              |
| [solr](skills/solr/SKILL.md)                         | A doc added to Solr isn't searchable yet (or appears sooner than expected), choosing `autoCommit`/`autoSoftCommit`/`commitWithin`, flaky commit tests, pysolr `always_commit` surprises                                                                                  | Solr 9.10.1, pysolr 3.11.0                    |
| [django](skills/django/SKILL.md)                     | Deciding where `atomic()` is needed, lost or un-rolled-back writes, `TransactionManagementError`, jobs running before their rows commit, index migrations on large tables, a migration that prints `OK` every run but never stays applied, a rollback that fails halfway | Django 5.2.17 and 6.1.2, PostgreSQL 16 and 18 |
| [postgres](skills/postgres/SKILL.md)                 | `current transaction is aborted`, `COMMIT` answered with `ROLLBACK`, sessions stuck idle in transaction, writes lost on close, a `CREATE INDEX CONCURRENTLY` that hangs or leaves an invalid index                                                                       | PostgreSQL 16.15 and 18.6                     |
| [linux](skills/linux/SKILL.md)                       | A script fails with `cannot execute: required file not found`, `bad interpreter`, `Too many levels of symbolic links` or `Exec format error`, `env` shebang options, CRLF shebangs, a script that runs from the terminal but not from cron, a hook or `subprocess`       | Linux 7.0, bash 5.2.21, coreutils 9.4         |
| [python-packaging](skills/python-packaging/SKILL.md) | Who writes `.venv/bin` launchers and why their shebang names the venv's `python`, launchers starting with `#!/bin/sh` and `'''exec'`, `.venv/bin` commands broken after moving a project, relocatable venvs                                                              | uv 0.13.0, pip 26.2.1                         |

## Install

### Claude Code

```text
/plugin marketplace add DhavalGojiya/skills
/plugin install pre-commit@dhavalgojiya-skills
/plugin install solr@dhavalgojiya-skills
/plugin install django@dhavalgojiya-skills
/plugin install postgres@dhavalgojiya-skills
/plugin install linux@dhavalgojiya-skills
/plugin install python-packaging@dhavalgojiya-skills
```

Or from a shell:

```bash
claude plugin marketplace add DhavalGojiya/skills
claude plugin install pre-commit@dhavalgojiya-skills
claude plugin install solr@dhavalgojiya-skills
claude plugin install django@dhavalgojiya-skills
claude plugin install postgres@dhavalgojiya-skills
claude plugin install linux@dhavalgojiya-skills
claude plugin install python-packaging@dhavalgojiya-skills
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
