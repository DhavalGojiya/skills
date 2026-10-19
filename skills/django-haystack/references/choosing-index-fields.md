# Choosing Which Model Fields Go in a SearchIndex

How to decide whether a model column belongs in a Haystack `SearchIndex` when results are served through drf-haystack, and what each choice costs.

Verified against django-haystack [3.4.0](https://github.com/django-haystack/django-haystack/tree/v3.4.0) and drf-haystack [1.9.3](https://github.com/django-commons/drf-haystack/tree/v1.9.3) source and by running `scripts/trace_response.py`. The mechanics this builds on (where each serializer field reads its value, what `load_all` loads) are in `response-pipeline.md`. Every "Source:" link points at the commit the tag names, with line numbers.

______________________________________________________________________

## Table of Contents

1. [The Rule](#1-the-rule)
2. [Display-Only Columns](#2-display-only-columns)
3. [`indexed` and `stored`](#3-indexed-and-stored)
4. [Related Objects](#4-related-objects)
5. [Removing a Field From an Index](#5-removing-a-field-from-an-index)

______________________________________________________________________

## 1. The Rule

Index a column only when the search engine itself has to use it:

| The search engine needs the column for     | Example                                                           | In the index?                         |
| ------------------------------------------ | ----------------------------------------------------------------- | ------------------------------------- |
| Full-text search                           | a user types part of a name                                       | yes, in the document field's template |
| Filtering                                  | `.filter(state="CA")`                                             | yes                                   |
| Sorting                                    | `.order_by("last_name")`                                          | yes                                   |
| Facet counts                               | results per party                                                 | yes, with `faceted=True`              |
| Returning it without a database query      | `load_all = False`, and the serializer reads it as an index field | yes                                   |
| Only displaying it, with `load_all = True` | `CharField(source="object.phone")`                                | no                                    |

______________________________________________________________________

## 2. Display-Only Columns

With `load_all = True` the page's rows are already loaded in one query per model (`response-pipeline.md`, section 5), so reading a display-only column through `.object` costs no extra query:

|                                    | Indexed, read as an index field        | Not indexed, read through `.object`                   |
| ---------------------------------- | -------------------------------------- | ----------------------------------------------------- |
| Value shown                        | as of the last `update_index`          | the current row                                       |
| After the column changes           | stale until the document is reindexed  | correct immediately                                   |
| Index size and `update_index` time | grows with every field                 | unchanged                                             |
| SQL per page                       | none, or one per model with `load_all` | one per model with `load_all`, one per result without |

`scripts/trace_response.py` section 4 shows the first row: after `first_name` changes in the database, the index field still returns the old value and `object.first_name` the new one.

A serializer that reads display-only columns from the row, and a view that loads the rows in bulk:

```python
from drf_haystack.serializers import HaystackSerializer
from drf_haystack.viewsets import HaystackViewSet
from rest_framework import serializers

from people.models import Person
from people.search_indexes import PersonIndex


class PersonSerializer(HaystackSerializer):
    phone = serializers.CharField(source="object.phone")

    class Meta:
        index_classes = [PersonIndex]
        fields = ["name", "state"]


class PersonSearchView(HaystackViewSet):
    index_models = [Person]
    serializer_class = PersonSerializer
    load_all = True
```

Declared fields are always included, so `phone` doesn't belong in `Meta.fields`. Listing it there does nothing unless `PersonIndex` also has a `phone` field.

______________________________________________________________________

## 3. `indexed` and `stored`

Both flags default to `True`:

Source: [`haystack/fields.py#L33-L40`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/fields.py#L33-L40)

```python
def __init__(
    self,
    model_attr=None,
    use_template=False,
    template_name=None,
    document=False,
    indexed=True,
    stored=True,
```

- `indexed=False` asks the backend not to make the value searchable, so it can be returned but not searched or filtered on.
- `stored=False` asks the backend not to return the value, so it can be searched or filtered on but never comes back in a hit.

Whether a flag is honoured depends on the backend's schema. Haystack's Whoosh backend sends every `CharField` to this branch, which indexes and stores it whatever the flags say:

Source: [`haystack/backends/whoosh_backend.py#L234-L240`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/backends/whoosh_backend.py#L234-L240)

```python
else:
    schema_fields[field_class.index_fieldname] = TEXT(
        stored=True,
        analyzer=field_class.analyzer or StemmingAnalyzer(),
        field_boost=field_class.boost,
        sortable=True,
    )
```

For Solr, `build_solr_schema` writes the flag into the generated schema:

Source: [`haystack/templates/search_configuration/schema.xml#L84`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/templates/search_configuration/schema.xml#L84)

```xml
<field name="{{ field.field_name }}" type="{{ field.type }}" indexed="{{ field.indexed }}" stored="{{ field.stored }}" multiValued="{{ field.multi_valued }}" />
```

Check what a hit really carries with `result.get_additional_fields()` before relying on either. When a hit lacks a field that the serializer lists, the response shows `null` for it, because `SearchResult` returns `None` for any missing attribute (`response-pipeline.md`, section 4).

______________________________________________________________________

## 4. Related Objects

`load_all` and per-result `.object` both load rows through the index's `read_queryset()`, and `update_index` uses `index_queryset()` instead (`response-pipeline.md`, section 5). Put `select_related()` for every relation the serializer reads into `read_queryset()`:

```python
from haystack import indexes

from people.models import Person


class PersonIndex(indexes.SearchIndex, indexes.Indexable):
    text = indexes.CharField(document=True, use_template=True)
    name = indexes.CharField(model_attr="name")
    state = indexes.CharField(model_attr="state", faceted=True)

    def get_model(self):
        return Person

    def read_queryset(self, using=None):
        return Person.objects.select_related("party")
```

A serializer field such as `CharField(source="object.party.name")` then costs no query of its own.

______________________________________________________________________

## 5. Removing a Field From an Index

A removed index field fails silently wherever a serializer still uses it, so check before deleting the line:

1. Search the serializers for the name in `Meta.fields`, `Meta.exclude` and every `source="..."`. In `Meta.fields` the key disappears from the response; as a plain `source` it turns into `null`.
2. Change each use to `source="object.<column>"`, or to a `SerializerMethodField` that reads `instance.object`.
3. Make sure the view sets `load_all = True`, or every page gains one query per result.
4. Search for queries that filter, sort or facet on the field (`.filter(`, `.order_by(`, `.facet(`); those need it to stay indexed.

Documents indexed before the change still contain the old field until they are reindexed. drf-haystack ignores it, because `get_fields()` only looks at the fields the index class declares (`response-pipeline.md`, section 6).
