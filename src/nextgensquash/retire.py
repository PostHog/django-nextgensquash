"""Canonical Django retirement: rewrite deps to the squashes, empty replaces, delete replaced files."""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

from nextgensquash.config import app_migration_dirs


def transform_dependencies(
    path: Path, transform: Callable[[tuple[str, str]], tuple[str, str] | None], attr: str = "dependencies"
) -> bool:
    """Rewrite `Migration.<attr> = [...]` (dependencies or run_before) in `path` through `transform`.

    `transform` maps each `(app, name)` tuple to a replacement tuple, or None
    to drop the entry. AST-based, so single-line and multi-line list styles
    both work. Untouched entries keep their original source text (sentinel
    strings and unusual expressions included); duplicate results collapse.
    Returns True when the file changed.
    """
    src = path.read_text()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False

    deps_assign = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Migration":
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == attr for t in stmt.targets):
                    deps_assign = stmt
                    break
    if deps_assign is None or not isinstance(deps_assign.value, ast.List):
        return False

    entries: list[str] = []
    seen: set[str] = set()
    changed = False
    for elt in deps_assign.value.elts:
        if (
            isinstance(elt, ast.Tuple)
            and len(elt.elts) == 2
            and all(isinstance(c, ast.Constant) and isinstance(c.value, str) for c in elt.elts)
        ):
            dep = cast("tuple[str, str]", tuple(cast("ast.Constant", c).value for c in elt.elts))
            result = transform(dep)
            if result is None:
                changed = True
                continue
            if result != dep:
                changed = True
                text = f'("{result[0]}", "{result[1]}")'
            else:
                text = ast.get_source_segment(src, elt) or f'("{dep[0]}", "{dep[1]}")'
            key = f"{result[0]}/{result[1]}"
        else:
            text = ast.get_source_segment(src, elt) or ""
            key = text
        if text and key not in seen:
            seen.add(key)
            entries.append(text)

    if not changed:
        return False

    indent = " " * 8
    lines = [f"    {attr} = ["] + [f"{indent}{e}," for e in entries] + ["    ]"]
    src_lines = src.splitlines()
    start, end = deps_assign.lineno - 1, cast("int", deps_assign.end_lineno) - 1
    new_src = "\n".join(src_lines[:start] + lines + src_lines[end + 1 :])
    if not new_src.endswith("\n"):
        new_src += "\n"
    path.write_text(new_src)
    return True


def _rewrite_deps_in_file(
    path: Path,
    replaced_to_app: dict[str, str],
    initials: dict[str, str],
    claimed_to_stub: dict[str, str],
) -> bool:
    """Rewrite every dep on a now-folded migration to the node that replaced it.

    Django's loader remaps a dependency on a replaced node to its replacement,
    and retire mirrors that: same-app and cross-app references alike go to the
    app's initial, or to the stub for the names the stub claims. Pointing a
    same-app reference at the post-finalize leaf instead builds the cycle
    young → leaf → young, because the tail depends on the app's last young
    migration to keep the app at a single leaf.
    """

    def transform(dep: tuple[str, str]) -> tuple[str, str]:
        key = f"{dep[0]}/{dep[1]}"
        if key not in replaced_to_app:
            return dep
        target_app = replaced_to_app[key]
        stub_name = claimed_to_stub.get(key)
        return (target_app, stub_name if stub_name is not None else initials[target_app])

    return transform_dependencies(path, transform)


def _empty_replaces_in_squash(path: Path) -> bool:
    """Replace the `replaces = [...]` literal with `replaces = []` in a squash file."""
    src = path.read_text()
    new = re.sub(r"replaces\s*=\s*\[[^\]]*\]", "replaces = []", src, count=1, flags=re.S)
    if new == src:
        return False
    path.write_text(new)
    return True


def _run_retire(args: argparse.Namespace) -> None:
    """Canonical Django retirement of the squashes already installed via `install`.

    Reads RETIRE_MANIFEST.json from the original emit dir, rewrites every
    `dependencies=[…]` entry that names a now-folded migration to point at the
    squash that replaced it, empties `replaces=[]` on the squashes, and deletes
    the replaced files on disk. Use this only once the squash has been applied
    in every environment that depends on this repo — per Django's docs.
    """
    import json as _json

    output_dir = args.input_dir
    manifest_path = output_dir / "RETIRE_MANIFEST.json"
    if not manifest_path.exists():
        sys.stderr.write(f"no RETIRE_MANIFEST.json at {manifest_path}; run `emit` first\n")
        sys.exit(2)
    manifest = _json.loads(manifest_path.read_text())
    replaced_to_app: dict[str, str] = manifest["replaced"]
    initials: dict[str, str] = manifest["initials"]
    # Absent from manifests written before stub claims were recorded.
    stubs: dict[str, str] = manifest.get("stubs", {})
    claimed_to_stub: dict[str, str] = manifest.get("claimed_to_stub", {})

    apps_dirs = app_migration_dirs()

    # 1. Rewrite dependencies across every on-disk migration file in managed apps.
    rewritten: list[Path] = []
    for app_mig_dir in apps_dirs.values():
        if not app_mig_dir.is_dir():
            continue
        for f in app_mig_dir.glob("*.py"):
            if f.name == "__init__.py":
                continue
            if _rewrite_deps_in_file(f, replaced_to_app, initials, claimed_to_stub):
                rewritten.append(f)
    sys.stderr.write(f"rewrote dependencies= in {len(rewritten)} files\n")

    # 2. Empty replaces=[] on every squash file: the initials, plus the stubs
    #    that claim a root node of their own.
    emptied: list[Path] = []
    for app, squash_name in [(a, initials[a]) for a in sorted(initials)] + [(a, stubs[a]) for a in sorted(stubs)]:
        mig_dir = apps_dirs.get(app)
        if mig_dir is None:
            continue
        squash = mig_dir / f"{squash_name}.py"
        if squash.exists() and _empty_replaces_in_squash(squash):
            emptied.append(squash)
    sys.stderr.write(f"emptied replaces= in {len(emptied)} squash files\n")

    # 3. Delete the replaced files on disk.
    deleted: list[Path] = []
    for replaced_key in replaced_to_app:
        replaced_app, replaced_name = replaced_key.split("/", 1)
        mig_dir = apps_dirs.get(replaced_app)
        if mig_dir is None:
            continue
        f = mig_dir / f"{replaced_name}.py"
        if f.exists():
            f.unlink()
            deleted.append(f)
    sys.stderr.write(f"deleted {len(deleted)} replaced files\n")

    log_path = output_dir / "RETIRED.txt"
    log_path.write_text(
        "REWRITTEN:\n"
        + "\n".join(str(p) for p in rewritten)
        + "\nEMPTIED:\n"
        + "\n".join(str(p) for p in emptied)
        + "\nDELETED:\n"
        + "\n".join(str(p) for p in deleted)
        + "\n"
    )
    sys.stderr.write(f"retire log at {log_path}\n")
    sys.stderr.write("\nNext: run `python manage.py migrate --check` against a fresh DB to validate.\n")
