# Database Connections Across `fork()`

What a forked child process inherits from Django's PostgreSQL connections, what happens when the child keeps using, closes or drops them, how `CONN_MAX_AGE` decides whether `close_old_connections()` closes one, and how to see which connection each process uses. It applies to `os.fork()` and to `multiprocessing` with the `fork` start method; for start methods and pool behaviour, see the `python` skill.

Verified against Django [6.1.2](https://github.com/django/django/tree/6.1.2) source (`db/utils.py`, `utils/connection.py`, `db/backends/base/base.py`, `db/backends/sqlite3/base.py`), psycopg2 [2.9.13](https://github.com/psycopg/psycopg2/tree/2.9.13) and psycopg [3.3.6](https://github.com/psycopg/psycopg/tree/3.3.6) source, and by running `scripts/check_fork_connections.py` against PostgreSQL 16 (Docker image `postgres:16`) on Python 3.15.0 with Django 6.1.2. The same delete, close and `CONN_MAX_AGE` cases gave identical results on Django 5.2.17 with both drivers. Every "Source:" link points at the commit the tag names, with line numbers.

______________________________________________________________________

## Table of Contents

1. [What the Child Inherits](#1-what-the-child-inherits)
2. [Using It From Both Processes Mixes Up Results](#2-using-it-from-both-processes-mixes-up-results)
3. [Drop It, Don't Close It](#3-drop-it-dont-close-it)
4. [When `close_old_connections()` Closes](#4-when-close_old_connections-closes)
5. [Two Safe Patterns](#5-two-safe-patterns)
6. [SQLite](#6-sqlite)
7. [Seeing Which Connection a Process Uses](#7-seeing-which-connection-a-process-uses)
8. [Measured Results](#8-measured-results)
9. [Quick Reference](#9-quick-reference)

______________________________________________________________________

## 1. What the Child Inherits

`fork()` makes the child a copy of the parent. Python objects are copied into the child's memory, but open file descriptors are duplicated: the child's copy of a socket is the same TCP connection to the same PostgreSQL session.

Django keeps one connection object per alias per thread, in a real thread-local:

Source: [`django/db/utils.py#L141-L148`](https://github.com/django/django/blob/2212534bae393714e1e51db2341d4fd5545e19f6/django/db/utils.py#L141-L148)

```python
class ConnectionHandler(BaseConnectionHandler):
    settings_name = "DATABASES"
    # Connections needs to still be an actual thread local, as it's truly
    # thread-critical. Database backends should use @async_unsafe to protect
    # their code from async contexts, but this will give those contexts
    # separate connections in case it's needed as well. There's no cleanup
    # after async contexts, though, so we don't allow that if we can help it.
    thread_critical = True
```

Source: [`django/utils/connection.py#L39-L41`](https://github.com/django/django/blob/2212534bae393714e1e51db2341d4fd5545e19f6/django/utils/connection.py#L39-L41)

```python
def __init__(self, settings=None):
    self._settings = settings
    self._connections = Local(self.thread_critical)
```

The child's only thread is a copy of the thread that called `fork()`, with its thread-local storage. So in the child, `connections["default"]` is the parent's connection object, and the first query goes over the parent's socket into the parent's session: in section 8, the child that keeps the inherited connection queries on the same server pid the parent opened. PostgreSQL sees one client and has no way to tell the two processes apart.

______________________________________________________________________

## 2. Using It From Both Processes Mixes Up Results

When both processes send queries over the shared socket at the same time, each one reads whatever reply arrives next, including replies to the other process's queries. Nothing raises. `scripts/check_fork_connections.py` runs 300 queries in each process that select their own `(tag, i)`; in every run most answers in both processes belonged to the other one, for example `asked ('child', 0), got ('parent', 0)` with psycopg2 (the last case in section 8).

Taking turns happens to work, because each reply is read before the next query is sent, but the processes still share one session: a transaction, `SET`, temporary table or lock in one is visible to the other.

______________________________________________________________________

## 3. Drop It, Don't Close It

The child has two ways to stop using an inherited connection, and only one leaves the parent alone.

**Deleting** the connection object (`del connections[alias]`) makes Django forget it, so the child's next query opens a new connection:

Source: [`django/utils/connection.py#L69-L70`](https://github.com/django/django/blob/2212534bae393714e1e51db2341d4fd5545e19f6/django/utils/connection.py#L69-L70)

```python
def __delitem__(self, key):
    delattr(self._connections, key)
```

The driver object is then garbage-collected. Both drivers skip closing a connection that another process created. psycopg2:

Source: [`psycopg/connection_type.c#L1390-L1398`](https://github.com/psycopg/psycopg2/blob/f650e7afed0c94f596594e8328a0c7fa65a9d0e5/psycopg/connection_type.c#L1390-L1398)

```c
    /* close the connection only if this is the same process it was created
     * into, otherwise using multiprocessing we may close the connection
     * belonging to another process. */
#ifdef CONN_CHECK_PID
    if (self->procpid == getpid())
#endif
    {
        conn_close(self);
    }
```

psycopg 3, in the C implementation used by `psycopg[binary]` and the pure-Python one:

Source: [`psycopg_c/psycopg_c/pq/pgconn.pyx#L62-L66`](https://github.com/psycopg/psycopg/blob/a67654d1e7afbf9b3a619557838f62de1c790e7c/psycopg_c/psycopg_c/pq/pgconn.pyx#L62-L66)

```python
def __dealloc__(self):
    # Close the connection only if it was created in this process,
    # not if this object is being GC'd after fork.
    if self._procpid == getpid():
        self.finish()
```

Source: [`psycopg/psycopg/pq/pq_ctypes.py#L99-L103`](https://github.com/psycopg/psycopg/blob/a67654d1e7afbf9b3a619557838f62de1c790e7c/psycopg/psycopg/pq/pq_ctypes.py#L99-L103)

```python
def __del__(self, __getpid: Callable[[], int] = getpid) -> None:
    # Close the connection only if it was created in this process,
    # not if this object is being GC'd after fork.
    if __getpid() == self._procpid:  # see note_on_del
        self.finish()
```

The child keeps its copy of the socket open until it exits (the script counts 1 socket left after the delete), and nothing is sent on it, so the parent's session carries on.

**Closing** the connection (`connection.close()`, or `close_old_connections()` when it decides to close) calls libpq's `PQfinish()`, which sends PostgreSQL a Terminate message over the shared socket. [The server ends the session](https://www.postgresql.org/docs/16/protocol-flow.html#PROTOCOL-FLOW-TERMINATION) for both processes, and the parent's next query fails:

| Driver    | Parent's next query after the child closed the connection                                             |
| --------- | ----------------------------------------------------------------------------------------------------- |
| psycopg2  | `django.db.utils.OperationalError: server closed the connection unexpectedly`                         |
| psycopg 3 | `django.db.utils.OperationalError: consuming input failed: server closed the connection unexpectedly` |

______________________________________________________________________

## 4. When `close_old_connections()` Closes

`close_old_connections()` calls `close_if_unusable_or_obsolete()` on every initialized connection; the function and its autocommit check are quoted in `transactions.md`, section 11. The check that decides the fork case is the age, set when the connection opens:

Source: [`django/db/backends/base/base.py#L248-L249`](https://github.com/django/django/blob/2212534bae393714e1e51db2341d4fd5545e19f6/django/db/backends/base/base.py#L248-L249)

```python
max_age = self.settings_dict["CONN_MAX_AGE"]
self.close_at = None if max_age is None else time.monotonic() + max_age
```

Source: [`django/db/backends/base/base.py#L614-L616`](https://github.com/django/django/blob/2212534bae393714e1e51db2341d4fd5545e19f6/django/db/backends/base/base.py#L614-L616)

```python
if self.close_at is not None and time.monotonic() >= self.close_at:
    self.close()
    return
```

| `CONN_MAX_AGE` | `close_at`                       | `close_old_connections()` in the child                      |
| -------------- | -------------------------------- | ----------------------------------------------------------- |
| `0` (default)  | the moment the connection opened | always closes, so the parent's session ends                 |
| `None`         | `None`                           | never closes for age, so only a delete would follow         |
| `60`           | 60 seconds after it opened       | closes only connections the parent opened over 60 s earlier |

The `60` row follows from the code and was not run. The function also closes a connection whose autocommit was left changed, or that had an error and no longer works, whatever `CONN_MAX_AGE` says.

So code that "resets connections" in a forked child with `close_old_connections()` ends the parent's session on default settings. Request-cycle code is the function's intended caller; in a child process, delete instead.

______________________________________________________________________

## 5. Two Safe Patterns

Drop every inherited connection in the child, before its first query:

```python
from django.db import connections

for conn in connections.all(initialized_only=True):
    del connections[conn.alias]
```

`all(initialized_only=True)` returns a list of only the connections this process has opened, so it is safe to delete while looping, and an alias that was never used is skipped (deleting one raises `AttributeError`):

Source: [`django/utils/connection.py#L75-L81`](https://github.com/django/django/blob/2212534bae393714e1e51db2341d4fd5545e19f6/django/utils/connection.py#L75-L81)

```python
def all(self, initialized_only=False):
    return [
        self[alias]
        for alias in self
        # If initialized_only is True, return only initialized connections.
        if not initialized_only or hasattr(self._connections, alias)
    ]
```

Or close the connections in the parent before forking, so the child inherits no open socket:

```python
from django.db import connections

connections.close_all()
```

The parent reconnects on its next query, in a new session (section 8).

______________________________________________________________________

## 6. SQLite

SQLite has no server session to share or end, so closing in the child does no harm to the parent. But Django refuses to close an in-memory database, because closing would destroy it:

Source: [`django/db/backends/sqlite3/base.py#L221-L227`](https://github.com/django/django/blob/2212534bae393714e1e51db2341d4fd5545e19f6/django/db/backends/sqlite3/base.py#L221-L227)

```python
def close(self):
    self.validate_thread_sharing()
    # If database is in memory, closing the connection destroys the
    # database. To prevent accidental data loss, ignore close requests on
    # an in-memory db.
    if not self.is_in_memory_db():
        BaseDatabaseWrapper.close(self)
```

Dropping an in-memory connection loses the database for that process: the next connection gets a new, empty one. Keep in-memory SQLite connections in a forked child; it reads its own copy of the data. For file-based databases, SQLite's documentation warns against [carrying an open connection across a `fork()`](https://www.sqlite.org/howtocorrupt.html#_carrying_an_open_database_connection_across_a_fork_), so drop those like PostgreSQL ones.

______________________________________________________________________

## 7. Seeing Which Connection a Process Uses

Django sends `connection_created` after every new connection:

Source: [`django/db/backends/base/base.py#L256-L259`](https://github.com/django/django/blob/2212534bae393714e1e51db2341d4fd5545e19f6/django/db/backends/base/base.py#L256-L259)

```python
self.connection = self.get_new_connection(conn_params)
self.set_autocommit(self.settings_dict["AUTOCOMMIT"])
self.init_connection_state()
connection_created.send(sender=self.__class__, connection=self)
```

A receiver that prints the process and the PostgreSQL session shows every reconnect, in the parent and in each child:

```python
import os

from django.db.backends.signals import connection_created
from django.dispatch import receiver


@receiver(connection_created)
def log_db_connection(sender, connection, **kwargs):
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        server_pid = cursor.fetchone()[0]
    print(f"process {os.getpid()} opened {connection.alias!r}, server pid {server_pid}")
```

A child that prints nothing before its first query is using an inherited connection.

From outside, list the sessions and the processes holding their sockets:

```sql
SELECT pid, client_port, backend_start, state, left(query, 60)
FROM pg_stat_activity
WHERE datname = current_database() AND pid <> pg_backend_pid();
```

```bash
ss -tnp '( dport = :5432 )'
```

`ss` prints each TCP connection with its local port and the processes holding it, as `users:(("python",pid=...,fd=...))`; match the local port to `client_port`. A connection listed under two pids is shared. After a child deletes an inherited connection it still holds the socket until it exits, so the parent's connection shows both pids until then, with nothing sent from the child. With `HOST` empty, Django connects over a Unix socket, `client_port` is empty, and the signal is the way to see it.

______________________________________________________________________

## 8. Measured Results

`scripts/check_fork_connections.py` with psycopg2 2.9.13. Each case opens a connection in the parent, forks, lets the child clean up and query, then queries in the parent again; the `opened a connection` lines come from a `connection_created` receiver:

```text
Django 6.1.2, psycopg2, Python 3.15.0

child deletes the connection, CONN_MAX_AGE=0
    process 1197484 opened a connection, server pid 272
    process 1197486 opened a connection, server pid 273
    child: 1 socket(s) left after cleanup; queries on 273
    parent: next query works, on same session (272)

child runs close_old_connections(), then deletes, CONN_MAX_AGE=0
    process 1197484 opened a connection, server pid 274
    process 1197489 opened a connection, server pid 275
    child: 0 socket(s) left after cleanup; queries on 275
    parent: next query FAILED: OperationalError: server closed the connection unexpectedly

child runs close_old_connections(), then deletes, CONN_MAX_AGE=None
    process 1197484 opened a connection, server pid 276
    process 1197492 opened a connection, server pid 277
    child: 1 socket(s) left after cleanup; queries on 277
    parent: next query works, on same session (276)

child deletes every initialized connection, CONN_MAX_AGE=0
    process 1197484 opened a connection, server pid 278
    process 1197495 opened a connection, server pid 279
    child: 1 socket(s) left after cleanup; queries on 279
    parent: next query works, on same session (278)

parent runs close_all() before fork, CONN_MAX_AGE=0
    process 1197484 opened a connection, server pid 280
    process 1197498 opened a connection, server pid 281
    child: 0 socket(s) left after cleanup; queries on 281
    process 1197484 opened a connection, server pid 282
    parent: next query works, on a new session (282)

child keeps using the inherited connection, CONN_MAX_AGE=0
    process 1197484 opened a connection, server pid 283
    child: 1 socket(s) left after cleanup; queries on 283
    parent: next query works, on same session (283)

parent and child query on the inherited connection at the same time
    process 1197484 opened a connection, server pid 284
    child:  253 of 300 answers were not its own; first: asked ('child', 0), got ('parent', 0)
    parent: 197 of 300 answers were not its own; first: asked ('parent', 0), got ('child', 1)
```

With psycopg 3.3.6 (`uv run --with "psycopg[binary]==3.3.6" scripts/check_fork_connections.py ...`) every case ended the same way; only the error text differs:

```text
Django 6.1.2, psycopg 3, Python 3.15.0

child deletes the connection, CONN_MAX_AGE=0
    process 1197517 opened a connection, server pid 285
    process 1197519 opened a connection, server pid 286
    child: 1 socket(s) left after cleanup; queries on 286
    parent: next query works, on same session (285)

child runs close_old_connections(), then deletes, CONN_MAX_AGE=0
    process 1197517 opened a connection, server pid 287
    process 1197522 opened a connection, server pid 288
    child: 0 socket(s) left after cleanup; queries on 288
    parent: next query FAILED: OperationalError: consuming input failed: server closed the connection unexpectedly

child runs close_old_connections(), then deletes, CONN_MAX_AGE=None
    process 1197517 opened a connection, server pid 289
    process 1197525 opened a connection, server pid 290
    child: 1 socket(s) left after cleanup; queries on 290
    parent: next query works, on same session (289)

child deletes every initialized connection, CONN_MAX_AGE=0
    process 1197517 opened a connection, server pid 291
    process 1197528 opened a connection, server pid 292
    child: 1 socket(s) left after cleanup; queries on 292
    parent: next query works, on same session (291)

parent runs close_all() before fork, CONN_MAX_AGE=0
    process 1197517 opened a connection, server pid 293
    process 1197531 opened a connection, server pid 294
    child: 0 socket(s) left after cleanup; queries on 294
    process 1197517 opened a connection, server pid 295
    parent: next query works, on a new session (295)

child keeps using the inherited connection, CONN_MAX_AGE=0
    process 1197517 opened a connection, server pid 296
    child: 1 socket(s) left after cleanup; queries on 296
    parent: next query works, on same session (296)

parent and child query on the inherited connection at the same time
    process 1197517 opened a connection, server pid 297
    parent: 267 of 300 answers were not its own; first: asked ('parent', 1), got ('child', 0)
    child:  165 of 300 answers were not its own; first: asked ('child', 0), got ('parent', 2)
```

______________________________________________________________________

## 9. Quick Reference

| Symptom                                                                         | Cause                                                                                                                  | Fix                                                                    |
| ------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| Wrong or swapped query results in a forked worker or its parent, no error       | both processes use the inherited connection (section 2)                                                                | drop it in the child (section 5)                                       |
| Parent fails with `server closed the connection unexpectedly` after a child ran | the child closed the inherited connection, often via `close_old_connections()` with `CONN_MAX_AGE = 0` (sections 3, 4) | delete instead of closing; or `connections.close_all()` before forking |
| `AttributeError` on `del connections[alias]`                                    | the alias was never used in this process                                                                               | loop over `connections.all(initialized_only=True)`                     |
| In-memory SQLite data missing in a child                                        | the child dropped the in-memory connection (section 6)                                                                 | keep SQLite in-memory connections                                      |
