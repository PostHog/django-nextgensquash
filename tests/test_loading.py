from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from nextgensquash import loading
from nextgensquash.config import Config

CUTOFF = date(2026, 1, 1)
OLD_DATE = date(2025, 6, 1)
YOUNG_DATE = date(2026, 6, 1)


class _FakeRunSQL:
    def __init__(self, sql):
        self.sql = sql


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1", "SELECT 1"),
        (["SELECT 1", "SELECT 2"], "SELECT 1 SELECT 2"),
        ([("SELECT %s", [1]), ("SELECT 2", None)], "SELECT %s SELECT 2"),
        (["SELECT 1", ("SELECT %s", [1])], "SELECT 1 SELECT %s"),
        (["SELECT 1", 42], "SELECT 1 42"),
    ],
)
def test_op_sql_text_flattens_every_runsql_shape(sql, expected):
    assert loading._op_sql_text(_FakeRunSQL(sql)) == expected


def test_git_dates_parse_keeps_the_first_commit_that_added_a_file():
    stdout = (
        "__C__2025-01-02T03:04:05+00:00\n"
        "app_a/migrations/0001_initial.py\n"
        "app_b/migrations/0002_thing.py\n"
        "\n"
        "__C__2026-05-06T07:08:09+00:00\n"
        "app_a/migrations/0001_initial.py\n"
        "app_c/migrations/0003_other.py\n"
    )

    parsed = loading.GitDates(Path("/repo"))._parse(stdout)

    assert parsed == {
        Path("/repo/app_a/migrations/0001_initial.py"): date(2025, 1, 2),
        Path("/repo/app_b/migrations/0002_thing.py"): date(2025, 1, 2),
        Path("/repo/app_c/migrations/0003_other.py"): date(2026, 5, 6),
    }


def test_git_dates_parse_ignores_paths_before_the_first_commit_line():
    assert loading.GitDates(Path("/repo"))._parse("app_a/migrations/0001_initial.py\n") == {}


@pytest.mark.parametrize(
    ("app", "name", "commit_date", "include_prior_squashes", "expected"),
    [
        ("core", "0001_initial", OLD_DATE, True, "old"),
        ("core", "0002_recent", YOUNG_DATE, True, "young"),
        ("core", "0003_undated", None, True, "young"),
        ("core", "0004_squash_2026_01_01_initial", YOUNG_DATE, True, "old"),
        ("core", "0004_squash_2026_01_01_initial", YOUNG_DATE, False, "young"),
        ("core", "0005_squash_stub", YOUNG_DATE, True, "old"),
        ("core", "0006_finalize_fks", YOUNG_DATE, True, "old"),
        ("core", "0007_schema_addons", YOUNG_DATE, True, "old"),
        ("core", "0008_thing_squashed_initial", YOUNG_DATE, True, "old"),
        ("core", "0001_initial_squashed_0284_caching", YOUNG_DATE, True, "young"),
        ("frozen", "0001_initial", OLD_DATE, True, "young"),
    ],
)
def test_partition_buckets(make_migration, app, name, commit_date, include_prior_squashes, expected):
    migration = make_migration(app, name, commit_date=commit_date)
    tree = loading.MigrationTree({migration.ref.key: migration}, Config(frozen_apps=frozenset({"frozen"})))

    old, young = tree.partition(CUTOFF, include_prior_squashes=include_prior_squashes)

    bucket = "old" if migration.ref.key in old else "young"
    assert bucket == expected
