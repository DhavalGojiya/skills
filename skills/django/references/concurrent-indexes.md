# Building Indexes on Large Tables From Django Migrations

How to add or drop an index on a busy PostgreSQL table without blocking writes, which migration shapes work, what makes a concurrent build wait, and how to find and repair the invalid index a failed build leaves behind.

Verified against Django [6.1.2](https://github.com/django/django/tree/6.1.2) source, identical on 5.2.17. Every migration shape below was run on psycopg2 2.9.13 and psycopg 3.3.6 against PostgreSQL 16.15 and 18.6; the blocking and repair behaviour was measured on both PostgreSQL versions. Every "Source:" link points at the commit that the `6.1.2` tag names, with line numbers.

______________________________________________________________________

## Table of Contents

1. [Why `CONCURRENTLY`](#1-why-concurrently)
2. [Shape 1: `AddIndexConcurrently`](#2-shape-1-addindexconcurrently)
3. [Shape 2: `SeparateDatabaseAndState` + `RunSQL`](#3-shape-2-separatedatabaseandstate--runsql)
4. [Shapes That Fail](#4-shapes-that-fail)
5. [What Makes a Concurrent Build Wait](#5-what-makes-a-concurrent-build-wait)
6. [Invalid Indexes](#6-invalid-indexes)
7. [Checklist for an Index Migration](#7-checklist-for-an-index-migration)

______________________________________________________________________

## 1. Why `CONCURRENTLY`

From the [PostgreSQL `CREATE INDEX` docs](https://www.postgresql.org/docs/18/sql-createindex.html#SQL-CREATEINDEX-CONCURRENTLY): a plain `CREATE INDEX` locks the table against writes (but not reads) until the build finishes, which on a table with hundreds of millions of rows can be hours. `CREATE INDEX CONCURRENTLY` builds without blocking writes, at the cost of:

- two scans of the table instead of one, so it takes longer;
- waiting for some other transactions to finish (section 5);
- refusing to run inside a transaction block, which is why the migration needs `atomic = False`;
- leaving an **invalid** index behind if it fails or is cancelled (section 6).

`DROP INDEX CONCURRENTLY` has the same transaction restriction.

______________________________________________________________________

## 2. Shape 1: `AddIndexConcurrently`

The simplest form, and the one to prefer:

```python
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [("shop", "0041_previous")]

    operations = [
        AddIndexConcurrently(
            "order",
            models.Index(
                fields=["customer", "-created_at"],
                name="shop_order_customer_created_idx",
                condition=models.Q(status="open"),
            ),
        ),
    ]
```

It updates Django's migration state and the database in one operation, so `makemigrations` sees the index declared in the model's `Meta.indexes` as already migrated. Its reverse runs `DROP INDEX CONCURRENTLY`. Measured: a partial index (`condition=`) was built as `CREATE INDEX ... WHERE (code > 1)` and recorded, with both drivers.

Its only guard is that the connection must not be inside an atomic block:

Source: [`contrib/postgres/operations.py#L114-L146`](https://github.com/django/django/blob/2212534bae393714e1e51db2341d4fd5545e19f6/django/contrib/postgres/operations.py#L114-L146)

```python
class NotInTransactionMixin:
    def _ensure_not_in_transaction(self, schema_editor):
        if schema_editor.connection.in_atomic_block:
            raise NotSupportedError(
                "The %s operation cannot be executed inside a transaction "
                "(set atomic = False on the migration)." % self.__class__.__name__
            )


class AddIndexConcurrently(NotInTransactionMixin, AddIndex):
    """Create an index using PostgreSQL's CREATE INDEX CONCURRENTLY syntax."""

    atomic = False
    category = OperationCategory.ADDITION

    def describe(self):
        return "Concurrently create index %s on field(s) %s of model %s" % (
            self.index.name,
            ", ".join(self.index.fields),
            self.model_name,
        )

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        self._ensure_not_in_transaction(schema_editor)
        model = to_state.apps.get_model(app_label, self.model_name)
        if self.allow_migrate_model(schema_editor.connection.alias, model):
            schema_editor.add_index(model, self.index, concurrently=True)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        self._ensure_not_in_transaction(schema_editor)
        model = from_state.apps.get_model(app_label, self.model_name)
        if self.allow_migrate_model(schema_editor.connection.alias, model):
            schema_editor.remove_index(model, self.index, concurrently=True)
```

No helper, no `connection.commit()` and no `set_autocommit()` is needed, with either psycopg2 or psycopg 3. `RemoveIndexConcurrently` is the drop counterpart.

______________________________________________________________________

## 3. Shape 2: `SeparateDatabaseAndState` + `RunSQL`

Use this when you need SQL Django won't generate, most often `IF NOT EXISTS`, for example because the index was built by hand on production ahead of the deploy:

```python
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [("shop", "0041_previous")]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=[
                        "CREATE INDEX CONCURRENTLY IF NOT EXISTS shop_order_customer_created_idx "
                        "ON shop_order (customer_id, created_at DESC) WHERE status = 'open'",
                    ],
                    reverse_sql=[
                        "DROP INDEX CONCURRENTLY IF EXISTS shop_order_customer_created_idx",
                    ],
                ),
            ],
            state_operations=[
                migrations.AddIndex(
                    "order",
                    models.Index(
                        fields=["customer", "-created_at"],
                        name="shop_order_customer_created_idx",
                        condition=models.Q(status="open"),
                    ),
                ),
            ],
        ),
    ]
```

- `database_operations` run against the database and do not touch Django's state.
- `state_operations` update Django's state and run no SQL. Keep the name, fields and condition identical to what the SQL builds, or the model and the database disagree.
- Pass `sql` and `reverse_sql` as **lists, one statement per item** (section 4).
- `IF NOT EXISTS` also skips an invalid index left by an earlier failed build (section 6).

`RunPython` with `with schema_editor.connection.cursor() as cur: cur.execute(...)` works the same way, one statement per `execute()`. The cursor starts no transaction; with `atomic = False` each `execute()` commits on its own.

______________________________________________________________________

## 4. Shapes That Fail

All measured, with both drivers:

| Migration                                                                                                     | Result                                                                                                                                 |
| ------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| Default `atomic = True` + `RunSQL("CREATE INDEX CONCURRENTLY ...")`                                           | `CREATE INDEX CONCURRENTLY cannot run inside a transaction block`                                                                      |
| `atomic = True` + `RunPython(..., atomic=False)` running `CONCURRENTLY`                                       | same error; the operation can't leave the migration's transaction                                                                      |
| `atomic = True` + `AddIndexConcurrently`                                                                      | `NotSupportedError: The AddIndexConcurrently operation cannot be executed inside a transaction (set atomic = False on the migration).` |
| `atomic = False` + **two** `CONCURRENTLY` statements in **one string**, in `RunSQL` or one `cursor.execute()` | `CREATE INDEX CONCURRENTLY cannot run inside a transaction block`                                                                      |
| `atomic = False` + a helper that forces autocommit off in `finally`                                           | builds the index, prints `OK`, never records the migration (`lost-migration-row.md`)                                                   |

The one-string case surprises people because the migration is non-atomic. Django's PostgreSQL backend sends a `RunSQL` string to the server unsplit:

Source: [`db/backends/postgresql/operations.py#L186-L187`](https://github.com/django/django/blob/2212534bae393714e1e51db2341d4fd5545e19f6/django/db/backends/postgresql/operations.py#L186-L187), [`db/migrations/operations/special.py#L125-L139`](https://github.com/django/django/blob/2212534bae393714e1e51db2341d4fd5545e19f6/django/db/migrations/operations/special.py#L125-L139)

```python
    def prepare_sql_script(self, sql):
        return [sql]
```

```python
    def _run_sql(self, schema_editor, sqls):
        if isinstance(sqls, (list, tuple)):
            for sql in sqls:
                params = None
                if isinstance(sql, (list, tuple)):
                    elements = len(sql)
                    if elements == 2:
                        sql, params = sql
                    else:
                        raise ValueError("Expected a 2-tuple but got %d" % elements)
                schema_editor.execute(sql, params=params)
        elif sqls != RunSQL.noop:
            statements = schema_editor.connection.ops.prepare_sql_script(sqls)
            for statement in statements:
                schema_editor.execute(statement, params=None)
```

PostgreSQL runs a query string that contains several statements as one implicit transaction, and `CONCURRENTLY` refuses to run inside it. A list makes Django call `execute()` once per statement; measured, two `CONCURRENTLY` statements passed as a list built both indexes and recorded the migration on every combination.

______________________________________________________________________

## 5. What Makes a Concurrent Build Wait

A concurrent build waits for some other open transactions to end. Which ones, measured on PostgreSQL 16 and 18 with another session left "idle in transaction" and the build run with `statement_timeout = '2s'`:

| The other session, idle in transaction, had…             | Build       |
| -------------------------------------------------------- | ----------- |
| nothing open                                             | built       |
| `READ COMMITTED`, ran `SELECT 1`                         | built       |
| `READ COMMITTED`, read the table being indexed           | built       |
| `READ COMMITTED`, **wrote to the table being indexed**   | **blocked** |
| `READ COMMITTED`, wrote to a different table             | built       |
| **`REPEATABLE READ`**, ran `SELECT 1` (holds a snapshot) | **blocked** |
| any transaction in a different database                  | built       |

So the build waits for transactions in the same database that hold row locks on the table, or that hold a snapshot taken before the build reached its waiting phase. A connection left "idle in transaction" after writing to the table blocks the build for as long as that connection lives, and some clusters let idle transactions live for hours (`idle_in_transaction_session_timeout`).

Before building on a busy table:

```sql
SELECT pid, usename, state, backend_xmin, xact_start, left(query, 60)
FROM pg_stat_activity
WHERE datname = current_database()
  AND state LIKE 'idle in transaction%'
ORDER BY xact_start;
```

During the build, from another session:

```sql
SELECT phase, lockers_total, lockers_done, current_locker_pid,
       blocks_done, blocks_total, tuples_done, tuples_total
FROM pg_stat_progress_create_index;
```

`current_locker_pid` is the session the build is waiting on.

A `statement_timeout` that cancels the wait does not undo the build: it leaves an invalid index, which section 6 covers. Measured: every "blocked" row above left `indisvalid = false`.

______________________________________________________________________

## 6. Invalid Indexes

When a concurrent build fails, because of a unique violation, a cancelled statement or a lost connection, PostgreSQL keeps the half-built index and marks it invalid (`indisvalid = false`). Queries never use it. What else it does depends on how far the build got, which `indisready` shows: if the build failed after the index was marked ready, writes keep maintaining it and a unique index still enforces uniqueness; if it failed earlier, as in the measurement below (`indisready = false`), writes ignore it and it enforces nothing.

### `IF NOT EXISTS` hides it

Measured with a migration running `CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ...` on a column with duplicates:

|                         | First run                                                             | Second run (duplicates still there) |
| ----------------------- | --------------------------------------------------------------------- | ----------------------------------- |
| `migrate` result        | `IntegrityError: could not create unique index "demo_item_code_uniq"` | `Applying ... OK`                   |
| Index                   | exists, `indisvalid = false`                                          | exists, `indisvalid = false`        |
| `django_migrations` row | none                                                                  | **written**                         |

On the second run PostgreSQL saw a relation with that name, skipped the statement with a notice, and Django recorded the migration. The index had `indisready = false`: an `INSERT` of another duplicate succeeded. The project now believes it has a working unique index, and nothing enforces uniqueness.

### Finding invalid indexes

```sql
SELECT n.nspname, c.relname AS index, t.relname AS table, i.indisready
FROM pg_index i
JOIN pg_class c ON c.oid = i.indexrelid
JOIN pg_class t ON t.oid = i.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE NOT i.indisvalid;
```

### Repairing one

Fix the cause first (remove the duplicates, end the blocking session), then either:

```sql
REINDEX INDEX CONCURRENTLY demo_item_code_uniq;
```

or drop and rebuild:

```sql
DROP INDEX CONCURRENTLY IF EXISTS demo_item_code_uniq;
CREATE UNIQUE INDEX CONCURRENTLY demo_item_code_uniq ON demo_item (code);
```

Measured: after removing the duplicate, `REINDEX INDEX CONCURRENTLY` turned `indisvalid` from `false` to `true`. Run each statement on its own, not as one string (section 4).

______________________________________________________________________

## 7. Checklist for an Index Migration

- [ ] The migration has `atomic = False` and contains only the index operations.
- [ ] It uses `AddIndexConcurrently` / `RemoveIndexConcurrently`, or `SeparateDatabaseAndState` with state operations that match the SQL exactly.
- [ ] Each `CONCURRENTLY` statement is its own `RunSQL` list item or its own `cursor.execute()`.
- [ ] It doesn't call `set_autocommit()` or `connection.commit()`.
- [ ] The reverse uses `DROP INDEX CONCURRENTLY IF EXISTS`.
- [ ] Before running it on production: no long "idle in transaction" sessions that wrote to the table, and no long `REPEATABLE READ` transactions.
- [ ] After running it: the row is in `django_migrations` **and** `indisvalid` is `true` for every index it builds.
