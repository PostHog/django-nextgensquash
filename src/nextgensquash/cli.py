"""Argparse entrypoint wiring the plan/emit/install/uninstall/retire subcommands."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import django
import yaml
from django.db.migrations.loader import MigrationLoader

from nextgensquash import cyclebreak, emit, install, loading, planning, retire
from nextgensquash.config import Config, project_root


def _run_plan(args: argparse.Namespace, config: Config) -> None:
    tree = loading.MigrationTree.load(project_root(), config)
    squasher = planning.Squasher(
        tree, args.cutoff, include_prior_squashes=args.include_prior_squashes, min_young=args.min_young
    )
    rendered = planning.TreeRenderer(squasher).render()
    text = yaml.safe_dump(rendered, sort_keys=False, default_flow_style=False, width=200)
    if args.output:
        args.output.write_text(text)
        sys.stderr.write(f"Wrote {args.output} ({len(text):,} bytes)\n")
    else:
        sys.stdout.write(text)


def _run_emit(args: argparse.Namespace, config: Config) -> None:
    tree = loading.MigrationTree.load(project_root(), config)
    squasher = planning.Squasher(
        tree, args.cutoff, include_prior_squashes=args.include_prior_squashes, min_young=args.min_young
    )
    state = planning.Snapshotter(squasher).final_state()
    cycle_breaker = cyclebreak.CycleBreaker(state)
    # One graph load shared by the young-safety check and every per-app
    # Emitter; rebuilding it per collector per app cost tens of seconds.
    loader = MigrationLoader(connection=None, ignore_no_migrations=True)
    emit.Emitter.check_young_against_deferred(squasher, cycle_breaker, loader)

    sys.stderr.write(
        f"cycle apps: {sorted(cycle_breaker.cycle_apps) or '(none)'}\n"
        f"apply order: {' -> '.join(cycle_breaker.apply_order[:12])}{' ...' if len(cycle_breaker.apply_order) > 12 else ''}\n"
        f"deferred FK fields: {len(cycle_breaker.deferred)}\n"
    )
    for fk in sorted(cycle_breaker.deferred, key=lambda f: (f.from_app, f.from_model, f.field_name)):
        sys.stderr.write(f"  defer  {fk.from_app}.{fk.from_model}.{fk.field_name} -> {fk.to_app}.{fk.to_model}\n")

    # Cross-app dependency entries to surgically remove from old migration files
    # so Django's `replaces` redirect doesn't carry them into our squash.
    cycle_edges = cycle_breaker.cycle_break_edges(squasher)
    run_before_edges = cycle_breaker.run_before_break_edges(squasher)
    if cycle_edges or run_before_edges:
        sys.stderr.write(f"\ncycle-break edge removals ({len(cycle_edges) + len(run_before_edges)}):\n")
        for frm_app, frm_name, to_app, to_name in cycle_edges:
            sys.stderr.write(f"  rewrite  {frm_app}.{frm_name}: drop dep ({to_app!r}, {to_name!r})\n")
        for frm_app, frm_name, to_app, to_name in run_before_edges:
            sys.stderr.write(f"  rewrite  {frm_app}.{frm_name}: drop run_before ({to_app!r}, {to_name!r})\n")

    if args.app:
        apps = [args.app]
    else:
        apps = sorted({m.ref.app for m in squasher.old.values()})

    writer = emit.FileWriter(args.output_dir)
    written: list[Path] = []
    dropped_runsql: list[str] = []
    # Retire manifest collected as we emit. Each replaced name maps to its owning
    # app and to the node that replaced it — the app's initial, or the stub for
    # the root nodes a stub claims. `leaves` is informational: retire points every
    # rewritten dependency at the replacement node, the way Django's own loader
    # remaps it.
    retire_manifest: dict[str, Any] = {
        "cutoff": args.cutoff.isoformat(),
        "leaves": {},  # app -> post-finalize leaf name
        "initials": {},  # app -> pre-finalize (initial) squash name
        "stubs": {},  # app -> stub name, for the apps that emit one
        "replaced": {},  # "app/name" -> app  (every name claimed by a squash)
        "claimed_to_stub": {},  # "app/name" -> stub name, for the stub's own claims
    }
    for app in apps:
        emitter = emit.Emitter(state, squasher, app, cycle_breaker, config, loader)
        squashes = emitter.build()
        dropped_runsql.extend(emitter.dropped_runsql)
        initial = next((sq for sq in squashes if sq.name == emitter.INITIAL_NAME), None)
        if initial is None:
            sys.stderr.write(f"skip {app}: no squash built\n")
            continue
        if not initial.operations:
            # Every model moved away (e.g. llm_analytics). Still emit the
            # empty "tombstone" squash: skipping the fold leaves the app's old
            # chain as live nodes woven between the other apps' squashes,
            # which closes a CircularDependencyError through the inherited
            # parent edges.
            sys.stderr.write(f"tombstone {app}: no models in final state, folding history only\n")
        for sq in squashes:
            path = writer.write(sq)
            written.append(path)
            sys.stderr.write(
                f"wrote {path}  ({len(sq.operations)} ops, replaces {len(sq.replaces)}, deps {len(sq.dependencies)})\n"
            )
        # Manifest entries
        finalize = next((sq for sq in squashes if sq.name == emitter.FINALIZE_NAME), None)
        addons = next((sq for sq in squashes if sq.name == emitter.SCHEMA_ADDONS_NAME), None)
        retire_manifest["initials"][app] = emitter.INITIAL_NAME
        if addons is not None:
            retire_manifest["leaves"][app] = emitter.SCHEMA_ADDONS_NAME
        elif finalize is not None:
            retire_manifest["leaves"][app] = emitter.FINALIZE_NAME
        else:
            retire_manifest["leaves"][app] = emitter.INITIAL_NAME
        for replaced_app, replaced_name in initial.replaces:
            retire_manifest["replaced"][f"{replaced_app}/{replaced_name}"] = replaced_app
        stub = next((sq for sq in squashes if sq.name == emitter.STUB_NAME), None)
        if stub is not None:
            retire_manifest["stubs"][app] = stub.name
            # The stub's claims are moved out of the initial's replaces, so
            # nothing else in the manifest would mention them.
            for claimed_app, claimed_name in stub.replaces:
                retire_manifest["replaced"][f"{claimed_app}/{claimed_name}"] = claimed_app
                retire_manifest["claimed_to_stub"][f"{claimed_app}/{claimed_name}"] = stub.name
    # Save cycle-break edge-removal list as a sidecar for `install` to act on.
    if cycle_edges or run_before_edges:
        edges_file = args.output_dir / "CYCLE_EDGE_REMOVALS.txt"
        lines = [f"{fa}/{fn} -> {ta}/{tn}" for (fa, fn, ta, tn) in cycle_edges]
        lines += [f"{fa}/{fn} -> {ta}/{tn} run_before" for (fa, fn, ta, tn) in run_before_edges]
        edges_file.write_text("\n".join(lines) + "\n")
        sys.stderr.write(f"\nwrote cycle-break edge-removal list to {edges_file}\n")

    if dropped_runsql:
        dropped_path = args.output_dir / "DROPPED_RUNSQL.txt"
        dropped_path.write_text("\n".join(dropped_runsql) + "\n")
        sys.stderr.write(f"wrote {len(dropped_runsql)} dropped-RunSQL entries to {dropped_path} — audit them\n")

    manifest_path = args.output_dir / "RETIRE_MANIFEST.json"
    manifest_path.write_text(json.dumps(retire_manifest, indent=2, sort_keys=True) + "\n")
    sys.stderr.write(
        f"wrote retire manifest ({len(retire_manifest['replaced'])} replaced names, "
        f"{len(retire_manifest['leaves'])} apps) to {manifest_path}\n"
    )

    # Format the output the way lint-staged will at commit time, so a re-emit
    # over an installed tree diffs clean.
    subprocess.run(["ruff", "format", "--quiet", str(args.output_dir)], check=False)
    subprocess.run(["ruff", "check", "--fix", "--quiet", str(args.output_dir)], check=False)
    sys.stderr.write(f"\ntotal: {len(written)} files emitted to {args.output_dir}\n")


def _bootstrap(settings_module: str | None) -> None:
    """Point Django at the project's settings and run django.setup()."""
    if not settings_module:
        sys.exit("No Django settings module. Pass --settings, or set DJANGO_SETTINGS_MODULE.")
    os.environ["DJANGO_SETTINGS_MODULE"] = settings_module
    # The console script does not put the working directory on sys.path the way
    # `python -m` does, and the settings module usually lives right there.
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    django.setup()


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--settings",
        default=os.environ.get("DJANGO_SETTINGS_MODULE"),
        help="Dotted path to the Django settings module. Defaults to $DJANGO_SETTINGS_MODULE.",
    )

    def _add_phase_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--cutoff", type=date.fromisoformat, required=True, help="ISO date; fold what predates it.")
        p.add_argument(
            "--min-young",
            type=int,
            default=3,
            help="Keep at least this many live migrations per app after the squash, moving the "
            "newest pre-cutoff migrations to young where the date cutoff alone leaves fewer. "
            "Stops the squash from becoming a dormant app's tip.",
        )
        p.add_argument(
            "--include-prior-squashes",
            action=argparse.BooleanOptionalAction,
            default=True,
            help="Treat existing nextgensquash output (stub/initial/finalize_fks/schema_addons) as old "
            "regardless of cutoff date, so a stacked phase-N can fold them into its own replaces list. "
            "Default on. Disable for a clean from-scratch (re-)squash that ignores prior phases.",
        )

    parser_plan = subparsers.add_parser(
        "plan", parents=[common], help="Emit the YAML description of the proposed squash tree."
    )
    _add_phase_args(parser_plan)
    parser_plan.add_argument("--output", type=Path, default=None)

    parser_emit = subparsers.add_parser(
        "emit", parents=[common], help="Emit real .py migration files for the squashed apps."
    )
    _add_phase_args(parser_emit)
    parser_emit.add_argument("--output-dir", type=Path, required=True)
    parser_emit.add_argument("--app", default=None, help="Only emit this app (testing).")

    parser_install = subparsers.add_parser(
        "install", parents=[common], help="Copy emitted files into the real migration dirs."
    )
    parser_install.add_argument("--input-dir", type=Path, required=True)

    parser_uninstall = subparsers.add_parser(
        "uninstall", parents=[common], help="Remove installed squash files; restore max_migration.txt."
    )
    parser_uninstall.add_argument("--input-dir", type=Path, required=True)

    parser_retire = subparsers.add_parser(
        "retire",
        parents=[common],
        help="Canonical Django retirement: rewrite young-migration deps to the squashes, "
        "empty replaces=[], and delete the replaced files on disk.",
    )
    parser_retire.add_argument("--input-dir", type=Path, required=True)

    args = parser.parse_args()
    _bootstrap(args.settings)
    config = Config.from_settings()

    if args.command == "emit":
        _run_emit(args, config)
    elif args.command == "install":
        install._run_install(args)
    elif args.command == "uninstall":
        install._run_uninstall(args)
    elif args.command == "retire":
        retire._run_retire(args)
    else:
        _run_plan(args, config)
