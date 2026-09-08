# django-nextgensquash

Rebuild a Django project's pre-cutoff migration history as a small from-scratch squash, per app, without breaking the databases that already ran the real history.

## What it does

Pick a cutoff date. Everything committed before it is "old" and gets folded; everything after it is "young" and stays as-is. For each app with old migrations the tool emits up to four files: `0000_squash_stub` (Postgres extensions, standalone early models, and the `__first__` anchor; only written when the app needs one), `0001_squash_<date>_initial` (one `CreateModel` per model in the final state, with `replaces=` listing every folded name), `<tip+1>_squash_<date>_finalize_fks` (the foreign keys, indexes, constraints, and `unique_together` tuples the initial had to leave out to break dependency cycles), and `<tip+2>_squash_<date>_schema_addons` (raw-SQL index and constraint DDL the `CreateModel` walk cannot reproduce). The two tail files are the app's graph leaf, so they take numbers after the app's current tip; Django numbers the next `makemigrations` output from the leaf, and this keeps the sequence going instead of restarting at 0004.

The state comes from Django's own `MigrationLoader`, so the squash is the project state at the cutoff rather than an optimizer pass over the operation list. A fresh database creates each table once instead of replaying years of `AddField`/`AlterField` churn.

Existing databases keep working. The `replaces=` list makes Django treat the whole folded chain as already applied, and the finalize and addons files carry no `replaces`, so Django plans them as ordinary migrations. Every operation in them probes the catalog first and skips when the object is already there (see `nextgensquash.operations`), so on an existing database they run once as fast no-ops and get recorded. Reversing them is a deliberate no-op, so a migration test that walks backwards does not drop schema the squash never created.

Django's own `squashmigrations` does not work at this scale. It squashes one app at a time, so cross-app dependency cycles have nowhere to go. It cannot fold `SeparateDatabaseAndState` (a model moved between apps) or `RunSQL` DDL, and `RunPython` blocks its optimizer, so a squash only helps in proportion to the operations it can actually remove. This tool rebuilds from the final state instead, computes a feedback-arc-minimizing apply order over the app graph, and defers the remaining back-edge foreign keys into `finalize_fks`.

## Install

```bash
uv add django-nextgensquash
```

The generated `finalize_fks` files import the idempotent operations by module path at migrate time. By default that path is `nextgensquash.operations`, which makes the package a runtime dependency of your project: it must be installed wherever migrations run, not only where you generate them.

To keep the package a dev-only tool instead, copy `src/nextgensquash/operations.py` into your project and point `OPERATIONS_MODULE` at it (see below). The generated files then import your copy, and the tool itself can run from an ephemeral environment:

```bash
uv run --with git+https://github.com/PostHog/django-nextgensquash python -m nextgensquash plan --cutoff 2026-08-21
```

## Configuration

Set `BASE_DIR` (Django's own setting, used as the repo root for file discovery and git dating) and add a `NEXTGENSQUASH` dict to your settings:

```python
NEXTGENSQUASH = {
    # Apps skipped entirely, as if they were third-party.
    "IGNORED_APPS": ["legacy_sessions"],

    # Apps whose migrations are never folded, regardless of cutoff. Use for apps
    # that rely on SeparateDatabaseAndState + RunPython to create non-Django DDL
    # (partitioned tables, custom views, materialized columns) - CreateModel
    # cannot reproduce that DDL, and folding would silently drop it.
    "FROZEN_APPS": ["warehouse_queue"],

    # Standalone (no FK in or out) models created in the stub so they exist
    # before any other migration runs. Use this when something running in
    # parallel with `manage.py migrate` reads a table early. Lowercase model
    # names.
    "EARLY_MODELS": {"myapp": ["instancesetting"]},

    # Parentless root nodes the stub claims via replaces=. Third-party
    # migrations already applied on live databases resolve swappable deps
    # (AUTH_USER_MODEL, swapped OAuth models) to ("<app>", "__first__"), and
    # check_consistent_history then demands the stub be applied - its squash
    # exemption only fires for nodes with a non-empty replaces. The claim must
    # be parentless, and must be a node that exists in the emit-time graph.
    # Only swappable-target apps need this.
    "STUB_CLAIMS": {"myapp": [["myapp", "0001_initial_squashed_0284_caching"]]},

    # Module the generated finalize files import for AddFieldIfMissing and
    # friends. Defaults to "nextgensquash.operations"; point it at an in-project
    # copy of that module to avoid a runtime dependency on this package.
    "OPERATIONS_MODULE": "myapp.migration_helpers.squash_operations",
}
```

An app counts as yours when its `AppConfig.path / "migrations"` is a directory inside `BASE_DIR` and no path component is `site-packages`. Unknown keys in the dict raise `ImproperlyConfigured`.

## Usage

Every subcommand needs a settings module, from `--settings` or `DJANGO_SETTINGS_MODULE`, and runs from the project root.

```bash
# What would the squash look like? Writes YAML: per-app counts, cross-app edges,
# detected cycles, dropped RunPython calls.
DJANGO_SETTINGS_MODULE=myproject.settings python -m nextgensquash plan --cutoff 2026-08-21

# Write the real migration files to a staging directory.
python -m nextgensquash emit --cutoff 2026-08-21 --output-dir /tmp/squash

# Copy them into the real migration dirs, strip claimed squashes' replaces=,
# remove the cycle-breaking dependency entries, update max_migration.txt.
python -m nextgensquash install --input-dir /tmp/squash

# Undo that: delete what install wrote, git-restore what it edited.
python -m nextgensquash uninstall --input-dir /tmp/squash

# Much later: canonical Django retirement.
python -m nextgensquash retire --input-dir /tmp/squash
```

The console script `nextgensquash` is equivalent to `python -m nextgensquash`.

Two flags shape the plan. `--min-young N` (default 3) keeps at least N live migrations per app after the squash, moving the newest pre-cutoff migrations to young where the date alone leaves fewer. Without it the squash becomes a dormant app's tip and claims names that in-flight branches still reference. `--no-include-prior-squashes` ignores output from earlier phases instead of folding it into the new one.

The lifecycle is `emit` plus `install` on a branch, review the diff, merge. The squash then ships as a normal change: fresh databases build from it, existing ones stamp it on their next migrate. Only once it has been applied in every environment that depends on the repo do you run `retire`, which rewrites remaining dependencies to point at the squash that replaced each folded name (the app's initial, or the stub for the root node a stub claims), empties `replaces=[]`, and deletes the replaced files from disk. That is Django's own advice, and doing it early strands any database that has not caught up.

## The planner guards

Five rules keep the old/young boundary in a place that produces a valid graph. Each one either pins a migration folded or forces it young; when the two conflict the tool raises rather than emitting something that fails minutes into a fresh migrate.

- **A migration with a cross-app old dependent stays folded.** Moving it young leaves a claimed migration depending on a live young node, and that edge weaves a `CircularDependencyError` through the `replaces` redirects. The date partition can never produce such an edge, so the `--min-young` tail rule must not create one either.
- **A `SeparateDatabaseAndState` migration stays folded.** It is a cross-app state move, and its two sides must fold together. Split across the boundary, the moved model lands in both apps' snapshots (both initials create the table) or in neither. A separate check fails the emit when two managed models in the snapshot map to one table.
- **A prior squash stays folded.** Its `replaces` set must stay claimed, so it cannot move young.
- **Raw-SQL-created names referenced by young migrations move young.** `schema_addons` runs after the young chain, so a young `ValidateForeignKey` or `RunSQL` naming an object that only folded `RunSQL` creates would fail on a fresh database. The creator moves young, and everything after it in its app moves with it. If the creator is pinned by one of the rules above, or is the app's root migration, the tool raises and tells you to bump the cutoff past it.
  A young migration that only re-creates the name (`CREATE INDEX IF NOT EXISTS`, or an idempotent helper built on it) does not count: the forwarded copy is rewritten to `IF NOT EXISTS`, so the two creates are order-independent.
- **A `run_before` never points from young into the fold.** A young migration that must run before a folded one would have to run before the squash that contains it. The `--min-young` tail rule leaves such a migration folded, and when the date cutoff itself splits the pair the tool raises and tells you to bump the cutoff past the source. Between two folded migrations of different apps, a `run_before` against the apply order is removed from the source file like any other cycle-breaking edge.

There is a fifth check at emit time: a young migration that touches a deferred foreign-key field is refused outright. Young migrations get no dependency edge onto `finalize_fks`, because such an edge fails `check_consistent_history` on every existing database. That is safe only while no young operation needs the deferred columns. Renaming or deleting the model that owns one is refused too, since the tail then cannot find the model it has to add the field to. The fix is the same, bump the cutoff.

## Limitations

- **`RunPython` is dropped.** Data migrations do not survive the fold. Every dropped call is listed in the plan output, so you can audit them and re-inline any a fresh database genuinely needs.
- **Only pure index work is forwarded from `RunSQL`.** A `RunSQL` that mixes table or column DDL with index creation is dropped and written to `DROPPED_RUNSQL.txt`, because the squash's `CreateModel` and `finalize_fks` already produce those tables and columns. Composite foreign keys added via `ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY` are forwarded per statement, wrapped in a `pg_constraint` existence guard.
- **`managed=False` tables are skipped.** Index and constraint SQL targeting a table Django does not manage is dropped, since the squash never creates it.
- **Non-deferrable foreign keys constrain the apply order.** Multi-table-inheritance parent links and primary-key foreign keys cannot be lifted into `finalize_fks` without leaving a `CreateModel` without a primary key. If the chosen order leaves one as a back edge, the tool raises rather than emitting it.
- **Parts of it are Postgres-only.** The idempotency probes use Django's introspection API, but the forwarded raw SQL and the `pg_constraint` guards assume Postgres.
- **Third-party dependencies of folded migrations are reported, not carried.** A third-party app whose own migrations hang off `("<app>", "__first__")` would otherwise be forced before the initial that creates the swapped user model. Each drop is printed during emit.

## License

MIT.
