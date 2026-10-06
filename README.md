# skills

Knowledge about developer tools that is hard to find in the docs and costly to rediscover: how things actually behave, verified against the tool's source, written so both people and AI agents can use it.

Each skill follows the [Agent Skills](https://agentskills.io) format: a `SKILL.md` with the rules an agent needs, plus `references/` for the deep dives and `scripts/` for runnable helpers.

## Skills

| Skill | Use it when | Verified against |
|---|---|---|
| [pre-commit](skills/pre-commit/SKILL.md) | A hook skips or checks the wrong files, `files`/`exclude`/`types` filters misbehave, `run` and `run -a` disagree | pre-commit 4.6.2 |

## Install

### Claude Code

```text
/plugin marketplace add DhavalGojiya/skills
/plugin install pre-commit@dhavalgojiya-skills
```

Or from a shell:

```bash
claude plugin marketplace add DhavalGojiya/skills
claude plugin install pre-commit@dhavalgojiya-skills
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
