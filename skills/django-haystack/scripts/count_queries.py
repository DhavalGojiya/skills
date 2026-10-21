# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "django==5.2.17",
#     "django-haystack==3.4.0",
#     "djangorestframework==3.18.1",
#     "drf-haystack==1.9.3",
#     "pysolr==3.11.0",
#     "whoosh==2.7.4",
# ]
# ///
"""Count search requests and SQL queries for each way of reading a SearchQuerySet.

Indexes 100 auth.User rows, then reads them through len(), slices, single items,
loops with HAYSTACK_ITERATOR_LOAD_PER_QUERY at 10, 50 and 200, and a drf-haystack
page, each with and without load_all(). Uses a temporary Whoosh index by default.
With --solr-url it counts real HTTP requests to that Solr core instead, adding the
fields it needs through the Schema API; point it at an empty, disposable core.
"""

import argparse
import json
import logging
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import warnings

import django
from django.conf import settings

# Whoosh 2.7.4 predates Python 3.12 escape-sequence warnings.
warnings.filterwarnings("ignore", category=SyntaxWarning)

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument(
    "--solr-url", help="Solr core URL, e.g. http://127.0.0.1:58983/solr/trace"
)
ARGS = parser.parse_args()

INDEX_DIR = tempfile.mkdtemp(prefix="haystack-count-")
if ARGS.solr_url:
    ENGINE = {"ENGINE": "haystack.backends.solr_backend.SolrEngine"}
    ENGINE["URL"] = ARGS.solr_url
else:
    ENGINE = {"ENGINE": "haystack.backends.whoosh_backend.WhooshEngine"}
    ENGINE["PATH"] = INDEX_DIR
settings.configure(
    ALLOWED_HOSTS=["testserver"],
    INSTALLED_APPS=[
        "django.contrib.contenttypes",
        "django.contrib.auth",
        "haystack",
        "rest_framework",
    ],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    HAYSTACK_CONNECTIONS={"default": ENGINE},
)
django.setup()
logging.getLogger("haystack").handlers = [logging.NullHandler()]

import haystack.query
import pysolr
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from drf_haystack.serializers import HaystackSerializer
from drf_haystack.viewsets import HaystackViewSet
from haystack import connections, indexes
from haystack.backends.whoosh_backend import WhooshSearchBackend
from haystack.query import SearchQuerySet
from haystack.utils.loading import UnifiedIndex
from rest_framework import serializers
from rest_framework.pagination import PageNumberPagination
from rest_framework.test import APIRequestFactory

ROWS = 100


class UserIndex(indexes.SearchIndex, indexes.Indexable):
    text = indexes.CharField(document=True, model_attr="username")
    username = indexes.CharField(model_attr="username")

    def get_model(self):
        return User


REQUESTS = []

if ARGS.solr_url:
    _original_select = pysolr.Solr._select

    def counting_select(self, params, handler=None):
        """Record one HTTP request to /select as (start, rows)."""
        params = dict(params)
        REQUESTS.append((params.get("start"), params.get("rows")))
        return _original_select(self, params, handler)

    pysolr.Solr._select = counting_select
else:
    _original_search = WhooshSearchBackend.search

    def counting_search(self, query_string, **kwargs):
        start, end = kwargs.get("start_offset"), kwargs.get("end_offset")
        REQUESTS.append((start, None if end is None else end - start))
        return _original_search(self, query_string, **kwargs)

    WhooshSearchBackend.search = counting_search


def add_solr_fields(core_url):
    """Add the single-valued fields UserIndex writes, if the core lacks them."""
    fields = {
        "django_ct": "string",
        "django_id": "string",
        "text": "text_general",
        "username": "string",
    }
    for name, field_type in fields.items():
        try:
            urllib.request.urlopen(f"{core_url}/schema/fields/{name}", timeout=10)
            continue
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
        field = {"name": name, "type": field_type, "multiValued": False}
        body = {"add-field": {**field, "stored": True, "indexed": True}}
        request = urllib.request.Request(
            f"{core_url}/schema",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(request, timeout=10)


class ObjectSerializer(HaystackSerializer):
    contact = serializers.CharField(source="object.email")

    class Meta:
        index_classes = (UserIndex,)
        fields = ("username",)


class FiftyPerPage(PageNumberPagination):
    page_size = 50


def drf_page(load_all):
    view_class = type(
        "UserSearchView",
        (HaystackViewSet,),
        {
            "index_models": [User],
            "serializer_class": ObjectSerializer,
            "pagination_class": FiftyPerPage,
            "authentication_classes": [],
            "permission_classes": [],
            "load_all": load_all,
        },
    )
    response = view_class.as_view({"get": "list"})(APIRequestFactory().get("/"))
    return len(response.data["results"])


def sqs():
    return SearchQuerySet().models(User)


def read_fields(results):
    return sum(1 for result in results if result.username)


def build_only():
    sqs().all()
    return 0


def read_objects(results):
    return sum(1 for result in results if result.object.email)


def cases():
    yield "sqs.all(), nothing read", build_only
    yield "len(sqs)", lambda: len(sqs())
    yield "if sqs: (truthiness)", lambda: 1 if sqs() else 0
    yield "sqs[7].object", lambda: read_objects([sqs()[7]])
    yield "sqs[0:50], index fields", lambda: read_fields(sqs()[0:50])
    yield "sqs[0:50], .object", lambda: read_objects(sqs()[0:50])
    yield "sqs.load_all()[0:50], .object", lambda: read_objects(sqs().load_all()[0:50])
    yield "sqs[50:] (no end), index fields", lambda: read_fields(sqs()[50:])
    for chunk in (10, 50, 200):
        yield f"chunk {chunk}", None
        yield "  for r in sqs, index fields", lambda: read_fields(sqs())
        yield "  for r in sqs, .object", lambda: read_objects(sqs())
        yield (
            "  for r in sqs.load_all(), .object",
            lambda: read_objects(sqs().load_all()),
        )
        yield "  list(sqs.load_all())", lambda: len(list(sqs().load_all()))
    yield "drf-haystack page of 50", None
    yield "  load_all = False, field reads .object", lambda: drf_page(False)
    yield "  load_all = True, field reads .object", lambda: drf_page(True)


def main():
    call_command("migrate", verbosity=0)
    User.objects.bulk_create(
        User(username=f"user{i:03}", email=f"u{i:03}@x.io") for i in range(1, ROWS + 1)
    )
    if ARGS.solr_url:
        add_solr_fields(ARGS.solr_url.rstrip("/"))
    unified = UnifiedIndex()
    unified.build(indexes=[UserIndex()])
    connections["default"]._index = unified
    backend = connections["default"].get_backend()
    backend.clear()
    backend.update(unified.get_index(User), User.objects.all())

    print(f"{'Solr ' + ARGS.solr_url if ARGS.solr_url else 'Whoosh'}, {ROWS} rows")
    header = f"{'read':40} {'got':>4} {'requests':>8} {'SQL':>4} {'ms':>4}"
    print(f"{header}  (start, rows) each")
    for label, run in cases():
        if run is None:
            chunk = label.removeprefix("chunk ")
            if chunk.isdigit():
                haystack.query.ITERATOR_LOAD_PER_QUERY = int(chunk)
                label = f"HAYSTACK_ITERATOR_LOAD_PER_QUERY = {chunk}"
            else:
                haystack.query.ITERATOR_LOAD_PER_QUERY = 10
            print(label)
            continue
        REQUESTS.clear()
        started = time.perf_counter()
        with CaptureQueriesContext(connection) as queries:
            got = run()
        elapsed = (time.perf_counter() - started) * 1000
        shown = REQUESTS if len(REQUESTS) <= 4 else [*REQUESTS[:3], "..."]
        print(
            f"{label:40} {got:>4} {len(REQUESTS):>8} {len(queries):>4}"
            f" {elapsed:>4.0f}  {shown}"
        )


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(INDEX_DIR, ignore_errors=True)
