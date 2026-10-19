# How drf-haystack Builds an API Response

What happens between a request to a drf-haystack view and the JSON it returns: which searches and SQL queries run, which fields the serializer has, and where each value is read from.

Verified against drf-haystack [1.9.3](https://github.com/django-commons/drf-haystack/tree/v1.9.3), django-haystack [3.4.0](https://github.com/django-haystack/django-haystack/tree/v3.4.0) and Django REST framework [3.18.1](https://github.com/encode/django-rest-framework/tree/3.18.1) source. drf-haystack 1.9.3 is yanked on PyPI ("Too many breaking changes for a patch release"); the serializer and view code quoted here is the same in 1.9.1 apart from Python 2 compatibility code. Every "Source:" link points at the commit the tag names, with line numbers. The outputs come from `scripts/trace_response.py`, which indexes 25 `auth.User` rows into Whoosh (Django 5.2.17, Whoosh 2.7.4, Python 3.15.0); the Solr backend builds search results the same way (section 4).

______________________________________________________________________

## Table of Contents

1. [The Request, Step by Step](#1-the-request-step-by-step)
2. [The View Builds a SearchQuerySet](#2-the-view-builds-a-searchqueryset)
3. [Pagination Runs Two Searches](#3-pagination-runs-two-searches)
4. [Each Hit Becomes a SearchResult](#4-each-hit-becomes-a-searchresult)
5. [Loading Database Rows](#5-loading-database-rows)
6. [Which Fields the Serializer Has](#6-which-fields-the-serializer-has)
7. [Where Each Field Reads Its Value](#7-where-each-field-reads-its-value)
8. [Rows Deleted From the Database but Still Indexed](#8-rows-deleted-from-the-database-but-still-indexed)
9. [Quick Reference](#9-quick-reference)

______________________________________________________________________

## 1. The Request, Step by Step

For a list request to a `HaystackViewSet` with `PageNumberPagination`:

1. `get_queryset()` creates a `SearchQuerySet`, limited to the view's `index_models`. No search runs yet.
2. `filter_queryset()` runs the filter backends (`HaystackFilter` by default) on the query string, then calls `.load_all()` if the view sets `load_all = True` (section 2).
3. The paginator asks for the total: one search that returns 1 row and the hit count (section 3).
4. The paginator slices out the page: a second search for `page_size` rows.
5. The backend turns each hit into a `SearchResult` whose attributes are the fields the search engine returned for that document (section 4).
6. With `load_all`, Haystack loads the page's rows with one `in_bulk()` query per model and attaches each row as `result.object`. Without it, nothing has touched the database yet (section 5).
7. The serializer builds its field list from the index class and its own declared fields (section 6).
8. Each field reads its value from the `SearchResult`, or from `result.object` when its source goes through `object`. Without `load_all`, that is one query per result (section 7).
9. Fields that don't belong to the result's index are removed, and `highlighted` is added when the search returned highlighting.

______________________________________________________________________

## 2. The View Builds a SearchQuerySet

`load_all` is a view attribute and defaults to `False`. The comment above it states the cost:

Source: [`drf_haystack/generics.py#L28-L31`](https://github.com/django-commons/drf-haystack/blob/86f5f587cf4a19160494c5e88782b73653e60565/drf_haystack/generics.py#L28-L31)

```python
# If set to False, DB lookups are done on a per-object basis,
# resulting in in many individual trips to the database. If True,
# the SearchQuerySet will group similar objects into a single query.
load_all = False
```

It is applied in `filter_queryset()`, after the filter backends:

Source: [`drf_haystack/generics.py#L96-L102`](https://github.com/django-commons/drf-haystack/blob/86f5f587cf4a19160494c5e88782b73653e60565/drf_haystack/generics.py#L96-L102)

```python
def filter_queryset(self, queryset):
    queryset = super().filter_queryset(queryset)

    if self.load_all:
        queryset = queryset.load_all()

    return queryset
```

The `retrieve` action goes through `get_object()` ([`drf_haystack/generics.py#L55-L94`](https://github.com/django-commons/drf-haystack/blob/86f5f587cf4a19160494c5e88782b73653e60565/drf_haystack/generics.py#L55-L94)), which calls `get_queryset()` but never `filter_queryset()`, so a detail request never uses `load_all`. Its one row is loaded when the serializer first reads `.object`.

______________________________________________________________________

## 3. Pagination Runs Two Searches

Django's `Paginator.count` calls `SearchQuerySet.count()`, which is `len()`:

Source: [`haystack/query.py#L522-L524`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/query.py#L522-L524)

```python
def count(self):
    """Returns the total number of matching results."""
    return len(self)
```

Source: [`haystack/query.py#L80-L89`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/query.py#L80-L89)

```python
def __len__(self):
    if self._result_count is None:
        self._result_count = self.query.get_count()

        # Some backends give weird, false-y values here. Convert to zero.
        if not self._result_count:
            self._result_count = 0

    # This needs to return the actual number of hits, not what's in the cache.
    return self._result_count - self._ignored_result_count
```

When the query hasn't run yet, `get_count()` runs it limited to one row, only to learn the hit count:

Source: [`haystack/backends/__init__.py#L645-L660`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/backends/__init__.py#L645-L660)

```python
if self._hit_count is None:
    # Limit the slice to 1 so we get a count without consuming
    # everything.
    if not self.end_offset:
        self.end_offset = 1

    if self._more_like_this:
        # Special case for MLT.
        self.run_mlt()
    elif self._raw_query:
        # Special case for raw queries.
        self.run_raw()
    else:
        self.run()

return self._hit_count
```

The page is a second search. `scripts/trace_response.py` records every backend `search()` call as `(start_offset, end_offset)`; one page of 10 makes two calls, `(0, 1)` for the count and `(0, 10)` for the page (trace in section 5).

______________________________________________________________________

## 4. Each Hit Becomes a SearchResult

The Solr backend turns every field of each returned document into a keyword argument:

Source: [`haystack/backends/solr_backend.py#L536-L549`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/backends/solr_backend.py#L536-L549)

```python
for key, value in raw_result.items():
    string_key = str(key)
    # re-map key if alternate name used
    if string_key in index_field_map:
        string_key = index_field_map[key]

    if string_key in index.fields and hasattr(
        index.fields[string_key], "convert"
    ):
        additional_fields[string_key] = index.fields[
            string_key
        ].convert(value)
    else:
        additional_fields[string_key] = self.conn._to_python(value)
```

Source: [`haystack/backends/solr_backend.py#L572-L578`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/backends/solr_backend.py#L572-L578)

```python
result = result_class(
    app_label,
    model_name,
    raw_result[DJANGO_ID],
    raw_result["score"],
    **additional_fields,
)
```

`SearchResult.__init__` stores each one as an attribute:

Source: [`haystack/models.py#L42-L45`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/models.py#L42-L45)

```python
for key, value in kwargs.items():
    if key not in self.__dict__:
        self.__dict__[key] = value
        self._additional_fields.append(key)
```

Reading any other attribute returns `None` instead of raising `AttributeError`:

Source: [`haystack/models.py#L60-L64`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/models.py#L60-L64)

```python
def __getattr__(self, attr):
    if attr == "__getnewargs__":
        raise AttributeError

    return self.__dict__.get(attr, None)
```

Two consequences:

- Every value the search engine returned is available as `result.<field>`, and `result.get_additional_fields()` returns them all as a dict ([`haystack/models.py#L188-L201`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/models.py#L188-L201)). It is the quickest way to see what a hit really carries.
- A missing or misspelled name never fails: `result.nickname` is `None`, and so is a serializer field that reads it (section 7).

______________________________________________________________________

## 5. Loading Database Rows

`load_all()` only sets a flag on a clone:

Source: [`haystack/query.py#L475-L479`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/query.py#L475-L479)

```python
def load_all(self):
    """Efficiently populates the objects in the search results."""
    clone = self._clone()
    clone._load_all = True
    return clone
```

When a page of results arrives, `post_process_results()` groups the primary keys by model and loads each model once:

Source: [`haystack/query.py#L159-L171`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/query.py#L159-L171)

```python
if self._load_all:
    models_pks = {}
    loaded_objects = {}

    # Remember the search position for each result so we don't have to resort later.
    for result in results:
        models_pks.setdefault(result.model, []).append(result.pk)

    # Load the objects for each model in turn.
    for model in models_pks:
        loaded_objects[model] = self._load_model_objects(
            model, models_pks[model]
        )
```

Source: [`haystack/query.py#L205-L214`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/query.py#L205-L214)

```python
def _load_model_objects(self, model, pks):
    try:
        ui = connections[self.query._using].get_unified_index()
        index = ui.get_index(model)
        objects = index.read_queryset(using=self.query._using)
        return objects.in_bulk(pks)
    except NotHandled:
        self.log.warning("Model '%s' not handled by the routers.", model)
        # Revert to old behaviour
        return model._default_manager.in_bulk(pks)
```

Without `load_all`, `result.object` loads its own row the first time it is read:

Source: [`haystack/models.py#L73-L96`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/models.py#L73-L96)

```python
def _get_object(self):
    if self._object is None:
        if self.model is None:
            self.log.error("Model could not be found for SearchResult '%s'.", self)
            return None

        try:
            try:
                self._object = self.searchindex.read_queryset().get(pk=self.pk)
            except NotHandled:
                self.log.warning(
                    "Model '%s.%s' not handled by the routers.",
                    self.app_label,
                    self.model_name,
                )
                # Revert to old behaviour
                self._object = self.model._default_manager.get(pk=self.pk)
        except ObjectDoesNotExist:
            self.log.error(
                "Object could not be found in database for SearchResult '%s'.", self
            )
            self._object = None

    return self._object
```

Both paths load rows through the index's `read_queryset()`, which defaults to `index_queryset()`:

Source: [`haystack/indexes.py#L153-L160`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/indexes.py#L153-L160)

```python
def read_queryset(self, using=None):
    """
    Get the default QuerySet for read actions.

    Subclasses can override this method to work with other managers.
    Useful when working with default managers that filter some objects.
    """
    return self.index_queryset(using=using)
```

`update_index` uses `index_queryset()` through `build_queryset()` ([`haystack/indexes.py#L162-L170`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/indexes.py#L162-L170)), so overriding `read_queryset()` changes how results are loaded without changing what gets indexed. Add `select_related()` there for every relation the serializer reads, or each result runs one more query per relation:

```python
from haystack import indexes

from people.models import Person


class PersonIndex(indexes.SearchIndex, indexes.Indexable):
    text = indexes.CharField(document=True, use_template=True)

    def get_model(self):
        return Person

    def read_queryset(self, using=None):
        return Person.objects.select_related("party")
```

One page of 10 results through a `HaystackViewSet` whose serializer reads `object.email`:

```text
5. Paginated HaystackViewSet list, page_size 10, field reads .object
  load_all=False  count=25  results=10  search calls (start, end)=[(0, 1), (0, 10)]  SQL queries=10
  load_all=True   count=25  results=10  search calls (start, end)=[(0, 1), (0, 10)]  SQL queries=1
```

`load_all=False` runs 10 SQL queries, one per `.object`; `load_all=True` runs 1. The searches are the same either way.

______________________________________________________________________

## 6. Which Fields the Serializer Has

drf-haystack copies your `Meta` onto a template class whose defaults are empty, so an unset `Meta.fields` is `()`:

Source: [`drf_haystack/serializers.py#L32-L45`](https://github.com/django-commons/drf-haystack/blob/86f5f587cf4a19160494c5e88782b73653e60565/drf_haystack/serializers.py#L32-L45)

```python
class Meta(type):
    """
    Template for the HaystackSerializerMeta.Meta class.
    """

    fields = ()
    exclude = ()
    search_fields = ()
    index_classes = ()
    serializers = ()
    ignore_fields = ()
    field_aliases = {}
    field_options = {}
    index_aliases = {}
```

Source: [`drf_haystack/serializers.py#L72-L73`](https://github.com/django-commons/drf-haystack/blob/86f5f587cf4a19160494c5e88782b73653e60565/drf_haystack/serializers.py#L72-L73)

```python
if getattr(cls, "Meta", None):
    cls.Meta = Meta("Meta", (Meta,), dict(cls.Meta.__dict__))
```

`get_fields()` walks the fields of the index classes, never the model's:

Source: [`drf_haystack/serializers.py#L180-L210`](https://github.com/django-commons/drf-haystack/blob/86f5f587cf4a19160494c5e88782b73653e60565/drf_haystack/serializers.py#L180-L210)

```python
for index_cls in self.Meta.index_classes:
    prefix = ""
    if prefix_field_names:
        prefix = f"_{self._get_index_class_name(index_cls)}__"
    for field_name, field_type in index_cls.fields.items():
        orig_name = field_name
        field_name = f"{prefix}{field_name}"

        # Don't use this field if it is in `ignore_fields`
        if orig_name in ignore_fields or field_name in ignore_fields:
            continue
        # When fields to include are decided by `exclude`
        if exclude and (orig_name in exclude or field_name in exclude):
            continue
        # When fields to include are decided by `fields`
        if fields and orig_name not in fields and field_name not in fields:
            continue

        # Look up the field attributes on the current index model,
        # in order to correctly instantiate the serializer field.
        model = index_cls().get_model()
        kwargs = self._get_default_field_kwargs(model, field_type)
        kwargs["prefix_field_names"] = prefix_field_names
        field_mapping[field_name] = self._field_mapping[field_type](**kwargs)

# Add any explicitly declared fields. They *will* override any index fields
# in case of naming collision!.
if declared_fields:
    for field_name in declared_fields:
        field_mapping[field_name] = declared_fields[field_name]
return field_mapping
```

Each index field passes three checks, and the declared fields are added on top:

| Check           | Lines   | Effect                                                                   |
| --------------- | ------- | ------------------------------------------------------------------------ |
| `ignore_fields` | 189     | the field is always skipped                                              |
| `exclude`       | 192     | applies only when `exclude` is set                                       |
| `fields`        | 195     | applies only when `fields` is truthy; an index field it omits is skipped |
| declared fields | 207-209 | always added, replacing an index field of the same name                  |

What follows from this code:

- Names in `Meta.fields` are never validated. The loop only asks whether each index field is listed, so a database column, a typo or an invented name matches nothing and disappears without an error.
- `ModelSerializer`'s `Field name 'x' is not valid for model 'Y'` check lives in `ModelSerializer.get_fields()`. `HaystackSerializer.get_fields()` never calls `super()`, so also inheriting `ModelSerializer` does not bring the check back.
- `if fields and ...` short-circuits for every falsy value. Unset (`()`), `None`, `""`, `False` and `[]` all mean "every index field".
- A non-empty string turns `in` into a substring test: `"name" in "username"` is `True`.
- With more than one class in `index_classes`, field names are prefixed `_<index>__<field>` internally (lines 181-186), and `to_representation()` maps them back for each result (section 7).

The index class's `fields` dict is built by Haystack's metaclass from its class-level `SearchField` attributes, plus a shadow facet field for every `faceted=True` field:

Source: [`haystack/indexes.py#L63-L82`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/indexes.py#L63-L82)

```python
built_fields = {}

for field_name, obj in attrs.items():
    if isinstance(obj, SearchField):
        field = attrs[field_name]
        field.set_instance_name(field_name)
        built_fields[field_name] = field

        # Only check non-faceted fields for the following info.
        if not hasattr(field, "facet_for"):
            if field.faceted:
                # If no other field is claiming this field as
                # ``facet_for``, create a shadow ``FacetField``.
                if field_name not in facet_fields:
                    shadow_facet_name = get_facet_field_name(field_name)
                    shadow_facet_field = field.facet_class(facet_for=field_name)
                    shadow_facet_field.set_instance_name(shadow_facet_name)
                    built_fields[shadow_facet_name] = shadow_facet_field

attrs["fields"].update(built_fields)
```

`prepare()` fills only those fields, so a `prepare_<name>()` method without a matching field never runs:

Source: [`haystack/indexes.py#L223-L230`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/indexes.py#L223-L230)

```python
for field_name, field in self.fields.items():
    # Use the possibly overridden name, which will default to the
    # variable name of the field.
    self.prepared_data[field.index_fieldname] = field.prepare(obj)

    if hasattr(self, "prepare_%s" % field_name):
        value = getattr(self, "prepare_%s" % field_name)(obj)
        self.prepared_data[field.index_fieldname] = value
```

The trace, for a `UserIndex` with `text`, `username`, `name` (from `first_name`) and `is_staff` (`faceted=True`):

```text
1. Field resolution. Index fields: text, username, name, is_staff,
   is_staff_exact. email is a DB column only; nickname exists nowhere.
   ignore_fields = ['text'].
  Meta.fields = ["username", "email"]
    serializer fields: ['username']
    output:            {"username": "user02"}
  Meta.fields = ["username", "nickname"]
    serializer fields: ['username']
    output:            {"username": "user02"}
  Meta.fields not set
    serializer fields: ['username', 'name', 'is_staff', 'is_staff_exact']
    output:            {"username": "user02", "name": "First02", "is_staff": true, "is_staff_exact": true}
  Meta.fields = None
    serializer fields: ['username', 'name', 'is_staff', 'is_staff_exact']
    output:            {"username": "user02", "name": "First02", "is_staff": true, "is_staff_exact": true}
  Meta.fields = ''
    serializer fields: ['username', 'name', 'is_staff', 'is_staff_exact']
    output:            {"username": "user02", "name": "First02", "is_staff": true, "is_staff_exact": true}
  Meta.fields = False
    serializer fields: ['username', 'name', 'is_staff', 'is_staff_exact']
    output:            {"username": "user02", "name": "First02", "is_staff": true, "is_staff_exact": true}
  Meta.fields = []
    serializer fields: ['username', 'name', 'is_staff', 'is_staff_exact']
    output:            {"username": "user02", "name": "First02", "is_staff": true, "is_staff_exact": true}
  Meta.fields = "username" (a string)
    serializer fields: ['username', 'name']
    output:            {"username": "user02", "name": "First02"}
```

`email` and `nickname` vanish without an error, and every falsy `Meta.fields` returns all index fields, including the shadow `is_staff_exact`.

______________________________________________________________________

## 7. Where Each Field Reads Its Value

drf-haystack's field classes default `source` to the field name:

Source: [`drf_haystack/fields.py#L35-L44`](https://github.com/django-commons/drf-haystack/blob/86f5f587cf4a19160494c5e88782b73653e60565/drf_haystack/fields.py#L35-L44)

```python
# self.source should default to being the same as the field name.
if self.source is None:
    self.source = self.convert_field_name(field_name)

# self.source_attrs is a list of attributes that need to be looked up
# when serializing the instance, or populating the validated data.
if self.source == "*":
    self.source_attrs = []
else:
    self.source_attrs = self.source.split(".")
```

DRF resolves `source_attrs` against the serializer's instance, which here is the `SearchResult`:

Source: [`rest_framework/fields.py#L426-L432`](https://github.com/encode/django-rest-framework/blob/dd2349525461e533a61ab140e746714738a59dd5/rest_framework/fields.py#L426-L432)

```python
def get_attribute(self, instance):
    """
    Given the *outgoing* object instance, return the primitive value
    that should be used for this field.
    """
    try:
        return get_attribute(instance, self.source_attrs)
```

and writes a `None` value straight to the output:

Source: [`rest_framework/serializers.py#L537-L552`](https://github.com/encode/django-rest-framework/blob/dd2349525461e533a61ab140e746714738a59dd5/rest_framework/serializers.py#L537-L552)

```python
for field in fields:
    try:
        attribute = field.get_attribute(instance)
    except SkipField:
        continue

    # We skip `to_representation` for `None` values so that fields do
    # not have to explicitly deal with that case.
    #
    # For related fields with `use_pk_only_optimization` we need to
    # resolve the pk value.
    check_for_none = attribute.pk if isinstance(attribute, PKOnlyObject) else attribute
    if check_for_none is None:
        ret[field.field_name] = None
    else:
        ret[field.field_name] = field.to_representation(attribute)
```

So each kind of field reads from a different place:

| Field                                                      | Reads                                          | SQL                               |
| ---------------------------------------------------------- | ---------------------------------------------- | --------------------------------- |
| index field kept by `Meta.fields`                          | `result.<name>`, the value from the last index | none                              |
| declared, `contact = CharField(source="phone")`            | `result.phone`; `None` if the hit lacks it     | none                              |
| declared, `contact = CharField(source="object.phone")`     | `result.object.phone`, the current row         | one per result without `load_all` |
| `SerializerMethodField` reading `instance.object`          | the current row                                | one per result without `load_all` |
| any field of `HaystackSerializerMixin` + `ModelSerializer` | the current row                                | one per result without `load_all` |

`HaystackSerializerMixin` swaps the instance before DRF sees it:

Source: [`drf_haystack/serializers.py#L424-L433`](https://github.com/django-commons/drf-haystack/blob/86f5f587cf4a19160494c5e88782b73653e60565/drf_haystack/serializers.py#L424-L433)

```python
class HaystackSerializerMixin:
    """
    This mixin can be added to a serializer to use the actual object as the data source for serialization rather
    than the data stored in the search index fields.  This makes it easy to return data from search results in
    the same format as elsewhere in your API and reuse your existing serializers
    """

    def to_representation(self, instance):
        obj = instance.object
        return super().to_representation(obj)
```

After DRF builds the dict, `to_representation()` removes fields that don't belong to this result's index, which matters when one view serves several models, and adds `highlighted`:

Source: [`drf_haystack/serializers.py#L212-L241`](https://github.com/django-commons/drf-haystack/blob/86f5f587cf4a19160494c5e88782b73653e60565/drf_haystack/serializers.py#L212-L241)

```python
def to_representation(self, instance):
    """
    If we have a serializer mapping, use that.  Otherwise, use standard serializer behavior
    Since we might be dealing with multiple indexes, some fields might
    not be valid for all results. Do not render the fields which don't belong
    to the search result.
    """
    if self.Meta.serializers:
        ret = self.multi_serializer_representation(instance)
    else:
        ret = super().to_representation(instance)
        prefix_field_names = len(self.Meta.index_classes) > 1
        current_index = self._get_index_class_name(type(instance.searchindex))
        for field in self.fields:
            orig_field = field
            if prefix_field_names:
                parts = field.split("__")
                if len(parts) > 1:
                    index = parts[0][1:]  # trim the preceding '_'
                    field = parts[1]
                    if index == current_index:
                        ret[field] = ret[orig_field]
                    del ret[orig_field]
            elif field not in chain(instance.searchindex.fields.keys(), self._declared_fields.keys()):
                del ret[orig_field]

    # include the highlighted field in either case
    if getattr(instance, "highlighted", None):
        ret["highlighted"] = instance.highlighted[0]
    return ret
```

The trace for declared fields, the mixin, and a `first_name` changed in the database without reindexing:

```text
2. Declared fields
  contact = CharField(source="email")
    serializer fields: ['username', 'contact']
    output:            {"username": "user02", "contact": null}
  contact = CharField(source="object.email")
    serializer fields: ['username', 'contact']
    output:            {"username": "user02", "contact": "u02@x.io"}
```

```text
3. HaystackSerializerMixin + ModelSerializer (data from .object)
    output:            {"username": "user02", "email": "u02@x.io", "first_name": "First02"}
```

```text
4. Index fields come from the stored search document, not the DB
  first_name updated in the DB, no reindex
    serializer fields: ['name', 'db_name']
    output:            {"name": "First02", "db_name": "ChangedInDB"}
```

`contact` with a plain `source` is `null` because the hit has no `email` attribute. The index field `name` still shows the indexed `First02` while `object.first_name` shows the current row; `load_all` doesn't change that.

______________________________________________________________________

## 8. Rows Deleted From the Database but Still Indexed

With `load_all`, a hit whose row is gone is dropped silently. When `in_bulk()` returned no row for its primary key, the `except KeyError` branch ([`haystack/query.py#L182-L192`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/query.py#L182-L192)) counts it as ignored and skips it:

Source: [`haystack/query.py#L188-L192`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/query.py#L188-L192)

```python
self._ignored_result_count += 1

# avoid an unfilled None at the end of the result cache
self._result_cache.pop()
continue
```

`_fill_cache()` then keeps searching until the requested slice is full:

Source: [`haystack/query.py#L251-L272`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/query.py#L251-L272)

```python
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
    else:
        break
```

The paginator read the count before the page loaded, when no result had been ignored yet, so `count` still includes the deleted row (`__len__` subtracts only the results ignored so far, section 3).

Without `load_all`, `result.object` logs `Object could not be found in database for SearchResult ...` and returns `None` ([`haystack/models.py#L90-L94`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/models.py#L90-L94)). What happens next depends on the field. DRF catches the `AttributeError` and decides:

Source: [`rest_framework/fields.py#L445-L465`](https://github.com/encode/django-rest-framework/blob/dd2349525461e533a61ab140e746714738a59dd5/rest_framework/fields.py#L445-L465)

```python
except (KeyError, AttributeError) as exc:
    if self.default is not empty:
        return self.get_default()
    if self.allow_null:
        return None
    if not self.required:
        raise SkipField()
    msg = (
        'Got {exc_type} when attempting to get a value for field '
        '`{field}` on serializer `{serializer}`.\nThe serializer '
        'field might be named incorrectly and not match '
        'any attribute or key on the `{instance}` instance.\n'
        'Original exception text was: {exc}.'.format(
            exc_type=type(exc).__name__,
            field=self.field_name,
            serializer=self.parent.__class__.__name__,
            instance=instance.__class__.__name__,
            exc=exc
        )
    )
    raise type(exc)(msg)
```

A `read_only=True` field is never required, so it is skipped:

Source: [`rest_framework/fields.py#L301-L303`](https://github.com/encode/django-rest-framework/blob/dd2349525461e533a61ab140e746714738a59dd5/rest_framework/fields.py#L301-L303)

```python
# If `required` is unset, then use `True` unless a default is provided.
if required is None:
    required = default is empty and not read_only
```

The trace deletes `user01` from the database without touching the index:

```text
6. A row deleted from the DB but still in the index (user01)
  load_all=True   count=25  results=10  first=user02  search calls (start, end)=[(0, 1), (0, 10), (10, 20)]  SQL queries=2
    haystack log: Object could not be found in database for SearchResult '<SearchResult: auth.user (pk='1')>'.
  load_all=False  AttributeError: Got AttributeError when attempting to get a value for field `contact` on serializer `UserSerializer`.
    haystack log: Object could not be found in database for SearchResult '<SearchResult: auth.user (pk='1')>'.
  load_all=False, read_only=True field: {"username": "user01"}
    haystack log: Object could not be found in database for SearchResult '<SearchResult: auth.user (pk='1')>'.
  load_all=False, method field: AttributeError: 'NoneType' object has no attribute 'email'
```

| Field reading `.object`, row gone                         | Result                                                                            |
| --------------------------------------------------------- | --------------------------------------------------------------------------------- |
| `CharField(source="object.email")`                        | `AttributeError: Got AttributeError when attempting to get a value for field ...` |
| the same with `read_only=True`                            | the key is silently missing                                                       |
| the same with `allow_null=True` or a `default`            | `null` or the default                                                             |
| `SerializerMethodField` returning `instance.object.email` | `AttributeError: 'NoneType' object has no attribute 'email'`                      |
| any field, with `load_all = True`                         | the result is dropped and the page refilled; `count` is too high                  |

Keep the index in step with deletes. `update_index --remove` deletes documents whose rows no longer exist:

Source: [`haystack/management/commands/update_index.py#L202-L208`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/management/commands/update_index.py#L202-L208)

```python
parser.add_argument(
    "-r",
    "--remove",
    action="store_true",
    default=False,
    help="Remove objects from the index that are no longer present in the database.",
)
```

and `RealtimeSignalProcessor` removes a document on `post_delete`:

Source: [`haystack/signals.py#L76-L79`](https://github.com/django-haystack/django-haystack/blob/eb05f193c9771a68dcc8cfac6674a0d48a52ee9d/haystack/signals.py#L76-L79)

```python
def setup(self):
    # Naive (listen to all model saves).
    models.signals.post_save.connect(self.handle_save)
    models.signals.post_delete.connect(self.handle_delete)
```

______________________________________________________________________

## 9. Quick Reference

| Symptom                                                                                               | Cause                                                                   | Fix                                                                      |
| ----------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| A name in `Meta.fields` is missing from the response                                                  | not an index field (section 6)                                          | declare it: `CharField(source="object.<column>")`                        |
| A declared field is always `null`                                                                     | its plain `source` names something the hit doesn't carry (section 7)    | `source="object.<column>"`, or index the column                          |
| The response has `<name>_exact` keys                                                                  | shadow fields of `faceted=True` fields, `Meta.fields` unset (section 6) | list the fields, or add them to `ignore_fields`                          |
| Values lag behind the database                                                                        | index fields come from the search document (section 7)                  | reindex, or read the column through `.object`                            |
| One SQL query per result                                                                              | `load_all = False`, the default (section 5)                             | `load_all = True` on the view                                            |
| One extra query per result for a relation                                                             | `read_queryset()` without `select_related()` (section 5)                | override `read_queryset()`                                               |
| `Got AttributeError when attempting to get a value for field` or `'NoneType' object has no attribute` | row deleted but still indexed, without `load_all` (section 8)           | `update_index --remove`, `RealtimeSignalProcessor`, or `load_all = True` |
| `count` is higher than the rows that exist                                                            | rows deleted but still indexed (section 8)                              | `update_index --remove` or `RealtimeSignalProcessor`                     |
