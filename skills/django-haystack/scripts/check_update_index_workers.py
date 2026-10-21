# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "django==5.2.17",
#     "django-haystack==3.4.0",
#     "psycopg2-binary==2.9.13",
#     "whoosh==2.7.4",
# ]
# ///
"""Reproduce how `update_index --workers` behaves in django-haystack 3.4.0.

Builds a throwaway Django project, indexes 40 auth.User rows into Whoosh with
`update_index -k 2`, and reports each case: the default multiprocessing start
method, forcing fork, and an unused DATABASES alias with and without touching it
first. With --postgres it also indexes from PostgreSQL with CONN_MAX_AGE 0 and None,
with and without --remove. For forked connections in general, see the django skill's
scripts/check_fork_connections.py.
"""

import argparse
import multiprocessing
import os
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

SETTINGS = """
import os
from urllib.parse import urlsplit

SECRET_KEY = "x"
INSTALLED_APPS = [
    "django.contrib.contenttypes", "django.contrib.auth", "haystack", "app",
]
DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": os.environ["DB"]}
}
if os.environ.get("PG"):
    url = urlsplit(os.environ["PG"])
    age = os.environ.get("CONN_MAX_AGE", "0")
    DATABASES["default"] = {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": url.hostname, "PORT": url.port or 5432, "NAME": url.path[1:],
        "USER": url.username, "PASSWORD": url.password,
        "CONN_MAX_AGE": None if age == "None" else int(age),
    }
if os.environ.get("UNUSED_ALIAS"):
    DATABASES["unused"] = {"ENGINE": "django.db.backends.dummy"}
HAYSTACK_CONNECTIONS = {"default": {
    "ENGINE": "haystack.backends.whoosh_backend.WhooshEngine",
    "PATH": os.environ["INDEX"],
}}
"""

SEARCH_INDEXES = """
from django.contrib.auth.models import User
from haystack import indexes


class UserIndex(indexes.SearchIndex, indexes.Indexable):
    text = indexes.CharField(document=True, model_attr="username")

    def get_model(self):
        return User
"""

MANAGE = """
import multiprocessing
import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
    if os.environ.get("FORCE_FORK"):
        multiprocessing.set_start_method("fork")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
"""

TOUCH_THEN_INDEX = """
import multiprocessing
import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
    multiprocessing.set_start_method("fork")
    import django

    django.setup()
    from django.core.management import call_command
    from django.db import connections

    for alias in connections:
        connections[alias]
    call_command("update_index", "auth", workers=2, batchsize=int(sys.argv[1]))
"""


def write_project(root):
    (root / "app").mkdir()
    (root / "app" / "__init__.py").write_text("")
    files = {
        "settings.py": SETTINGS,
        "app/search_indexes.py": SEARCH_INDEXES,
        "manage.py": MANAGE,
        "touch_then_index.py": TOUCH_THEN_INDEX,
    }
    for name, body in files.items():
        (root / name).write_text(textwrap.dedent(body).lstrip())


ERROR_LINE = re.compile(r"^[\w.]+(?:Error|Exception|NotReady): .*")


def errors_in(output):
    return [line for line in output.splitlines() if ERROR_LINE.match(line)]


def run(root, env, *args, timeout=30):
    """Run a project command in its own process group; return a one-line outcome."""
    command = [sys.executable, "-W", "ignore::SyntaxWarning", *args]
    process = subprocess.Popen(
        command,
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        raised = sorted(set(errors_in(stderr)))
        return f"hung, killed after {timeout} s; workers raised {raised}"
    if process.returncode == 0:
        return "OK, " + (stdout.strip().splitlines() or ["no output"])[-1].strip()
    errors = errors_in(stdout + stderr)
    last = errors[-1] if errors else (stdout + stderr)[-200:]
    return f"failed (exit {process.returncode}): {last}"


def seed(root, env):
    python = [sys.executable, "-W", "ignore::SyntaxWarning", "manage.py"]
    subprocess.run([*python, "migrate", "-v", "0"], cwd=root, env=env, check=True)
    create = (
        "from django.contrib.auth.models import User; User.objects.all().delete(); "
        "User.objects.bulk_create(User(username=f'u{i:03}') for i in range(40))"
    )
    subprocess.run(
        [*python, "shell", "-c", create],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--postgres",
        help="URL of a disposable database, "
        "e.g. postgresql://postgres:test@127.0.0.1:55432/postgres",
    )
    args = parser.parse_args()

    print(
        f"Python {sys.version.split()[0]}, default start method "
        f"{multiprocessing.get_start_method()}; 40 rows, update_index -k 2"
    )
    with tempfile.TemporaryDirectory(prefix="haystack-workers-") as tmp:
        root = Path(tmp)
        write_project(root)
        env = {**os.environ, "DB": str(root / "db.sqlite3"), "INDEX": str(root / "ix")}
        seed(root, env)
        index = ["manage.py", "update_index", "auth", "-k", "2"]
        fork = {**env, "FORCE_FORK": "1"}
        unused = {**fork, "UNUSED_ALIAS": "1"}

        cases = [
            ("default start method, -b 10", env, [*index, "-b", "10"]),
            ("start method forced to fork, -b 10", fork, [*index, "-b", "10"]),
            ("unused DATABASES alias, fork, -b 10", unused, [*index, "-b", "10"]),
            (
                "unused alias touched first, -b 20 (2 batches)",
                unused,
                ["touch_then_index.py", "20"],
            ),
            (
                "unused alias touched first, -b 10 (4 batches)",
                unused,
                ["touch_then_index.py", "10"],
            ),
        ]
        if args.postgres:
            pg = {**fork, "PG": args.postgres, "INDEX": str(root / "ix-pg")}
            seed(root, pg)
            for age in ("0", "None"):
                aged = {**pg, "CONN_MAX_AGE": age}
                label = f"PostgreSQL, CONN_MAX_AGE={age}, -b 10"
                cases += [
                    (label, aged, [*index, "-b", "10"]),
                    (f"{label} --remove", aged, [*index, "-b", "10", "--remove"]),
                ]
        for label, case_env, command in cases:
            print(f"{label:52} {run(root, case_env, *command)}")


if __name__ == "__main__":
    main()
