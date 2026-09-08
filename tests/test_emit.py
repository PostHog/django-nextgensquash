from __future__ import annotations

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
