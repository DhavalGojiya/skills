# How pre-commit Selects Files for Each Hook

A deep dive into the order in which pre-commit builds the list of files it passes to every hook, based on the source of the official repository ([pre-commit/pre-commit](https://github.com/pre-commit/pre-commit), verified against release [4.6.2](https://github.com/pre-commit/pre-commit/tree/v4.6.2), files `pre_commit/main.py`, `pre_commit/commands/run.py` and `pre_commit/git.py`).

Every "Source:" link points at that tag with line numbers, so it shows exactly the code quoted here.

---

## Table of Contents

1. [The Big Picture](#1-the-big-picture)
2. [Entry Point: `pre-commit` vs `pre-commit run`](#2-entry-point-pre-commit-vs-pre-commit-run)
3. [Stage 0: Where the File List Comes From](#3-stage-0-where-the-file-list-comes-from)
4. [Stashing Unstaged Changes](#4-stashing-unstaged-changes)
5. [Stage 1: Top-Level `files:` / `exclude:`](#5-stage-1-top-level-files--exclude)
6. [Stage 2: `os.path.lexists`](#6-stage-2-ospathlexists)
7. [Stage 3: Hook-Level `files:` / `exclude:`](#7-stage-3-hook-level-files--exclude)
8. [Stage 4: `types`, `types_or`, `exclude_types`](#8-stage-4-types-types_or-exclude_types)
9. [The Per-Hook Loop](#9-the-per-hook-loop)
10. [Worked Example with Real Numbers](#10-worked-example-with-real-numbers)
11. [`pre-commit run` vs `pre-commit run -a`](#11-pre-commit-run-vs-pre-commit-run--a)
12. [Regex Gotchas](#12-regex-gotchas)
13. [Debugging Your Filters](#13-debugging-your-filters)
14. [Quick Reference](#14-quick-reference)

---

## 1. The Big Picture

Every time pre-commit runs, it narrows a list of filenames through five stages. The first three run **once** per invocation and produce a shared list. The last two run **separately for each hook**.

```
                    ┌─────────────────────────────────────────────┐
   once per run     │ Stage 0  file source (git / CLI args)       │
                    │ Stage 1  top-level files: / exclude:        │
                    │ Stage 2  os.path.lexists                    │
                    └──────────────────────┬──────────────────────┘
                                           │  shared list
                 ┌─────────────────────────┼─────────────────────────┐
                 ▼                         ▼                         ▼
   per hook  ┌────────────┐          ┌────────────┐          ┌────────────┐
             │ Stage 3    │          │ Stage 3    │          │ Stage 3    │
             │ hook files │          │ hook files │          │ hook files │
             │ + exclude  │          │ + exclude  │          │ + exclude  │
             ├────────────┤          ├────────────┤          ├────────────┤
             │ Stage 4    │          │ Stage 4    │          │ Stage 4    │
             │ types      │          │ types      │          │ types      │
             └─────┬──────┘          └─────┬──────┘          └─────┬──────┘
                   ▼                       ▼                       ▼
                hook A                  hook B                  hook C
```

Key consequences of this design:

- A filter can only **remove** files. No later stage can add back a file that an earlier stage dropped.
- Anything removed at the top level (stage 1) is invisible to **every** hook.
- Hook-level filters (stages 3 and 4) are independent between hooks. What hook A filters out has no effect on hook B.

---

## 2. Entry Point: `pre-commit` vs `pre-commit run`

Running `pre-commit` with no arguments is exactly the same as `pre-commit run`. Python's `argparse` has no notion of a default subcommand, so `main.py` inserts one:

Source: [`main.py#L367-L370`](https://github.com/pre-commit/pre-commit/blob/v4.6.2/pre_commit/main.py#L367-L370)

```python
# pre_commit/main.py
# argparse doesn't really provide a way to use a `default` subparser
if len(argv) == 0:
    argv = ['run']
args = parser.parse_args(argv)
```

```bash
# These two are identical
pre-commit
pre-commit run
```

The Git hook that `pre-commit install` writes into `.git/hooks/pre-commit` also calls `run`, so committing behaves like a plain `pre-commit run` (with the hook stage set to `pre-commit`).

---

## 3. Stage 0: Where the File List Comes From

The very first list of filenames is produced by `_all_filenames()`. It checks a chain of conditions and returns the **first** one that applies:

Source: [`commands/run.py#L253-L271`](https://github.com/pre-commit/pre-commit/blob/v4.6.2/pre_commit/commands/run.py#L253-L271)

```python
# pre_commit/commands/run.py
def _all_filenames(args: argparse.Namespace) -> Iterable[str]:
    # these hooks do not operate on files
    if args.hook_stage in {
        'post-checkout', 'post-commit', 'post-merge', 'post-rewrite',
        'pre-rebase',
    }:
        return ()
    elif args.hook_stage in {'prepare-commit-msg', 'commit-msg'}:
        return (args.commit_msg_filename,)
    elif args.from_ref and args.to_ref:
        return git.get_changed_files(args.from_ref, args.to_ref)
    elif args.files:
        return args.files
    elif args.all_files:
        return git.get_all_files()
    elif git.is_in_merge_conflict():
        return git.get_conflicted_files()
    else:
        return git.get_staged_files()
```

### Mapping CLI usage to Stage 0

| Command | Branch taken | Source of filenames |
|---|---|---|
| `pre-commit run` | `else` | Staged files |
| `pre-commit run -a` / `--all-files` | `args.all_files` | Every tracked file |
| `pre-commit run --files a.py b.py` | `args.files` | Exactly the paths you list |
| `pre-commit run --from-ref X --to-ref Y` | `from_ref and to_ref` | Files changed between two refs |
| `pre-commit run` during a merge conflict | `is_in_merge_conflict()` | Conflicted files |
| `--hook-stage commit-msg` | commit-msg branch | The commit message file only |
| `--hook-stage post-commit` (and similar) | first branch | No files at all |

Note the priority: `--from-ref/--to-ref` beats `--files`, which beats `--all-files`.

### The Git commands behind it

Source: [`git.py#L135-L143`](https://github.com/pre-commit/pre-commit/blob/v4.6.2/pre_commit/git.py#L135-L143), [`git.py#L154-L155`](https://github.com/pre-commit/pre-commit/blob/v4.6.2/pre_commit/git.py#L154-L155), [`git.py#L158-L167`](https://github.com/pre-commit/pre-commit/blob/v4.6.2/pre_commit/git.py#L158-L167)

```python
# pre_commit/git.py
def get_staged_files(cwd: str | None = None) -> list[str]:
    return zsplit(
        cmd_output(
            'git', 'diff', '--staged', '--name-only', '--no-ext-diff', '-z',
            # Everything except for D
            '--diff-filter=ACMRTUXB',
            cwd=cwd,
        )[1],
    )


def get_all_files() -> list[str]:
    return zsplit(cmd_output('git', 'ls-files', '-z', '--deduplicate')[1])


def get_changed_files(old: str, new: str) -> list[str]:
    diff_cmd = ('git', 'diff', '--name-only', '--no-ext-diff', '-z')
    try:
        _, out, _ = cmd_output(*diff_cmd, f'{old}...{new}')
    except CalledProcessError:  # pragma: no cover (new git)
        # on newer git where old and new do not have a merge base git fails
        # so we try a full diff (this is what old git did for us!)
        _, out, _ = cmd_output(*diff_cmd, f'{old}..{new}')

    return zsplit(out)
```

You can reproduce Stage 0 by hand:

```bash
# What `pre-commit run` starts with
git diff --staged --name-only --diff-filter=ACMRTUXB

# What `pre-commit run -a` starts with
git ls-files --deduplicate

# Count them
git ls-files --deduplicate | wc -l
```

### About `--diff-filter=ACMRTUXB`

This is **hardcoded** in pre-commit and has nothing to do with your config's `files:` or `exclude:`. It is a Git option that filters by **change status**, not by path:

| Letter | Meaning |
|---|---|
| `A` | Added |
| `C` | Copied |
| `M` | Modified |
| `R` | Renamed |
| `T` | Type changed (e.g. regular file to symlink) |
| `U` | Unmerged |
| `X` | Unknown |
| `B` | Pairing broken |

The only status left out is `D` (deleted): a deleted file has nothing on disk for a hook to check.

---

## 4. Stashing Unstaged Changes

This is the second (and last) behavioural difference between modes. It does not change **which** files are selected, but it changes **what content** hooks see in those files.

Source: [`commands/run.py#L344`](https://github.com/pre-commit/pre-commit/blob/v4.6.2/pre_commit/commands/run.py#L344), [`commands/run.py#L420-L421`](https://github.com/pre-commit/pre-commit/blob/v4.6.2/pre_commit/commands/run.py#L420-L421)

```python
# pre_commit/commands/run.py
stash = not args.all_files and not args.files
...
if stash:
    exit_stack.enter_context(staged_files_only(store.directory))
```

| Mode | Stash? | Hooks see |
|---|---|---|
| `pre-commit run` | Yes | Only the staged content (exactly what would be committed) |
| `pre-commit run -a` | No | The current working tree, including unstaged edits |
| `pre-commit run --files ...` | No | The current working tree |

Example of why this matters:

```bash
# 1. Stage a clean version of the file
echo 'x = 1' > app.py
git add app.py

# 2. Introduce an unstaged problem
echo 'x =   1   ' > app.py

# 3a. Checks staged content "x = 1": passes
pre-commit run

# 3b. Checks working tree "x =   1   ": may fail
pre-commit run -a
```

---

## 5. Stage 1: Top-Level `files:` / `exclude:`

### What they are

Global path filters defined at the **root** of `.pre-commit-config.yaml`, at the same indentation level as `repos:`. They apply to every hook.

```yaml
# .pre-commit-config.yaml
files: ^(src|scheduler)/          # only consider these directories
exclude: ^(vendor/|docs/legacy/)  # never pass these to any hook

repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v5.0.0
    hooks:
      - id: trailing-whitespace
```

### Defaults

Source: [`clientlib.py#L517-L518`](https://github.com/pre-commit/pre-commit/blob/v4.6.2/pre_commit/clientlib.py#L517-L518)

| Key | Default | Effect |
|---|---|---|
| `files` | `''` | An empty regex matches every path |
| `exclude` | `'^$'` | Matches only an empty path, so excludes nothing |

So if you never set them, Stage 1 is a no-op and the count does not change.

### How they are applied

Source: [`commands/run.py#L290-L292`](https://github.com/pre-commit/pre-commit/blob/v4.6.2/pre_commit/commands/run.py#L290-L292)

```python
# pre_commit/commands/run.py
def _run_hooks(config, hooks, skips, args) -> int:
    ...
    classifier = Classifier.from_config(
        _all_filenames(args), config['files'], config['exclude'],
    )
```

Source: [`commands/run.py#L112-L126`](https://github.com/pre-commit/pre-commit/blob/v4.6.2/pre_commit/commands/run.py#L112-L126)

```python
class Classifier:
    @classmethod
    def from_config(
            cls,
            filenames: Iterable[str],
            include: str,
            exclude: str,
    ) -> Classifier:
        # on windows we normalize all filenames to use forward slashes
        # this makes it easier to filter using the `files:` regex
        # this also makes improperly quoted shell-based hooks work better
        # see #1173
        if os.altsep == '/' and os.sep == '\\':
            filenames = (f.replace(os.sep, os.altsep) for f in filenames)
        filenames = filter_by_include_exclude(filenames, include, exclude)
        return Classifier(filenames)
```

Notice the Windows normalization: paths always use `/`, so the same regex works on every OS.

### The filter function (shared by Stage 1 and Stage 3)

Source: [`commands/run.py#L60-L70`](https://github.com/pre-commit/pre-commit/blob/v4.6.2/pre_commit/commands/run.py#L60-L70)

```python
def filter_by_include_exclude(
        names: Iterable[str],
        include: str,
        exclude: str,
) -> Generator[str]:
    include_re, exclude_re = re.compile(include), re.compile(exclude)
    return (
        filename for filename in names
        if include_re.search(filename)       # 1st: must match files:
        if not exclude_re.search(filename)   # 2nd: must NOT match exclude:
    )
```

### Which runs first: `files` or `exclude`?

Per filename, Python evaluates the two `if` clauses **left to right**:

1. `files:` is checked first. If it does not match, the file is dropped and `exclude:` is never evaluated for it.
2. `exclude:` is checked second, only for files that passed `files:`.

But the result is a plain logical AND:

```
keep(file) = files_regex.search(file) AND NOT exclude_regex.search(file)
```

So swapping the order would produce exactly the same set. The order only saves a little work. It also means there is **no intermediate count** between `files` and `exclude`; they form one pass and produce one number.

You can prove it with a tiny standalone script:

```python
import re

paths = [
    'scheduler/models.py',
    'scheduler/tests/test_models.py',
    'docs/index.md',
    'vendor/lib.py',
]

files_re = re.compile(r'^scheduler/')
exclude_re = re.compile(r'/tests/')

a = [p for p in paths if files_re.search(p) and not exclude_re.search(p)]
b = [p for p in paths if not exclude_re.search(p) and files_re.search(p)]

print(a)       # ['scheduler/models.py']
print(a == b)  # True: order does not change the result
```

---

## 6. Stage 2: `os.path.lexists`

Right after Stage 1, the `Classifier` constructor drops any path that no longer exists on disk:

Source: [`commands/run.py#L74-L75`](https://github.com/pre-commit/pre-commit/blob/v4.6.2/pre_commit/commands/run.py#L74-L75)

```python
class Classifier:
    def __init__(self, filenames: Iterable[str]) -> None:
        self.filenames = [f for f in filenames if os.path.lexists(f)]
```

- `lexists` (not `exists`) returns `True` for **broken symlinks** too, so a dangling symlink is still passed to hooks (useful for `check-symlinks`).
- With plain `run`, deleted files are already removed by `--diff-filter` in Stage 0, so this stage rarely changes the count.
- With `run -a`, `git ls-files` lists everything in the index. If you deleted a file from disk without `git rm`, it is still in the index, and this stage is what removes it.

```bash
# Demonstration: file deleted from disk but still tracked
rm scheduler/old_module.py      # no `git rm`
git ls-files | grep old_module  # still listed (Stage 0)
pre-commit run -a               # removed at Stage 2, hooks never see it
```

This is the last stage that runs once. `self.filenames` is now the shared list every hook starts from.

---

## 7. Stage 3: Hook-Level `files:` / `exclude:`

Now pre-commit loops over hooks. For each one it calls `filenames_for_hook()`:

Source: [`commands/run.py#L100-L110`](https://github.com/pre-commit/pre-commit/blob/v4.6.2/pre_commit/commands/run.py#L100-L110)

```python
class Classifier:
    def filenames_for_hook(self, hook: Hook) -> Generator[str]:
        return self.by_types(
            filter_by_include_exclude(
                self.filenames,     # the shared list from Stage 2
                hook.files,         # Stage 3
                hook.exclude,       # Stage 3
            ),
            hook.types,             # Stage 4
            hook.types_or,          # Stage 4
            hook.exclude_types,     # Stage 4
        )
```

Read it inside out: `filter_by_include_exclude` runs first (Stage 3), and its output feeds `by_types` (Stage 4).

Stage 3 uses the **same** `filter_by_include_exclude` function as Stage 1, so everything in section 5 applies: `files` before `exclude`, logical AND, `re.search`, one combined pass.

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.6.9
    hooks:
      - id: ruff
        files: ^scheduler/     # Stage 3: only the scheduler package
        exclude: /tests/       # Stage 3: but not test directories
```

Hook-level `files` and `exclude` can come from two places:

- The hook's definition in the hook repository's `.pre-commit-hooks.yaml`.
- Your override in `.pre-commit-config.yaml`, which **replaces** the repository's value for that key. Keys are merged one by one with `dict.update` ([`repository.py#L109-L115`](https://github.com/pre-commit/pre-commit/blob/v4.6.2/pre_commit/repository.py#L109-L115)), so a key you do not set keeps the repository's value.

---

## 8. Stage 4: `types`, `types_or`, `exclude_types`

The final filter matches files by **type tags**, which come from the [`identify`](https://github.com/pre-commit/identify) library.

Source: [`commands/run.py#L77-L98`](https://github.com/pre-commit/pre-commit/blob/v4.6.2/pre_commit/commands/run.py#L77-L98)

```python
class Classifier:
    @functools.cache
    def _types_for_file(self, filename: str) -> set[str]:
        return tags_from_path(filename)

    def by_types(
            self,
            names: Iterable[str],
            types: Iterable[str],
            types_or: Iterable[str],
            exclude_types: Iterable[str],
    ) -> Generator[str]:
        types = frozenset(types)
        types_or = frozenset(types_or)
        exclude_types = frozenset(exclude_types)
        for filename in names:
            tags = self._types_for_file(filename)
            if (
                    tags >= types and
                    (not types_or or tags & types_or) and
                    not tags & exclude_types
            ):
                yield filename
```

### The three conditions

Source: [`clientlib.py#L248-L250`](https://github.com/pre-commit/pre-commit/blob/v4.6.2/pre_commit/clientlib.py#L248-L250)

| Key | Python check | Meaning | Default |
|---|---|---|---|
| `types` | `tags >= types` | File must have **all** of these tags | `[file]` |
| `types_or` | `tags & types_or` | File must have **at least one** of these tags (skipped if empty) | `[]` |
| `exclude_types` | `not tags & exclude_types` | File must have **none** of these tags | `[]` |

All three are combined with AND in a single `if`, so like Stage 3 there is no intermediate count inside Stage 4.

### What tags look like

```python
from identify.identify import tags_from_path

print(tags_from_path('scheduler/models.py'))
# {'file', 'text', 'python', 'non-executable'}

print(tags_from_path('scripts/deploy.sh'))
# {'file', 'text', 'shell', 'executable'}  (if chmod +x)

print(tags_from_path('docs/logo.png'))
# {'file', 'binary', 'image', 'png', 'non-executable'}
```

Tags are computed from the filename, extension, shebang line and file mode. The result is cached with `functools.cache`, so each file is identified only once even if 20 hooks ask.

### Examples

```yaml
hooks:
  # Must be Python (and a file)
  - id: black
    types: [python]

  # Python OR .pyi stub files
  - id: mypy
    types_or: [python, pyi]

  # Any text file, but not Markdown or SVG
  - id: end-of-file-fixer
    types: [text]
    exclude_types: [markdown, svg]

  # Executable files that are shell scripts
  - id: shellcheck
    types: [shell, executable]
```

Since `types` means "all of", `types: [python, yaml]` would match nothing (no file is both). Use `types_or: [python, yaml]` for "either".

---

## 9. The Per-Hook Loop

Putting it together, `_run_hooks` builds the shared list once, then runs each hook against it:

Source: [`commands/run.py#L282-L303`](https://github.com/pre-commit/pre-commit/blob/v4.6.2/pre_commit/commands/run.py#L282-L303)

```python
# pre_commit/commands/run.py (simplified)
classifier = Classifier.from_config(               # Stages 0, 1, 2
    _all_filenames(args), config['files'], config['exclude'],
)
for hook in hooks:
    current_retval, prior_diff = _run_single_hook(
        classifier, hook, skips, cols, prior_diff,
        verbose=args.verbose, use_color=args.color,
    )
    retval |= current_retval
    fail_fast = (config['fail_fast'] or hook.fail_fast or args.fail_fast)
    if current_retval and fail_fast:
        break
```

Inside `_run_single_hook`, `classifier.filenames_for_hook(hook)` applies Stages 3 and 4. Then:

- If the hook id or its `alias` is in the `SKIP` environment variable, it shows **Skipped**.
- If the final list is empty and the hook does not set `always_run: true`, it shows **(no files to check) Skipped**.
- Otherwise the hook runs. If `pass_filenames: true` (the default), the filtered list is appended to the command line.

Source: [`commands/run.py#L151-L168`](https://github.com/pre-commit/pre-commit/blob/v4.6.2/pre_commit/commands/run.py#L151-L168), [`clientlib.py#L256`](https://github.com/pre-commit/pre-commit/blob/v4.6.2/pre_commit/clientlib.py#L256), [`clientlib.py#L258`](https://github.com/pre-commit/pre-commit/blob/v4.6.2/pre_commit/clientlib.py#L258)

```bash
# Skip specific hooks for one run
SKIP=mypy,ruff pre-commit run
```

```yaml
# Run even when no files match (filenames still filtered, possibly empty)
- id: pytest-check
  name: pytest
  entry: pytest
  language: system
  pass_filenames: false
  always_run: true
```

---

## 10. Worked Example with Real Numbers

### Config

```yaml
# .pre-commit-config.yaml  (no top-level files/exclude set)
repos:
  - repo: https://github.com/psf/black
    rev: 24.8.0
    hooks:
      - id: black
        files: ^scheduler/
        exclude: /tests/
        types: [python]   # added on top of black's default types_or: [python, pyi], so .pyi stubs drop out
```

### Run

```bash
pre-commit run black -a --verbose
```

### Stage table

```
stage 0  git ls-files                       : 181  (all types, from the index)
stage 1  top-level files/exclude            : 181  (defaults = no-op)
stage 2  os.path.lexists                    : 181  (clean tree = no-op)
stage 3  files=^scheduler/ + exclude=/tests/:  96
stage 4  types=[python]                     :  80
```

Why each number:

- **181**: every tracked file in the repository.
- **181**: no top-level `files`/`exclude`, so defaults (`''` / `'^$'`) keep everything.
- **181**: no tracked file was deleted from disk.
- **96**: files whose path starts with `scheduler/` and does not contain `/tests/`.
- **80**: of those 96, the ones `identify` tags as `python` (the other 16 are templates, static assets, migrations data, etc.).

### Reproduce the numbers yourself

```python
# count_stages.py
import os
import re
import subprocess

from identify.identify import tags_from_path

# Stage 0
out = subprocess.check_output(['git', 'ls-files', '-z', '--deduplicate'])
s0 = [p for p in out.decode().split('\0') if p]

# Stage 1 (top-level defaults)
top_files, top_exclude = re.compile(''), re.compile('^$')
s1 = [p for p in s0 if top_files.search(p) and not top_exclude.search(p)]

# Stage 2
s2 = [p for p in s1 if os.path.lexists(p)]

# Stage 3 (hook files/exclude)
hook_files, hook_exclude = re.compile(r'^scheduler/'), re.compile(r'/tests/')
s3 = [p for p in s2 if hook_files.search(p) and not hook_exclude.search(p)]

# Stage 4 (types)
types = frozenset({'python'})
s4 = [p for p in s3 if tags_from_path(p) >= types]

for name, lst in [('stage 0', s0), ('stage 1', s1), ('stage 2', s2),
                  ('stage 3', s3), ('stage 4', s4)]:
    print(f'{name}: {len(lst)}')
```

```bash
pip install identify
python count_stages.py
```

Swap Stage 0 for `git diff --staged --name-only -z --diff-filter=ACMRTUXB` to simulate plain `pre-commit run`.

A version that takes the filters as flags lives in [`scripts/count_stages.py`](../scripts/count_stages.py):

```bash
uv run scripts/count_stages.py -a --files '^scheduler/' --exclude /tests/ --types python
```

---

## 11. `pre-commit run` vs `pre-commit run -a`

Only two things differ. Everything from Stage 1 onward is identical code.

| | `pre-commit run` | `pre-commit run -a` |
|---|---|---|
| Stage 0 command | `git diff --staged --name-only --diff-filter=ACMRTUXB` | `git ls-files --deduplicate` |
| Files included | Staged files only | All tracked files |
| Deleted files | Removed by `--diff-filter` | Listed, then removed by Stage 2 |
| Untracked files | Not included | Not included |
| Unstaged changes stashed | Yes | No |
| Content hooks see | Staged version | Working tree version |
| Typical use | Automatic on `git commit` | CI, or after adding/changing hooks |

Same config, different starting sizes. Suppose you staged three files: `scheduler/models.py`, `scheduler/admin.py` and `scheduler/tests/test_admin.py`:

```
                                          run     run -a
stage 0  file source                    :   3      181
stage 1  top-level files/exclude        :   3      181
stage 2  os.path.lexists                :   3      181
stage 3  ^scheduler/ + exclude /tests/  :   2       96
stage 4  types=[python]                 :   2       80
```

If you stage only `README.md`, Stage 4 reaches 0 for `black` and the output shows:

```
black................................................(no files to check)Skipped
```

---

## 12. Regex Gotchas

### `re.search`, not `re.match`

pre-commit uses `re.search`, which matches **anywhere** in the path. Anchors are your responsibility.

```python
import re

re.search(r'vendor/', 'src/vendor/lib.py')   # match! (probably unintended)
re.search(r'^vendor/', 'src/vendor/lib.py')  # None
re.search(r'^vendor/', 'vendor/lib.py')      # match
```

### Leading slash and top-level directories

Paths are relative to the repo root and never start with `/`:

```python
re.search(r'/tests/', 'scheduler/tests/test_x.py')  # match
re.search(r'/tests/', 'tests/test_x.py')            # None! no leading slash
re.search(r'(^|/)tests/', 'tests/test_x.py')        # match
```

### Dots and extensions

```yaml
# Wrong: "." matches any character, and no end anchor
exclude: .min.js

# Right
exclude: \.min\.js$
```

### Long lists: verbose mode

```yaml
exclude: |
  (?x)^(
    vendor/.*|
    scheduler/migrations/.*|
    .*\.min\.js|
    docs/_build/.*
  )$
```

`(?x)` makes the regex ignore whitespace and newlines, so you can format it one entry per line.

### These are regexes, not globs

```yaml
# Glob thinking (wrong): matches "<any char>py" anywhere
files: '*.py'        # actually an invalid regex: nothing to repeat

# Regex (right)
files: \.py$
```

---

## 13. Debugging Your Filters

### `--verbose`

Shows hook id, duration and output even for passing hooks:

```bash
pre-commit run black -a --verbose
```

### The `identity` meta hook

Prints exactly the filenames a hook would receive after all four stages:

```yaml
repos:
  - repo: meta
    hooks:
      - id: identity
        files: ^scheduler/
        exclude: /tests/
        types: [python]
```

```bash
pre-commit run identity -a --verbose
```

### Catch filters that do nothing

```yaml
repos:
  - repo: meta
    hooks:
      - id: check-hooks-apply       # fails if a hook matches zero files
      - id: check-useless-excludes  # fails if an exclude matches zero files
```

Both hooks are defined with `files: ^\.pre-commit-config\.yaml$` ([`clientlib.py#L376-L398`](https://github.com/pre-commit/pre-commit/blob/v4.6.2/pre_commit/clientlib.py#L376-L398)), so on a normal commit they run only when the config file itself is staged. They then check against every tracked file (`git ls-files`), not the staged list. Run them on demand with `pre-commit run check-hooks-apply -a`.

### Test a single file through the pipeline

```bash
pre-commit run black --files scheduler/models.py --verbose
```

The file still goes through Stages 1 to 4, so if your filters exclude it, the hook reports `(no files to check)`.

### Inspect a file's tags

```bash
python -c "from identify.identify import tags_from_path as t; print(t('scheduler/models.py'))"
```

---

## 14. Quick Reference

```
pre-commit  ==  pre-commit run

ONCE PER RUN
  Stage 0  _all_filenames()
             --from-ref/--to-ref  > --files > -a > merge conflict > staged
             staged: git diff --staged --name-only --diff-filter=ACMRTUXB
             -a:     git ls-files --deduplicate
           (+ stash unstaged changes unless -a or --files)
  Stage 1  top-level  files: then exclude:    (defaults '' and '^$')
  Stage 2  os.path.lexists                    (broken symlinks kept)

PER HOOK
  Stage 3  hook files: then exclude:          (same function as Stage 1)
  Stage 4  types (ALL) + types_or (ANY) + exclude_types (NONE)
           tags from `identify`, default types: [file]

THEN
  SKIP env var          -> Skipped
  empty + !always_run   -> (no files to check) Skipped
  otherwise             -> run, filenames appended if pass_filenames
```

| Rule | Detail |
|---|---|
| Filters only narrow | No stage can restore a file an earlier stage dropped |
| `files` vs `exclude` order | `files` evaluated first, but it is a logical AND, so order does not affect the result |
| Regex flavour | Python `re.search`, paths relative to repo root, always `/` separators |
| Top-level vs hook-level | Top-level affects every hook; hook-level affects only that hook |
| Hook override | Your `files`/`exclude`/`types` in config replace the hook repo's defaults |
| `--diff-filter` | Hardcoded Git status filter, unrelated to your config |
