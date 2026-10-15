# /// script
# requires-python = ">=3.14"
# ///
"""Map an MWAA deploy bundle to its container paths and report what will break there.

Checks a local dags/ folder, plugins.zip (or plugins/ folder), requirements.txt and
startup.sh against how the MWAA image consumes them: amazon-mwaa-docker-images
32b2a2e (images/airflow/3.0.6), Airflow 3.0.6 sys.path handling and pip 26.2.1
requirements-file parsing.

Checks, by flag:
  --plugins       a single top-level folder (extracts one level too deep), __pycache__
                  or .pyc files, wheels below the top level, airflow_local_settings.py
  --dags/plugins  top-level names shadowed on sys.path: mwaa, standard-library modules,
                  the same name in both dags/ and plugins
  --startup       CRLF line endings, bash syntax errors, set -e / errexit, exit N,
                  exports of reserved variables
  --requirements  a constraint form MWAA's regex misses or a comment it matches,
                  relative -c/-r/--find-links paths, referenced files missing from
                  dags/ or plugins, --no-index without --find-links, ${VARS} that
                  startup.sh does not export, pins of MWAA's essential packages

Output: PROBLEM breaks the deploy, WARNING is a likely mistake, note is information.
Exit status is 1 if any PROBLEM was reported.

Example, from the directory holding the bundle:
  uv run check_mwaa_layout.py --dags dags --plugins plugins.zip \\
    --requirements requirements.txt --startup startup.sh
"""

import argparse
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

AIRFLOW_HOME = "/usr/local/airflow"
DAGS = f"{AIRFLOW_HOME}/dags"
PLUGINS = f"{AIRFLOW_HOME}/plugins"
REQ_DIR = f"{AIRFLOW_HOME}/requirements"
REQ_FILE = f"{REQ_DIR}/requirements.txt"
STARTUP_FILE = f"{AIRFLOW_HOME}/startup/startup.sh"

# user_requirements.py _requirements_has_constraints()
MWAA_CONSTRAINT_RE = re.compile(r"-c |--constraint ")
# pip req_file.py ENV_VAR_RE and COMMENT_RE
PIP_ENV_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")
PIP_COMMENT_RE = re.compile(r"(^|\s+)#.*$")
SCHEME_RE = re.compile(r"^(http|https|file)://", re.IGNORECASE)
REQ_NAME_RE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(==|>=|<=|~=|!=|<|>)"
)

# mwaa_essential_constraints.txt, always passed to pip by safe-pip-install
ESSENTIAL = {
    "apache-airflow", "flask-appbuilder", "flask-babel", "flask-bcrypt", "flask-caching",
    "flask-jwt-extended", "flask-limiter", "flask-login", "flask-sqlalchemy", "flask-session",
    "flask-wtf", "flask", "gunicorn", "billiard", "celery", "gevent", "greenlet", "kombu",
    "vine", "sqlalchemy-jsonfield", "sqlalchemy-utils", "sqlalchemy", "statsd", "watchtower",
    "fluent-logger",
}  # fmt: skip
# setup_environment.py _is_protected_os_environ(), plus the User Guide's reserved list
RESERVED = {
    "AIRFLOW_ENV_ID", "AIRFLOW_ENV_NAME", "AIRFLOW_HOME", "AIRFLOW_TASK_REVISION_ID",
    "AIRFLOW_VERSION", "AIRFLOW__AWS_MWAA__REDIRECT_URL", "JWT_PUBLIC_KEY",
    "AIRFLOW__WEBSERVER__BASE_URL", "AIRFLOW__API__BASE_URL", "AWS_DEFAULT_REGION",
    "AWS_REGION", "CUSTOMER_ACCOUNT_ID", "ECS_AGENT_URI", "ECS_CONTAINER_METADATA_URI",
    "ECS_CONTAINER_METADATA_URI_V4", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_EXECUTION_ENV", "PYTHONPATH", "PYTHONUNBUFFERED", "WATCHTOWER_VERSION",
    "USE_NON_CRITICAL_LOGGING", "AIRFLOW__CORE__DATABASE_ACCESS_ISOLATION",
    "MWAA__AIRFLOW__COMPONENT", "AIRFLOW__WEBSERVER__SECRET_KEY", "AIRFLOW__CORE__FERNET_KEY",
    "AIRFLOW__CELERY__BROKER_URL", "AIRFLOW__CELERY__RESULT_BACKEND", "AIRFLOW__CORE__EXECUTOR",
    "AIRFLOW__CORE__LOAD_EXAMPLES", "SQL_ALCHEMY_CONN", "AIRFLOW__CORE__SQL_ALCHEMY_CONN",
    "AIRFLOW__CELERY__DEFAULT_QUEUE", "AIRFLOW__OPERATORS__DEFAULT_QUEUE",
    "AIRFLOW_CONN_AWS_DEFAULT", "AIRFLOW__CELERY__WORKER_AUTOSCALE",
}  # fmt: skip
ERREXIT_RE = re.compile(r"^\s*set\s+(-[A-Za-z]*e[A-Za-z]*\b|.*-o\s+errexit\b)")
EXIT_NONZERO_RE = re.compile(r"(?:^|[;&|]\s*)exit\s+([1-9][0-9]*)")
EXPORT_RE = re.compile(r"^\s*(?:export|declare\s+-x)\s+(.+)")
ASSIGN_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

problems = 0


def problem(message: str) -> None:
    global problems
    problems += 1
    print(f"  PROBLEM: {message}")


def warn(message: str) -> None:
    print(f"  WARNING: {message}")


def info(message: str) -> None:
    print(f"  note: {message}")


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def list_plugins(path: Path) -> tuple[list[str], bool]:
    """Return the file names plugins/ will hold, relative to it, and whether it is a zip."""
    if path.is_dir():
        names = [p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()]
        return sorted(names), False
    with zipfile.ZipFile(path) as zf:
        return sorted(n for n in zf.namelist() if not n.endswith("/")), True


def top_level_modules(names: list[str]) -> set[str]:
    """Importable top-level names from files relative to a sys.path directory."""
    modules = set()
    for name in names:
        first, _, rest = name.partition("/")
        if rest and first.isidentifier() and first != "__pycache__":
            modules.add(first)
        elif not rest and first.endswith(".py") and first[:-3].isidentifier():
            modules.add(first[:-3])
    return modules


def check_plugins(names: list[str], is_zip: bool, label: str) -> None:
    print(f"\n{label} -> {PLUGINS}/")
    for name in names[:15]:
        print(f"    {PLUGINS}/{name}")
    if len(names) > 15:
        print(f"    ... {len(names) - 15} more")
    tops = {n.partition("/")[0] for n in names}
    if len(tops) == 1 and all("/" in n for n in names):
        (top,) = tops
        if f"{top}/__init__.py" not in names:
            problem(
                f"everything is inside '{top}/', so it extracts to {PLUGINS}/{top}/ and "
                f"'from <module> import ...' fails with ModuleNotFoundError; zip the folder's "
                f"contents instead: cd {top} && zip -r ../plugins.zip ."
            )
    if is_zip and any("__pycache__/" in n or n.endswith(".pyc") for n in names):
        warn(
            "contains __pycache__/ or .pyc files compiled elsewhere; zip with -x '*__pycache__*'"
        )
    nested_wheels = [n for n in names if n.endswith(".whl") and "/" in n]
    if nested_wheels:
        warn(
            f"wheels below the top level ({nested_wheels[0]}, ...): pip does not search "
            f"subdirectories of --find-links {PLUGINS}"
        )
    if "airflow_local_settings.py" in names:
        warn(
            f"airflow_local_settings.py is shadowed by MWAA's {AIRFLOW_HOME}/config/"
            "airflow_local_settings.py, which is earlier on sys.path"
        )


def check_modules(dags_names: list[str] | None, plugin_names: list[str] | None) -> None:
    print("\nimport names on sys.path")
    found = False
    sources = [("dags/", dags_names), ("plugins", plugin_names)]
    modules = {label: top_level_modules(names) for label, names in sources if names}
    for label, names in modules.items():
        for name in sorted(names):
            if name == "mwaa":
                found = True
                problem(
                    f"{label} module 'mwaa' is shadowed by MWAA's /python/mwaa (PYTHONPATH)"
                )
            elif name in sys.stdlib_module_names:
                found = True
                warn(
                    f"{label} module '{name}': 'import {name}' gets the standard library"
                )
    if len(modules) == 2:
        for name in sorted(modules["dags/"] & modules["plugins"]):
            found = True
            warn(
                f"'{name}' exists in dags/ and plugins; 'import {name}' gets the plugins one"
            )
    if not found:
        info("no shadowed top-level names (installed packages are not checked)")


def startup_exports(text: str) -> tuple[set[str], set[str]]:
    exported, assigned = set(), set()
    for line in text.splitlines():
        if m := EXPORT_RE.match(line):
            for token in m.group(1).split():
                name = token.partition("=")[0]
                if NAME_RE.match(name):
                    exported.add(name)
        elif m := ASSIGN_RE.match(line):
            assigned.add(m.group(1))
    return exported, assigned - exported


def check_startup(path: Path) -> None:
    print(f"\n{path} -> {STARTUP_FILE}")
    data = path.read_bytes()
    text = data.decode(errors="replace")
    if b"\r\n" in data:
        problem(
            "CRLF line endings: bash keeps the \\r, so commands fail with $'\\r': command "
            "not found and exported values end in a carriage return; fix with sed -i 's/\\r$//'"
        )
    first = text.splitlines()[0] if text else ""
    if first.startswith("#!") and "bash" not in first:
        info(f"'{first}' is ignored: MWAA sources the file with bash")
    if bash := shutil.which("bash"):
        result = subprocess.run(
            [bash, "-n", str(path)], capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            problem(f"bash -n reports a syntax error: {result.stderr.strip()}")
    for number, line in enumerate(text.splitlines(), 1):
        code = PIP_COMMENT_RE.sub("", line)
        if ERREXIT_RE.match(code):
            problem(
                f"line {number}: '{line.strip()}': a failing command then ends the script before "
                "MWAA saves its environment, and the container exits with 'Failed to access "
                "customer environment variables file'"
            )
        if m := EXIT_NONZERO_RE.search(code):
            warn(
                f"line {number}: 'exit {m.group(1)}' is reported as success; the status is discarded"
            )
    exported, _ = startup_exports(text)
    for name in sorted(exported):
        if name in RESERVED or name.startswith("MWAA__"):
            warn(f"export {name} is ignored: MWAA restores its own value")


def parse_requirements(text: str) -> list[tuple[int, str, str]]:
    """Return (line number, option, value) for path-taking options, as pip reads them."""
    options = {
        "-c": "constraint", "--constraint": "constraint", "-r": "requirement",
        "--requirement": "requirement", "-f": "find-links", "--find-links": "find-links",
    }  # fmt: skip
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        line = PIP_COMMENT_RE.sub("", line).strip()
        if not line.startswith("-"):
            continue
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        for i, token in enumerate(tokens):
            flag, eq, value = token.partition("=")
            if flag in options and eq:
                found.append((number, options[flag], value))
            elif token in options and i + 1 < len(tokens):
                found.append((number, options[token], tokens[i + 1]))
            elif token[:2] in ("-c", "-r", "-f") and len(token) > 2:
                found.append((number, options[token[:2]], token[2:]))
    return found


def check_requirements(
    path: Path,
    dags_dir: Path | None,
    plugin_names: list[str] | None,
    startup: Path | None,
) -> None:
    print(f"\n{path} -> {REQ_FILE}")
    text = path.read_text(errors="replace")
    lines = text.splitlines()
    refs = parse_requirements(text)
    detected = [n for n, line in enumerate(lines, 1) if MWAA_CONSTRAINT_RE.search(line)]
    active = [r for r in refs if r[1] == "constraint"]
    if not detected and active:
        problem(
            f"line {active[0][0]}: MWAA's check (regex '-c |--constraint ') misses this form, so "
            "it also adds -c airflow_constraints.txt and pip must satisfy both files; "
            'write --constraint "<path>" with a space'
        )
    elif not detected:
        info(
            "no constraint: MWAA adds the image's Airflow constraints ('Forcing local constraints')"
        )
    elif not active:
        warn(
            f"line {detected[0]} matches MWAA's regex but sets no constraint (a comment?): MWAA "
            "then adds no Airflow constraints, only its essential pins"
        )
    for number, kind, value in refs:
        if SCHEME_RE.match(value):
            continue
        target = posixpath.normpath(posixpath.join(REQ_DIR, value))
        where = f"line {number}: {kind} '{value}'"
        if not value.startswith("/"):
            if target == PLUGINS or target.startswith((DAGS + "/", PLUGINS + "/")):
                warn(
                    f"{where} resolves to {target} only via {REQ_DIR}/; use the absolute path"
                )
            else:
                problem(
                    f"{where} resolves to {target}: pip joins it to {REQ_DIR}/, not your repo"
                )
                continue
        if target.startswith(DAGS + "/"):
            rel = target.removeprefix(DAGS + "/")
            if dags_dir is None:
                info(f"{where}: upload it to dags/{rel} before the environment update")
            elif not (dags_dir / rel).exists():
                problem(
                    f"{where}: {dags_dir / rel} does not exist; MWAA's install will fail"
                )
        elif target == PLUGINS or target.startswith(PLUGINS + "/"):
            rel = target.removeprefix(PLUGINS).lstrip("/")
            if plugin_names is not None and kind == "find-links":
                prefix = rel + "/" if rel else ""
                wheels = [
                    n
                    for n in plugin_names
                    if n.startswith(prefix) and n.endswith(".whl")
                ]
                if not any("/" not in n.removeprefix(prefix) for n in wheels):
                    problem(f"{where}: no wheels directly in {target}")
            elif plugin_names is not None and rel not in plugin_names:
                problem(f"{where}: {rel} is not in the plugins archive")
        elif target.startswith(REQ_DIR + "/"):
            problem(f"{where}: only requirements.txt is placed in {REQ_DIR}/")
        else:
            warn(f"{where}: MWAA does not populate {target} from your bucket")
    if any(line.strip() == "--no-index" for line in lines) and not any(
        r[1] == "find-links" for r in refs
    ):
        warn("--no-index without --find-links: pip has nowhere to install from")
    exported, assigned = (
        startup_exports(startup.read_text(errors="replace"))
        if startup
        else (set(), set())
    )
    for name in sorted({m for line in lines for m in PIP_ENV_VAR_RE.findall(line)}):
        if name.startswith("AIRFLOW__"):
            info(f"${{{name}}} must be set as an Airflow configuration option")
        elif name in exported:
            continue
        elif name in assigned:
            problem(
                f"${{{name}}} is assigned in startup.sh but not exported; pip sees it literally"
            )
        elif startup:
            problem(
                f"${{{name}}} is not exported by startup.sh; pip leaves it in the line literally"
            )
        else:
            warn(f"${{{name}}} must be exported by startup.sh")
    for number, line in enumerate(lines, 1):
        if (m := REQ_NAME_RE.match(line)) and normalize(m.group(1)) in ESSENTIAL:
            warn(
                f"line {number}: {m.group(1)} is pinned by MWAA's essential constraints; "
                "a different version makes pip fail"
            )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dags", type=Path, help="local folder uploaded as the DAG folder"
    )
    parser.add_argument(
        "--plugins", type=Path, help="plugins.zip, or the folder you zip"
    )
    parser.add_argument("--requirements", type=Path, help="requirements.txt")
    parser.add_argument("--startup", type=Path, help="startup.sh")
    args = parser.parse_args()
    if not any((args.dags, args.plugins, args.requirements, args.startup)):
        parser.error(
            "give at least one of --dags, --plugins, --requirements, --startup"
        )
    for path in (args.dags, args.plugins, args.requirements, args.startup):
        if path and not path.exists():
            parser.error(f"{path} does not exist")

    dags_names = None
    if args.dags:
        dags_names = sorted(
            p.relative_to(args.dags).as_posix()
            for p in args.dags.rglob("*")
            if p.is_file()
        )
        print(f"{args.dags}/ ({len(dags_names)} files) -> {DAGS}/  (synced every 30 s)")
    plugin_names = None
    if args.plugins:
        plugin_names, is_zip = list_plugins(args.plugins)
        label = args.plugins.name if is_zip else f"{args.plugins}/ (zip its contents)"
        check_plugins(plugin_names, is_zip, label)
    if args.dags or args.plugins:
        check_modules(dags_names, plugin_names)
    if args.startup:
        check_startup(args.startup)
    if args.requirements:
        check_requirements(args.requirements, args.dags, plugin_names, args.startup)
    if args.plugins or args.requirements or args.startup:
        print(
            "\nplugins, requirements and startup changes apply only after an environment update"
        )
    print(f"\n{problems} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
