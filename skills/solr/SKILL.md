---
name: solr
description: When Apache Solr makes indexed documents searchable and durable: soft vs hard commits, commitWithin, autoCommit, autoSoftCommit, openSearcher, the transaction log, and pysolr's commit flags. Use when a document added to Solr is not showing up in search, shows up earlier or later than expected, when choosing commit settings in solrconfig.xml, when a test that waits for commitWithin is flaky, or when pysolr always_commit, commit, softCommit or commitWithin behave unexpectedly.
---

# Solr commits

Verified against Solr [9.10.1](https://github.com/apache/solr/tree/releases/solr/9.10.1) (`CommitTracker.java`, `DirectUpdateHandler2.java`, `SolrConfig.java`) and pysolr [3.11.0](https://github.com/django-haystack/pysolr/tree/v3.11.0), with timings measured on a live 9.10.1 node. Line-level source links are in `references/commits.md`.

## Rules

1. There is one pending commit per tracker (soft, hard) per core, not per document. Every write proposes a deadline, the earliest wins, and all pending documents become visible together.
2. A document is findable only after a searcher opens: a soft commit, or a hard commit with `openSearcher=true`. `autoCommit` with `openSearcher=false` makes docs durable but never visible.
3. Only a hard commit fsyncs segments and truncates the transaction log. Without hard commits the tlog grows and a crash replays all of it before the node serves traffic.
4. `commitWithin=N` is a **maximum** delay, never a minimum. Any other write with an earlier deadline, any explicit `commit=true`, or a restart makes the doc visible sooner.
5. `commitWithin` **replaces** `autoSoftCommit maxTime` for that write; it is not capped by it. `commitWithin=20000` waits 20 s even with `autoSoftCommit=3000`.
6. `commitWithin` is one-shot. The next write without it falls back to `autoSoftCommit maxTime`.
7. With no `commitWithin` and `autoSoftCommit maxTime=-1`, a doc never becomes visible until something commits explicitly.
8. By default `commitWithin` drives the soft tracker and never affects `autoCommit` (`<commitWithin><softCommit>true`). On SolrCloud **TLOG replicas** Solr forces it onto the hard tracker and forces `openSearcher=true`.
9. `openSearcher=false` is ignored on a soft commit.
10. Commits are demand-driven. An idle core never commits; only adds, updates and **deletes** arm a tracker. Reads never do.
11. Scheduled commits land 15-35 ms late, never early. A test grace period under ~50 ms will flake.
12. After a crash, tlog replay ends in a commit: un-hard-committed docs survive and are visible immediately. Pending timers are lost.
13. pysolr `always_commit=True` overrides a per-call `commitWithin` or `softCommit` (precedence `commit` > `softCommit` > `commitWithin`). Pass `commit=False` on that call. pysolr `delete()` has no `commitWithin` argument.

## Debug procedure

1. Read the effective commit config:
   `curl -s 'http://localhost:8983/solr/<core>/config/updateHandler?wt=json'`
   Note `autoCommit.maxTime`, `autoCommit.openSearcher`, `autoSoftCommit.maxTime`, `commitWithin.softCommit`.
2. Check the request actually sent. With pysolr, log the URL or check `always_commit`; a `commit=true` in the query string means `commitWithin` was dropped.
3. Watch the commit counters before and after the write:
   `curl -s 'http://localhost:8983/solr/<core>/admin/mbeans?stats=true&cat=UPDATE&wt=json'`
   and compare `softAutoCommits`, `autoCommits`, `commits.count`. No change means nothing armed a tracker.
4. If the doc is still invisible, force visibility to separate indexing problems from commit timing:
   `curl 'http://localhost:8983/solr/<core>/update?softCommit=true'`
   If it appears now, the issue is commit timing; if not, the doc was never indexed or the query does not match it.
5. Check for other writers to the same core; they shorten every pending `commitWithin` (rule 4).

## Choosing settings

| Goal | Setting |
|---|---|
| Bounded visibility delay for all writes | `autoSoftCommit maxTime` (for example 1000-5000 ms) |
| Bounded tlog size and crash replay time | `autoCommit maxTime` with `openSearcher=false` (for example 15000-60000 ms) |
| One write visible sooner than the default | `commitWithin` on that request |
| Visible immediately, at a cost | `softCommit=true` on the request; avoid per-document in bulk loads |

## References

- `references/commits.md`: the full model with measured timings, quoted Solr and pysolr source with line links, crash behaviour, and how to write non-flaky `commitWithin` tests. Read it when the rules above do not explain an observed timing, or when you need to cite the source.
