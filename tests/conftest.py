from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from nextgensquash import loading


@pytest.fixture
def make_migration():
    def _make(
        app: str,
        name: str,
        commit_date: date | None = None,
        operations: list[loading.OpInfo] | None = None,
        replaces: list[loading.MigrationRef] | None = None,
        dependencies: list[loading.MigrationRef] | None = None,
    ) -> loading.Migration:
        return loading.Migration(
            ref=loading.MigrationRef(app=app, name=name),
            file_path=Path(f"/repo/{app}/migrations/{name}.py"),
            commit_date=commit_date,
            dependencies=dependencies or [],
            replaces=replaces or [],
            run_before=[],
            operations=operations or [],
        )

    return _make
