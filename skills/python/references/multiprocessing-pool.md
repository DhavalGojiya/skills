# multiprocessing.Pool: Start Methods, map, close and join

How a `multiprocessing.Pool` starts its workers and what they inherit, whether `map()` and `map_async()` wait, how work is split into chunks, why `close()` must come before `join()`, and what happens to running tasks when the main process ends without `join()`.

Verified against CPython [v3.15.0](https://github.com/python/cpython/tree/v3.15.0) source (`Lib/multiprocessing/context.py`, `pool.py`, `util.py`), and by running `scripts/pool_lifecycle.py` on Linux 7.0 with Python 3.15.0. The default start method was also checked on 3.13.16 (`fork`) and 3.14.8 (`forkserver`). Every "Source:" link points at the commit the `v3.15.0` tag names, with line numbers.

______________________________________________________________________

## Table of Contents

1. [Start Methods and What a Worker Inherits](#1-start-methods-and-what-a-worker-inherits)
2. [A Task That Can't Be Unpickled Hangs `map()`](#2-a-task-that-cant-be-unpickled-hangs-map)
3. [`map()` Blocks, `map_async()` Doesn't](#3-map-blocks-map_async-doesnt)
4. [How `map()` Splits the Work](#4-how-map-splits-the-work)
5. [`close()`, Then `join()`](#5-close-then-join)
6. [Ending the Main Process Without `join()`](#6-ending-the-main-process-without-join)
7. [Quick Reference](#7-quick-reference)

______________________________________________________________________

## 1. Start Methods and What a Worker Inherits

Python 3.14 changed the default start method on Linux from `fork` to `forkserver`; macOS uses `spawn`:

Source: [`Lib/multiprocessing/context.py#L331-L337`](https://github.com/python/cpython/blob/d220409a13b77de7a6c30abe16ea90db01528edc/Lib/multiprocessing/context.py#L331-L337)

```python
# bpo-33725: running arbitrary code after fork() is no longer reliable
# on macOS since macOS 10.14 (Mojave). Use spawn by default instead.
# gh-84559: We changed everyones default to a thread safeish one in 3.14.
if reduction.HAVE_SEND_HANDLE and sys.platform != 'darwin':
    _default_context = DefaultContext(_concrete_contexts['forkserver'])
else:
    _default_context = DefaultContext(_concrete_contexts['spawn'])
```

| Start method                           | How a worker starts                                            | What it has from the parent                                                      |
| -------------------------------------- | -------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `fork` (Linux default to 3.13)         | a copy of the parent process at the moment the pool is created | everything: module state, settings, open files, sockets and database connections |
| `forkserver` (Linux default from 3.14) | forked from a clean server process started at the first pool   | only what importing the main module and its imports sets up                      |
| `spawn` (macOS and Windows default)    | a fresh interpreter                                            | only what importing the main module and its imports sets up                      |

Under `forkserver` and `spawn`, the worker imports the main module under the name `__mp_main__`, so the `if __name__ == "__main__":` block never runs there. Anything that block sets up, such as `django.setup()` in a `manage.py` or state assigned at run time, is missing in the worker. Task functions and their arguments are pickled, and they have to unpickle in that bare process.

`scripts/pool_lifecycle.py` sets a dictionary entry inside its `__main__` block and reads it back in one worker per start method:

```text
1. What a worker inherits (STATE set inside the __main__ block)
  fork       worker sees: inherited
  forkserver worker sees: missing
  spawn      worker sees: missing
```

Choose the start method once, in the `__main__` block before any pool is created, with `multiprocessing.set_start_method("fork")`, or per pool with `multiprocessing.get_context("fork").Pool(...)`.

______________________________________________________________________

## 2. A Task That Can't Be Unpickled Hangs `map()`

When a worker raises while unpickling a task, the worker process dies, the pool starts a replacement, and the task is never run or answered, so `map()` waits forever. The script's `Payload` object can only be rebuilt after the `__main__` block has run:

```text
6. A task that only unpickles after main-block setup
  fork                     exited after 0.1 s; ["['payload', 'payload']"]; []
  forkserver               still running, killed after 10.0 s; []; ['RuntimeError: main-block setup has not run in this process']
  forkserver + initializer exited after 0.1 s; ["['payload', 'payload']"]; []
```

Under `fork` the worker has the parent's state and returns both payloads. Under `forkserver` every worker dies with the `RuntimeError` and the call never returns; the script kills it after 10 seconds. An `initializer=` function that repeats the setup in each worker makes `forkserver` work too. Django code hits this as `django.core.exceptions.AppRegistryNotReady: Apps aren't loaded yet.` when a task argument is a model class or instance, because `django.setup()` ran only in the parent's `__main__` block.

______________________________________________________________________

## 3. `map()` Blocks, `map_async()` Doesn't

`map()` is `map_async()` followed by `.get()`:

Source: [`Lib/multiprocessing/pool.py#L362-L367`](https://github.com/python/cpython/blob/d220409a13b77de7a6c30abe16ea90db01528edc/Lib/multiprocessing/pool.py#L362-L367)

```python
def map(self, func, iterable, chunksize=None):
    '''
    Apply `func` to each element in `iterable`, collecting the results
    in a list that is returned.
    '''
    return self._map_async(func, iterable, mapstar, chunksize).get()
```

```text
2. map() blocks, map_async() does not (6 tasks of 0.5 s, 3 workers)
  map() returned after        1.0 s
  map_async() returned after  0.0 s
  .get() returned after       1.0 s
```

Both run the same tasks on the same workers in the same time. `map_async()` only lets the caller do something else before calling `.get()`, run several jobs on one pool, attach `callback=` and `error_callback=`, or wait with `.get(timeout=...)`.

______________________________________________________________________

## 4. How `map()` Splits the Work

Without a `chunksize`, `map()` makes about four chunks per worker:

Source: [`Lib/multiprocessing/pool.py#L480-L483`](https://github.com/python/cpython/blob/d220409a13b77de7a6c30abe16ea90db01528edc/Lib/multiprocessing/pool.py#L480-L483)

```python
if chunksize is None:
    chunksize, extra = divmod(len(iterable), len(self._pool) * 4)
    if extra:
        chunksize += 1
```

84 tasks on 3 workers gives `divmod(84, 12) == (7, 0)`: 12 chunks of 7. A free worker takes the next whole chunk and runs its tasks one after another:

```text
3. How map() chunks 84 tasks over 3 workers
  worker 1196763: 4 chunks of [7], starting at [0, 21, 42, 63]
  worker 1196764: 4 chunks of [7], starting at [7, 28, 49, 70]
  worker 1196765: 4 chunks of [7], starting at [14, 35, 56, 77]
```

Which worker gets which chunk depends on timing, so the split differs between runs, and one fast worker can take more chunks than the others. Code that does setup or cleanup per task runs it once per task, several times in the same worker process.

______________________________________________________________________

## 5. `close()`, Then `join()`

`close()` only stops the pool from accepting new tasks; the workers finish what is queued and then exit. `join()` waits for them, and refuses to run while the pool still accepts work, because workers in an open pool never exit on their own:

Source: [`Lib/multiprocessing/pool.py#L647-L664`](https://github.com/python/cpython/blob/d220409a13b77de7a6c30abe16ea90db01528edc/Lib/multiprocessing/pool.py#L647-L664)

```python
def close(self):
    util.debug('closing pool')
    if self._state == RUN:
        self._state = CLOSE
        self._worker_handler._state = CLOSE
        self._change_notifier.put(None)

def terminate(self):
    util.debug('terminating pool')
    self._state = TERMINATE
    self._terminate()

def join(self):
    util.debug('joining pool')
    if self._state == RUN:
        raise ValueError("Pool is still running")
    elif self._state not in (CLOSE, TERMINATE):
        raise ValueError("In unknown state")
```

```text
4. join() before close()
  ValueError: Pool is still running
  close() then join(): returned normally
```

After a blocking `map()`, every task is already done, so `close()` and `join()` return almost at once.

______________________________________________________________________

## 6. Ending the Main Process Without `join()`

Pool workers are daemon processes:

Source: [`Lib/multiprocessing/pool.py#L328`](https://github.com/python/cpython/blob/d220409a13b77de7a6c30abe16ea90db01528edc/Lib/multiprocessing/pool.py#L328)

```python
w.daemon = True
```

The operating system doesn't stop a child when its parent exits, so `multiprocessing` stops them itself. Every pool registers a finalizer:

Source: [`Lib/multiprocessing/pool.py#L255-L260`](https://github.com/python/cpython/blob/d220409a13b77de7a6c30abe16ea90db01528edc/Lib/multiprocessing/pool.py#L255-L260)

```python
self._terminate = util.Finalize(
    self, self._terminate_pool,
    args=(self._taskqueue, self._inqueue, self._outqueue, self._pool,
          self._change_notifier, self._worker_handler, self._task_handler,
          self._result_handler, self._cache),
    exitpriority=15
```

and an `atexit` hook runs the finalizers, then terminates the remaining daemon children and joins every child:

Source: [`Lib/multiprocessing/util.py#L403-L405`](https://github.com/python/cpython/blob/d220409a13b77de7a6c30abe16ea90db01528edc/Lib/multiprocessing/util.py#L403-L405)

```python
info('process shutting down')
debug('running all "atexit" finalizers with priority >= 0')
_run_finalizers(0)
```

Source: [`Lib/multiprocessing/util.py#L421-L428`](https://github.com/python/cpython/blob/d220409a13b77de7a6c30abe16ea90db01528edc/Lib/multiprocessing/util.py#L421-L428)

```python
for p in active_children():
    if p.daemon:
        info('calling terminate() for daemon %s', p.name)
        p._popen.terminate()

for p in active_children():
    info('calling join() for process %s', p.name)
    p.join()
```

The pool's finalizer discards queued tasks and terminates workers still running one. The script starts 6 tasks of 3 seconds on 3 workers with `map_async()`, calls `close()` after 1 second, and lets the main process end:

```text
5. The main process ends without join() while 6 tasks of 3 s are queued
   0.0s  task 0 started
   0.0s  task 1 started
   0.0s  task 2 started
   1.1s  last line of main
  process exited after 1.1 s
```

Tasks 0 to 2 were killed mid-task, tasks 3 to 5 never started, and the process exited with status 0 a fraction of a second after its last line, without any error. `close()` followed by `join()` would have let all 6 finish. After a blocking `map()` nothing is lost, because nothing is left to run.

______________________________________________________________________

## 7. Quick Reference

| Symptom                                                                                   | Cause                                                                  | Fix                                                                                                                   |
| ----------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| Workers lack settings or state the parent set up; `AppRegistryNotReady` in Django workers | `forkserver` (Linux, Python 3.14+) or `spawn` start method (section 1) | `multiprocessing.set_start_method("fork")` in the `__main__` block, or repeat the setup in `initializer=` (section 2) |
| `map()` never returns; workers keep dying with a traceback from `_ForkingPickler.loads`   | a task can't be unpickled in the worker (section 2)                    | the same; or pass picklable arguments that need no setup                                                              |
| `ValueError: Pool is still running`                                                       | `join()` before `close()` (section 5)                                  | `pool.close()`, then `pool.join()`                                                                                    |
| Tasks silently missing after the program ends                                             | main process ended without `join()` after `map_async()` (section 6)    | `close()` and `join()`, or `.get()` the result                                                                        |
| Per-task setup runs many times in one worker                                              | each worker runs several chunks of several tasks (section 4)           | do per-process setup in `initializer=`                                                                                |
