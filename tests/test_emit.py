from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.db import migrations, models

from nextgensquash import loading
from nextgensquash.emit import Emitter, FileWriter, SquashFile

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
    monkeypatch.setattr("nextgensquash.emit._managed_table_columns", lambda: {"app_thing": frozenset({"a", "b"})})
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


def _tail_emitter() -> Emitter:
    emitter = object.__new__(Emitter)
    emitter.app = "app"
    emitter.squasher = SimpleNamespace(cutoff=date(2026, 1, 1), max_number=lambda app: 42)
    return emitter


def _squash(name: str, dependencies: list[tuple[str, str]] | None = None) -> SquashFile:
    return SquashFile(app="app", name=name, operations=[], dependencies=dependencies or [], replaces=[])


def test_connect_stub_to_tail_adds_the_stub_to_the_first_tail_only():
    emitter = _tail_emitter()
    stub = ("app", emitter.STUB_NAME)
    files = [
        _squash(emitter.STUB_NAME),
        _squash(emitter.INITIAL_NAME, [stub]),
        _squash(emitter.FINALIZE_NAME, [("app", emitter.INITIAL_NAME)]),
        _squash(emitter.SCHEMA_ADDONS_NAME, [("app", emitter.FINALIZE_NAME)]),
    ]

    emitter._connect_stub_to_tail(files)

    assert stub in files[2].dependencies
    assert stub not in files[3].dependencies


def test_connect_stub_to_tail_refuses_a_stub_without_a_tail():
    emitter = _tail_emitter()

    with pytest.raises(RuntimeError, match="second leaf"):
        emitter._connect_stub_to_tail([_squash(emitter.STUB_NAME), _squash(emitter.INITIAL_NAME)])


@pytest.mark.parametrize(
    ("replaces", "writes_run_before"),
    [
        ([("app", "0001_initial")], False),
        ([], True),
    ],
)
def test_run_before_is_written_only_once_the_squash_replaces_nothing(tmp_path, replaces, writes_run_before):
    if not settings.configured:
        settings.configure()
    squash = SquashFile(
        app="app",
        name="0001_squash_2026_01_01_initial",
        operations=[],
        dependencies=[],
        replaces=replaces,
        run_before=[("oauth2_provider", "0001_initial")],
    )

    text = FileWriter(tmp_path).write(squash).read_text()

    assert ('("oauth2_provider", "0001_initial")' in text) is writes_run_before


def _restack_emitter(make_migration) -> Emitter:
    # Phase N over phase N-1: the old stub already claims the root that the config names.
    hidden_root = loading.MigrationRef(app="app", name="0001_initial_squashed_0010")
    old = [
        make_migration("app", "0000_squash_stub", replaces=[hidden_root]),
        make_migration("app", "0001_squash_2026_01_01_initial", replaces=[loading.MigrationRef("app", "0011_x")]),
        make_migration("app", "0012_y"),
    ]
    emitter = object.__new__(Emitter)
    emitter.app = "app"
    emitter.config = SimpleNamespace(stub_claims={"app": [hidden_root.key]})
    emitter.squasher = SimpleNamespace(old={m.ref.key: m for m in old})
    return emitter


def test_stub_claim_hidden_behind_a_prior_stub_stays_claimable(make_migration):
    assert ("app", "0001_initial_squashed_0010") in _restack_emitter(make_migration)._claimable_keys()


@pytest.mark.parametrize(
    ("emit_stub", "claims_prior_stub"),
    [
        (True, False),  # the new stub has the prior stub's name; claiming it drops the new stub
        (False, True),  # no new stub, so the prior one folds like any old migration
    ],
)
def test_initial_claims_the_prior_stub_only_without_a_new_stub(make_migration, emit_stub, claims_prior_stub):
    replaces = _restack_emitter(make_migration)._replaces(emit_stub)

    assert (("app", "0000_squash_stub") in replaces) is claims_prior_stub
    assert ("app", "0001_initial_squashed_0010") not in replaces
    assert ("app", "0011_x") in replaces


def test_forwarded_fk_add_from_a_prior_squash_keeps_one_guard(monkeypatch):
    add = "ALTER TABLE app_thing ADD CONSTRAINT thing_owner_fk FOREIGN KEY (a, b) REFERENCES app_owner (a, b) NOT VALID"
    guarded = (
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'thing_owner_fk') THEN\n"
        f"{add};\nEND IF; END $$;"
    )
    ops = _forwarder(
        monkeypatch, [("0045_squash_2026_01_01_schema_addons", migrations.RunSQL(guarded))]
    )._collect_index_runsql_ops()

    assert [op.sql for op in ops] == [guarded]


@pytest.mark.parametrize(
    ("sql", "forwarded"),
    [
        ('CREATE INDEX CONCURRENTLY IF NOT EXISTS "idx_gone" ON "app_thing" ("dropped_col")', 0),
        ('CREATE INDEX "idx_two" ON "app_thing" USING btree ("a", b DESC)', 1),
        ('CREATE INDEX "idx_expr" ON "app_thing" (lower("a"), (b + 1))', 1),
    ],
)
def test_forwarded_index_on_a_dropped_column_is_left_out(monkeypatch, sql, forwarded):
    assert len(_forwarder(monkeypatch, [("0001", migrations.RunSQL(sql))])._collect_index_runsql_ops()) == forwarded
