# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Show how a Solr core is split into segments and how each segment defines a field.

Reads /admin/segments?fieldInfo=true. Without field names it lists the segments;
with field names it prints, per segment, the index options, doc values type and
points of each field (the terms Lucene uses in "cannot change field ... to
inconsistent ..." errors) and Solr's own comparison with the current schema, which
checks indexed, docValues, norms, term vectors and offsets but not analysis or
point sizes.

    uv run scripts/field_segments.py http://localhost:8983/solr/<core> [field ...]

It lists the segments of the latest hard commit, so hard-commit first.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

DOC_VALUES = {
    "----": "NONE",
    "Dnum": "NUMERIC",
    "Dbin": "BINARY",
    "Dsrt": "SORTED",
    "Dsrn": "SORTED_NUMERIC",
    "Dsrs": "SORTED_SET",
}


def fetch(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        sys.exit(f"{url}: HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
    except urllib.error.URLError as e:
        sys.exit(f"{url}: {e.reason}")


def decode(flags: str) -> tuple[str, str, str]:
    """Split a segments-API flag string ("IDsrt-OF----:1:1:4") into
    (index options, doc values type, points)."""
    base, _, points = flags.partition(":")
    if base[0] == "-":
        index_options = "NONE"
    elif base[7] == "F":
        index_options = "DOCS"
    elif base[8] == "P":
        index_options = "DOCS_AND_FREQS"
    elif base[9] == "H":
        index_options = "DOCS_AND_FREQS_AND_POSITIONS_AND_OFFSETS"
    else:
        index_options = "DOCS_AND_FREQS_AND_POSITIONS"
    doc_values = DOC_VALUES.get(base[1:5], base[1:5])
    if points:
        dims, index_dims, num_bytes = points.split(":")
        points = f"{dims}d/{index_dims}d, {num_bytes} bytes"
    return index_options, doc_values, points or "-"


def table(rows: list[list[str]]) -> None:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "core_url", help="for example http://localhost:8983/solr/mycore"
    )
    parser.add_argument("fields", nargs="*", help="field names to inspect")
    args = parser.parse_args()
    core = args.core_url.rstrip("/")

    data = fetch(f"{core}/admin/segments?fieldInfo=true&wt=json")
    info, segments = data["info"], data["segments"]
    created = {s.get("createdVersionMajor") for s in segments.values()} - {None}
    created_major = min(created) if created else None
    mode = "strict" if created_major and created_major >= 9 else "lenient"
    print(
        f"{core}: {info['numSegments']} segments, maxDoc {info['totalMaxDoc']}, "
        f"Lucene {info.get('commitLuceneVersion', '?')}"
    )
    if created_major:
        print(
            f"index created by Lucene {created_major}: {mode} field consistency checks"
        )

    if not args.fields:
        rows = [["segment", "source", "docs", "deleted", "fields"]]
        for name, s in segments.items():
            fields = s.get("fields")
            rows.append(
                [
                    name,
                    s.get("source", "?"),
                    str(s["size"]),
                    str(s["delCount"]),
                    ", ".join(sorted(fields)) if fields else "?",
                ]
            )
        if len(rows) > 1:
            table(rows)
        return

    for field in args.fields:
        print(f'\nfield "{field}"')
        rows = [
            [
                "segment",
                "source",
                "docs",
                "deleted",
                "docs with terms",
                "index options",
                "doc values",
                "points",
                "schema check",
            ]
        ]
        definitions: dict[tuple[str, str, str], list[str]] = {}
        schema_type = None
        for name, s in segments.items():
            fi = (s.get("fields") or {}).get(field)
            if fi is None:
                continue
            schema_type = fi.get("schemaType", schema_type)
            definition = decode(fi["flags"])
            definitions.setdefault(definition, []).append(name)
            mismatch = fi.get("nonCompliant") or {}
            detail = "; ".join(
                f"{k} {v}" for k, v in mismatch.items() if k != "schemaField"
            )
            rows.append(
                [
                    name,
                    s.get("source", "?"),
                    str(s["size"]),
                    str(s["delCount"]),
                    str(fi.get("docCount", 0)),
                    *definition,
                    f"differs: {detail}" if mismatch else "same",
                ]
            )
        if len(rows) == 1:
            print(
                "  in no segment. If writes of it still fail, the IndexWriter holds the old"
            )
            print("  definition in memory: RELOAD the core.")
            continue
        print(f"  schema type now: {schema_type}")
        table(rows)
        if len(definitions) > 1:
            print("  definitions on disk:")
            for (opts, dv, pts), names in definitions.items():
                print(
                    f"    index options={opts}, doc values={dv}, points={pts}: {', '.join(names)}"
                )


if __name__ == "__main__":
    main()
