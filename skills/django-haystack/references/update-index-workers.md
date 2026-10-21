# How `update_index --workers` Runs

What django-haystack's `update_index -k N` does with processes and database connections: how it splits the rows into batches and the batches into worker tasks, why it hangs on Python 3.14 and later, why an unused `DATABASES` alias crashes every worker, why `--remove` fails on PostgreSQL with default settings, and what each batch does to the connection.

Verified against django-haystack [3.4.0](https://github.com/django-haystack/django-haystack/tree/v3.4.0) source (`haystack/management/commands/update_index.py`), and reproduced with `scripts/check_update_index_workers.py` on Python 3.15.0, Django 5.2.17, Whoosh 2.7.4, SQLite, and PostgreSQL 16 (Docker image `postgres:16`) with psycopg2 2.9.13. The default start method was also run on Python 3.13.16, where indexing worked, and 3.14.8, where it hung. Every "Source:" link points at the commit the `v3.4.0` tag names, with line numbers.

The general mechanics live in other skills and are not repeated here: start methods, `map()` chunking, `close()` and `join()` in the `python` skill's `references/multiprocessing-pool.md`, and what a forked child does to inherited Django connections in the `django` skill's `references/connections-across-fork.md`.

______________________________________________________________________

## Table of Contents

1. [How the Work Is Split](#1-how-the-work-is-split)
2. [Python 3.14+: Workers Hang](#2-python-314-workers-hang)
3. [How Each Worker Resets Its Connections](#3-how-each-worker-resets-its-connections)
4. [An Unused `DATABASES` Alias Crashes Every Worker](#4-an-unused-databases-alias-crashes-every-worker)
5. [`--remove` Fails on PostgreSQL With `CONN_MAX_AGE = 0`](#5---remove-fails-on-postgresql-with-conn_max_age--0)
6. [Every Batch Opens a New Connection](#6-every-batch-opens-a-new-connection)
7. [Commits](#7-commits)
8. [Measured Results](#8-measured-results)
9. [Quick Reference](#9-quick-reference)

______________________________________________________________________

## 1. How the Work Is Split

For each model, the parent counts the rows and builds one task tuple per batch of `--batch-size` rows:

Source: [`haystack/management/commands/update_index.py#L331-L338`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/management/commands/update_index.py#L331-L338)

```python
batch_size = self.batchsize or backend.batch_size

if self.workers > 0:
    ghetto_queue = []

max_pk = None
for start in range(0, total, batch_size):
    end = min(start + batch_size, total)
```

Then it creates a pool of `-k` workers, hands the whole list to `pool.map()`, and waits:

Source: [`haystack/management/commands/update_index.py#L369-L384`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/management/commands/update_index.py#L369-L384)

```python
if self.workers > 0:
    pool = multiprocessing.Pool(self.workers)

    successful_tasks = pool.map(update_worker, ghetto_queue)

    if len(ghetto_queue) != len(successful_tasks):
        self.stderr.write(
            "Queued %d tasks but only %d completed"
            % (len(ghetto_queue), len(successful_tasks))
        )
        for i in ghetto_queue:
            if i not in successful_tasks:
                self.stderr.write("Incomplete task: %s" % repr(i))

    pool.close()
    pool.join()
```

`pool.map()` calls `update_worker(task)` once per tuple, so each call indexes one batch. It groups the tuples into chunks of `ceil(batches / (workers * 4))` and a free worker takes the next chunk. For example, 16,748 rows with `-b 200 -k 3` make 84 batches in 12 chunks of 7, and the three workers start at rows 1, 1,401 and 2,801. The parent waits inside `pool.map()` for the whole model; `close()` and `join()` then return at once. A new pool is created for every model.

______________________________________________________________________

## 2. Python 3.14+: Workers Hang

On Linux, Python 3.14 made `forkserver` the default start method. A `forkserver` worker starts without `manage.py`'s setup, so unpickling its first task, whose first element is the model class, fails with `django.core.exceptions.AppRegistryNotReady: Apps aren't loaded yet.`. The pool replaces the dead worker, the task is never answered, and `update_index` hangs after printing `Indexing N <models>`, repeating the traceback. Python 3.13 and earlier default to `fork` and are not affected.

Haystack's own test runner forces `fork` for this reason:

Source: [`test_haystack/run_tests.py#L15-L19`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/test_haystack/run_tests.py#L15-L19)

```python
# Python >= 3.14: default start method changed from `fork` --> `forkserver`
# (No state inheritance in child processes)
# https://docs.python.org/3/library/multiprocessing.html#contexts-and-start-methods
if sys.version_info >= (3, 14):
    multiprocessing.set_start_method("fork", force=True)
```

Do the same in `manage.py`, before Django runs the command:

```python
import multiprocessing
import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "proj.settings")
    multiprocessing.set_start_method("fork")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
```

Or index with `-k 0`. Why `forkserver` workers miss the setup and why a task that fails to unpickle hangs `map()` is explained in the `python` skill's `references/multiprocessing-pool.md`, sections 1 and 2.

______________________________________________________________________

## 3. How Each Worker Resets Its Connections

A `fork` worker inherits the parent's open database connections. Every `update_worker` call drops them before indexing its batch:

Source: [`haystack/management/commands/update_index.py#L42-L47`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/management/commands/update_index.py#L42-L47)

```python
# FIXME: confirm that this is still relevant with modern versions of Django:
# We need to reset the connections, otherwise the different processes
# will try to share the connection, which causes things to blow up.
from django.db import connections

for alias, info in connections.databases.items():
```

Source: [`haystack/management/commands/update_index.py#L50-L58`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/management/commands/update_index.py#L50-L58)

```python
if "sqlite3" not in info["ENGINE"]:
    try:
        close_old_connections()
        if isinstance(connections._connections, dict):
            del connections._connections[alias]
        else:
            delattr(connections._connections, alias)
    except KeyError:
        pass
```

For every non-SQLite alias it runs `close_old_connections()`, then deletes the alias's connection from Django's thread-local storage. SQLite aliases are skipped because, as the comment between those lines says ([`haystack/management/commands/update_index.py#L48-L49`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/management/commands/update_index.py#L48-L49)), wiping SQLite connections destroys in-memory databases. Sections 4 and 5 are the two ways this loop goes wrong.

______________________________________________________________________

## 4. An Unused `DATABASES` Alias Crashes Every Worker

Since Django 4.1, `close_old_connections()` touches only connections the process has opened, so an alias the parent never used has no entry in the thread-local storage, and `delattr()` raises `AttributeError`, which the `except KeyError` doesn't catch:

```text
AttributeError: '_thread._local' object has no attribute 'unused'
```

Every worker fails on its first batch, and `update_index` exits with status 1. Indexing with `-k 0` never runs this loop. This is [django-haystack issue #2086](https://github.com/django-haystack/django-haystack/issues/2086).

Touching every alias in the parent before calling `update_index` (`for alias in connections: connections[alias]`) is not a reliable workaround. The worker's first batch deletes the touched alias, so its second batch fails the same way. It only helps when every worker happens to get exactly one batch (section 8: 2 batches pass, 4 batches fail).

Workarounds until the fix is released: index with `-k 0`, or run `update_index` with a settings module whose `DATABASES` lists only the aliases the indexed models use.

______________________________________________________________________

## 5. `--remove` Fails on PostgreSQL With `CONN_MAX_AGE = 0`

The `close_old_connections()` in the loop runs in the worker, on the connection inherited from the parent. With the default `CONN_MAX_AGE = 0` that connection counts as expired, so the worker closes it, which ends the parent's PostgreSQL session too (the mechanism is in the `django` skill's `references/connections-across-fork.md`, sections 3 and 4).

The parent doesn't notice until it queries again. With `--remove` it does, right after the workers finish, to collect the primary keys still in the database:

Source: [`haystack/management/commands/update_index.py#L386-L395`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/management/commands/update_index.py#L386-L395)

```python
if self.remove:
    if self.start_date or self.end_date or total <= 0:
        # They're using a reduced set, which may not incorporate
        # all pks. Rebuild the list with everything.
        qs = index.index_queryset(using=using).values_list("pk", flat=True)
        database_pks = {smart_bytes(pk) for pk in qs}
    else:
        database_pks = {
            smart_bytes(pk) for pk in qs.values_list("pk", flat=True)
        }
```

and fails:

```text
django.db.utils.OperationalError: server closed the connection unexpectedly
```

Without `--remove`, a single model indexes fine. With several models, the parent's own `close_old_connections()` before each model throws the dead connection away before its next query:

Source: [`haystack/management/commands/update_index.py#L313-L317`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/management/commands/update_index.py#L313-L317)

```python
if self.workers > 0:
    # workers resetting connections leads to references to models / connections getting
    # stale and having their connection disconnected from under them. Resetting before
    # the loop continues and it accesses the ORM makes it better.
    close_old_connections()
```

Set `CONN_MAX_AGE = None` for the indexing run (section 8: `--remove` then succeeds), or run `--remove` with `-k 0`.

______________________________________________________________________

## 6. Every Batch Opens a New Connection

A worker runs `update_worker` once per batch, and each call starts with the reset loop (section 3). On the first call it drops the connection inherited from the parent; on every later call it finds the connection the previous batch opened, closes it (with `CONN_MAX_AGE = 0`) and deletes it. The batch's first query then opens a new one: `do_update()` evaluates the slice with `list(current_qs)` ([`haystack/management/commands/update_index.py#L99`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/management/commands/update_index.py#L99)).

So each worker holds one connection at a time, and the run opens about one connection per batch: 84 for the 84 batches above. Each worker's last connection stays open until the worker process exits. To watch this, use the `connection_created` receiver in the `django` skill's `references/connections-across-fork.md`, section 7.

______________________________________________________________________

## 7. Commits

Every batch calls `backend.update(index, current_qs, commit=commit)`, so on Solr each batch is an update request with `commit=true`. `--nocommit` passes `commit=False` to every batch, and `update_index` sends no commit at the end either; the documents become visible only when Solr's `autoCommit` or `autoSoftCommit` fires or something else commits. How those commits behave is in the `solr` skill.

______________________________________________________________________

## 8. Measured Results

`scripts/check_update_index_workers.py` builds a throwaway project, indexes 40 rows with `update_index -k 2` for each case, and kills a case after 30 seconds:

```text
Python 3.15.0, default start method forkserver; 40 rows, update_index -k 2
default start method, -b 10                          hung, killed after 30 s; workers raised ["django.core.exceptions.AppRegistryNotReady: Apps aren't loaded yet."]
start method forced to fork, -b 10                   OK, Indexing 40 users
unused DATABASES alias, fork, -b 10                  failed (exit 1): AttributeError: '_thread._local' object has no attribute 'unused'
unused alias touched first, -b 20 (2 batches)        OK, Indexing 40 users
unused alias touched first, -b 10 (4 batches)        failed (exit 1): AttributeError: '_thread._local' object has no attribute 'unused'
PostgreSQL, CONN_MAX_AGE=0, -b 10                    OK, Indexing 40 users
PostgreSQL, CONN_MAX_AGE=0, -b 10 --remove           failed (exit 1): django.db.utils.OperationalError: server closed the connection unexpectedly
PostgreSQL, CONN_MAX_AGE=None, -b 10                 OK, Indexing 40 users
PostgreSQL, CONN_MAX_AGE=None, -b 10 --remove        OK, Indexing 40 users
```

______________________________________________________________________

## 9. Quick Reference

| Symptom                                                                                                | Cause                                                                         | Fix                                                                  |
| ------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| `update_index -k N` hangs after `Indexing ...`; `AppRegistryNotReady: Apps aren't loaded yet.` repeats | Python 3.14+ `forkserver` default (section 2)                                 | `multiprocessing.set_start_method("fork")` in `manage.py`, or `-k 0` |
| Every worker fails with `AttributeError: '_thread._local' object has no attribute '<alias>'`           | an alias in `DATABASES` the parent never used (section 4)                     | `-k 0`, or a settings module without the unused alias                |
| The same `AttributeError` even after touching every alias first                                        | a worker ran a second batch (section 4)                                       | the same                                                             |
| `--remove` with `-k` fails with `server closed the connection unexpectedly`                            | the worker's `close_old_connections()` ended the parent's session (section 5) | `CONN_MAX_AGE = None` for the run, or `--remove` with `-k 0`         |
| Documents indexed with `--nocommit` never show up                                                      | no commit is ever sent (section 7)                                            | rely on Solr's `autoSoftCommit`, or commit once after the run        |
