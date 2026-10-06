# skills

Knowledge about developer tools that is hard to find in the docs and costly to rediscover: how things actually behave, verified against the tool's source, written so both people and AI agents can use it.

Each skill follows the [Agent Skills](https://agentskills.io) format: a `SKILL.md` with the rules an agent needs, plus `references/` for the deep dives and `scripts/` for runnable helpers.

## Skills

| Skill | Use it when | Verified against |
|---|---|---|

## Install

### Claude Code

```text
/plugin marketplace add DhavalGojiya/skills
```

Or from a shell:

```bash
claude plugin marketplace add DhavalGojiya/skills
```

Each skill is its own plugin, so install only the ones you need.

### Any agent that reads `SKILL.md`

Copy a skill directory into the agent's skills folder, for example for Claude Code without the plugin system:

```bash
git clone https://github.com/DhavalGojiya/skills
cp -r skills/skills/<tool> ~/.claude/skills/        # all projects
cp -r skills/skills/<tool> .claude/skills/          # one project
```

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
