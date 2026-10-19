# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "django==5.2.17",
#     "django-haystack==3.4.0",
#     "djangorestframework==3.18.1",
#     "drf-haystack==1.9.3",
#     "whoosh==2.7.4",
# ]
# ///
"""Build drf-haystack responses end to end and print where each value comes from.

Indexes 25 auth.User rows into a temporary Whoosh index, then serializes search
results with different serializer setups and a paginated HaystackViewSet.
Mirrors drf-haystack 1.9.3 on django-haystack 3.4.0. Needs no Django project.
"""

import json
import logging
import shutil
import sys
import tempfile
import warnings

import django
from django.conf import settings

# Whoosh 2.7.4 predates Python 3.12 escape-sequence warnings.
warnings.filterwarnings("ignore", category=SyntaxWarning)

INDEX_DIR = tempfile.mkdtemp(prefix="drf-haystack-trace-")
settings.configure(
    ALLOWED_HOSTS=["testserver"],
    INSTALLED_APPS=[
        "django.contrib.contenttypes",
        "django.contrib.auth",
        "haystack",
        "rest_framework",
    ],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    HAYSTACK_CONNECTIONS={
        "default": {
            "ENGINE": "haystack.backends.whoosh_backend.WhooshEngine",
            "PATH": INDEX_DIR,
        }
    },
)
django.setup()

# Haystack's AppConfig.ready() logs to stderr; print its messages inline instead.
_log_handler = logging.StreamHandler(sys.stdout)
_log_handler.setFormatter(logging.Formatter("    haystack log: %(message)s"))
logging.getLogger("haystack").handlers = [_log_handler]

from importlib.metadata import version

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from drf_haystack.serializers import (
    HaystackSerializer,
    HaystackSerializerMixin,
)
from drf_haystack.viewsets import HaystackViewSet
from haystack import connections, indexes
from haystack.backends.whoosh_backend import WhooshSearchBackend
from haystack.query import SearchQuerySet
from haystack.utils.loading import UnifiedIndex
from rest_framework import serializers
from rest_framework.pagination import PageNumberPagination
from rest_framework.test import APIRequestFactory


class UserIndex(indexes.SearchIndex, indexes.Indexable):
    text = indexes.CharField(document=True, model_attr="username")
    username = indexes.CharField(model_attr="username")
    name = indexes.CharField(model_attr="first_name")
    is_staff = indexes.BooleanField(model_attr="is_staff", faceted=True)

    def get_model(self):
        return User


def serializer(meta_fields, **declared):
    """Return a HaystackSerializer class with these Meta.fields and declared fields."""
    meta = type("Meta", (), {"index_classes": [UserIndex], "ignore_fields": ["text"]})
    if meta_fields is not ...:
        meta.fields = meta_fields
    return type("UserSerializer", (HaystackSerializer,), {**declared, "Meta": meta})


def first_result():
    return SearchQuerySet().models(User).filter(username="user02")[0]


def show(title, serializer_class, result):
    data = serializer_class(result).data
    print(f"  {title}")
    print(f"    serializer fields: {list(serializer_class().fields)}")
    print(f"    output:            {json.dumps(dict(data))}")


SEARCH_CALLS = []
_original_search = WhooshSearchBackend.search


def counting_search(self, query_string, **kwargs):
    SEARCH_CALLS.append((kwargs.get("start_offset"), kwargs.get("end_offset")))
    return _original_search(self, query_string, **kwargs)


WhooshSearchBackend.search = counting_search


class TenPerPage(PageNumberPagination):
    page_size = 10


def list_page(load_all, serializer_class):
    """GET a HaystackViewSet list page; return response data, search calls, SQL."""
    view_class = type(
        "UserSearchView",
        (HaystackViewSet,),
        {
            "index_models": [User],
            "serializer_class": serializer_class,
            "pagination_class": TenPerPage,
            "authentication_classes": [],
            "permission_classes": [],
            "load_all": load_all,
        },
    )
    view = view_class.as_view({"get": "list"})
    SEARCH_CALLS.clear()
    with CaptureQueriesContext(connection) as queries:
        response = view(APIRequestFactory().get("/"))
    return response.data, list(SEARCH_CALLS), len(queries)


def main():
    call_command("migrate", verbosity=0)
    User.objects.bulk_create(
        User(
            username=f"user{i:02}",
            first_name=f"First{i:02}",
            email=f"u{i:02}@x.io",
        )
        for i in range(1, 26)
    )
    unified = UnifiedIndex()
    unified.build(indexes=[UserIndex()])
    connections["default"]._index = unified
    connections["default"].get_backend().update(
        unified.get_index(User), User.objects.all()
    )
    result = first_result()

    packages = ["drf-haystack", "django-haystack", "djangorestframework", "django"]
    print(", ".join(f"{name} {version(name)}" for name in packages), end=", ")
    print(f"Python {sys.version.split()[0]}\n")

    print("1. Field resolution. Index fields: text, username, name, is_staff,")
    print("   is_staff_exact. email is a DB column only; nickname exists nowhere.")
    print("   ignore_fields = ['text'].")
    for names in (["username", "email"], ["username", "nickname"]):
        show(f"Meta.fields = {json.dumps(names)}", serializer(names), result)
    for value in (..., None, "", False, []):
        label = "Meta.fields not set" if value is ... else f"Meta.fields = {value!r}"
        show(label, serializer(value), result)
    show('Meta.fields = "username" (a string)', serializer("username"), result)

    print("\n2. Declared fields")
    show(
        'contact = CharField(source="email")',
        serializer(["username"], contact=serializers.CharField(source="email")),
        result,
    )
    show(
        'contact = CharField(source="object.email")',
        serializer(["username"], contact=serializers.CharField(source="object.email")),
        result,
    )

    print("\n3. HaystackSerializerMixin + ModelSerializer (data from .object)")

    class UserModelSerializer(HaystackSerializerMixin, serializers.ModelSerializer):
        class Meta:
            model = User
            fields = ("username", "email", "first_name")

    data = UserModelSerializer(result).data
    print(f"    output:            {json.dumps(dict(data))}")

    print("\n4. Index fields come from the stored search document, not the DB")
    User.objects.filter(username="user02").update(first_name="ChangedInDB")
    show(
        "first_name updated in the DB, no reindex",
        serializer(["name"], db_name=serializers.CharField(source="object.first_name")),
        first_result(),
    )

    print("\n5. Paginated HaystackViewSet list, page_size 10, field reads .object")
    page_serializer = serializer(
        ["username"], contact=serializers.CharField(source="object.email")
    )
    for load_all in (False, True):
        data, searches, sql = list_page(load_all, page_serializer)
        print(
            f"  load_all={load_all!s:5}  count={data['count']}"
            f"  results={len(data['results'])}"
            f"  search calls (start, end)={searches}  SQL queries={sql}"
        )

    print("\n6. A row deleted from the DB but still in the index (user01)")
    User.objects.filter(username="user01").delete()
    data, searches, sql = list_page(True, page_serializer)
    print(
        f"  load_all=True   count={data['count']}  results={len(data['results'])}"
        f"  first={data['results'][0]['username']}"
        f"  search calls (start, end)={searches}  SQL queries={sql}"
    )
    try:
        list_page(False, page_serializer)
    except AttributeError as exc:
        print(f"  load_all=False  {type(exc).__name__}: {str(exc).splitlines()[0]}")
    skip_serializer = serializer(
        ["username"],
        contact=serializers.CharField(source="object.email", read_only=True),
    )
    data, _, _ = list_page(False, skip_serializer)
    print(f"  load_all=False, read_only=True field: {json.dumps(data['results'][0])}")
    method_serializer = serializer(
        ["username"],
        contact=serializers.SerializerMethodField(),
        get_contact=lambda self, instance: instance.object.email,
    )
    try:
        list_page(False, method_serializer)
    except AttributeError as exc:
        print(f"  load_all=False, method field: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(INDEX_DIR, ignore_errors=True)
