---
name: django-haystack
description: How django-haystack and drf-haystack fetch search results and build API responses, and how update_index --workers runs. Covers serializer fields (Meta.fields, ignore_fields, declared fields, _exact facet fields), values from the search document vs SearchResult.object, load_all, the searches and SQL each read costs (len, slices, for r in sqs, HAYSTACK_ITERATOR_LOAD_PER_QUERY), and rows deleted but still indexed. Use when a Meta.fields entry is missing from a HaystackSerializer response, a declared field is always null, a search runs one SQL query per result or too many Solr requests, sqs[n:] returns too few results, on "Got AttributeError when attempting to get a value for field" or "Object could not be found in database for SearchResult", when update_index -k hangs on Python 3.14 (AppRegistryNotReady), fails with "'_thread._local' object has no attribute" or with "server closed the connection unexpectedly" on --remove, or when choosing which model fields to index.
---

# django-haystack and drf-haystack: reading results, API responses and update_index workers

Verified against drf-haystack [1.9.3](https://github.com/django-commons/drf-haystack/tree/v1.9.3) (`serializers.py`, `generics.py`, `fields.py`; yanked on PyPI, same logic in 1.9.1), django-haystack [3.4.0](https://github.com/django-haystack/django-haystack/tree/v3.4.0) (`models.py`, `query.py`, `indexes.py`, `backends/solr_backend.py`) and Django REST framework [3.18.1](https://github.com/encode/django-rest-framework/tree/3.18.1) (`fields.py`, `serializers.py`) source, and by running `scripts/trace_response.py`, `scripts/count_queries.py` and `scripts/check_update_index_workers.py` on Django 5.2.17, Whoosh 2.7.4, Solr 9.10.1 (pysolr 3.11.0), PostgreSQL 16 (psycopg2 2.9.13) and Python 3.15.0, with the start-method default also run on Python 3.13.16 and 3.14.8. Line-level source links are in `references/`.

## Serializer field rules

1. A `HaystackSerializer`'s fields are the fields of its `Meta.index_classes`, filtered by `Meta.ignore_fields`, then `Meta.exclude` or `Meta.fields`, plus every field declared on the serializer. Index fields are the class-level `indexes.*Field(...)` attributes of the index class, listed in `MyIndex.fields`.
2. A name in `Meta.fields` that is not an index field is dropped with no error and no key in the output: database columns, typos and invented names alike. To return a database column, declare it (rule 9).
3. Also inheriting `ModelSerializer` does not bring back its `Field name ... is not valid for model` check, because `HaystackSerializer.get_fields()` replaces `ModelSerializer`'s.
4. An unset or falsy `Meta.fields` (`None`, `""`, `False`, `[]`) includes every index field not in `ignore_fields` or `exclude`. Always use a list or tuple: a string is matched by substring, so `fields = "username"` also includes an index field called `name`.
5. Every `faceted=True` index field adds a shadow `<name>_exact` index field, which appears in the response unless `Meta.fields` lists the fields or `ignore_fields` names it.
6. Only class-level declarations create index fields. A `prepare_<name>()` method without a matching field never runs, and extra keys added in an overridden `prepare()` reach the search engine but never the serializer.
7. Declared serializer fields are always included and replace an index field with the same name.

## Where each value comes from

08. Index fields are read from the search document the engine returned, not from the database, even with `load_all`. They show the value from the last `update_index`.
09. A declared field with a plain `source` also reads the search result, and `SearchResult` returns `None` for any attribute the hit lacks, so a database-only column gives `"key": null` with no error. Read database columns through the row: `CharField(source="object.phone")`, or `instance.object.phone` in a `SerializerMethodField`.
10. To serialize the database row with an existing `ModelSerializer`, put `HaystackSerializerMixin` before it in the bases; it serializes `result.object` instead of the search result.

## Searches and SQL per request

11. A paginated list request runs two searches: a count limited to one row, then the page. The database is queried only to load rows for `.object`.
12. drf-haystack views default to `load_all = False`, so each `.object` read is its own query (`read_queryset().get(pk=...)`), one per result per page. Set `load_all = True` on the view to load each page with one `in_bulk()` query per model. The `retrieve` action never applies `load_all`; it reads one row.
13. Both paths load rows through the index's `read_queryset()`. Override it with `select_related()` for every relation the serializer reads. It defaults to `index_queryset()`, and `update_index` uses `index_queryset()`, so overriding `read_queryset()` leaves indexing unchanged.

## Rows deleted from the database but still indexed

14. With `load_all`, such a hit is dropped silently and Haystack runs more searches and SQL to refill the page, but the page's `count` still includes the deleted row.
15. Without `load_all`, `.object` is `None` and Haystack logs `Object could not be found in database for SearchResult ...`. A declared `source="object.x"` field then raises `AttributeError: Got AttributeError when attempting to get a value for field ...`, a `read_only=True` one is silently left out, and a method field reading `instance.object.x` raises `AttributeError: 'NoneType' object has no attribute 'x'`.
16. Keep deletes out of the index with `update_index --remove`, or `HAYSTACK_SIGNAL_PROCESSOR = "haystack.signals.RealtimeSignalProcessor"`, which removes a document on `post_delete`.

## Choosing index fields

17. Index a model column only when the search engine has to use it: full-text search (through the document field's template), filtering, sorting, faceting, or returning it without a database query. With `load_all = True`, read display-only columns through `.object` instead: the index stays smaller, `update_index` is faster, and the value is never stale.
18. `stored=False` and `indexed=False` are requests the backend's schema may ignore. Haystack's Whoosh backend indexes and stores every `CharField` whatever the flags say. Check what a hit really carries with `result.get_additional_fields()`.
19. Before removing a field from an index, search the serializers for its name in `Meta.fields`, `Meta.exclude` and `source="..."`. Each of those silently becomes a missing key or `null`; switch them to `source="object.<column>"`.

## Reading a SearchQuerySet

20. A `SearchQuerySet` sends nothing until it is read: building it, `.all()`, `.filter()` and `.load_all()` cost no request. `len()`, `count()` and `if sqs:` send one request with `rows=1`, `sqs[i]` one with `rows=1`, and `sqs[a:b]` one with `rows=b-a`.
21. `for r in sqs` fetches `HAYSTACK_ITERATOR_LOAD_PER_QUERY` results per request, 10 by default, so 100 hits take 10 requests. `list(sqs)` sends one more, a count, because `list()` calls `len()` first. Set the value in `settings.py`: Haystack reads it once, at import.
22. With `load_all()`, every search response costs one SQL query per model; without it, every `.object` read costs one. The chunk size therefore sets both the request count and the SQL count of a loop.
23. Always give a slice an end. `sqs[50:]` sends no `rows`: on Solr it returned 10 of the 50 remaining hits (Solr's default `rows`), and on Whoosh every hit from the first.
24. `HAYSTACK_SEARCH_RESULTS_PER_PAGE` sizes only Haystack's own views. A drf-haystack page has the size of DRF's pagination class.
25. To read every hit, as in an export, loop over explicit slices (`sqs.load_all()[i:i + 500]`) or raise `HAYSTACK_ITERATOR_LOAD_PER_QUERY`: each block is then one request and one SQL query per model.

## update_index --workers

26. On Linux with Python 3.14 or later, `update_index -k N` hangs after `Indexing ...` while workers keep dying with `AppRegistryNotReady: Apps aren't loaded yet.`: the `forkserver` default start method skips `manage.py`'s setup. Call `multiprocessing.set_start_method("fork")` in `manage.py` before `execute_from_command_line()`, or use `-k 0`.
27. In 3.4.0, a `DATABASES` alias the parent process never used makes every worker fail with `AttributeError: '_thread._local' object has no attribute '<alias>'` (Django 4.1 and later, issue #2086). Touching the aliases first stops helping as soon as a worker runs a second batch. Index with `-k 0`, or with a settings module whose `DATABASES` lists only the aliases the indexed models use.
28. Each worker runs `close_old_connections()` on the connection it inherited from the parent. With the default `CONN_MAX_AGE = 0` that ends the parent's PostgreSQL session, so `--remove` with `-k` fails with `OperationalError: server closed the connection unexpectedly`. Use `CONN_MAX_AGE = None` for the run, or `--remove` with `-k 0`.
29. `update_index` makes one task per `-b` batch and `pool.map()` hands them out in chunks of `ceil(batches / (workers * 4))`. Each batch closes the previous batch's connection and opens a new one: one connection per worker at a time, about one per batch over the run.
30. `--nocommit` sends no commit at all, not even at the end; the documents appear only when Solr's `autoCommit` or `autoSoftCommit`, or another commit, runs.

## Debug procedure

Work through these in order and stop when the cause is clear. Replace `myapp`, `MySerializer`, `MyIndex` and `MyModel` with your names.

1. List the fields the serializer really has:
   `python manage.py shell -c "from myapp.serializers import MySerializer; print(list(MySerializer().fields))"`
   A name from `Meta.fields` that is missing here is not an index field (rule 2).
2. List the index fields:
   `python manage.py shell -c "from myapp.search_indexes import MyIndex; print(list(MyIndex.fields))"`
3. Print what the search engine returned for one hit:
   `python manage.py shell -c "from haystack.query import SearchQuerySet; from myapp.models import MyModel; print(SearchQuerySet().models(MyModel)[0].get_additional_fields())"`
   A value that differs from the database row means the index is stale; run `update_index`. A field missing here is `null` in the response (rules 8 and 18).
4. For a `null` value, check the field's `source`: it must name a field from step 3 or start with `object.` (rule 9).
5. For too many SQL queries, check `load_all` on the view and `read_queryset()` on the index (rules 12 and 13). With `load_all = True`, a page needs one query per model.
6. Run `uv run scripts/trace_response.py` to see the serializer and `load_all` cases reproduced against a throwaway Whoosh index, with the searches and SQL counted per page.
7. For too many search requests, compare the read pattern with rules 20 to 25, and run `uv run scripts/count_queries.py` (add `--solr-url <core>` for a disposable Solr core) to see each pattern's requests and SQL.
8. For `update_index -k` problems, print the start method in the project's Python: `python -c "import multiprocessing; print(multiprocessing.get_start_method())"` (rule 26), then run `uv run scripts/check_update_index_workers.py`, adding `--postgres <url>` for the PostgreSQL cases (rules 27 and 28).

## Scripts

None of them needs a Django project; each builds what it uses in a temporary directory.

- `scripts/trace_response.py` indexes 25 `auth.User` rows into Whoosh and prints, for each serializer setup, the resolved fields and the output, plus the searches and SQL of a paginated `HaystackViewSet` with and without `load_all` and after a row is deleted.
- `scripts/count_queries.py` indexes 100 rows and prints the search requests and SQL queries of `len()`, `if sqs:`, single items, slices, open-ended slices, loops with `HAYSTACK_ITERATOR_LOAD_PER_QUERY` at 10, 50 and 200, `list()` and a drf-haystack page, each with and without `load_all()`. Uses Whoosh, or a Solr core with `--solr-url`.
- `scripts/check_update_index_workers.py` runs `update_index -k 2` on 40 rows under the default start method, with `fork` forced, and with an unused `DATABASES` alias, plus the PostgreSQL `CONN_MAX_AGE` and `--remove` cases with `--postgres`.

```bash
uv run scripts/trace_response.py
uv run scripts/count_queries.py --solr-url http://127.0.0.1:58983/solr/trace
uv run scripts/check_update_index_workers.py --postgres postgresql://postgres:test@127.0.0.1:55432/postgres
```

## References

- `references/response-pipeline.md`: the request step by step, from the view's `SearchQuerySet` through pagination, `SearchResult`, `load_all` and the serializer, with quoted drf-haystack, django-haystack and DRF source and the trace output. Read it when the rules above do not explain an observed response, or when you need to cite the source.
- `references/choosing-index-fields.md`: deciding which model columns belong in a `SearchIndex`, what `indexed` and `stored` do per backend, loading related objects, and a checklist for removing a field. Read it when designing or trimming an index.
- `references/reading-results.md`: why a `SearchQuerySet` is lazy, what each read sends, iteration chunks and `HAYSTACK_ITERATOR_LOAD_PER_QUERY`, SQL with and without `load_all()`, a measured table of Solr requests vs SQL queries, the open-ended slice bug, and which setting limits what. Read it when a search sends more requests or returns fewer results than expected.
- `references/update-index-workers.md`: how `update_index -k` splits batches into worker tasks, the Python 3.14 `forkserver` hang, the unused-alias crash, `--remove` failing on PostgreSQL, the per-batch reconnect and `--nocommit`, with quoted source and measured results. Read it when `update_index -k` hangs, crashes or loses its database connection.
- For start methods, `map()` chunking and `close()`/`join()`, use the `python` skill; for what a forked child does to Django connections, the `django` skill; for Solr commit behaviour, the `solr` skill.
