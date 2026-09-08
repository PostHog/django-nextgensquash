from __future__ import annotations

from nextgensquash import retire

MIGRATION_SOURCE = """from django.db import migrations


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("core", "0001_initial"),
        ("other", "0007_thing"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    run_before = [("core", "0009_later")]

    operations = [
        migrations.RunSQL("SELECT 1"),
    ]
"""


def test_transform_dependencies_drops_one_entry_and_leaves_the_rest_alone(tmp_path):
    path = tmp_path / "0002_thing.py"
    path.write_text(MIGRATION_SOURCE)

    changed = retire.transform_dependencies(path, lambda dep: None if dep == ("other", "0007_thing") else dep)

    assert changed is True
    result = path.read_text()
    assert '("core", "0001_initial"),' in result
    assert "0007_thing" not in result
    assert "migrations.swappable_dependency(settings.AUTH_USER_MODEL)," in result
    assert result.startswith("from django.db import migrations\n")
    assert 'migrations.RunSQL("SELECT 1"),' in result


def test_transform_dependencies_rewrites_and_collapses_duplicates(tmp_path):
    path = tmp_path / "0002_thing.py"
    path.write_text(MIGRATION_SOURCE)

    changed = retire.transform_dependencies(path, lambda dep: ("core", "0001_squash_initial"))

    assert changed is True
    assert path.read_text().count("0001_squash_initial") == 1


def test_transform_dependencies_reports_no_change_when_nothing_matches(tmp_path):
    path = tmp_path / "0002_thing.py"
    path.write_text(MIGRATION_SOURCE)

    assert retire.transform_dependencies(path, lambda dep: dep) is False
    assert path.read_text() == MIGRATION_SOURCE


YOUNG_SOURCE = """from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_folded"),
        ("core", "0001_ancient"),
        ("other", "0007_thing"),
    ]

    operations = []
"""

REPLACED_TO_APP = {"core/0004_folded": "core", "core/0001_ancient": "core"}
INITIALS = {"core": "0001_squash_2026_01_01_initial"}
CLAIMED_TO_STUB = {"core/0001_ancient": "0000_squash_stub"}


def test_rewrite_points_a_same_app_dep_at_the_initial_not_the_leaf(tmp_path):
    path = tmp_path / "0009_young.py"
    path.write_text(YOUNG_SOURCE)

    changed = retire._rewrite_deps_in_file(path, REPLACED_TO_APP, INITIALS, {})

    assert changed is True
    result = path.read_text()
    assert '("core", "0001_squash_2026_01_01_initial"),' in result
    assert "0004_folded" not in result
    assert '("other", "0007_thing"),' in result


def test_rewrite_points_a_stub_claimed_dep_at_the_stub(tmp_path):
    path = tmp_path / "0009_young.py"
    path.write_text(YOUNG_SOURCE)

    changed = retire._rewrite_deps_in_file(path, REPLACED_TO_APP, INITIALS, CLAIMED_TO_STUB)

    assert changed is True
    result = path.read_text()
    assert '("core", "0000_squash_stub"),' in result
    assert "0001_ancient" not in result
    assert '("core", "0001_squash_2026_01_01_initial"),' in result


def test_transform_dependencies_can_target_run_before(tmp_path):
    path = tmp_path / "0002_thing.py"
    path.write_text(MIGRATION_SOURCE)

    changed = retire.transform_dependencies(path, lambda dep: None, attr="run_before")

    assert changed is True
    result = path.read_text()
    assert "0009_later" not in result
    assert '("other", "0007_thing"),' in result
