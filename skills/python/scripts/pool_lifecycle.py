# /// script
# requires-python = ">=3.14"
# ///
"""Show how multiprocessing.Pool starts workers, splits work and shuts down.

Prints the default start method and what a worker inherits under each method,
whether map() and map_async() block, how map() chunks 84 tasks over 3 workers,
what join() before close() raises, what happens to busy workers when the main
process ends without join(), and a worker that cannot unpickle its task.
"""

import collections
import multiprocessing
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time

STATE = {}


def sleepy(n):
    time.sleep(0.5)
    return n


def report_state(_):
    return STATE.get("set_in_main", "missing")


def which_worker(n):
    time.sleep(0.01)
    return n, os.getpid()


EXIT_WITHOUT_JOIN = """
import multiprocessing, os, time
START = time.monotonic()

def task(n):
    print(f"{time.monotonic() - START:4.1f}s  task {n} started", flush=True)
    time.sleep(3)
    print(f"{time.monotonic() - START:4.1f}s  task {n} FINISHED", flush=True)

if __name__ == "__main__":
    pool = multiprocessing.Pool(3)
    pool.map_async(task, range(6), chunksize=1)
    time.sleep(1)
    pool.close()
    print(f"{time.monotonic() - START:4.1f}s  last line of main", flush=True)
"""

UNPICKLABLE_IN_WORKER = """
import multiprocessing, sys

READY = False

def rebuild():
    if not READY:
        raise RuntimeError("main-block setup has not run in this process")
    return "payload"

class Payload:
    def __reduce__(self):
        return (rebuild, ())

def task(payload):
    return payload

def set_ready():
    global READY
    READY = True

if __name__ == "__main__":
    method, use_initializer = sys.argv[1], sys.argv[2] == "initializer"
    multiprocessing.set_start_method(method)
    READY = True
    initializer = set_ready if use_initializer else None
    with multiprocessing.Pool(2, initializer=initializer) as pool:
        print(pool.map(task, [Payload(), Payload()]), flush=True)
"""


def run_child(code, *args, timeout):
    """Run code as a script file in its own process group; kill it on timeout."""
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "child.py")
        with open(script, "w") as handle:
            handle.write(textwrap.dedent(code))
        started = time.monotonic()
        process = subprocess.Popen(
            [sys.executable, script, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            output, _ = process.communicate(timeout=timeout)
            ended = "exited"
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            output, _ = process.communicate()
            ended = "still running, killed"
    return output, ended, time.monotonic() - started


def main():
    method = multiprocessing.get_start_method()
    print(f"Python {sys.version.split()[0]}, default start method: {method}")

    print("\n1. What a worker inherits (STATE set inside the __main__ block)")
    STATE["set_in_main"] = "inherited"
    for method in ("fork", "forkserver", "spawn"):
        with multiprocessing.get_context(method).Pool(1) as pool:
            print(f"  {method:10} worker sees: {pool.map(report_state, [0])[0]}")

    print("\n2. map() blocks, map_async() does not (6 tasks of 0.5 s, 3 workers)")
    with multiprocessing.Pool(3) as pool:
        started = time.monotonic()
        pool.map(sleepy, range(6))
        print(f"  map() returned after        {time.monotonic() - started:.1f} s")
        started = time.monotonic()
        result = pool.map_async(sleepy, range(6))
        print(f"  map_async() returned after  {time.monotonic() - started:.1f} s")
        result.get()
        print(f"  .get() returned after       {time.monotonic() - started:.1f} s")

    print("\n3. How map() chunks 84 tasks over 3 workers")
    with multiprocessing.Pool(3) as pool:
        runs = collections.defaultdict(list)
        for n, pid in pool.map(which_worker, range(84)):
            if runs[pid] and runs[pid][-1][-1] == n - 1:
                runs[pid][-1].append(n)
            else:
                runs[pid].append([n])
    for pid, blocks in runs.items():
        sizes = sorted({len(block) for block in blocks})
        starts = [block[0] for block in blocks]
        print(f"  worker {pid}: {len(blocks)} chunks of {sizes}, starting at {starts}")

    print("\n4. join() before close()")
    pool = multiprocessing.Pool(1)
    try:
        pool.join()
    except ValueError as exc:
        print(f"  ValueError: {exc}")
    pool.close()
    pool.join()
    print("  close() then join(): returned normally")

    print("\n5. The main process ends without join() while 6 tasks of 3 s are queued")
    output, ended, took = run_child(EXIT_WITHOUT_JOIN, timeout=30)
    for line in output.splitlines():
        print(f"  {line}")
    print(f"  process {ended} after {took:.1f} s")

    print("\n6. A task that only unpickles after main-block setup")
    variants = [("fork", "-"), ("forkserver", "-"), ("forkserver", "initializer")]
    for method, setup in variants:
        output, ended, took = run_child(
            UNPICKLABLE_IN_WORKER, method, setup, timeout=10
        )
        lines = output.splitlines()
        errors = sorted({line for line in lines if "Error:" in line})
        results = [line for line in lines if line.startswith("[")]
        label = f"{method} + initializer" if setup == "initializer" else method
        print(f"  {label:24} {ended} after {took:.1f} s; {results}; {errors}")


if __name__ == "__main__":
    main()
