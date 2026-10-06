---
name: pre-commit
description: How pre-commit chooses which files each hook receives, and how to debug its files/exclude/types filters. Use when writing or editing .pre-commit-config.yaml, when a hook prints "(no files to check) Skipped", when an exclude or files regex does not match what you expect, when a hook unexpectedly runs on (or skips) a file, or when `pre-commit run` and `pre-commit run -a` give different results.
---

# pre-commit file selection

Verified against pre-commit [4.6.2](https://github.com/pre-commit/pre-commit/tree/v4.6.2) source (`pre_commit/commands/run.py`, `pre_commit/git.py`, `pre_commit/clientlib.py`). Line-level source links are in `references/file-selection-order.md`.

## How a hook's file list is built

Five stages, each one can only remove files:

| Stage | Runs     | Filter                                                                                      |
| ----- | -------- | ------------------------------------------------------------------------------------------- |
| 0     | once     | File source: `--from-ref/--to-ref` > `--files` > `-a` > merge conflict files > staged files |
| 1     | once     | Top-level `files:` then `exclude:` (defaults `''` and `'^$'`, both no-ops)                  |
| 2     | once     | `os.path.lexists` (drops deleted files, keeps broken symlinks)                              |
| 3     | per hook | Hook `files:` then `exclude:` (same function as stage 1)                                    |
| 4     | per hook | `types` (has ALL) + `types_or` (has ANY) + `exclude_types` (has NONE), tags from `identify` |

An empty result without `always_run: true` prints `(no files to check) Skipped`.

## Rules

01. Patterns are Python regexes applied with `re.search`, not globs. `'*.py'` is invalid; use `\.py$`. Anchor with `^` or the pattern matches anywhere: `vendor/` also matches `src/vendor/x.py`.
02. Paths are repo-relative, always use `/`, and never start with `/`. So `/tests/` misses top-level `tests/x.py`; use `(^|/)tests/`.
03. Escape dots: `\.min\.js$`, not `.min.js`.
04. Top-level `exclude:` hides a file from every hook. Hook-level filters affect only that hook.
05. A hook's `files`, `exclude`, `types` set in your config **replace** the hook repo's values from `.pre-commit-hooks.yaml`; they never merge. Overriding `types` on a hook that defaulted to `types_or: [python, pyi]` keeps that `types_or` too.
06. `types: [python, yaml]` matches nothing (no file has both tags). Use `types_or: [python, yaml]`.
07. `pre-commit run` checks staged content only (unstaged edits are stashed). `pre-commit run -a` and `--files` check the working tree. A failure that appears in one mode only is usually unstaged edits.
08. Neither mode sees untracked files. `git add` a new file before expecting any hook to check it.
09. Long excludes: use `(?x)` verbose regex in a YAML block scalar, one alternative per line, wrapped in `^(...)$`.
10. pre-commit warns on `/*` in a pattern (glob thinking) and on `[\\/]`-style separators (unneeded, paths are already normalized to `/`). Treat these warnings as bugs in the config.

## Debug procedure

Work through these in order and stop when the cause is clear.

1. Inspect the tags of a file whose inclusion surprises you:
   `python -c "from identify.identify import tags_from_path as t; print(t('path/to/file'))"`

2. Push one file through all stages for one hook:
   `pre-commit run <hook-id> --files path/to/file --verbose`

3. Print the exact list a filter set produces with the `identity` meta hook, copying the hook's filters onto it:

   ```yaml
   - repo: meta
     hooks:
       - id: identity
         files: ^src/
         exclude: (^|/)tests/
         types: [python]
   ```

   Run `pre-commit run identity -a`.

4. Count files per stage with `scripts/count_stages.py` (below) to see which stage drops them.

5. Add `check-hooks-apply` and `check-useless-excludes` from `repo: meta` to catch hooks and excludes that match nothing. Both have `files: ^\.pre-commit-config\.yaml$`, so they only run when the config file itself is staged, or with `-a`.

## Script

`scripts/count_stages.py` prints the file count after each stage for a given set of filters, run from the target repo root:

```bash
uv run <skill-dir>/scripts/count_stages.py --all-files \
  --files '^src/' --exclude '(^|/)tests/' --types python
```

Flags: `--all-files` (default is staged files), `--top-files`, `--top-exclude`, `--files`, `--exclude`, `--types`, `--types-or`, `--exclude-types`, `--list` (print the surviving paths). Pass the hook repo's default `types`/`types_or` explicitly; the script does not read `.pre-commit-hooks.yaml`.

## References

- `references/file-selection-order.md`: stage-by-stage walkthrough with the quoted source, the stash behaviour, a worked example with real counts, and the regex gotchas in detail. Read it when the rules above do not explain what you see, or when you need to cite the source.
