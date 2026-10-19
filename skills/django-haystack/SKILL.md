---
name: django-haystack
description: How django-haystack and drf-haystack turn search hits into an API response, covering which serializer fields exist (index_classes, Meta.fields, exclude, ignore_fields, declared fields, faceted _exact fields), where each value is read from (the stored search document or the database row via SearchResult.object), load_all and the searches and SQL queries per page, and rows deleted from the database but still indexed. Use when a field listed in Meta.fields is missing from a HaystackSerializer response, a declared field is always null, the API shows stale values or unexpected _exact keys, a search endpoint runs one SQL query per result, on "Got AttributeError when attempting to get a value for field", "'NoneType' object has no attribute" or "Object could not be found in database for SearchResult", or when deciding which model fields belong in a SearchIndex.
---

# django-haystack and drf-haystack: from search hits to an API response

Verified against drf-haystack [1.9.3](https://github.com/django-commons/drf-haystack/tree/v1.9.3) (`serializers.py`, `generics.py`, `fields.py`; yanked on PyPI, same logic in 1.9.1), django-haystack [3.4.0](https://github.com/django-haystack/django-haystack/tree/v3.4.0) (`models.py`, `query.py`, `indexes.py`, `backends/solr_backend.py`) and Django REST framework [3.18.1](https://github.com/encode/django-rest-framework/tree/3.18.1) (`fields.py`, `serializers.py`) source, and by running `scripts/trace_response.py` on Django 5.2.17, Whoosh 2.7.4 and Python 3.15.0. Line-level source links are in `references/`.

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
6. Run `uv run scripts/trace_response.py` to see every case above reproduced against a throwaway Whoosh index, with the searches and SQL counted per page.

## Script

`scripts/trace_response.py` needs no project. It indexes 25 `auth.User` rows into a temporary Whoosh index and prints, for each case, the serializer's resolved fields and its output, plus the search calls and SQL queries of a paginated `HaystackViewSet` with and without `load_all` and after a row is deleted:

```bash
uv run scripts/trace_response.py
```

## References

- `references/response-pipeline.md`: the request step by step, from the view's `SearchQuerySet` through pagination, `SearchResult`, `load_all` and the serializer, with quoted drf-haystack, django-haystack and DRF source and the trace output. Read it when the rules above do not explain an observed response, or when you need to cite the source.
- `references/choosing-index-fields.md`: deciding which model columns belong in a `SearchIndex`, what `indexed` and `stored` do per backend, loading related objects, and a checklist for removing a field. Read it when designing or trimming an index.
