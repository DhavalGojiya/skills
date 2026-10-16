---
name: python
description: >-
  How Python installers (uv, pip) turn a package's [project.scripts] entry into the console-script launchers in .venv/bin, and why those launchers break. Use when asking who generates .venv/bin/<tool> (the installer, not the build backend or the package's repository), why its shebang names the venv's python, why a launcher starts with "#!/bin/sh" and "'''exec'", how the uv and pip launcher templates differ, or what sys.exit(main()) does with an entry point's return value; when .venv/bin commands fail after a project directory is moved or renamed ("cannot execute: required file not found", "bad interpreter: No such file or directory", "exec: .../.venv/bin/python: not found") while .venv/bin/python -m still works; or when choosing uv venv --relocatable.
---

# Python console-script launchers

Verified against uv [0.13.0](https://github.com/astral-sh/uv/tree/0.13.0) (`crates/uv-install-wheel/src/wheel.rs`), pip [26.2.1](https://github.com/pypa/pip/tree/26.2.1) (`src/pip/_internal/operations/install/wheel.py`, vendored distlib 0.4.2) and CPython [v3.15.0](https://github.com/python/cpython/tree/v3.15.0) (`Modules/getpath.py`) source, and by installing a package with a console script into fresh uv and pip venvs on Linux 7.0 with Python 3.15.0. Line-level source links are in `references/console-scripts.md`.

## Where a launcher comes from

```text
pyproject.toml   [project.scripts] hello = "hello:main"
      │  build backend (uv_build, setuptools, hatchling): copies the declaration
      ▼
wheel            hello-1.0.dist-info/entry_points.txt      (no bin/hello in the wheel)
      │  installer (uv, pip), on the target machine at install time
      ▼
.venv/bin/hello  "#!" + absolute path of .venv/bin/python + fixed template
                 listed in RECORD (uninstall removes it), installer named in INSTALLER
```

## Rules

01. The installer writes `.venv/bin/<tool>`, at install time, from the wheel's `entry_points.txt`. The package's repository and its build backend only declare the command; the wheel contains no launcher file. `INSTALLER` and `RECORD` in the `*.dist-info` directory say who wrote it.
02. Do not edit a launcher by hand; the next install overwrites it.
03. The shebang is the absolute path of the venv's `python` at install time, which is why the launcher works without activating the venv. It names the interpreter; a shebang naming the launcher itself loops until `Too many levels of symbolic links`.
04. Run the venv's interpreter by its venv path. `.venv/bin/python` is a symlink to the base interpreter, and CPython finds `pyvenv.cfg` from the path it was started as (one directory up, then the same directory); running the resolved target gives the base `sys.prefix` and none of the venv's packages.
05. Expect a three-line header `#!/bin/sh` / `'''exec' '/path/python' "$0" "$@"` / `' '''` when the venv path contains a space or `#!` plus the path plus the newline passes 127 bytes, and always in a `uv venv --relocatable` venv. It is valid shell and valid Python at once, not corruption.
06. Expect uv and pip launchers to differ: uv 0.13.0 keeps `# -*- coding: utf-8 -*-` and strips `-script.pyw` or `.exe` from `sys.argv[0]`; pip 26.2.1 strips only `.exe`. In the `/bin/sh` header uv single-quotes the path, pip double-quotes a path with a space and leaves a long one unquoted.
07. `sys.exit(main())` makes the entry point's return value the exit status: `None` gives 0, an int gives that status, a string is printed to stderr with status 1. A git hook running a launcher blocks the commit when `main()` returns non-zero.
08. After moving or renaming a project, every launcher still points at the old Python path. Recreate the venv (`rm -rf .venv && uv sync`) or reinstall (`uv sync --reinstall`); plain `uv sync` leaves dependency launchers broken. Meanwhile run `uv run python -m <module>`: `uv run <tool>` executes the same broken launcher, and `.venv/bin/python -m` cannot import the project's own editable package, whose `.pth` file holds the old `src` path.
09. Read the post-move error by launcher form: a plain shebang gives bash 5.2's `cannot execute: required file not found` (127), bash 5.1's `bad interpreter: No such file or directory` (126), or Python's `FileNotFoundError` naming the launcher; a `/bin/sh` header gives `<launcher>: 2: exec: /old/path/.venv/bin/python: not found` (127).
10. Create venvs that will be moved, copied or symlinked into with `uv venv --relocatable`: its launchers find `python` relative to their own resolved path at run time.

## Debug procedure

1. See the launcher's form and the interpreter it names: `head -3 .venv/bin/<tool>`.
2. Check that interpreter exists: `ls -lL /path/from/line/1/or/2/python`.
3. See who wrote the launcher and that it is tracked: `cat .venv/lib/python3*/site-packages/<dist>-*.dist-info/INSTALLER; echo; grep bin/<tool> .venv/lib/python3*/site-packages/<dist>-*.dist-info/RECORD`.
4. Check the venv itself still works: `.venv/bin/python -c 'import sys; print(sys.prefix)'`.
5. For anything at the shebang level (CRLF, a line over 255 bytes, options, `env`, chains), use the `linux` skill and its `scripts/check_shebang.py`.

## References

- `references/console-scripts.md`: who writes launchers (with the wheel and dist-info contents), the uv and pip templates quoted side by side, why the shebang names the venv's Python with CPython's `pyvenv.cfg` lookup, the `/bin/sh` header line by line with each installer's condition and quoting, moved projects with each error and the fixes, and relocatable venvs. Read it to explain or repair a launcher.
- For how the kernel runs any `#!` file, its limits, and what bash, dash, `env`, Python and git print when it fails, use the `linux` skill.
