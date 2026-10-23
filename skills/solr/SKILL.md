---
name: solr
description: >-
  How Apache Solr makes documents searchable and durable (soft vs hard commits, commitWithin, autoCommit, autoSoftCommit, openSearcher, the tlog, pysolr commit flags) and how it stores them (segments, merges, TieredMergePolicy, optimize, deleted documents, field definitions). Use when a document is not showing up in search or appears earlier or later than expected, when choosing commit settings, when a commitWithin test is flaky, when pysolr always_commit, commit, softCommit or commitWithin surprise, when changing a field's type, multiValued, docValues or indexed setting or removing a field, when an add fails with HTTP 400 "cannot change field ... to inconsistent index options", "inconsistent doc values type" or "cannot change DocValues type", when an atomic update fails with "unknown field", when only documents with one field fail to index, or when explaining segment counts, deletedDocs, maxDoc vs numDocs, merges, or whether optimize is safe.
---

# Solr

Verified against Solr [9.10.1](https://github.com/apache/solr/tree/releases/solr/9.10.1) (`CommitTracker.java`, `DirectUpdateHandler2.java`, `SolrConfig.java`, `SolrCore.java`), Lucene [9.12.3](https://github.com/apache/lucene/tree/releases/lucene/9.12.3) (`IndexWriter.java`, `IndexingChain.java`, `FieldInfos.java`, `SegmentMerger.java`, `TieredMergePolicy.java`) and pysolr [3.11.0](https://github.com/django-haystack/pysolr/tree/v3.11.0), with timings and errors measured on a live 9.10.1 node and schema-change errors also on Solr 8.11.4. Line-level source links are in the reference files.

## Rules

### Commits

01. There is one pending commit per tracker (soft, hard) per core, not per document. Every write proposes a deadline, the earliest wins, and all pending documents become visible together.
02. A document is findable only after a searcher opens: a soft commit, or a hard commit with `openSearcher=true`. `autoCommit` with `openSearcher=false` makes docs durable but never visible.
03. Only a hard commit fsyncs segments and truncates the transaction log. Without hard commits the tlog grows and a crash replays all of it before the node serves traffic.
04. `commitWithin=N` is a **maximum** delay, never a minimum. Any other write with an earlier deadline, any explicit `commit=true`, or a restart makes the doc visible sooner.
05. `commitWithin` **replaces** `autoSoftCommit maxTime` for that write; it is not capped by it. `commitWithin=20000` waits 20 s even with `autoSoftCommit=3000`.
06. `commitWithin` is one-shot. The next write without it falls back to `autoSoftCommit maxTime`.
07. With no `commitWithin` and `autoSoftCommit maxTime=-1`, a doc never becomes visible until something commits explicitly.
08. By default `commitWithin` drives the soft tracker and never affects `autoCommit` (`<commitWithin><softCommit>true`). On SolrCloud **TLOG replicas** Solr forces it onto the hard tracker and forces `openSearcher=true`.
09. `openSearcher=false` is ignored on a soft commit.
10. Commits are demand-driven. An idle core never commits; only adds, updates and **deletes** arm a tracker. Reads never do.
11. Scheduled commits land 15-35 ms late, never early. A test grace period under ~50 ms will flake.
12. After a crash, tlog replay ends in a commit: un-hard-committed docs survive and are visible immediately. Pending timers are lost.
13. pysolr `always_commit=True` overrides a per-call `commitWithin` or `softCommit` (precedence `commit` > `softCommit` > `commitWithin`). Pass `commit=False` on that call. pysolr `delete()` has no `commitWithin` argument.

### Segments

14. Treat a core as a list of immutable segments. Every flush (a commit with pending docs, or the buffer passing `ramBufferSizeMB`, 100 MB by default, even mid-request) writes a new one; Lucene never appends to an old one, and documents of every kind share them. One large request becomes several segments; sequential requests share one until a flush. A flushed segment joins the index only when a hard commit lists it in `segments_N`.
15. A document has exactly its segment's field-table fields or fewer, never a field the table lacks, and it lives whole in one segment: no copy and no field of it is in another. The only exception is an old version an update marked deleted, which waits in its old segment while the new, complete version is live in a new one.
16. A delete only marks the document in its segment's `.liv` file. The segment is dropped once all its documents are deleted; otherwise the bytes stay until a merge copies the live documents out. An update is a delete plus an add, even for an identical document (Solr never compares content), so `maxDoc - numDocs` grows with every reindex.
17. Merges happen only after flushes, commits, deletes or an explicit request, never on a timer; an idle index keeps its segments. A natural merge runs only when the index has more segments than its size-based budget (never below 10) or more deleted documents than `deletesPctAllowed` of the whole index (20% on Lucene 9, 33% on Lucene 8); no per-segment percentage triggers one. Segments over 2.5 GB stay out of natural merges unless both they and the index are over that percentage.
18. A merge copies only live documents, combines up to 10 similar-size segments (`maxMergeAtOnce`) totalling at most 5 GB (`maxMergedSegmentMB`), and produces about their live size; one check can start several merges in parallel, and merged segments merge again at the next level. Its field table is the union of its sources' tables, so a field definition survives even when no live document has the field.
19. Run `optimize` only on an index that has stopped changing: the request blocks until the merge ends, other hard commits wait for it (searches, plain adds and soft commits do not), and Lucene wants free disk of twice the index size. Natural merging keeps the segment count bounded without it.

### Changing a field's definition

20. Lucene allows one definition per field name in the whole index: index options, doc values type, points, and on indexes created by Lucene 9+ also norms and term vectors. The IndexWriter builds it from every segment when it opens; editing the schema never changes it.
21. Expect HTTP 400 `cannot change field "f" from index options=X to inconsistent index options=Y` (or `doc values type=`, `points ... numBytes=`) only for documents that carry a value for `f`. Documents without `f` still index.
22. A failing document stops its request: documents before it are written but the request's `commit=true` does not run, and it and every later document are lost. A batching client loses the rest of that batch.
23. Check the strictness first: on an index created by Lucene 8, turning `docValues` on or `indexed` off is accepted silently; on Lucene 9+ both fail. `scripts/field_segments.py` prints which.
24. Analyzer-only changes never fail, but old documents keep the old tokens and stop matching. Reindex every document after any analysis change.
25. To change a field without downtime, add a new field name, reindex into it, switch reads, then drop the old field. Or build a new core or collection and swap it in.
26. To keep the same name, delete every document in every segment that lists the old definition, not only the documents that use the field: other documents in those segments keep the segment and its definition alive. In practice that means all documents. Confirm with `scripts/field_segments.py` that no segment lists the field, RELOAD the core or collection, then reindex.
27. Do not rely on `delete *:*` to reset definitions: with an update log Solr runs it as an ordinary delete, not `IndexWriter.deleteAll()`. RELOAD after it.
28. Do not use `optimize` or `expungeDeletes` to remove an old definition; merges copy it into the new segment.
29. Keep a removed stored field's definition in the schema until every document has been fully re-sent without it. Atomic updates rebuild documents from stored values, including fields the schema dropped, and fail with `unknown field 'f'` until then.

## Debug procedure

### A document is not visible

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

### An add fails with `cannot change field ... inconsistent ...`

1. Read the field name and the two definitions from the error: "from" is what the index holds, "to inconsistent" is what the current schema produces.
2. List the segments that define the field, from the skill directory:
   `uv run scripts/field_segments.py http://localhost:8983/solr/<core> <field>`
   Look at `index options`, `doc values` and `points` per segment, and at the strict or lenient line.
3. If the script finds no segment with the field but adds still fail, the IndexWriter holds the old definition in memory: `curl -s 'http://localhost:8983/solr/admin/cores?action=RELOAD&core=<core>'` and retry.
4. Otherwise choose a fix (rules 25-26). For the same name, delete, rerun step 2 until no segment lists the old definition, RELOAD, then reindex.
5. Verify: add one document with the field, then rerun step 2; the newest segment must show the new definition.

## Choosing settings

| Goal                                      | Setting                                                                     |
| ----------------------------------------- | --------------------------------------------------------------------------- |
| Bounded visibility delay for all writes   | `autoSoftCommit maxTime` (for example 1000-5000 ms)                         |
| Bounded tlog size and crash replay time   | `autoCommit maxTime` with `openSearcher=false` (for example 15000-60000 ms) |
| One write visible sooner than the default | `commitWithin` on that request                                              |
| Visible immediately, at a cost            | `softCommit=true` on the request; avoid per-document in bulk loads          |

## References

- `references/commits.md`: the full commit model with measured timings, quoted Solr and pysolr source with line links, crash behaviour, and how to write non-flaky `commitWithin` tests. Read it when the commit rules do not explain an observed timing, or when you need to cite the source.
- `references/segments.md`: how a core is split into segments, with diagrams of flush, delete, merge and the field table; commit points and `.liv` generations; when merges run and how `TieredMergePolicy` budgets and scores them (Solr 9 and 8 defaults); merged sizes and `optimize` costs, measured; the two document invariants (fields never exceed the table, one document never spans segments); how to read `/admin/segments` flags; and a Docker walkthrough. Read it to explain segment counts, deleted documents, index growth, merge behaviour, or why a field definition outlives its documents.
- `references/field-type-changes.md`: every schema change that fails with its exact Solr 9 and Solr 8 message, why deleting documents, merging and `delete *:*` do not clear the old definition, how RELOAD does, why removing a stored field breaks atomic updates, a fix decision chart, and a Docker reproduction. Read it before changing or removing an indexed field, or when an add fails with `cannot change field` or an atomic update with `unknown field`.
- `scripts/field_segments.py`: lists a core's segments, or per segment how a field is defined, decoded into the terms Lucene's errors use. Run it with `uv run scripts/field_segments.py <core-url> [field ...]`.
