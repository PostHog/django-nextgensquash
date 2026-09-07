"""Project-specific knobs, plus the app discovery every other module builds on."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.apps import apps as django_apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

SETTINGS_KEY = "NEXTGENSQUASH"


def project_root() -> Path:
    """The repo root, taken from `settings.BASE_DIR`.

    Read lazily so importing this package never needs a configured Django.
    """
    base_dir = getattr(settings, "BASE_DIR", None)
    if base_dir is None:
        raise ImproperlyConfigured(
            "nextgensquash needs BASE_DIR in your Django settings — it is the root "
            "the migration files are discovered and git-dated against"
        )
    return Path(base_dir)


def app_migration_dirs() -> dict[str, Path]:
    """Map app_label -> on-disk migrations directory, for every installed app."""
    out: dict[str, Path] = {}
    for cfg in django_apps.get_app_configs():
        candidate = Path(cfg.path) / "migrations"
        if candidate.is_dir():
            out[cfg.label] = candidate
    return out


def managed_app_migration_dirs(config: Config) -> dict[str, Path]:
    """The subset of `app_migration_dirs()` this project owns.

    An app is managed when its migrations directory resolves inside
    `project_root()` and no path component is `site-packages` (a virtualenv can
    live inside the project root), minus `Config.ignored_apps`.
    """
    root = project_root().resolve()
    out: dict[str, Path] = {}
    for label, migrations_dir in app_migration_dirs().items():
        if label in config.ignored_apps:
            continue
        resolved = migrations_dir.resolve()
        if not resolved.is_relative_to(root):
            continue
        if "site-packages" in resolved.parts:
            continue
        out[label] = migrations_dir
    return out


@dataclass(frozen=True)
class Config:
    """The project-specific inputs a squash run needs.

    Built from the `NEXTGENSQUASH` settings dict; see `from_dict` for the keys.
    """

    # Apps skipped entirely, as if they were third-party. Nothing is loaded
    # from them, nothing is folded, and no squash file is emitted for them.
    ignored_apps: frozenset[str] = frozenset()

    # Apps whose migrations are never folded, regardless of cutoff. Use for apps
    # that rely on SeparateDatabaseAndState + RunPython to create non-Django DDL
    # (partitioned tables, custom views, materialized columns) — Django's
    # CreateModel can't reproduce that DDL, and folding would silently drop it.
    frozen_apps: frozenset[str] = frozenset()

    # Standalone (no FK in/out) models that must be created in the stub so they
    # exist before any other migration runs. Mostly to dodge races against work
    # a migrate wrapper kicks off in parallel with `manage.py migrate` (e.g. a
    # ClickHouse migration step reading an instance-settings table).
    # Lowercase model names, per ModelState.name.lower().
    early_models: dict[str, frozenset[str]] = field(default_factory=dict)

    # The stub owns ("<app>", "__first__"). Third-party migrations already
    # applied on live DBs resolve swappable deps (AUTH_USER_MODEL, swapped
    # OAuth models) to exactly that sentinel, and check_consistent_history
    # then demands the stub be applied — its squash exemption only fires for
    # nodes with a non-empty `replaces`. Claim the app's ROOT node so the
    # exemption applies and check_replacements stamps the stub. Two rules:
    # the claim MUST be parentless (a mid-chain claim weaves stub and initial
    # into a CircularDependencyError, because remove_replaced_nodes re-parents
    # the claimed node's neighbours onto both replacement nodes), and it must
    # be a node that exists in the EMIT-TIME graph — the loader substitutes a
    # historical squash for the ancient names it replaces, so "0001_initial"
    # is not a node on a clean tree. Only swappable-target apps need this.
    stub_claims: dict[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)

    # Module that provides AddFieldIfMissing, AddIndexIfMissing,
    # AddConstraintIfMissing, and AlterUniqueTogetherIfMissing. Generated
    # finalize files import it by path wherever migrations run. Point it at a
    # copy of `nextgensquash/operations.py` inside the project to keep the
    # package a dev-only tool.
    operations_module: str = "nextgensquash.operations"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Config:
        """Build a Config from the `NEXTGENSQUASH` settings dict.

        Keys: IGNORED_APPS (list of app labels), FROZEN_APPS (list of app
        labels), EARLY_MODELS (app label -> list of lowercase model names),
        STUB_CLAIMS (app label -> list of (app, migration name) pairs),
        OPERATIONS_MODULE (dotted module path, see `operations_module`).
        """
        known = {"IGNORED_APPS", "FROZEN_APPS", "EARLY_MODELS", "STUB_CLAIMS", "OPERATIONS_MODULE"}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ImproperlyConfigured(f"unknown {SETTINGS_KEY} key(s): {', '.join(unknown)}")
        return cls(
            ignored_apps=frozenset(raw.get("IGNORED_APPS") or ()),
            frozen_apps=frozenset(raw.get("FROZEN_APPS") or ()),
            early_models={app: frozenset(names) for app, names in (raw.get("EARLY_MODELS") or {}).items()},
            stub_claims={
                app: tuple((str(a), str(n)) for a, n in claims)
                for app, claims in (raw.get("STUB_CLAIMS") or {}).items()
            },
            operations_module=raw.get("OPERATIONS_MODULE") or cls.operations_module,
        )

    @classmethod
    def from_settings(cls) -> Config:
        return cls.from_dict(getattr(settings, SETTINGS_KEY, {}) or {})
