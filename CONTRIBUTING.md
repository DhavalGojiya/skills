# Contributing

A skill here captures behaviour that is hard to discover, checked against the tool's source or a reproducible experiment. If the official docs already say it clearly, it does not belong here.

## Where things go

| Content | Location |
|---|---|
| One tool | `skills/<tool>/` (lowercase, hyphens, matches `name:` in `SKILL.md`) |
| Rules an agent must follow, debug steps | `skills/<tool>/SKILL.md` |
| Long explanations, quoted source, worked examples | `skills/<tool>/references/<topic>.md` |
| Anything meant to be run | `skills/<tool>/scripts/` |

Keep one skill per tool and add topics as reference files. Split a tool into several skills only when the topics answer clearly different requests and `SKILL.md` would pass about 500 lines.

## SKILL.md

```markdown
---
name: <tool>
description: <What it covers>. Use when <task>, when <exact error or output text>, when <symptom>.
---

# <Title>

Verified against <tool> <version> (<source files or experiment>).

## Rules
Numbered, imperative, one fact each, with the correct form inline.

## Debug procedure
Ordered steps, each a command to run and what to look for.

## References
- `references/<topic>.md`: what it contains and when to read it.
```

- **`description` decides when an agent loads the skill.** Agents see only `name` and `description` until the skill triggers. Write it from the user's side: the task, the exact error text, the symptom. Keep it under 1024 characters.
- **The body is loaded in full when the skill triggers**, so every line costs context. Put the rules first and leave the explanations to `references/`.
- Write rules as instructions ("Anchor with `^`"), not observations ("It is worth noting that anchoring may help").
- Give each reference file a one-line pointer in `SKILL.md` saying what it holds and when to read it. An agent will not open a file it does not know about.
- Use paths relative to the skill directory (`scripts/count_stages.py`).

## Recording a gotcha

```markdown
### <Short statement of the surprise>
- Symptom: what the user sees, with exact output text
- Cause: the mechanism, citing the source file or function
- Fix: the corrected config or command
- Verify: a command that proves the fix worked
```

## Verification

- State the version you checked, at the top of `SKILL.md` and of each reference file.
- Quote source only from that version, with its file path.
- Every command and code block must run as written. Scripts declare their dependencies inline (PEP 723) so `uv run scripts/<name>.py` works with no setup.
- When a new release changes the behaviour, update the text and the version line together.

## Registering a skill

Add an entry to `.claude-plugin/marketplace.json`:

```json
{
  "name": "<tool>",
  "description": "<one line>",
  "source": "./",
  "strict": false,
  "skills": ["./skills/<tool>"]
}
```

Add a row to the table in `README.md`. If you have the Claude Code CLI, run `claude plugin validate .` before opening a pull request.

## Style

- No em dashes; use commas, semicolons or separate sentences.
- No personal names in skill content.
- Tables for comparisons, fenced code blocks with a language for everything runnable.
