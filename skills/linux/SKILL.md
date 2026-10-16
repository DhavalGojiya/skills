---
name: linux
description: >-
  How Linux runs a script through its #! (shebang) line, the limits on that line, and what bash, sh, env, Python and git print when it fails. Use when writing or reviewing a shebang or an env -S line; on "cannot execute: required file not found", "bad interpreter", "Too many levels of symbolic links" with no symlink involved, "Exec format error", "env: 'python3 -u': No such file or directory", "use -[v]S to pass options in shebang lines", "invalid option -- ' '" or Python's "Unknown option: -"; when subprocess raises FileNotFoundError for a script that exists; when a Python file prints "from: command not found"; when a script works from the terminal but not from cron, a git hook or subprocess; or when a git hook is silently skipped.
---

# Linux script execution

Verified against Linux [7.0](https://github.com/torvalds/linux/tree/v7.0) source (`fs/binfmt_script.c`, `fs/exec.c`) and by experiment on Linux 7.0.0 with bash 5.2.21, dash 0.5.12, GNU coreutils 9.4, glibc 2.39, git 2.43.0, Python 3.12.3 and 3.15.0. Line-level source links are in `references/`.

## How a `#!` script runs

The shebang names the program that **reads** the file. Inside one `execve()`, the kernel reads the first 256 bytes, splits the first line into an interpreter path and at most one argument, and runs:

```text
first line of ./tool:  #!/home/u/proj/.venv/bin/python -I
command:               ./tool run --all
kernel runs:           /home/u/proj/.venv/bin/python   -I    ./tool   run --all
                       └──────── interpreter ───────┘  └arg┘ └ path ┘ └argv[1:]┘
```

When that fails, the kernel returns only an errno; the message comes from the caller, and most callers name the script, not the interpreter.

## Rules

### Writing a shebang

01. Name the interpreter, never the script itself: a venv launcher's line is `#!/abs/.venv/bin/python`. A shebang that names its own file loops until `ELOOP`: bash prints `bad interpreter: Too many levels of symbolic links` (exit 126), though no symlink is involved.
02. Use an absolute interpreter path. The kernel opens a relative one (`#!.venv/bin/python`, `#!python3`) against the caller's current directory and never searches `PATH`.
03. Put at most one argument after the interpreter, as one word: `#!/usr/bin/python3 -Iu`, `-Xdev`. Everything after the path is a single argument, so `-I -u` fails with `Unknown option: -` (exit 2) and `-X dev` silently leaves dev mode off.
04. For several options, use `#!/usr/bin/env -S python3 -I -u`. Without `-S`, `env` looks for a program named `python3 -I` (`No such file or directory`, then `use -[v]S to pass options in shebang lines`, exit 127), and `#!/usr/bin/env -i python3` fails with `invalid option -- ' '` (exit 125). Debug the split by changing `-S` to `-vS`.
05. Use `#!/usr/bin/env python3` only when the caller's `PATH` should pick the interpreter. It is looked up at run time: cron (`/usr/bin:/bin`), systemd services and git started from an IDE get a different `python3`, and a dangling venv `python3` earlier on `PATH` silently falls through to the next one.
06. Keep the first line at most 255 bytes before the newline (interpreter path at most 253). A longer path fails with `ENOEXEC`; a longer argument is cut silently, and a cut that turns `-I` into `-` makes Python read its program from stdin instead of the file: from a pipe or `/dev/null` it runs that input and exits 0, from a terminal it opens the interactive prompt.
07. Save scripts with LF line endings. A CR stays part of the line: `#!/usr/bin/python3\r` gives bash 5.2's `cannot execute: required file not found` (127) with no `^M` shown; with `env` it is `env: 'python3\r': No such file or directory` plus a misleading `-S` hint. Fix with `sed -i 's/\r$//' FILE`.
08. Give every executable script a `#!` line, with no UTF-8 BOM before it. Without one, `execve()` fails with `ENOEXEC`; bash, dash, glibc `execvp()` (`env`, `xargs`) and git hooks then run the file as shell code (`from: command not found`, or ImageMagick's `import` grabbing the screen), while Python raises `OSError: [Errno 8] Exec format error`. A script that works from the terminal can fail from Python, and the reverse.
09. Give scripts read and execute permission (`chmod 755`). The kernel needs `x` on the script and on the interpreter; the interpreter then reopens the script and needs `r`.
10. Keep interpreter chains short: a `#!` interpreter may itself be a `#!` script, up to 4 levels; the 5th fails with `ELOOP`.

### Reading the error

11. On bash 5.2, `cannot execute: required file not found` means the script exists but the interpreter on its `#!` line does not (a moved venv, a CR, a typo). bash 5.1 and older said `bad interpreter: No such file or directory` for the same thing; 5.2 prints `bad interpreter` only for other errors, such as `EACCES`, `ELOOP` and `ENOTDIR`, with the path cut at 125 bytes.
12. Do not trust which file an error names. For a missing interpreter, dash (`not found`), `env` and Python (`FileNotFoundError: [Errno 2] No such file or directory: './s'`) name the script exactly as when the script itself is missing, and git prints `cannot exec '<hook>'` (a missing hook prints nothing). Run `test -e` on the script, then check its `#!` line.
13. A git hook without execute permission is skipped with only a `hint: The '.git/hooks/pre-commit' hook was ignored ...`, and the commit succeeds; `advice.ignoredHook=false` hides even that.

## Debug procedure

1. Check the script exists, and its mode: `test -e ./s && ls -l ./s` (needs `r` and `x`).
2. Look at the first line's bytes, up to the 256 the kernel reads: `head -1 ./s | head -c 256 | od -c`. It must start with `#!`; `\r \n` at the end means CRLF; `357 273 277` before `#!` is a BOM.
3. Check the interpreter it names: `ls -lL "$(head -1 ./s | sed 's/^#![[:blank:]]*//; s/[[:blank:]].*//')"`. For a `#!/bin/sh` launcher, check the path on line 2 instead.
4. Run the checker from this skill's directory, which parses the line exactly as the kernel does and follows chains, `env` lookups and installer wrappers: `uv run scripts/check_shebang.py --cwd /path/to/project ./s [ARGS]`.
5. See each `execve()` and its errno: `strace -f -e trace=execve ./s 2>&1 | grep execve`. strace itself has no `ENOEXEC` fallback; to see a caller's `/bin/sh` retry, trace that caller: `strace -f -e trace=execve sh -c ./s 2>&1 | grep execve`.
6. For an `env` shebang, run `env python3 -c 'import sys; print(sys.executable)'` in the failing environment (cron, hook, service) to see which interpreter it gets.

## Script

`scripts/check_shebang.py` mirrors `load_script()` and the exec depth limit, and reports each problem with the message callers will print:

```bash
uv run scripts/check_shebang.py --cwd /path/to/project .venv/bin/pre-commit run
```

Run it from this skill's directory. Flags go before the script: `--cwd DIR` (the directory the caller runs from; the script path and relative interpreters are resolved against it), `--path PATH` (the caller's `PATH`, for `env` lookups). Everything after the script is passed through as its arguments. Exit status is 1 when it finds a problem.

## References

- `references/shebang.md`: the kernel's parsing with quoted source and an argv diagram, the single-argument rule, `/usr/bin/env` and `-S` splitting, the 255-byte limit and silent truncation, CRLF, relative paths, chains and `ELOOP`, the `ENOEXEC` shell fallback, permissions. Read it to write a shebang or to explain why one fails.
- `references/exec-errors.md`: a table of every failure against bash, dash, `env`, Python and git with exact messages and exit statuses, the bash 5.2 message change with its source, and a flowchart from message to cause. Read it when an error message does not make sense.
- For how uv and pip write the launchers in `.venv/bin` (templates, the `#!/bin/sh` header, moved projects, relocatable venvs), use the `python` skill.
