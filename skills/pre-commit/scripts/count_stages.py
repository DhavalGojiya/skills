# /// script
# requires-python = ">=3.14"
# dependencies = ["identify"]
# ///
"""Print how many files survive each stage of pre-commit's file selection.

Mirrors pre-commit 4.x: stage 0 file source, stage 1 top-level files/exclude,
stage 2 os.path.lexists, stage 3 hook files/exclude, stage 4 types.
Run from the root of the repository being checked.
"""

import argparse
import os
import re
import subprocess

from identify.identify import tags_from_path


def git_files(all_files: bool) -> list[str]:
    if all_files:
        cmd = ["git", "ls-files", "-z", "--deduplicate"]
    else:
        cmd = [
            "git",
            "diff",
            "--staged",
            "--name-only",
            "--no-ext-diff",
            "-z",
            "--diff-filter=ACMRTUXB",
        ]
    out = subprocess.check_output(cmd).decode()
    return [p for p in out.split("\0") if p]


def include_exclude(paths: list[str], include: str, exclude: str) -> list[str]:
    include_re, exclude_re = re.compile(include), re.compile(exclude)
    return [p for p in paths if include_re.search(p) and not exclude_re.search(p)]


def by_types(
    paths: list[str],
    types: list[str],
    types_or: list[str],
    exclude_types: list[str],
) -> list[str]:
    all_of, any_of, none_of = (
        frozenset(types),
        frozenset(types_or),
        frozenset(exclude_types),
    )
    kept = []
    for p in paths:
        tags = tags_from_path(p)
        if tags >= all_of and (not any_of or tags & any_of) and not tags & none_of:
            kept.append(p)
    return kept


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-a",
        "--all-files",
        action="store_true",
        help="start from git ls-files instead of staged files",
    )
    parser.add_argument("--top-files", default="")
    parser.add_argument("--top-exclude", default="^$")
    parser.add_argument("--files", default="", help="hook-level files regex")
    parser.add_argument(
        "--exclude",
        default="^$",
        help="hook-level exclude regex",
    )
    parser.add_argument("--types", nargs="*", default=["file"])
    parser.add_argument("--types-or", nargs="*", default=[])
    parser.add_argument("--exclude-types", nargs="*", default=[])
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the paths left after stage 4",
    )
    args = parser.parse_args()

    s0 = git_files(args.all_files)
    s1 = include_exclude(s0, args.top_files, args.top_exclude)
    s2 = [p for p in s1 if os.path.lexists(p)]
    s3 = include_exclude(s2, args.files, args.exclude)
    s4 = by_types(s3, args.types, args.types_or, args.exclude_types)

    source = "git ls-files" if args.all_files else "staged files"
    rows = [
        (f"stage 0  {source}", s0),
        ("stage 1  top-level files/exclude", s1),
        ("stage 2  os.path.lexists", s2),
        ("stage 3  hook files/exclude", s3),
        ("stage 4  types/types_or/exclude_types", s4),
    ]
    width = max(len(name) for name, _ in rows)
    for name, paths in rows:
        print(f"{name:<{width}} : {len(paths)}")

    if args.list:
        print()
        print("\n".join(s4))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
