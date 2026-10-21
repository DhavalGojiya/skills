# How a SearchQuerySet Fetches Results

When a `SearchQuerySet` talks to the search engine and the database: why nothing runs until you read it, how many requests `len()`, slices, `for r in sqs` and `list(sqs)` send, what `HAYSTACK_ITERATOR_LOAD_PER_QUERY` changes, and how many SQL queries each pattern costs with and without `load_all()`.

Verified against django-haystack [3.4.0](https://github.com/django-haystack/django-haystack/tree/v3.4.0) source, and measured with `scripts/count_queries.py` on 100 indexed rows against Solr 9.10.1 (Docker image `solr:9.10`, pysolr 3.11.0, counting HTTP requests to `/select`) and Whoosh 2.7.4, with Django 5.2.17 on Python 3.15.0. Both backends sent the same requests in every case except the open-ended slice (section 6). Every "Source:" link points at the commit the tag names, with line numbers.

______________________________________________________________________

## Table of Contents

1. [Nothing Runs Until You Read](#1-nothing-runs-until-you-read)
2. [What Each Read Sends](#2-what-each-read-sends)
3. [Iteration Fetches in Chunks](#3-iteration-fetches-in-chunks)
4. [SQL: `.object` vs `load_all()`](#4-sql-object-vs-load_all)
5. [Measured: Search Requests vs SQL Queries](#5-measured-search-requests-vs-sql-queries)
6. [Open-Ended Slices Return the Wrong Results](#6-open-ended-slices-return-the-wrong-results)
7. [Which Setting Limits What](#7-which-setting-limits-what)
8. [Choosing a Read Pattern](#8-choosing-a-read-pattern)

______________________________________________________________________

## 1. Nothing Runs Until You Read

A `SearchQuerySet` is lazy by design:

Source: [`haystack/query.py#L13-L18`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/query.py#L13-L18)

```python
class SearchQuerySet:
    """
    Provides a way to specify search parameters and lazily load results.

    Supports chaining (a la QuerySet) to narrow the search.
    """
```

Building one, and chaining `.filter()`, `.models()`, `.order_by()` or `.load_all()`, only builds a query. Each call returns a clone with an empty result cache:

Source: [`haystack/query.py#L626-L633`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/query.py#L626-L633)

```python
def _clone(self, klass=None):
    if klass is None:
        klass = self.__class__

    query = self.query._clone()
    clone = klass(query=query)
    clone._load_all = self._load_all
    return clone
```

Source: [`haystack/query.py#L32-L36`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/query.py#L32-L36)

```python
self._result_cache = []
self._result_count = None
self._cache_full = False
self._load_all = False
self._ignored_result_count = 0
```

The reason is the shape of a search: a query can match millions of documents, and a search engine returns them a page at a time (`start` and `rows` on Solr). Haystack asks only for the part you read and keeps it. On the first fetch it sizes the cache to the full hit count, one `None` placeholder per hit, and fills slots as results arrive:

Source: [`haystack/query.py#L237-L244`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/query.py#L237-L244)

```python
# Setup the full cache now that we know how many results there are.
# We need the ``None``s as placeholders to know what parts of the
# cache we have/haven't filled.
# Using ``None`` like this takes up very little memory. In testing,
# an array of 100,000 ``None``s consumed less than .5 Mb, which ought
# to be an acceptable loss for consistent and more efficient caching.
if len(self._result_cache) == 0:
    self._result_cache = [None] * self.query.get_count()
```

Slots already filled are never fetched again from the same `SearchQuerySet` (`__getitem__` fetches only when its slice still holds `None`, section 2). A clone from any chained call, or a new `SearchQuerySet()`, starts again from nothing.

______________________________________________________________________

## 2. What Each Read Sends

| Read                                                     | Search requests                                       | `rows` per request                              |
| -------------------------------------------------------- | ----------------------------------------------------- | ----------------------------------------------- |
| `SearchQuerySet()`, `.all()`, `.filter()`, `.load_all()` | 0                                                     |                                                 |
| `len(sqs)`, `sqs.count()`, `if sqs:`, `bool(sqs)`        | 1                                                     | 1, only for the hit count                       |
| `sqs[7]`                                                 | 1                                                     | 1                                               |
| `sqs[0:50]`                                              | 1                                                     | 50                                              |
| `for r in sqs`                                           | hits / `HAYSTACK_ITERATOR_LOAD_PER_QUERY`, rounded up | `HAYSTACK_ITERATOR_LOAD_PER_QUERY` (default 10) |
| `list(sqs)`                                              | 1 more than `for r in sqs`                            | 1, then as `for r in sqs`                       |
| drf-haystack list page                                   | 2                                                     | 1, then the page size                           |

`len()` and `count()` run the query with a one-row limit when it hasn't run yet (`response-pipeline.md`, section 3). `SearchQuerySet` defines no `__bool__`, so `if sqs:` falls back to `__len__` and sends the same count request. `list(sqs)` calls `len()` first to size the list, which is the extra request.

Indexing and slicing go through `__getitem__`, which fetches exactly the slice asked for:

Source: [`haystack/query.py#L303-L316`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/query.py#L303-L316)

```python
# We need check to see if we need to populate more of the cache.
if len(self._result_cache) <= 0 or (
    None in self._result_cache[start:bound] and not self._cache_is_full()
):
    try:
        self._fill_cache(start, bound)
    except StopIteration:
        # There's nothing left, even though the bound is higher.
        pass

# Cache should be full enough for our needs.
if is_slice:
    return self._result_cache[start:bound]
return self._result_cache[start]
```

______________________________________________________________________

## 3. Iteration Fetches in Chunks

`for r in sqs` doesn't fetch all hits at once. It yields what the cache holds and fetches the next chunk when it runs out:

Source: [`haystack/query.py#L134-L153`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/query.py#L134-L153)

```python
while True:
    if len(self._result_cache) > 0:
        try:
            current_cache_max = self._result_cache.index(None)
        except ValueError:
            current_cache_max = len(self._result_cache)

    while current_position < current_cache_max:
        yield self._result_cache[current_position]
        current_position += 1

    if self._cache_is_full():
        return

    # We've run out of results and haven't hit our limit.
    # Fill more of the cache.
    if not self._fill_cache(
        current_position, current_position + ITERATOR_LOAD_PER_QUERY
    ):
        return
```

The chunk size is a module constant read from settings once, at import:

Source: [`haystack/constants.py#L46-L47`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/constants.py#L46-L47)

```python
# Number of SearchResults to load at a time.
ITERATOR_LOAD_PER_QUERY = getattr(settings, "HAYSTACK_ITERATOR_LOAD_PER_QUERY", 10)
```

`haystack/query.py` imports it by name ([`haystack/query.py#L7`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/query.py#L7)), so set `HAYSTACK_ITERATOR_LOAD_PER_QUERY` in `settings.py`; changing the setting after Haystack is imported has no effect. It applies to every `for` loop and `list()` over a `SearchQuerySet`, and to the loop that refills a page after `load_all()` drops a deleted row (`response-pipeline.md`, section 8). It doesn't change slices or drf-haystack pages, which ask for exactly their size. With the default of 10, looping over 10,000 hits sends 1,000 requests.

______________________________________________________________________

## 4. SQL: `.object` vs `load_all()`

Each search response is post-processed as one batch (`response-pipeline.md`, section 5). With `load_all()`, every batch is one `in_bulk()` query per model; without it, every `.object` read is its own query. So the chunk size sets both counts:

| Pattern, 100 hits, reading `.object` | Search requests | SQL without `load_all()` | SQL with `load_all()` |
| ------------------------------------ | --------------- | ------------------------ | --------------------- |
| `for r in sqs`, chunk 10             | 10              | 100                      | 10                    |
| `for r in sqs`, chunk 50             | 2               | 100                      | 2                     |
| `for r in sqs`, chunk 200            | 1               | 100                      | 1                     |
| `sqs[0:50]`                          | 1               | 50                       | 1                     |
| drf-haystack page of 50              | 2               | 50                       | 1                     |

Reading only index fields (`r.username`) costs no SQL at all, with or without `load_all()`.

______________________________________________________________________

## 5. Measured: Search Requests vs SQL Queries

`scripts/count_queries.py` against Solr 9.10.1. `got` is the number of results the read returned, `requests` the HTTP requests to `/select`, `SQL` the database queries, `ms` the wall time on one development machine with Solr in a local container (read it as an order of magnitude), and the last column each request's `start` and `rows`:

```text
Solr http://127.0.0.1:58983/solr/trace, 100 rows
read                                      got requests  SQL   ms  (start, rows) each
sqs.all(), nothing read                     0        0    0    1  []
len(sqs)                                  100        1    0   15  [(0, 1)]
if sqs: (truthiness)                        1        1    0    8  [(0, 1)]
sqs[7].object                               1        1    1    6  [(7, 1)]
sqs[0:50], index fields                    50        1    0   18  [(0, 50)]
sqs[0:50], .object                         50        1   50   30  [(0, 50)]
sqs.load_all()[0:50], .object              50        1    1   11  [(0, 50)]
sqs[50:] (no end), index fields            10        2    0   11  [(50, None), (100, 10)]
HAYSTACK_ITERATOR_LOAD_PER_QUERY = 10
  for r in sqs, index fields              100       10    0   58  [(0, 10), (10, 10), (20, 10), '...']
  for r in sqs, .object                   100       10  100  121  [(0, 10), (10, 10), (20, 10), '...']
  for r in sqs.load_all(), .object        100       10   10   56  [(0, 10), (10, 10), (20, 10), '...']
  list(sqs.load_all())                    100       11   10   56  [(0, 1), (0, 10), (10, 10), '...']
HAYSTACK_ITERATOR_LOAD_PER_QUERY = 50
  for r in sqs, index fields              100        2    0   12  [(0, 50), (50, 50)]
  for r in sqs, .object                   100        2  100   56  [(0, 50), (50, 50)]
  for r in sqs.load_all(), .object        100        2    2   28  [(0, 50), (50, 50)]
  list(sqs.load_all())                    100        3    2   19  [(0, 1), (0, 50), (50, 50)]
HAYSTACK_ITERATOR_LOAD_PER_QUERY = 200
  for r in sqs, index fields              100        1    0   11  [(0, 200)]
  for r in sqs, .object                   100        1  100   40  [(0, 200)]
  for r in sqs.load_all(), .object        100        1    1   13  [(0, 200)]
  list(sqs.load_all())                    100        2    1   17  [(0, 1), (0, 200)]
drf-haystack page of 50
  load_all = False, field reads .object    50        2   50   45  [(0, 1), (0, 50)]
  load_all = True, field reads .object     50        2    1   13  [(0, 1), (0, 50)]
```

What the numbers show:

- `sqs.all()` alone sends nothing; `if sqs:` sends a count request.
- An explicit slice is always one request, whatever its size. `sqs.load_all()[0:50]` reads 50 rows with 1 request and 1 SQL query; the same slice reading `.object` without `load_all()` costs 50 SQL queries.
- A loop's request count is set by the chunk size, not by how many results you use. With `load_all()`, the SQL count follows the request count.
- `list(sqs.load_all())` costs one request more than the loop, for the count.
- A drf-haystack page is always a count request plus the page; `load_all` decides between 1 and 50 SQL queries.
- `sqs[50:]` returned only 10 of the 50 remaining results on Solr (section 6).

Whoosh produced the same `requests` and `SQL` columns, except `sqs[50:]`, which returned 100 results in 1 request.

______________________________________________________________________

## 6. Open-Ended Slices Return the Wrong Results

A slice without an end (`sqs[50:]`) asks the backend for "everything from 50". Neither backend does that.

On Solr, Haystack sends no `rows` parameter when the end is missing:

Source: [`haystack/backends/solr_backend.py#L230-L234`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/backends/solr_backend.py#L230-L234)

```python
if start_offset is not None:
    kwargs["start"] = start_offset

if end_offset is not None:
    kwargs["rows"] = end_offset - start_offset
```

Solr then applies its own default of 10 rows. `_fill_cache()` sees the slice is still incomplete, but its refill loop starts at the hit count instead of where the first response ended (`fill_start = fill_end`), finds nothing there, and trims the cache:

Source: [`haystack/query.py#L246-L270`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/query.py#L246-L270)

```python
fill_start, fill_end = start, end
if fill_end is None:
    fill_end = self.query.get_count()
cache_start = fill_start

while True:
    to_cache = self.post_process_results(results)

    # Assign by slice.
    self._result_cache[cache_start : cache_start + len(to_cache)] = to_cache

    if None in self._result_cache[start:end]:
        fill_start = fill_end
        fill_end += ITERATOR_LOAD_PER_QUERY
        cache_start += len(to_cache)

        # Tell the query where to start from and how many we'd like.
        self.query._reset()
        self.query.set_limits(fill_start, fill_end)
        results = self.query.get_results()

        if results is None or len(results) == 0:
            # No more results. Trim missing stuff from the result cache
            self._result_cache = self._result_cache[:cache_start]
            break
```

The measured result was `user051` to `user060`: 10 results, requests `(50, None)` and `(100, 10)`.

On Whoosh, the backend turns a missing end into 1,000,000 and computes the page from it, which lands on page 1 whatever the start:

Source: [`haystack/backends/whoosh_backend.py#L366-L388`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/backends/whoosh_backend.py#L366-L388)

```python
def calculate_page(self, start_offset=0, end_offset=None):
    # Prevent against Whoosh throwing an error. Requires an end_offset
    # greater than 0.
    if end_offset is not None and end_offset <= 0:
        end_offset = 1

    # Determine the page.
    page_num = 0

    if end_offset is None:
        end_offset = 1000000

    if start_offset is None:
        start_offset = 0

    page_length = end_offset - start_offset

    if page_length and page_length > 0:
        page_num = int(start_offset / page_length)

    # Increment because Whoosh uses 1-based page numbers.
    page_num += 1
    return page_num, page_length
```

The measured result was all 100 hits from `user001`, ignoring the start.

Always give a slice an end: `sqs[50:100]`, or `sqs[50:len(sqs)]` when you need everything after an offset (one count request more).

______________________________________________________________________

## 7. Which Setting Limits What

| Setting                                                 | Default | Controls                                                                      |
| ------------------------------------------------------- | ------- | ----------------------------------------------------------------------------- |
| `HAYSTACK_ITERATOR_LOAD_PER_QUERY`                      | 10      | rows per request when iterating a `SearchQuerySet`, and when refilling a page |
| `HAYSTACK_SEARCH_RESULTS_PER_PAGE`                      | 20      | page size of Haystack's own `SearchView` and generic views only               |
| DRF `PAGE_SIZE`, or `page_size` on the pagination class | none    | rows per drf-haystack list page                                               |
| Solr's `rows` default                                   | 10      | rows Solr returns when Haystack sends no `rows`, as for `sqs[50:]`            |

`HAYSTACK_SEARCH_RESULTS_PER_PAGE` is read by `haystack/views.py` ([`haystack/views.py#L9`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/views.py#L9)) and `haystack/generic_views.py` ([`haystack/generic_views.py#L10`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/generic_views.py#L10)), not by drf-haystack, whose page size comes from DRF's pagination class.

______________________________________________________________________

## 8. Choosing a Read Pattern

| Goal                              | Do                                                                                                  | Cost                                          |
| --------------------------------- | --------------------------------------------------------------------------------------------------- | --------------------------------------------- |
| Only the number of hits           | `sqs.count()`                                                                                       | 1 request, `rows=1`                           |
| An API page                       | let drf-haystack paginate, with `load_all = True` on the view                                       | 2 requests, 1 SQL query per model             |
| A fixed block of results          | `sqs.load_all()[start:end]`                                                                         | 1 request, 1 SQL query per model              |
| Every hit, for an export or a job | loop over explicit slices, `sqs.load_all()[i:i + 500]`, or raise `HAYSTACK_ITERATOR_LOAD_PER_QUERY` | 1 request and 1 SQL query per model per block |
| A check before looping            | skip `if sqs:`; loop and handle the empty case, or reuse a `len()` you already need                 | saves 1 request                               |
