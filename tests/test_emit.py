from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from django.db import migrations, models

from nextgensquash.emit import Emitter

DEFERRED = {("thing", "team")}


class _FakeRunSQL:
    def __init__(self, sql):
        self.sql = sql


@pytest.mark.parametrize(
    ("op", "expected"),
    [
        (migrations.AlterField(model_name="thing", name="team", field=models.IntegerField()), "AlterField thing.team"),
        (migrations.RemoveField(model_name="thing", name="team"), "RemoveField thing.team"),
        (migrations.AlterField(model_name="thing", name="other", field=models.IntegerField()), None),
        (
            migrations.AddIndex(model_name="thing", index=models.Index(fields=["team"], name="idx_team")),
            "AddIndex on thing references a deferred field",
        ),
        (migrations.AddIndex(model_name="thing", index=models.Index(fields=["other"], name="idx_other")), None),
        (migrations.RenameModel(old_name="Thing", new_name="Widget"), "RenameModel thing"),
        (migrations.RenameModel(old_name="Other", new_name="Widget"), None),
        (migrations.DeleteModel(name="Thing"), "DeleteModel thing"),
        (migrations.DeleteModel(name="Other"), None),
        (migrations.RunSQL("UPDATE thing SET team_id = 1"), "RunSQL mentions deferred column(s) ['team_id']"),
        (migrations.RunSQL("UPDATE thing SET other = 1"), None),
    ],
)
def test_deferred_violation_flags_only_ops_that_need_the_deferred_column(op, expected):
    assert Emitter._deferred_violation(op, DEFERRED) == expected


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1", "SELECT 1"),
        (["SELECT 1", "SELECT 2"], "SELECT 1;\nSELECT 2"),
        ([("SELECT %s", [1]), "SELECT 2"], "SELECT %s;\nSELECT 2"),
    ],
)
def test_runsql_text_keeps_statement_boundaries(sql, expected):
    assert Emitter._runsql_text(_FakeRunSQL(sql)) == expected


def test_idempotent_index_sql_keeps_a_plain_string_a_string():
    op = _FakeRunSQL("CREATE INDEX a ON t (x)")

    assert Emitter._idempotent_index_sql(op) == "CREATE INDEX IF NOT EXISTS a ON t (x)"


def test_idempotent_index_sql_keeps_list_elements_and_their_params():
    op = _FakeRunSQL(["CREATE INDEX a ON t (x)", ("CREATE INDEX b ON t (y) WHERE z = %s", [1])])

    assert Emitter._idempotent_index_sql(op) == [
        "CREATE INDEX IF NOT EXISTS a ON t (x)",
        ("CREATE INDEX IF NOT EXISTS b ON t (y) WHERE z = %s", [1]),
    ]


def _forwarder(monkeypatch, claimed: list[tuple[str, migrations.RunSQL]]) -> Emitter:
    # Enough of an Emitter for `_collect_index_runsql_ops`; no loader, no Django app registry.
    emitter = object.__new__(Emitter)
    emitter.app = "app"
    emitter.dropped_runsql = []
    emitter._forwarded_fk_constraint_names = set()
    monkeypatch.setattr(emitter, "_claimed_ops", lambda: iter(claimed))
    monkeypatch.setattr(emitter, "_final_state_index_names", lambda: set())
    monkeypatch.setattr("nextgensquash.emit._managed_table_names", lambda: frozenset({"app_thing"}))
    return emitter


@pytest.mark.parametrize(
    ("constraint_name", "forwarded"),
    [
        ("unique_thing", 0),  # renamed: a forwarded create would build a second index under the old name
        ("idx_thing", 0),  # same name: the constraint's index already answers IF NOT EXISTS
    ],
)
def test_using_index_constraint_cancels_the_forwarded_create(monkeypatch, constraint_name, forwarded):
    claimed = [
        ("0001", migrations.RunSQL('CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "idx_thing" ON "app_thing" ("a")')),
        (
            "0002",
            migrations.RunSQL(f"ALTER TABLE app_thing ADD CONSTRAINT {constraint_name} UNIQUE USING INDEX idx_thing"),
        ),
    ]
    assert len(_forwarder(monkeypatch, claimed)._collect_index_runsql_ops()) == forwarded


def test_forwarded_create_survives_without_a_consuming_constraint(monkeypatch):
    claimed = [
        ("0001", migrations.RunSQL('CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "idx_thing" ON "app_thing" ("a")'))
    ]
    assert len(_forwarder(monkeypatch, claimed)._collect_index_runsql_ops()) == 1


def test_schema_addons_deps_skips_apps_on_another_database(monkeypatch):
    alias_of = {"app": "default", "sibling": "default", "routed": "other_db"}
    monkeypatch.setattr("nextgensquash.emit.connections", ["default", "other_db"])
    monkeypatch.setattr(
        "nextgensquash.emit.router",
        SimpleNamespace(allow_migrate=lambda alias, app_label, **hints: alias_of[app_label] == alias),
    )

    # Enough of an Emitter for `_schema_addons_deps`; no state, no planner, no cycle breaker.
    emitter = object.__new__(Emitter)
    emitter.app = "app"
    emitter.state = SimpleNamespace(models={(app, "thing"): None for app in alias_of})
    emitter.squasher = SimpleNamespace(
        cutoff=date(2026, 1, 1),
        max_number=lambda app: 42,
        old={app: SimpleNamespace(ref=SimpleNamespace(app=app)) for app in alias_of},
    )
    emitter.cycle_breaker = SimpleNamespace(deferred_for_app=lambda app: set())

    assert emitter._schema_addons_deps("0001_x") == [
        ("app", "0001_x"),
        ("sibling", "0001_squash_2026_01_01_initial"),
    ]
