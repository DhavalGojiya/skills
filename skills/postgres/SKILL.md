---
name: postgres
description: PostgreSQL behaviour that bites applications, covering transactions and the aborted state, isolation levels and serialization failures, row locks (FOR UPDATE, SKIP LOCKED, deadlocks, lost updates), the lock queue behind DDL and every timeout setting, and CREATE INDEX CONCURRENTLY with invalid indexes. Use on "current transaction is aborted, commands ignored until end of transaction block", "could not serialize access", "deadlock detected", "could not obtain lock on row", "canceling statement due to lock timeout" or "statement timeout", "terminating connection due to idle-in-transaction timeout", "CREATE INDEX CONCURRENTLY cannot run inside a transaction block", an INVALID index, a COMMIT answered with ROLLBACK, sessions stuck idle in transaction, queries piling up behind an ALTER TABLE, writes lost when a connection closes, or concurrent updates that overwrite each other or update 0 rows.
---

# PostgreSQL transactions, locks and index builds

Verified by experiment, with statement sequences read from the server log (`log_statement = all`) and lock modes, wait events, phases and catalog flags read from `pg_locks`, `pg_stat_activity`, `pg_stat_progress_create_index` and `pg_index` during the runs. Transactions, row locks and concurrent index builds were measured on PostgreSQL 16.15 and 18.6 with psycopg2 2.9.13, psycopg 3.3.6 and `psql`; isolation levels and the lock queue and timeouts on 18.6 with psycopg 3.3.6. Details and links to the PostgreSQL docs are in `references/`.

## Transactions

1. Outside an explicit `BEGIN`, every statement is its own transaction and commits when it finishes. The client sends no `BEGIN` or `COMMIT` for it.
2. Drivers that follow PEP 249 (psycopg2, psycopg 3) start with autocommit **off**: they send `BEGIN` before the first statement, even a `SELECT`, and the transaction stays open until you call `commit()` or `rollback()`. Closing the connection rolls it back, so a forgotten `commit()` silently loses the writes.
3. A query string holding several statements separated by `;` runs as **one** implicit transaction. If a later statement fails, the earlier ones are rolled back too, and statements that refuse transaction blocks fail, such as `CREATE INDEX CONCURRENTLY` and `DROP INDEX CONCURRENTLY`. Send those one per `execute()`.
4. After any error inside a transaction, the transaction is aborted. Every later statement fails with `current transaction is aborted, commands ignored until end of transaction block`, `pg_stat_activity.state` shows `idle in transaction (aborted)`, and `COMMIT` is answered with `ROLLBACK`. The statements that succeeded earlier in it are lost.
5. Catching the error in application code changes nothing on the server. Only `ROLLBACK`, or `ROLLBACK TO SAVEPOINT`, ends the aborted state.
6. To keep the work done before a risky statement, run `SAVEPOINT s1` before it and `ROLLBACK TO SAVEPOINT s1` when it fails. The transaction is then usable again, and `COMMIT` saves what came before the savepoint.
7. Don't leave sessions idle in a transaction. One that read a table blocks any `ALTER TABLE` on it (rule 18), one that wrote to a table keeps its row locks, and one in `REPEATABLE READ` keeps its snapshot, until it ends. Bound them with `idle_in_transaction_session_timeout`.

## Isolation levels

08. `READ COMMITTED`, the default, takes a new snapshot for every statement. `REPEATABLE READ` and `SERIALIZABLE` take one at the first query after `BEGIN` (`SHOW` and `SET` don't count) and keep it for the whole transaction. `SET TRANSACTION ISOLATION LEVEL` after a query fails with `SET TRANSACTION ISOLATION LEVEL must be called before any query`.
09. At `REPEATABLE READ` and `SERIALIZABLE`, an `UPDATE`, `DELETE` or `SELECT ... FOR UPDATE` of a row another transaction changed after the snapshot fails with `could not serialize access due to concurrent update` (SQLSTATE 40001). At `READ COMMITTED` the write goes ahead and re-checks its `WHERE` against the new row version, so a compare-and-set such as `UPDATE orders SET status = 'cancelled' WHERE id = 1 AND status = 'pending'` can wait and then return `UPDATE 0`; check the row count.
10. Write skew (two transactions each check a condition, then write different rows) commits at `READ COMMITTED` and `REPEATABLE READ`. Only `SERIALIZABLE` stops it, with `could not serialize access due to read/write dependencies among transactions`, unless you lock every row the check reads with `FOR UPDATE`.
11. Retry the **whole** transaction on SQLSTATE 40001 and 40P01: re-read everything, back off briefly, cap the attempts. `ROLLBACK TO SAVEPOINT` keeps the old snapshot, so retrying one statement fails again, and at `SERIALIZABLE` the error can come from `COMMIT` itself, so the retry must include the commit.

## Row locks

12. A row lock (`SELECT ... FOR UPDATE`) belongs to its transaction: it is held until `COMMIT` or `ROLLBACK`, not until the `SELECT` finishes. With autocommit on, the statement is its own transaction, so the lock is gone before your next line of code runs. PostgreSQL accepts that silently; take row locks inside `BEGIN … COMMIT`.
13. A row held `FOR UPDATE` makes `UPDATE`, `DELETE`, `FOR UPDATE` and `FOR SHARE` of that row wait until the holder's transaction ends. Plain `SELECT`s never wait, and other rows are unaffected.
14. At `READ COMMITTED`, reading, computing in the application, then writing, with no lock, loses concurrent updates. Either compute in SQL (`UPDATE t SET balance = balance - 30 WHERE id = 2`), or lock the row with `FOR UPDATE` in a short transaction, and do no slow I/O while holding it: every other writer of the row waits. At `REPEATABLE READ` and `SERIALIZABLE` the second writer gets 40001 instead (rule 9), even for the single-statement `UPDATE`, so retry.
15. `FOR UPDATE NOWAIT` fails at once with `could not obtain lock on row in relation "<table>"`; `FOR UPDATE SKIP LOCKED` leaves locked rows out, which is how to build a work queue on a table.
16. `FOR UPDATE` on a join locks the matching rows of every joined table. Name the table you mean with `FOR UPDATE OF <table>`. Use `FOR NO KEY UPDATE` when you won't change the row's key; unlike `FOR UPDATE`, it doesn't block inserts of child rows that reference it.
17. Two transactions locking the same rows in opposite order deadlock; after `deadlock_timeout` (1 s by default) PostgreSQL aborts one with `deadlock detected`. Lock rows in a consistent order (`ORDER BY id ... FOR UPDATE`), and retry the aborted side.

## Lock queue and timeouts

18. An `ALTER TABLE` waiting for `AccessExclusiveLock`, even behind an idle transaction that only read the table, makes every later `SELECT` and `INSERT` on that table queue behind it until the holder's transaction ends. Run DDL on a busy table as `BEGIN; SET LOCAL lock_timeout = '200ms'; <DDL>; COMMIT` and retry on `canceling statement due to lock timeout` (SQLSTATE 55P03).
19. "Fast" DDL still takes `AccessExclusiveLock`: `ADD COLUMN` with a constant default, `SET DEFAULT`, widening a `varchar`, `ADD CONSTRAINT ... NOT VALID`. Only `VALIDATE CONSTRAINT`, `ANALYZE`, `VACUUM` and `CREATE INDEX CONCURRENTLY` (`ShareUpdateExclusiveLock`) let reads and writes continue. Commit DDL in its own short transaction: the lock lasts until `COMMIT`, not until the statement returns.
20. `lock_timeout` (55P03) and `statement_timeout` (`canceling statement due to statement timeout`, 57014) cancel only the statement. `idle_in_transaction_session_timeout` (`terminating connection due to idle-in-transaction timeout`, 25P03) and `transaction_timeout` (PostgreSQL 17 and later, 25P04) end the session, and the client finds out only on its next statement.
21. `SET LOCAL` works only inside a transaction block; in autocommit mode it just emits `WARNING: SET LOCAL can only be used in transaction blocks`. A plain `SET` lasts for the rest of the session, and a `SET` inside a transaction that rolls back is undone.
22. End an idle lock holder with `pg_terminate_backend(pid)`; `pg_cancel_backend(pid)` returns true but leaves an `idle in transaction` session and its locks in place.
23. `ADD CONSTRAINT ... NOT NULL col NOT VALID` exists only from PostgreSQL 18 (on 16 it is a `syntax error at or near "NOT"`). On 16 and 17, add `CHECK (col IS NOT NULL) NOT VALID`, then `VALIDATE CONSTRAINT`.

## Concurrent index builds

24. A plain `CREATE INDEX` takes a `ShareLock` on the table: reads continue, but every write waits until the build finishes. `CREATE INDEX CONCURRENTLY` takes a `ShareUpdateExclusiveLock`, so writes continue, but it can't run inside a transaction block and it takes longer.
25. A concurrent build waits for older transactions in the same database. In phase `waiting for writers before build` it waits for transactions that wrote to the table; in `waiting for old snapshots`, for transactions holding a snapshot, such as `REPEATABLE READ`. `pg_stat_progress_create_index.current_locker_pid` names the session it is waiting for. Read-only `READ COMMITTED` sessions, writes to other tables and other databases don't block it.
26. A failed or cancelled concurrent build leaves an **invalid** index (`indisvalid = false`) that queries never use. If it failed after being marked ready (`indisready = true`), writes still maintain it and a unique one still enforces uniqueness; if it failed earlier, it does neither. A `statement_timeout` that cancels the wait counts as a failure.
27. `CREATE INDEX ... IF NOT EXISTS` skips a same-named invalid index with only a notice. After any failed build, check `indisvalid` before trusting a rerun.
28. Repair an invalid index once the cause is gone (duplicates removed, blocker ended): `REINDEX INDEX CONCURRENTLY <name>`, or `DROP INDEX CONCURRENTLY <name>` and build it again. Run each as its own statement.

## Debug procedure

1. Find sessions holding a transaction open, oldest first:
   `SELECT pid, usename, state, backend_xmin, xact_start, left(query, 60) FROM pg_stat_activity WHERE state LIKE 'idle in transaction%' ORDER BY xact_start;`
2. Find sessions waiting for a lock, and who blocks them (a row-lock wait shows as `Lock` / `transactionid`, a table-lock wait as `Lock` / `relation`):
   `SELECT pid, wait_event_type, wait_event, pg_blocking_pids(pid) AS blocked_by, left(query, 60) FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0;`
3. Find the root blocker of a queue: a session that blocks others but is not blocked itself. Queued readers name the waiting `ALTER`, not the idle session behind it:
   `SELECT a.pid, a.state, now() - a.xact_start AS xact_age, left(a.query, 60) FROM pg_stat_activity a WHERE a.pid IN (SELECT unnest(pg_blocking_pids(w.pid)) FROM pg_stat_activity w) AND cardinality(pg_blocking_pids(a.pid)) = 0;`
4. See which locks are held or awaited on a table:
   `SELECT pid, mode, granted FROM pg_locks WHERE relation = '<table>'::regclass;`
5. Watch a concurrent build, and see who it is waiting for:
   `SELECT pid, phase, current_locker_pid, blocks_done, blocks_total, tuples_done, tuples_total FROM pg_stat_progress_create_index;`
6. List invalid indexes:
   `SELECT c.relname, t.relname AS table, i.indisready FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_class t ON t.oid = i.indrelid WHERE NOT i.indisvalid;`
7. Check a driver connection's transaction state: psycopg 3 `conn.info.transaction_status` (`IDLE`, `INTRANS`, `INERROR`); psycopg2 `conn.get_transaction_status()` (`0` idle, `2` in transaction, `3` in error). In `psql`, the default prompt shows `*` inside a transaction and `!` inside a failed one.
8. To see exactly what a client sends, run a test server with `-c log_statement=all` and read its log for `BEGIN`, `SAVEPOINT`, `COMMIT` and `ROLLBACK`. Set `log_lock_waits = on` to log every lock wait longer than `deadlock_timeout`.

## References

- `references/transactions.md`: the server's transaction model, how drivers emulate "autocommit off", multi-statement strings, the aborted state and why the server waits for `ROLLBACK`, savepoints, and idle-in-transaction sessions, with measured results. Read it when a transaction behaves unexpectedly.
- `references/isolation-levels.md`: `READ COMMITTED` vs `REPEATABLE READ` vs `SERIALIZABLE` with concurrent sessions (non-repeatable reads, phantoms, when the snapshot is taken, `READ COMMITTED` re-checks returning `UPDATE 0`, lost update vs 40001, write skew, unexpected serialization failures), retrying the whole transaction, and setting the level from SQL, psycopg 3 and Django. Read it before choosing an isolation level, or when you see SQLSTATE 40001 or a concurrent update that silently did nothing.
- `references/row-locks.md`: what `FOR UPDATE` blocks and for how long, the lock outside a transaction, the lost-update race and its two fixes, `NOWAIT`, `SKIP LOCKED`, joins and `OF`, `FOR NO KEY UPDATE` with foreign keys, deadlocks, and finding the blocking session. Read it before writing lock-then-update code, or when sessions wait on each other.
- `references/lock-queue-and-timeouts.md`: the lock-queue outage (an `ALTER` waiting behind an idle-in-transaction reader blocks every new query), reading `pg_locks`, `pg_blocking_pids()` and `log_lock_waits`, cancel vs terminate, each timeout with its error text, SQLSTATE and effect on the session, `SET` vs `SET LOCAL`, the `lock_timeout` and retry pattern for DDL, and the table lock each common DDL takes. Read it before running DDL on a busy table, or when queries pile up behind a migration.
- `references/concurrent-indexes.md`: plain vs concurrent builds, lock modes, the build's phases and what blocks each, invalid indexes, `IF NOT EXISTS`, monitoring and repair. Read it before building an index on a busy table, or when a build hangs or fails.
- For Django-specific behaviour (`atomic()`, `ATOMIC_REQUESTS`, migrations, `AddIndexConcurrently`, bulk operations), use the `django` skill.
