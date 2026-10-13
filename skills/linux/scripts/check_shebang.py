# /// script
# requires-python = ">=3.14"
# ///
"""Parse a file's #! line the way Linux load_script() does and report what would fail.

Mirrors Linux 7.0 fs/binfmt_script.c and the depth limit in fs/exec.c. Follows chains of
#! interpreters, the uv/pip "#!/bin/sh" + "'''exec'" launcher wrapper, and /usr/bin/env
lookups on PATH.
"""

import argparse
import os
import re
import shlex
import stat
import sys

BINPRM_BUF_SIZE = 256
MAX_SCRIPT_DEPTH = 4  # exec_binprm() fails with ELOOP once depth > 5
PY_OPTS_WITH_VALUE = "cmWX"
RELOCATABLE_RE = re.compile(
    r"\"\$\(dirname -- \"\$\(realpath -- \"\$0\"\)\"\)\"/'([^']*)'"
)

problems: list[str] = []


def problem(message: str) -> None:
    problems.append(message)
    print(f"  PROBLEM: {message}")


def spacetab(c: int) -> bool:
    return c in b" \t"


def next_non_spacetab(buf: bytes, first: int, last: int) -> int | None:
    for i in range(first, last + 1):
        if not spacetab(buf[i]):
            return i
    return None


def next_terminator(buf: bytes, first: int, last: int) -> int | None:
    for i in range(first, last + 1):
        if spacetab(buf[i]) or buf[i] == 0:
            return i
    return None


def parse_shebang(
    raw: bytes, size: int = BINPRM_BUF_SIZE
) -> tuple[bytes, bytes | None] | str:
    """Return (interpreter, argument) or an errno name, like load_script() with a size-byte buffer."""
    buf = raw[:size].ljust(size, b"\0")
    if buf[:2] != b"#!":
        return "ENOEXEC"
    buf_end = size - 1
    i_end = buf.find(b"\n")
    if i_end == -1:
        start = next_non_spacetab(buf, 2, buf_end)
        if start is None or next_terminator(buf, start, buf_end) is None:
            return "ENOEXEC"
        i_end = buf_end
    while spacetab(buf[i_end - 1]):
        i_end -= 1
    i_name = next_non_spacetab(buf, 2, i_end)
    if i_name is None or i_name == i_end:
        return "ENOEXEC"
    i_arg = None
    i_sep = next_terminator(buf, i_name, i_end)
    if i_sep is not None and buf[i_sep] != 0:
        i_arg = next_non_spacetab(buf, i_sep, i_end)
    name_end = i_sep if i_arg is not None else i_end
    name = buf[i_name:name_end].split(b"\0")[0]
    arg = buf[i_arg:i_end].split(b"\0")[0] if i_arg is not None else None
    return name, arg


def check_executable(path: str, role: str) -> bool:
    """Apply the checks do_open_execat() makes, returning False on ENOENT or EACCES."""
    if not os.path.exists(path):
        dangling = " (a dangling symlink)" if os.path.islink(path) else ""
        if role == "script":
            problem(
                f"ENOENT: script {path!r} does not exist{dangling}: "
                "'No such file or directory', exit 127."
            )
        else:
            problem(
                f"ENOENT: interpreter {path!r} does not exist{dangling}. bash 5.2 prints "
                "'cannot execute: required file not found' (127); dash, env, Python and git "
                "report 'No such file or directory' or 'not found' against the script."
            )
        return False
    if not stat.S_ISREG(os.stat(path).st_mode):
        problem(f"EACCES: {role} {path!r} is not a regular file.")
        return False
    if os.statvfs(path).f_flag & os.ST_NOEXEC:
        problem(f"EACCES: {role} {path!r} is on a noexec mount.")
        return False
    if not os.access(path, os.X_OK):
        problem(f"EACCES: {role} {path!r} has no execute permission (chmod +x).")
        return False
    return True


def check_python_arg(arg: str) -> None:
    """Report how CPython's getopt treats a single shebang argument holding spaces."""
    if not arg.startswith("-") or arg.startswith("--") or " " not in arg:
        return
    for i, c in enumerate(arg[1:], start=1):
        if c == " ":
            problem(
                f"Python gets {arg!r} as ONE argument and stops at the space: "
                "'Unknown option: - ', exit 2. Write options as one word (-Iu) "
                "or use '#!/usr/bin/env -S python3 ...'."
            )
            return
        if c in PY_OPTS_WITH_VALUE:
            value = arg[i + 1 :]
            if value.startswith(" "):
                problem(
                    f"Python gets -{c} with the value {value!r} (leading space); "
                    f"-X ignores it silently. Write -{c}{value.strip()}."
                )
            return


def check_env(arg: str | None, env_path: str) -> str | None:
    """Return the program /usr/bin/env would run, reporting single-argument mistakes."""
    if arg is None:
        problem("env with no command just prints the environment.")
        return None
    first = arg.split()[0] if arg.split() else arg
    short_s = first.startswith("-") and not first.startswith("--") and "S" in first
    if short_s or first.startswith("--split-string"):
        print(
            "  env -S splits this argument; run the script with '-vS' to see the words."
        )
        return None
    if any(c in arg for c in " \t\n\v\f\r"):
        if arg.startswith("-"):
            problem(
                f"env receives {arg!r} as one argument: 'invalid option -- ' '', "
                "exit 125. Put everything after -S."
            )
        else:
            problem(
                f"env looks for a program literally named {arg!r}: "
                "'No such file or directory' plus 'use -[v]S ...', exit 127. "
                + (
                    "The whitespace is a CR: convert to LF."
                    if "\r" in arg
                    else "Use -S."
                )
            )
        return None
    denied = None
    for d in env_path.split(os.pathsep):
        candidate = os.path.join(d or ".", arg)
        if os.path.islink(candidate) and not os.path.exists(candidate):
            print(f"  note: {candidate} is a dangling symlink; env skips it")
        elif os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            print(f"  env resolves {arg!r} on PATH to {candidate}")
            return candidate
        elif os.path.exists(candidate):
            denied = denied or candidate
    if denied:
        problem(f"env cannot execute {denied}: '{arg}': Permission denied, exit 126.")
    else:
        problem(f"env finds no {arg!r} on PATH: 'No such file or directory', exit 127.")
    return None


def check_sh_wrapper(script: str, shown: str, raw: bytes) -> None:
    """Check the interpreter named on line 2 of a uv/pip '#!/bin/sh' launcher."""
    lines = raw.split(b"\n")
    if len(lines) < 2 or not lines[1].startswith(b"'''exec'"):
        return
    line2 = os.fsdecode(lines[1])
    reloc = RELOCATABLE_RE.search(line2)
    if reloc:
        target = os.path.join(os.path.dirname(os.path.realpath(script)), reloc.group(1))
        print(f"  relocatable launcher, runs {target}")
    else:
        target = shlex.split(line2)[1]
        print(f"  /bin/sh wrapper (installer launcher), runs {target}")
    if not os.path.exists(target):
        problem(
            f"{target!r} does not exist: sh prints '{shown}: 2: exec: {target}: not found', "
            "exit 127. Recreate the venv or reinstall (the project was probably moved)."
        )


def enoexec_reason(raw: bytes) -> str:
    line1 = raw.split(b"\n", 1)[0]
    if not raw.startswith(b"#!"):
        return "there is no #! line"
    if not line1[2:].strip(b" \t"):
        return "the #! line names no interpreter"
    return (
        "the #! line is over 255 bytes and the interpreter path does not end "
        "within the first 256"
    )


def check(script: str, args: list[str], cwd: str, env_path: str) -> None:
    argv = [script, *args]
    seen: set[str] = set()
    current = script
    for depth in range(MAX_SCRIPT_DEPTH + 2):
        path = os.path.join(cwd, current)
        real = os.path.realpath(path)
        print(f"[depth {depth}] {current!r}")
        if not check_executable(path, "script" if depth == 0 else "interpreter"):
            return
        try:
            with open(path, "rb") as f:
                raw = f.read(4096)
        except PermissionError:
            if depth == 0:
                problem(
                    f"{current!r} is not readable: the kernel runs it, the interpreter "
                    "cannot open it (chmod a+r)."
                )
            else:
                print(
                    f"  {current!r} is not readable; cannot tell whether it is ELF or a script."
                )
            return
        if raw.startswith(b"\x7fELF"):
            print(f"  ELF binary; the kernel runs: {argv}")
            return
        if raw.startswith(b"\xef\xbb\xbf#!"):
            problem(
                "a UTF-8 BOM precedes '#!': the kernel sees no #! line (ENOEXEC). Save without BOM."
            )
            return
        parsed = parse_shebang(raw)
        if parsed == "ENOEXEC":
            problem(
                f"ENOEXEC (Exec format error): {enoexec_reason(raw)}. bash, dash, glibc execvp "
                "(env) and git run the file as shell code instead; Python raises OSError [Errno 8]."
            )
            return
        name_b, arg_b = parsed
        name = os.fsdecode(name_b)
        arg = os.fsdecode(arg_b) if arg_b is not None else None
        print(f"  interpreter: {name!r}")
        print(f"  argument:    {arg!r}" if arg is not None else "  argument:    none")
        full = parse_shebang(raw, max(BINPRM_BUF_SIZE, len(raw) + 2))
        full_arg = full[1] if full != "ENOEXEC" else None
        if full_arg != arg_b:
            verb = "drops" if arg_b is None else "cuts"
            problem(
                f"the #! line passes 255 bytes: the kernel silently {verb} the argument "
                f"{os.fsdecode(full_arg or b'')!r}."
            )
        if "\r" in name or (arg and "\r" in arg and os.path.basename(name) != "env"):
            problem("CR in the #! line (CRLF endings). Fix: sed -i 's/\\r$//' FILE")
        if not name.startswith("/"):
            problem(
                f"relative interpreter {name!r} is opened against the caller's cwd ({cwd}), "
                "not the script's directory, and never searched on PATH."
            )
        argv = [name, *([arg] if arg is not None else []), *argv]
        print(f"  the kernel runs: {argv}")
        interp = os.path.join(cwd, name)
        if real == os.path.realpath(interp) or real in seen:
            problem(
                "the #! line points back at a script in this chain: ELOOP, "
                "'Too many levels of symbolic links'."
            )
            return
        seen.add(real)
        base = os.path.basename(name)
        if base == "env":
            if not check_executable(interp, "interpreter"):
                return
            target = check_env(arg, env_path)
            if target and re.fullmatch(r"python[0-9.]*", os.path.basename(target)):
                print("  (python options after env need -S)")
            return
        if re.fullmatch(r"python[0-9.]*", base) and arg:
            check_python_arg(arg)
        if base == "sh" and os.path.realpath(interp) == os.path.realpath("/bin/sh"):
            check_sh_wrapper(path, current, raw)
        current = name
    if not check_executable(os.path.join(cwd, current), "interpreter"):
        return
    problem(
        f"more than {MAX_SCRIPT_DEPTH} script interpreters in a chain: ELOOP, "
        "'Too many levels of symbolic links'."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--cwd", default=os.getcwd(), help="directory the caller runs from"
    )
    parser.add_argument(
        "--path", default=os.environ.get("PATH", ""), help="PATH the caller has"
    )
    parser.add_argument(
        "script", help="the file to check, as you would run it (./tool)"
    )
    parser.add_argument(
        "args", nargs=argparse.REMAINDER, help="arguments, to show the final argv"
    )
    opts = parser.parse_args()
    check(opts.script, opts.args, opts.cwd, opts.path)
    print(
        "OK: no problems found" if not problems else f"{len(problems)} problem(s) found"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
