from __future__ import annotations

from datetime import date

import pytest

from nextgensquash import loading, planning
from nextgensquash.config import Config

CUTOFF = date(2026, 1, 1)
OLD_DATE = date(2025, 6, 1)
YOUNG_DATE = date(2026, 6, 1)


def _sql_created_names(sql: str) -> set[str]:
    names: set[str] = set()
    for rx in planning.Squasher._SQL_CREATED_NAME_RES:
        for quoted, bare in rx.findall(sql):
            names.add(quoted or bare)
    return names


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            'ALTER TABLE t ADD CONSTRAINT "unique group types for project" UNIQUE (a)',
            {"unique group types for project"},
        ),
        ("ALTER TABLE t ADD CONSTRAINT core_team_uniq UNIQUE (a)", {"core_team_uniq"}),
        ('CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "idx with spaces" ON t (a)', {"idx with spaces"}),
        ("CREATE INDEX idx_bare ON t (a)", {"idx_bare"}),
        ("create index if not exists idx_lower on t (a)", {"idx_lower"}),
        ("DROP INDEX idx_bare", set()),
    ],
)
def test_sql_created_name_extraction(sql, expected):
    assert _sql_created_names(sql) == expected


def _squasher_for(make_migration, young_sql: str) -> planning.Squasher:
    migrations = [
        make_migration("core", "0001_initial", commit_date=OLD_DATE),
        make_migration(
            "core",
            "0002_index",
            commit_date=OLD_DATE,
            operations=[loading.OpInfo(kind="RunSQL", sql="CREATE INDEX idx_foo ON core_thing (a)")],
        ),
        make_migration(
            "core",
            "0003_young",
            commit_date=YOUNG_DATE,
            operations=[loading.OpInfo(kind="RunSQL", sql=young_sql)],
        ),
    ]
    tree = loading.MigrationTree({m.ref.key: m for m in migrations}, Config())
    return planning.Squasher(tree, CUTOFF, min_young=0)


@pytest.mark.parametrize(
    ("young_sql", "creator_stays_folded"),
    [
        ("SELECT 1 FROM idx_foo", False),
        ("SELECT 1 FROM idx_foobar", True),
        ("SELECT 1 FROM prefix_idx_foo", True),
        ("ALTER INDEX idx_foo RENAME TO idx_bar", False),
        # A young re-create is order-independent: the forwarded copy is IF NOT EXISTS.
        ("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_foo ON core_thing (a)", True),
        # A rebuild that drops first still needs the object to exist.
        ("DROP INDEX idx_foo; CREATE INDEX idx_foo ON core_thing (a)", False),
    ],
)
def test_young_sql_pulls_only_on_a_whole_token_match(make_migration, young_sql, creator_stays_folded):
    squasher = _squasher_for(make_migration, young_sql)

    assert (("core", "0002_index") in squasher.old) is creator_stays_folded


def test_pulling_the_root_migration_asks_for_a_higher_cutoff(make_migration):
    migrations = [
        make_migration(
            "core",
            "0001_initial",
            commit_date=OLD_DATE,
            operations=[loading.OpInfo(kind="RunSQL", sql="CREATE INDEX idx_root ON core_thing (a)")],
        ),
        make_migration(
            "core",
            "0002_young",
            commit_date=YOUNG_DATE,
            operations=[loading.OpInfo(kind="ValidateConstraint", target="idx_root")],
        ),
    ]
    tree = loading.MigrationTree({m.ref.key: m for m in migrations}, Config())

    with pytest.raises(RuntimeError, match="bump the cutoff"):
        planning.Squasher(tree, CUTOFF, min_young=0)


def _run_before_tree(make_migration, young_date: date) -> loading.MigrationTree:
    # `other` sits first so the min-young pass meets the run_before source
    # while its target is still folded.
    migrations = [
        make_migration("other", "0001_initial", commit_date=OLD_DATE),
        make_migration("other", "0002_backfill", commit_date=young_date),
        make_migration("core", "0001_initial", commit_date=OLD_DATE),
        make_migration("core", "0002_target", commit_date=OLD_DATE),
    ]
    migrations[1].run_before = [loading.MigrationRef("core", "0002_target")]
    return loading.MigrationTree({m.ref.key: m for m in migrations}, Config())


def test_young_run_before_into_the_fold_asks_for_a_higher_cutoff(make_migration):
    tree = _run_before_tree(make_migration, young_date=YOUNG_DATE)

    with pytest.raises(RuntimeError, match="run before folded core.0002_target"):
        planning.Squasher(tree, CUTOFF, min_young=0)


def test_min_young_keeps_a_run_before_source_folded(make_migration):
    tree = _run_before_tree(make_migration, young_date=OLD_DATE)

    squasher = planning.Squasher(tree, CUTOFF, min_young=1)

    assert ("other", "0002_backfill") in squasher.old
    assert ("core", "0002_target") in squasher.young


def test_max_number_ignores_unnumbered_names(make_migration):
    migrations = [
        make_migration("core", "0001_initial", commit_date=OLD_DATE),
        make_migration("core", "1342_latest", commit_date=YOUNG_DATE),
        make_migration("core", "squashed_legacy", commit_date=OLD_DATE),
        make_migration("other", "0007_thing", commit_date=OLD_DATE),
    ]
    tree = loading.MigrationTree({m.ref.key: m for m in migrations}, Config())

    squasher = planning.Squasher(tree, CUTOFF, min_young=0)

    assert squasher.max_number("core") == 1342
    assert squasher.max_number("other") == 7
    assert squasher.max_number("missing") == 0
