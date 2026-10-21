# /// script
# requires-python = ">=3.14"
# dependencies = ["django==6.1.2", "psycopg2-binary==2.9.13"]
# ///
"""Fork with an open Django PostgreSQL connection and see what the child's cleanup
does to the parent's session.

Each case opens a connection in the parent, forks, lets the child clean up and run
its own query, then runs a query in the parent again. Add
`--with "psycopg[binary]==3.3.6"` to `uv run` to test psycopg 3; Django prefers it
when both drivers are installed. Point --postgres at a disposable database.
"""

import argparse
import os
import sys
from urllib.parse import urlsplit

import django
from django.conf import settings

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument(
    "--postgres",
    required=True,
    help="e.g. postgresql://postgres:test@127.0.0.1:55432/postgres",
)
url = urlsplit(parser.parse_args().postgres)
settings.configure(
    DATABASES={
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "HOST": url.hostname,
            "PORT": url.port or 5432,
            "NAME": url.path[1:],
            "USER": url.username,
            "PASSWORD": url.password,
        }
    }
)
django.setup()

from django.db import Error, close_old_connections, connections
from django.db.backends.postgresql.psycopg_any import is_psycopg3
from django.db.backends.signals import connection_created


def server_pid(connection):
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


def log_new_connection(sender, connection, **kwargs):
    print(
        f"    process {os.getpid()} opened a connection, "
        f"server pid {server_pid(connection)}",
        flush=True,
    )


def open_sockets():
    count = 0
    for fd in os.listdir("/proc/self/fd"):
        try:
            count += os.readlink(f"/proc/self/fd/{fd}").startswith("socket:")
        except FileNotFoundError:
            pass
    return count


def child_delete_only():
    del connections["default"]


def child_close_then_delete():
    close_old_connections()
    del connections["default"]


def child_delete_initialized():
    for conn in connections.all(initialized_only=True):
        del connections[conn.alias]


CLOSE_THEN_DELETE = "child runs close_old_connections(), then deletes"
CASES = [
    ("child deletes the connection", 0, None, child_delete_only),
    (CLOSE_THEN_DELETE, 0, None, child_close_then_delete),
    (CLOSE_THEN_DELETE, None, None, child_close_then_delete),
    ("child deletes every initialized connection", 0, None, child_delete_initialized),
    ("parent runs close_all() before fork", 0, "close_all", None),
    ("child keeps using the inherited connection", 0, None, lambda: None),
]


def tagged_queries(tag, count=300):
    """Run queries that echo (tag, i); count answers that belong to someone else."""
    wrong, first = 0, None
    for i in range(count):
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT %s, %s, pg_sleep(0.001)", [tag, i])
            answer = cursor.fetchone()[:2]
        if answer != (tag, i):
            wrong += 1
            first = first or f"asked {(tag, i)}, got {answer}"
    return f"{wrong} of {count} answers were not its own; first: {first}"


def run_concurrent_case():
    print("\nparent and child query on the inherited connection at the same time")
    connections["default"].close()
    server_pid(connections["default"])
    if os.fork() == 0:
        print(f"    child:  {tagged_queries('child')}", flush=True)
        os._exit(0)
    print(f"    parent: {tagged_queries('parent')}", flush=True)
    os.wait()


def run_case(label, max_age, parent_step, child_step):
    connections["default"].close()
    del connections["default"]
    connections.settings["default"]["CONN_MAX_AGE"] = max_age
    print(f"\n{label}, CONN_MAX_AGE={max_age}")
    parent_session = server_pid(connections["default"])
    if parent_step == "close_all":
        connections.close_all()
    if os.fork() == 0:
        if child_step is not None:
            child_step()
        sockets = open_sockets()
        try:
            own = server_pid(connections["default"])
            print(f"    child: {sockets} socket(s) left after cleanup; queries on", own)
        except Error as exc:
            failure = f"{type(exc).__name__}: {exc}".splitlines()[0]
            print(f"    child query failed: {failure}")
        sys.stdout.flush()
        os._exit(0)
    os.wait()
    try:
        now = server_pid(connections["default"])
        same = "same session" if now == parent_session else "a new session"
        print(f"    parent: next query works, on {same} ({now})")
    except Error as exc:
        first_line = str(exc).strip().splitlines()[0]
        print(f"    parent: next query FAILED: {type(exc).__name__}: {first_line}")


def main():
    driver = "psycopg 3" if is_psycopg3 else "psycopg2"
    print(f"Django {django.__version__}, {driver}, Python {sys.version.split()[0]}")
    connection_created.connect(log_new_connection)
    for case in CASES:
        run_case(*case)
    run_concurrent_case()


if __name__ == "__main__":
    main()
