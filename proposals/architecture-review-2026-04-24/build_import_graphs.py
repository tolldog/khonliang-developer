#!/usr/bin/env python3
"""Per-application import graphs across khonliang-* repos.

Walks each known repo's Python package, parses every `.py` via `ast`, and
emits a `.dot` per package showing:

- **Solid edges** — imports between modules inside the same package
  (the app's own internal structure).
- **Dotted edges** — imports from other ecosystem packages (e.g. developer
  importing from `khonliang_bus`, `khonliang_researcher`, `khonliang`).
  Labels carry the actual symbol names, truncated after a small cap.
- **Dashed edges** — imports from third-party packages (httpx, mcp, etc.).
  Labels likewise.
- **Stdlib imports are omitted** (too noisy; visible via `ast` if needed).

Internal modules are drawn inside a styled cluster; external groupings
(one node per source package) sit outside. One `.dot` per app → the
viewer can render any/all of them.

Run:
    python3 build_import_graphs.py            # emit all per the REPOS table
    python3 build_import_graphs.py developer  # just developer
"""

from __future__ import annotations

import ast
import sys
import argparse
from collections import defaultdict
from pathlib import Path

# (dir_containing_the_package, package_name_to_walk, friendly_slug)
REPOS: list[tuple[Path, str, str]] = [
    (Path("/mnt/dev/ttoll/dev/khonliang-bus"), "bus", "bus"),
    (Path("/mnt/dev/ttoll/dev/khonliang-bus-lib"), "khonliang_bus", "bus-lib"),
    (Path("/mnt/dev/ttoll/dev/khonliang-developer"), "developer", "developer"),
    (Path("/mnt/dev/ttoll/dev/khonliang-researcher"), "researcher", "researcher"),
    (Path("/mnt/dev/ttoll/dev/khonliang-researcher-lib"), "khonliang_researcher", "researcher-lib"),
    (Path("/mnt/dev/ttoll/dev/khonliang-reviewer"), "reviewer", "reviewer"),
    (Path("/mnt/dev/ttoll/dev/khonliang-reviewer-lib"), "khonliang_reviewer", "reviewer-lib"),
    (Path("/mnt/dev/ttoll/ollama-khonliang/src"), "khonliang", "ollama-khonliang"),
]

ECOSYSTEM_ROOTS = {"bus", "khonliang_bus", "developer", "researcher", "khonliang_researcher", "reviewer", "khonliang_reviewer", "khonliang"}

ECOSYSTEM_COLOR = "#0088aa"
THIRDPARTY_COLOR = "#9a5900"
INTERNAL_COLOR = "#333333"
CLUSTER_FILL = "#f7f7f7"

MAX_SYMBOLS_PER_EDGE = 5


def module_name_for(file: Path, pkg_root: Path, pkg_name: str) -> str:
    """developer/agent.py -> developer.agent ; developer/__init__.py -> developer"""
    rel = file.relative_to(pkg_root).with_suffix("")
    parts = [pkg_name] + [p for p in rel.parts if p != "__init__"]
    return ".".join(parts)


def resolve_relative(module: str | None, level: int, current: str) -> str:
    """Turn (module, level) from ImportFrom into an absolute module.

    level=0: absolute; level>=1: walk up from current package.
    """
    if level == 0:
        return module or ""
    parts = current.split(".")
    # package containing `current` is everything except the last part (if it's a leaf)
    # but for `level=1` relative imports from __init__ module we keep the package.
    base = parts[:-level] if len(parts) >= level else []
    if module:
        return ".".join(base + [module])
    return ".".join(base)


def classify(module: str, self_pkg: str) -> str:
    if not module:
        return "skip"
    top = module.split(".", 1)[0]
    if top == self_pkg:
        return "internal"
    if top in ECOSYSTEM_ROOTS:
        return "ecosystem"
    if top in sys.stdlib_module_names:  # py3.10+
        return "stdlib"
    return "thirdparty"


def collect_imports(file: Path, self_module: str, self_pkg: str):
    """Yields (target_module, symbols_tuple, kind). kind: internal/ecosystem/stdlib/thirdparty."""
    try:
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
    except (SyntaxError, UnicodeDecodeError) as e:
        print(f"  ! skip {file}: {e}", file=sys.stderr)
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            target = resolve_relative(node.module, node.level, self_module)
            if not target:
                continue
            syms = tuple(a.name for a in node.names)
            yield target, syms, classify(target, self_pkg)
        elif isinstance(node, ast.Import):
            for a in node.names:
                yield a.name, (a.asname or a.name,), classify(a.name, self_pkg)


def short_label(mod: str, self_pkg: str) -> str:
    """Drop the package prefix for internal-cluster node labels."""
    if mod == self_pkg:
        return "__init__"
    if mod.startswith(self_pkg + "."):
        return mod[len(self_pkg) + 1:]
    return mod


def collapse(module: str, self_pkg: str, depth: int) -> str:
    """Collapse a module to its first `depth` dotted segments beneath self_pkg.

    depth=0 -> keep full name (no collapsing).
    depth=1 -> "developer.foo.bar" becomes "developer.foo".
    depth=2 -> "developer.foo.bar.baz" becomes "developer.foo.bar".
    The self_pkg segment itself is always kept.
    """
    if depth <= 0:
        return module
    if not module.startswith(self_pkg):
        return module
    rel = module[len(self_pkg):].lstrip(".")
    if not rel:
        return self_pkg
    parts = rel.split(".")
    kept = parts[:depth]
    return ".".join([self_pkg] + kept)


def build_dot(repo_dir: Path, pkg_name: str, slug: str, collapse_depth: int = 0) -> str:
    pkg_root = repo_dir / pkg_name
    if not pkg_root.is_dir():
        raise SystemExit(f"package root not found: {pkg_root}")

    # internal module set
    files = sorted(p for p in pkg_root.rglob("*.py") if "__pycache__" not in p.parts)
    modules: dict[str, Path] = {}
    for f in files:
        mod = module_name_for(f, pkg_root, pkg_name)
        modules[mod] = f

    # aggregate edges
    # internal: src_mod -> dst_mod (set of seen pairs; symbol list collected for label)
    internal_edges: dict[tuple[str, str], list[str]] = defaultdict(list)
    # ecosystem / thirdparty: (src_mod, source_top_pkg) -> [symbols...]
    ecosystem_edges: dict[tuple[str, str], list[str]] = defaultdict(list)
    thirdparty_edges: dict[tuple[str, str], list[str]] = defaultdict(list)
    # external source pkg set (for node emission outside cluster)
    external_pkgs: set[str] = set()
    thirdparty_pkgs: set[str] = set()

    # When collapse_depth > 0, we fold file-level modules to their collapsed
    # group and track how many raw modules hide behind each group for the
    # node label (e.g. "trading (28 modules)").
    group_counts: dict[str, int] = defaultdict(int)
    for raw_mod in modules:
        group_counts[collapse(raw_mod, pkg_name, collapse_depth)] += 1

    for src_mod_raw, src_file in modules.items():
        src_mod = collapse(src_mod_raw, pkg_name, collapse_depth)
        for target, syms, kind in collect_imports(src_file, src_mod_raw, pkg_name):
            if kind == "skip" or kind == "stdlib":
                continue
            if kind == "internal":
                # direct "from target import syms" — resolve to nearest real module
                dst_raw = target
                if dst_raw not in modules:
                    parts = target.split(".")
                    while parts:
                        cand = ".".join(parts)
                        if cand in modules:
                            dst_raw = cand
                            break
                        parts.pop()
                dst = collapse(dst_raw, pkg_name, collapse_depth)
                if src_mod == dst:
                    # drop self-loops introduced by collapsing
                    continue
                internal_edges[(src_mod, dst)].extend(syms)
            elif kind == "ecosystem":
                top = target.split(".", 1)[0]
                external_pkgs.add(top)
                ecosystem_edges[(src_mod, top)].extend(list(syms) + ([f"<{target}>"] if target != top else []))
            elif kind == "thirdparty":
                top = target.split(".", 1)[0]
                thirdparty_pkgs.add(top)
                thirdparty_edges[(src_mod, top)].extend(syms)

    # --- emit dot ---
    lines: list[str] = []
    w = lines.append
    w(f"// import graph for {slug} ({pkg_name}) — generated by build_import_graphs.py")
    w(f"// internal modules: {len(modules)} · ecosystem links: {len(ecosystem_edges)} · thirdparty links: {len(thirdparty_edges)}")
    w("digraph import_graph {")
    w("  rankdir=LR;")
    w('  graph [fontname="Helvetica", fontsize=10, splines=true, nodesep=0.35, ranksep=0.7, concentrate=false];')
    w('  node  [fontname="Helvetica", fontsize=9, style=filled, shape=box];')
    w('  edge  [fontname="Helvetica", fontsize=8];')

    # internal cluster
    w(f'  subgraph cluster_{_safe(pkg_name)} {{')
    label_suffix = " (internal)" if collapse_depth == 0 else f" (internal, collapsed to depth {collapse_depth})"
    w(f'    label="{pkg_name}/{label_suffix}"; style=filled; fillcolor="{CLUSTER_FILL}"; color="#bbbbbb";')
    if collapse_depth == 0:
        for mod in sorted(modules):
            w(f'    "{mod}" [label="{short_label(mod, pkg_name)}", fillcolor="#ffffff"];')
    else:
        # emit one node per collapsed group, labelled with count-of-modules
        for group in sorted(group_counts):
            cnt = group_counts[group]
            w(f'    "{group}" [label="{short_label(group, pkg_name)}\\n({cnt} module{"s" if cnt != 1 else ""})", fillcolor="#ffffff"];')
    w("  }")

    # ecosystem external nodes
    if external_pkgs:
        w("  // ecosystem external packages")
        for pkg in sorted(external_pkgs):
            w(f'  "ext::{pkg}" [label="{pkg}\\n(ecosystem)", fillcolor="#e1f0ff", color="{ECOSYSTEM_COLOR}"];')

    # thirdparty external nodes
    if thirdparty_pkgs:
        w("  // third-party external packages")
        for pkg in sorted(thirdparty_pkgs):
            w(f'  "tp::{pkg}" [label="{pkg}\\n(third-party)", fillcolor="#fdf2e9", color="{THIRDPARTY_COLOR}"];')

    # internal edges
    w("  // internal edges (solid)")
    w(f'  edge [style=solid, color="{INTERNAL_COLOR}", arrowhead=vee];')
    for (src, dst), _syms in sorted(internal_edges.items()):
        w(f'  "{src}" -> "{dst}";')

    # ecosystem edges
    if ecosystem_edges:
        w("  // ecosystem edges (dotted, symbol-labelled)")
        w(f'  edge [style=dotted, color="{ECOSYSTEM_COLOR}", arrowhead=normal, penwidth=1.2];')
        for (src, pkg), syms in sorted(ecosystem_edges.items()):
            w(f'  "{src}" -> "ext::{pkg}" [label="{_label(syms)}"];')

    # thirdparty edges
    if thirdparty_edges:
        w("  // third-party edges (dashed, symbol-labelled)")
        w(f'  edge [style=dashed, color="{THIRDPARTY_COLOR}", arrowhead=normal];')
        for (src, pkg), syms in sorted(thirdparty_edges.items()):
            w(f'  "{src}" -> "tp::{pkg}" [label="{_label(syms)}"];')

    w("}")
    return "\n".join(lines) + "\n"


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s)


def _label(syms: list[str]) -> str:
    uniq = []
    seen = set()
    for s in syms:
        if s in seen:
            continue
        seen.add(s)
        uniq.append(s)
    if len(uniq) > MAX_SYMBOLS_PER_EDGE:
        shown = uniq[:MAX_SYMBOLS_PER_EDGE]
        return "\\n".join(shown) + f"\\n+{len(uniq) - MAX_SYMBOLS_PER_EDGE} more"
    return "\\n".join(uniq)


def main():
    ap = argparse.ArgumentParser(
        description="Per-app import graph generator. Runs against the built-in "
                    "REPOS table by default, OR against an ad-hoc --repo/--pkg "
                    "target for apps outside the khonliang-* ecosystem."
    )
    ap.add_argument("slugs", nargs="*", help="which built-in repos to emit (default: all)")
    ap.add_argument("--out", default=".", help="output directory (default: cwd)")
    ap.add_argument("--repo", help="ad-hoc run: repo root dir containing the package")
    ap.add_argument("--pkg", help="ad-hoc run: Python package name to walk (under --repo)")
    ap.add_argument("--slug", help="ad-hoc run: output filename slug (produces import-graph-<slug>.dot)")
    ap.add_argument("--collapse-depth", type=int, default=0,
                    help="collapse modules to first N dotted segments below the package "
                         "(0 = full detail, 1 = top-level subpackages, 2 = one-level deeper)")
    args = ap.parse_args()

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Ad-hoc mode: render a single graph from user-supplied path/pkg/slug.
    # Keeps private or external apps out of the built-in table.
    if args.repo or args.pkg or args.slug:
        if not (args.repo and args.pkg and args.slug):
            ap.error("--repo, --pkg, --slug must be provided together for ad-hoc mode")
        repo_dir = Path(args.repo).resolve()
        print(f"[build] {args.slug} (ad-hoc: {repo_dir} :: {args.pkg})")
        dot = build_dot(repo_dir, args.pkg, args.slug, collapse_depth=args.collapse_depth)
        suffix = f"-depth{args.collapse_depth}" if args.collapse_depth else ""
        out_path = out_dir / f"import-graph-{args.slug}{suffix}.dot"
        out_path.write_text(dot, encoding="utf-8")
        print(f"  wrote {out_path} ({len(dot)} bytes)")
        print("\n1 graph emitted.")
        return

    selected = set(args.slugs) if args.slugs else None
    produced = []
    for repo_dir, pkg_name, slug in REPOS:
        if selected and slug not in selected:
            continue
        print(f"[build] {slug} ({repo_dir} :: {pkg_name})")
        dot = build_dot(repo_dir, pkg_name, slug, collapse_depth=args.collapse_depth)
        suffix = f"-depth{args.collapse_depth}" if args.collapse_depth else ""
        out_path = out_dir / f"import-graph-{slug}{suffix}.dot"
        out_path.write_text(dot, encoding="utf-8")
        print(f"  wrote {out_path} ({len(dot)} bytes)")
        produced.append(out_path)

    print(f"\n{len(produced)} graphs emitted.")


if __name__ == "__main__":
    main()
