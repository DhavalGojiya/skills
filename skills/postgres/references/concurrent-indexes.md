# `CREATE INDEX CONCURRENTLY`: Locks, Waits and Invalid Indexes

What a concurrent index build locks, what it waits for, what it leaves behind when it fails, and how to watch, unblock and repair it.

Verified by experiment on PostgreSQL 16.15 and 18.6, with identical results; lock modes from `pg_locks`, phases from `pg_stat_progress_create_index`, and index flags from `pg_index`, all read while the builds ran. Background from the [PostgreSQL `CREATE INDEX` docs](https://www.postgresql.org/docs/18/sql-createindex.html#SQL-CREATEINDEX-CONCURRENTLY).

______________________________________________________________________

## Table of Contents

1. [Plain vs Concurrent Build](#1-plain-vs-concurrent-build)
2. [The Phases and What Each Waits For](#2-the-phases-and-what-each-waits-for)
3. [What Blocks a Concurrent Build](#3-what-blocks-a-concurrent-build)
4. [Running It Correctly](#4-running-it-correctly)
5. [Invalid Indexes](#5-invalid-indexes)
6. [Monitoring and Unblocking](#6-monitoring-and-unblocking)
7. [Quick Reference](#7-quick-reference)

______________________________________________________________________

## 1. Plain vs Concurrent Build

Measured with each build in progress and a second session trying to use the table:

|                                     | `CREATE INDEX`                                      | `CREATE INDEX CONCURRENTLY`                                              |
| ----------------------------------- | --------------------------------------------------- | ------------------------------------------------------------------------ |
| Lock on the table (`pg_locks.mode`) | `ShareLock`                                         | `ShareUpdateExclusiveLock`                                               |
| Another session's `SELECT`          | runs                                                | runs                                                                     |
| Another session's `INSERT`          | **waits** until the build ends (hit `lock_timeout`) | runs                                                                     |
| Inside a transaction block          | allowed                                             | `ERROR: CREATE INDEX CONCURRENTLY cannot run inside a transaction block` |
| If it fails                         | nothing left behind                                 | an **invalid** index left behind (section 5)                             |
| Cost                                | one table scan                                      | two table scans plus waits for other transactions                        |

The [lock-mode table](https://www.postgresql.org/docs/18/explicit-locking.html#LOCKING-TABLES) explains the difference: `ShareLock` conflicts with the `RowExclusiveLock` every `INSERT`, `UPDATE` and `DELETE` takes, while `ShareUpdateExclusiveLock` doesn't. On a large, busy table a plain build means every write waits for the whole build, which can be hours.

`DROP INDEX CONCURRENTLY` has the same restriction on transaction blocks.

______________________________________________________________________

## 2. The Phases and What Each Waits For

From the docs: the index is first entered into the catalog as invalid, then the table is scanned twice, in separate transactions. Before each scan the build waits for existing transactions that modified the table, and after the second scan it waits for transactions holding older snapshots.

`pg_stat_progress_create_index.phase` ([docs](https://www.postgresql.org/docs/18/progress-reporting.html#CREATE-INDEX-PROGRESS-REPORTING)) names the step. The two waits measured here:

| Blocker                                                 | Phase shown while waiting          | `current_locker_pid` |
| ------------------------------------------------------- | ---------------------------------- | -------------------- |
| A session idle in a transaction that wrote to the table | `waiting for writers before build` | that session's pid   |
| A session idle in a `REPEATABLE READ` transaction       | `waiting for old snapshots`        | that session's pid   |

The build waits until the blocking transaction ends. It doesn't time out on its own.

______________________________________________________________________

## 3. What Blocks a Concurrent Build

Measured with a second session left idle in a transaction, then the build run with `statement_timeout = '2s'`:

| The other session, idle in a transaction, had…         | Build       |
| ------------------------------------------------------ | ----------- |
| nothing open                                           | built       |
| `READ COMMITTED`, ran `SELECT 1`                       | built       |
| `READ COMMITTED`, read the table being indexed         | built       |
| `READ COMMITTED`, **wrote to the table being indexed** | **blocked** |
| `READ COMMITTED`, wrote to a different table           | built       |
| **`REPEATABLE READ`**, ran `SELECT 1`                  | **blocked** |
| any transaction in a **different database**            | built       |

So the blockers are:

- open transactions that wrote to the table, because of the row locks they hold;
- transactions holding a snapshot taken before the build's last wait, such as a `REPEATABLE READ` transaction in the same database, whatever table it touched (measured). `SERIALIZABLE` transactions hold a snapshot the same way.

A forgotten "idle in transaction" connection that once wrote to the table blocks the build for as long as the connection lives. Clusters that allow idle transactions to live for hours (`idle_in_transaction_session_timeout` unset or large) can stall a build that long.

______________________________________________________________________

## 4. Running It Correctly

- **Autocommit on, one statement per call.** In `psql`, a `-c` string or a script line with one statement. From a driver, a connection with autocommit on and one `execute()` per statement. Several statements in one string run as one implicit transaction and fail (`transactions.md` section 3).
- **Clear blockers first.** Check for idle transactions that wrote to the table or hold snapshots (section 6).
- **Avoid `statement_timeout` as a guard.** Cancelling the build leaves an invalid index. If you must bound it, plan to repair afterwards.
- **Be careful with `IF NOT EXISTS`.** It skips any relation with that name, including an invalid index from an earlier failed build, with only a notice (section 5).

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS orders_customer_created_idx
    ON orders (customer_id, created_at DESC)
    WHERE status = 'open';
```

Partial and expression indexes can be built concurrently too.

______________________________________________________________________

## 5. Invalid Indexes

When a concurrent build fails, whether from a unique violation, a deadlock, `statement_timeout`, a cancelled query or a lost connection, PostgreSQL keeps the index and marks it invalid. Queries never use an invalid index. Two flags in `pg_index` describe it:

| `indisvalid` | `indisready` | Meaning                                                                                                                 |
| ------------ | ------------ | ----------------------------------------------------------------------------------------------------------------------- |
| `true`       | `true`       | normal, usable index                                                                                                    |
| `false`      | `true`       | failed after being marked ready: writes still maintain it (update overhead), and a unique one still enforces uniqueness |
| `false`      | `false`      | failed earlier: writes ignore it, and it enforces nothing                                                               |

Measured: a `CREATE UNIQUE INDEX CONCURRENTLY` on a column with duplicates failed with `could not create unique index`, leaving `indisvalid = false, indisready = false`. Inserting yet another duplicate then succeeded. Every build cancelled by `statement_timeout` in section 3 also left `indisvalid = false`. `psql`'s `\d <table>` lists such an index with `INVALID` after it.

### `IF NOT EXISTS` hides it

Rerunning `CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS` with the same name, duplicates still present, printed a notice and **succeeded**, and the index stayed invalid. Any tool that treats "the statement succeeded" as "the index exists" (a migration framework, a deploy script) now records an index that does nothing.

### Find them

```sql
SELECT n.nspname AS schema, c.relname AS index, t.relname AS table,
       i.indisvalid, i.indisready
FROM pg_index i
JOIN pg_class c ON c.oid = i.indexrelid
JOIN pg_class t ON t.oid = i.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE NOT i.indisvalid;
```

### Repair them

Remove the cause first (the duplicates, the blocking session), then either rebuild in place:

```sql
REINDEX INDEX CONCURRENTLY orders_customer_created_idx;
```

or drop and build again, as two separate statements:

```sql
DROP INDEX CONCURRENTLY IF EXISTS orders_customer_created_idx;
CREATE INDEX CONCURRENTLY orders_customer_created_idx ON orders (customer_id, created_at DESC);
```

The docs recommend dropping and rebuilding, and name [`REINDEX INDEX CONCURRENTLY`](https://www.postgresql.org/docs/18/sql-reindex.html#SQL-REINDEX-CONCURRENTLY) as the alternative. Measured: after deleting the duplicate, `REINDEX INDEX CONCURRENTLY` turned `indisvalid` from `false` to `true`.

______________________________________________________________________

## 6. Monitoring and Unblocking

Before the build, list sessions in the same database that could make it wait:

```sql
SELECT pid, usename, state, backend_xmin, xact_start, left(query, 60) AS last_query
FROM pg_stat_activity
WHERE datname = current_database()
  AND state LIKE 'idle in transaction%'
ORDER BY xact_start;
```

During the build, from another session:

```sql
SELECT pid, phase, current_locker_pid, lockers_total, lockers_done,
       blocks_done, blocks_total, tuples_done, tuples_total
FROM pg_stat_progress_create_index;
```

`current_locker_pid` is the session the build is waiting for. Look it up in `pg_stat_activity`; if it is a forgotten connection, ending it lets the build continue:

```sql
SELECT pg_terminate_backend(<current_locker_pid>);
```

That rolls back whatever the terminated session had open, so check what it is first.

______________________________________________________________________

## 7. Quick Reference

| Question                                                        | Answer                                                                                                                                 |
| --------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| Does a plain `CREATE INDEX` block writes?                       | Yes, `ShareLock` for the whole build; reads continue                                                                                   |
| Does `CONCURRENTLY` block writes?                               | No, `ShareUpdateExclusiveLock`                                                                                                         |
| Can it run inside `BEGIN … COMMIT` or a multi-statement string? | No                                                                                                                                     |
| What makes it wait?                                             | Open transactions that wrote to the table; transactions holding old snapshots (`REPEATABLE READ`, `SERIALIZABLE`) in the same database |
| What doesn't?                                                   | Read-only `READ COMMITTED` sessions, writes to other tables, other databases                                                           |
| How do I see what it waits for?                                 | `pg_stat_progress_create_index.phase` and `current_locker_pid`                                                                         |
| What if it fails or is cancelled?                               | Invalid index left behind; check `indisvalid` and `indisready`                                                                         |
| Does `IF NOT EXISTS` notice an invalid index?                   | No, it skips it                                                                                                                        |
| How do I repair one?                                            | `REINDEX INDEX CONCURRENTLY`, or `DROP INDEX CONCURRENTLY` then build again                                                            |
