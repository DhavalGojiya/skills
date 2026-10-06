# skills repo

Agent skills for developer tools. Follow @CONTRIBUTING.md for layout, `SKILL.md` format and verification rules.

- Before stating how a tool behaves, check it against the installed tool's source (for example `uvx --from <tool> python -c "import <pkg>; print(<pkg>.__file__)"`) and record the version.
- A new skill needs three edits: `skills/<tool>/`, an entry in `.claude-plugin/marketplace.json`, a row in `README.md`.
- Test scripts from a real git repo with `uv run skills/<tool>/scripts/<name>.py`.
- Run `prek run -a` before committing and commit its fixes. A new file must be `git add`ed first or hooks do not see it.
- Pin `Source:` links to commit SHAs, not tags; see CONTRIBUTING.md.
