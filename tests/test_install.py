from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from nextgensquash import install

STUB = "from django.db import migrations\n\n\nclass Migration(migrations.Migration):\n    replaces = []\n"


def test_overwritten_prior_stub_is_restored_not_deleted_on_uninstall(monkeypatch, tmp_path: Path):
    target = tmp_path / "app" / "migrations"
    target.mkdir(parents=True)
    (target / "0000_squash_stub.py").write_text(STUB)
    emitted = tmp_path / "out" / "app" / "migrations"
    emitted.mkdir(parents=True)
    (emitted / "0000_squash_stub.py").write_text(STUB)
    (emitted / "0001_squash_2026_01_01_initial.py").write_text(STUB)
    monkeypatch.setattr(install, "app_migration_dirs", lambda: {"app": target})
    monkeypatch.setattr(install, "_compute_all_app_graph_leaves", lambda apps: {})

    install._run_install(argparse.Namespace(input_dir=tmp_path / "out"))

    log = (tmp_path / "out" / "INSTALLED.txt").read_text()
    installed, rest = log.split("STRIPPED:")
    edited = rest.split("EDITED:")[1]
    assert "0000_squash_stub.py" not in installed
    assert "0000_squash_stub.py" in edited
    assert "0001_squash_2026_01_01_initial.py" in installed


@pytest.mark.parametrize(("nested", "keeps_prior_squash"), [(False, False), (True, True)])
def test_install_keeps_a_claimed_prior_squash_only_when_nested(monkeypatch, tmp_path: Path, nested, keeps_prior_squash):
    target = tmp_path / "app" / "migrations"
    target.mkdir(parents=True)
    prior = target / "0001_squash_2025_01_01_initial.py"
    prior.write_text(STUB.replace("replaces = []", 'replaces = [("app", "0001_x")]'))
    out = tmp_path / "out"
    emitted = out / "app" / "migrations"
    emitted.mkdir(parents=True)
    claims_prior = 'replaces = [("app", "0001_squash_2025_01_01_initial"), ("app", "0001_x")]'
    (emitted / "0001_squash_2026_01_01_initial.py").write_text(STUB.replace("replaces = []", claims_prior))
    (out / "RETIRE_MANIFEST.json").write_text(json.dumps({"nested": nested}))
    monkeypatch.setattr(install, "app_migration_dirs", lambda: {"app": target})
    monkeypatch.setattr(install, "_compute_all_app_graph_leaves", lambda apps: {})

    install._run_install(argparse.Namespace(input_dir=out))

    assert prior.exists() is keeps_prior_squash
