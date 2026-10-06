# Solr Commits: Soft vs Hard, `commitWithin`, `autoCommit`

How Solr decides when an indexed document becomes searchable and durable. Every number here was measured against Solr 9.10.1 with this update handler config, close to the default configset:

```xml
<autoCommit>
  <maxTime>15000</maxTime>
  <openSearcher>false</openSearcher>
</autoCommit>
<autoSoftCommit>
  <maxTime>3000</maxTime>
</autoSoftCommit>
```

Source verified against tag [`releases/solr/9.10.1`](https://github.com/apache/solr/tree/releases/solr/9.10.1) (the files quoted are identical in 9.10.0) and [pysolr 3.11.0](https://github.com/django-haystack/pysolr/tree/v3.11.0). Every "Source:" link points at the commit those tags name, with line numbers.

______________________________________________________________________

## Table of Contents

01. [The Single Idea Everything Follows From](#1-the-single-idea-everything-follows-from)
02. [Three States of a Document](#2-three-states-of-a-document)
03. [Soft vs Hard Commit](#3-soft-vs-hard-commit)
04. [Request Parameters vs Config Settings](#4-request-parameters-vs-config-settings)
05. [What `commitWithin` Actually Means](#5-what-commitwithin-actually-means)
06. [Commits Are Demand-Driven, Not a Ticker](#6-commits-are-demand-driven-not-a-ticker)
07. [Crash Behaviour](#7-crash-behaviour)
08. [pysolr Specifics](#8-pysolr-specifics)
09. [Testing `commitWithin` Without Flakiness](#9-testing-commitwithin-without-flakiness)
10. [Quick Reference](#10-quick-reference)

______________________________________________________________________

## 1. The Single Idea Everything Follows From

**Solr never commits one document. There is one pending commit per tracker for the whole core, and every write proposes a time for it. The earliest proposal wins.**

When it fires, *every* pending document becomes visible at once. There is no way to make doc B visible without also making doc A visible.

______________________________________________________________________

## 2. Three States of a Document

| State    | What happened                                                   | Survives a crash?        | Searchable? |
| -------- | --------------------------------------------------------------- | ------------------------ | ----------- |
| accepted | written to the transaction log (tlog), `add()` returned         | yes, replayed on startup | no          |
| indexed  | analysed, written into index segments, flushed by a hard commit | yes                      | **no**      |
| visible  | a new searcher has been opened                                  | yes                      | yes         |

Writing a document and making it findable are separate steps. Opening a searcher is what makes it findable.

______________________________________________________________________

## 3. Soft vs Hard Commit

|                                        | Soft commit           | Hard commit                 |
| -------------------------------------- | --------------------- | --------------------------- |
| Opens a searcher (makes docs findable) | always                | only if `openSearcher=true` |
| fsyncs segments to disk                | no                    | yes                         |
| Truncates the transaction log          | no                    | yes                         |
| After a crash                          | tlog must be replayed | nothing to replay           |

Measured on 20,000 docs: the tlog stayed at 11 files after `softCommit=true` and dropped to 1 file after `commit=true`.

Tlog length is startup cost. A node that crashes with a huge tlog replays every update before serving traffic, which is why the shipped config comments say to keep some hard `autoCommit` "to limit the log size".

______________________________________________________________________

## 4. Request Parameters vs Config Settings

Per-request instructions, sent on `/update`:

| Param             | Effect                                                                  |
| ----------------- | ----------------------------------------------------------------------- |
| `commit=true`     | hard commit now, `openSearcher=true` by default                         |
| `softCommit=true` | soft commit now                                                         |
| `commitWithin=N`  | schedule a commit within N ms (a soft commit by default, see section 5) |

Standing policies live in `solrconfig.xml` and are never request params. The config at the top reads as: *every 15 s flush to disk, every 3 s refresh what searches can see.*

| Config                             | Equivalent request                                              |
| ---------------------------------- | --------------------------------------------------------------- |
| `autoSoftCommit`                   | `/update?softCommit=true`, deferred by `maxTime`                |
| `autoCommit` (as configured above) | `/update?commit=true&openSearcher=false`, deferred by `maxTime` |

`autoCommit` is **not** the same as plain `commit=true`: with `openSearcher=false` it changes nothing about visibility.

### `openSearcher=false` is ignored on a soft commit

Opening a searcher is the only thing a soft commit does, so the soft branch opens one unconditionally; only the hard branch checks `cmd.openSearcher`. Verified: `softCommit=true&openSearcher=false` still made the doc visible.

Source: [`DirectUpdateHandler2.java#L797-L809`](https://github.com/apache/solr/blob/c135e6335c7158fa26e96b0dc386f825255b47c0/solr/core/src/java/org/apache/solr/update/DirectUpdateHandler2.java#L797-L809)

```java
if (cmd.softCommit) {
  synchronized (solrCoreState.getUpdateLock()) {
    if (ulog != null) ulog.preSoftCommit(cmd);
    core.getSearcher(true, false, waitSearcher, true);   // no openSearcher check
    ...
} else {
  synchronized (solrCoreState.getUpdateLock()) {
    if (ulog != null) ulog.preSoftCommit(cmd);
    if (cmd.openSearcher) {
      core.getSearcher(true, false, waitSearcher);
```

### Changing the standing policy

- System property, because the default config uses `${solr.autoSoftCommit.maxTime:3000}`: `-Dsolr.autoSoftCommit.maxTime=6000`
- Config API at runtime:
  `POST /solr/<core>/config {"set-property": {"updateHandler.autoSoftCommit.maxTime": -1}}`

______________________________________________________________________

## 5. What `commitWithin` Actually Means

**A deadline: "commit no later than N ms from now."** A maximum, never a minimum. Solr's own internal name for the value is `commitMaxTime`.

Source: [`CommitTracker.java#L128-L134`](https://github.com/apache/solr/blob/c135e6335c7158fa26e96b0dc386f825255b47c0/solr/core/src/java/org/apache/solr/update/CommitTracker.java#L128-L134)

```java
private void _scheduleCommitWithinIfNeeded(long commitWithin) {
  long ctime = (commitWithin > 0) ? commitWithin : timeUpperBound;

  if (ctime > 0) {
    _scheduleCommitWithin(ctime);
  }
}
```

- You passed `commitWithin`: use it.
- You didn't: use the tracker's `maxTime` (`autoSoftCommit` 3000 here).
- Neither exists: nothing is scheduled, and **no soft commit ever happens**.

Measured:

```
commitWithin=40000, autoSoftCommit=3000   ->  visible at 40061 ms
no commitWithin,    autoSoftCommit=3000   ->  visible at  3036 ms
no commitWithin,    autoSoftCommit=-1     ->  never visible (30 s, then only an explicit commit)
```

It **replaces** `autoSoftCommit`; it is not capped by it. A 20-second `commitWithin` is honoured despite a 3-second `autoSoftCommit`:

```
commitWithin= 1000  ->  1015 ms
commitWithin= 9000  ->  9031 ms
commitWithin=10000  -> 10033 ms
commitWithin=15000  -> 15033 ms
commitWithin=20000  -> 20019 ms
```

It always lands 15-35 ms **late**, never early: scheduler plus searcher-open overhead. With one writer that makes it look like an exact delay, which is misleading.

### Which tracker receives it

Each add arms both trackers. `commitWithin` goes to exactly one of them, chosen by `commitWithinSoftCommit`:

Source: [`DirectUpdateHandler2.java#L395-L402`](https://github.com/apache/solr/blob/c135e6335c7158fa26e96b0dc386f825255b47c0/solr/core/src/java/org/apache/solr/update/DirectUpdateHandler2.java#L395-L402), deletes: [`#L488-L494`](https://github.com/apache/solr/blob/c135e6335c7158fa26e96b0dc386f825255b47c0/solr/core/src/java/org/apache/solr/update/DirectUpdateHandler2.java#L488-L494)

```java
if ((cmd.getFlags() & UpdateCommand.IGNORE_AUTOCOMMIT) == 0) {
  if (commitWithinSoftCommit) {
    commitTracker.addedDocument(-1, this::getCurrentTLogSize);   // hard: always its own maxTime
    softCommitTracker.addedDocument(cmd.commitWithin);           // soft: your N, else its maxTime
  } else {
    softCommitTracker.addedDocument(-1);
    commitTracker.addedDocument(cmd.commitWithin, this::getCurrentTLogSize);
  }
}
```

`commitWithinSoftCommit` comes from `<updateHandler><commitWithin><softCommit>` and defaults to `true` ([`SolrConfig.java#L870-L871`](https://github.com/apache/solr/blob/c135e6335c7158fa26e96b0dc386f825255b47c0/solr/core/src/java/org/apache/solr/core/SolrConfig.java#L870-L871)). So by default `commitWithin` never touches `autoCommit`.

**Exception: SolrCloud TLOG replicas.** On a TLOG replica Solr forces `commitWithinSoftCommit = false` and `openSearcher = true` on the hard tracker, so `commitWithin` there becomes a hard commit that opens a searcher.

Source: [`DirectUpdateHandler2.java#L152-L159`](https://github.com/apache/solr/blob/c135e6335c7158fa26e96b0dc386f825255b47c0/solr/core/src/java/org/apache/solr/update/DirectUpdateHandler2.java#L152-L159)

### It is one-shot, not a mode

```
1st add, commitWithin=10000                    visible at  10179 ms
2nd add, NO commitWithin (after the 10 s one)  visible at   3051 ms
3rd add, NO commitWithin                       visible at   3018 ms
```

`commitWithin` reads `timeUpperBound` and never writes it. The field is set when the tracker is constructed ([`CommitTracker.java#L91`](https://github.com/apache/solr/blob/c135e6335c7158fa26e96b0dc386f825255b47c0/solr/core/src/java/org/apache/solr/update/CommitTracker.java#L91)) and otherwise only by a test-only setter ([`#L324-L327`](https://github.com/apache/solr/blob/c135e6335c7158fa26e96b0dc386f825255b47c0/solr/core/src/java/org/apache/solr/update/CommitTracker.java#L324-L327)).

### Anything can cut it short

Source: [`CommitTracker.java#L136-L165`](https://github.com/apache/solr/blob/c135e6335c7158fa26e96b0dc386f825255b47c0/solr/core/src/java/org/apache/solr/update/CommitTracker.java#L136-L165)

```java
// simplified
if (pending != null && pending.getDelay(TimeUnit.MILLISECONDS) <= commitMaxTime) {
  return;                                       // existing one is sooner, leave it
}
if (pending != null) { pending.cancel(false); } // ours is sooner, replace it
pending = scheduler.schedule(this, commitMaxTime, TimeUnit.MILLISECONDS);
```

Measured, doc with `commitWithin=70000`:

| What else happened                       | Doc became visible at            |
| ---------------------------------------- | -------------------------------- |
| nothing                                  | still invisible at 30 s          |
| 2nd doc at t=2 s with no `commitWithin`  | 5038 ms (2 s + the 3 s fallback) |
| 2nd doc at t=2 s with `commitWithin=500` | 2528 ms                          |

A doc with `commitWithin=20000` followed by a plain add at t=1 s became visible at 4030 ms, not 20000 ms.

Also cut short by an explicit `commit=true` from any client (a 1-hour `commitWithin` became searchable in 180 ms), and by a restart (section 7).

### The superseded task does not fire again

Doc A `commitWithin=30000`, doc B plain at t=1 s. The `softAutoCommits` counter went 1 to 2 and stayed at 2 through t=36 s: one commit, not two. The old `ScheduledFuture` is cancelled, and `run()` sets `pending = null` without rescheduling.

Source: [`CommitTracker.java#L256-L260`](https://github.com/apache/solr/blob/c135e6335c7158fa26e96b0dc386f825255b47c0/solr/core/src/java/org/apache/solr/update/CommitTracker.java#L256-L260)

______________________________________________________________________

## 6. Commits Are Demand-Driven, Not a Ticker

`autoSoftCommit maxTime=3000` does **not** mean "commit every 3 seconds". It means "a pending change may sit at most 3 seconds". An idle core never commits.

```
start:                  softAutoCommits=0  autoCommits=0
after 20 s idle:        softAutoCommits=0  autoCommits=0     <- zero, not 6
--- added ONE doc ---
5 s after the add:      softAutoCommits=1  autoCommits=0     <- soft fired at 3 s
after 20 s more idle:   softAutoCommits=1  autoCommits=1     <- hard fired at 15 s
```

Only writes arm a tracker, through exactly two entry points:

Source: [`CommitTracker.java#L174-L190`](https://github.com/apache/solr/blob/c135e6335c7158fa26e96b0dc386f825255b47c0/solr/core/src/java/org/apache/solr/update/CommitTracker.java#L174-L190), [`#L213-L215`](https://github.com/apache/solr/blob/c135e6335c7158fa26e96b0dc386f825255b47c0/solr/core/src/java/org/apache/solr/update/CommitTracker.java#L213-L215)

```java
public void addedDocument(int commitWithin, LongSupplier currentTlogSize)
public void deletedDocument(int commitWithin)
```

So adds, updates and deletes. **Deletes count.** 60 read requests across `/select`, faceting, `/admin/ping` and `/admin/luke` moved the counters by zero.

Counters ([`DirectUpdateHandler2.java#L220-L228`](https://github.com/apache/solr/blob/c135e6335c7158fa26e96b0dc386f825255b47c0/solr/core/src/java/org/apache/solr/update/DirectUpdateHandler2.java#L220-L228)):

```
/solr/<core>/admin/mbeans?stats=true&cat=UPDATE&wt=json
  UPDATE.updateHandler.softAutoCommits
  UPDATE.updateHandler.autoCommits
  UPDATE.updateHandler.commits.count
```

______________________________________________________________________

## 7. Crash Behaviour

`SIGKILL` at t=1 s, before the 15 s hard commit, with `commitWithin=40000` pending:

```
t=1 s, before restart:     numFound = 0
after SIGKILL + restart:   numFound = 1
```

The doc survived (tlog) **and became visible immediately**, not at 40 s. Log replay ends in a commit, and the pending timer only ever existed in memory.

______________________________________________________________________

## 8. pysolr Specifics

`always_commit` is a **pysolr-only** constructor flag, never sent to Solr. It only supplies a default for `commit`.

Source: [`pysolr.py#L459-L469`](https://github.com/django-haystack/pysolr/blob/9243537cb34c69c4325c61f70732b64a4a1a8f7e/pysolr.py#L459-L469)

```python
if commit is None:
    commit = self.always_commit

if min_rf:
    query_vars.append("min_rf=%i" % min_rf)
if commit:
    query_vars.append("commit=%s" % str(bool(commit)).lower())
elif softCommit:
    query_vars.append("softCommit=%s" % str(bool(softCommit)).lower())
elif commitWithin is not None:
    query_vars.append("commitWithin=%s" % str(int(commitWithin)))
```

**Gotcha:** the `elif` chain gives precedence `commit` > `softCommit` > `commitWithin`, so an instance-level `always_commit=True` silently discards a per-call `commitWithin` or `softCommit`:

```
always_commit=True   add(commitWithin=1000)               -> update?commit=true
always_commit=False  add(commitWithin=1000)               -> update?commitWithin=1000
always_commit=True   add(commitWithin=1000, commit=False) -> update?commitWithin=1000
always_commit=True   add(softCommit=True)                 -> update?commit=true
```

The fix is to pass `commit=False` explicitly on the call that should use `commitWithin` or `softCommit`.

Defaults: `always_commit=False`, and `add(commit=None)`, so a bare `add()` sends plain `update` with no commit parameter at all ([`#L920-L929`](https://github.com/django-haystack/pysolr/blob/9243537cb34c69c4325c61f70732b64a4a1a8f7e/pysolr.py#L920-L929)).

`delete()` has no `commitWithin` parameter ([`#L997-L1003`](https://github.com/django-haystack/pysolr/blob/9243537cb34c69c4325c61f70732b64a4a1a8f7e/pysolr.py#L997-L1003)). A pysolr delete becomes visible through `commit`, `softCommit`, the `autoSoftCommit` fallback, or another write's commit.

______________________________________________________________________

## 9. Testing `commitWithin` Without Flakiness

A typical test makes two assertions that pull in opposite directions:

```python
solr.add([doc], commitWithin=commit_within_ms)
assert len(solr.search(q)) == 0                 # not visible yet
time.sleep(commit_within_ms / 1000 + grace)
assert len(solr.search(q)) == 1                 # visible now
```

The first assertion needs `commitWithin` to be **long** enough, and the second needs `grace` to be **long** enough. Fixing only one leaves the test flaky:

| `commitWithin` / grace | Condition | Runs | Failures              |
| ---------------------- | --------- | ---- | --------------------- |
| 50 ms / 0.01 s         | 0.05 CPU  | 15   | 13                    |
| 50 ms / 0.01 s         | normal    | 50   | 0                     |
| 1000 ms / 0.01 s       | normal    | 100  | **7** (all on `== 1`) |
| 1000 ms / 0.5 s        | normal    | 195  | 0                     |
| 1000 ms / 0.5 s        | 0.05 CPU  | 15   | 0                     |

The 7-in-100 row is the systematic one: commits land 15-35 ms late, so a 10 ms grace misses.

The first assertion tests something **Solr never promises**. There is no guaranteed minimum delay; the doc stays hidden only because nothing else writes to the core during the test. Any parallel test writing to the same core, or any client sending `commit=true`, makes it fail.

Recommendations (the first is measured above, the rest follow from section 5):

- Use `commitWithin` of at least 1000 ms and a grace of at least 0.5 s.
- Give `commitWithin` tests their own core, or run them serially.
- Prefer polling until visible with a timeout over a single fixed sleep.
- Treat "not visible yet" as best effort; drop that assertion if the core is shared.

______________________________________________________________________

## 10. Quick Reference

| Rule                                   | Detail                                                                                            |
| -------------------------------------- | ------------------------------------------------------------------------------------------------- |
| One commit per tracker per core        | Every write proposes a time; earliest wins; all pending docs become visible together              |
| Visibility                             | Only a searcher open makes docs findable: soft commit, or hard commit with `openSearcher=true`    |
| Durability                             | Only a hard commit fsyncs and truncates the tlog                                                  |
| `commitWithin`                         | A maximum delay, one-shot, replaces (not capped by) `autoSoftCommit maxTime`, lands 15-35 ms late |
| No `commitWithin`, `autoSoftCommit=-1` | Never visible until an explicit commit                                                            |
| `commitWithin` and `autoCommit`        | Independent by default; on TLOG replicas `commitWithin` drives the hard tracker                   |
| `openSearcher=false` on a soft commit  | Ignored                                                                                           |
| Idle core                              | Never commits; only adds, updates and deletes arm a tracker                                       |
| Restart                                | Tlog replay ends in a commit, pending timers are lost, docs visible immediately                   |
| pysolr `always_commit=True`            | Overrides per-call `commitWithin` and `softCommit`; pass `commit=False`                           |
