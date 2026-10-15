#!/usr/bin/env python3
"""codegraph.py — code graph проекта как артефакт репозитория (E14).

Строит машиночитаемый граф зависимостей модулей: backend (Python, AST-парс
import'ов) + frontend (JS, парс ES-module import'ов). Никаких внешних
инструментов — только stdlib (по принципу «скриптами, не агентами»: детерминированно,
воспроизводимо, без установки зависимостей).

Usage:
  python3 scripts/codegraph.py <repo-root>          # пишет <repo>/codegraph.json + печатает отчет

Выход: codegraph.json — узлы (модули) и ребра (зависимости), плюс codegraph.md-сводка в stdout.

Правило поддержки (E14): артефакт обновляется скриптом, коммитится при изменении
импортов (Definition of Done dev-задач, меняющих импорты).
"""
import ast
import json
import re
import sys
from pathlib import Path

PY_DIR = "backend/app"
JS_DIRS = ("frontend/static/js",)
# Конфигурация по проектам (E14): репозиторий без backend/app (например ai-factory)
# обрабатывается в js-only режиме + py-граф по указанным каталогам.
PROJECT_CONFIG = {
    "ai-factory": {"py_dirs": ("scripts",), "js_dirs": ()},
}


def config_for(repo: Path):
    cfg = PROJECT_CONFIG.get(repo.name)
    if cfg:
        return cfg.get("py_dirs", ()), cfg.get("js_dirs", ())
    return (PY_DIR,), JS_DIRS
STDIN_MARKER = re.compile(r"^(app|frontend)")


def py_imports(path: Path, repo: Path):
    """Импорты python-модуля: [('app.db', 'from'), ...]."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.append((a.name, "import"))
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                out.append((node.module, "from"))
    return out


def js_imports(path: Path):
    """Импорты ES-модуля: import ... from './x.js'."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return [m.group(1) for m in re.finditer(r"""import\s+[^;]*?from\s+['"](\.[^'"]+)['"]""", text)]


def norm_js(base: Path, spec: str) -> str:
    resolved = (base.parent / spec).resolve()
    try:
        return str(resolved.relative_to(REPO))
    except ValueError:
        return spec


def build(repo: Path):
    global REPO
    REPO = repo
    py_dirs, js_dirs = config_for(repo)
    nodes, edges = set(), []

    # Python backend
    for py_dir in py_dirs:
        for py in sorted((repo / py_dir).glob("*.py")):
            rel = str(py.relative_to(repo))
            mod = rel.replace("/", ".").removesuffix(".py")
            nodes.add(mod)
            for target, kind in py_imports(py, repo):
                if target.startswith("app."):
                    tmod = f"{py_dir.replace('/', '.')}.{target.split('.', 1)[1]}"
                elif target == "app":
                    tmod = py_dir.replace("/", ".")
                else:
                    continue  # внешние зависимости не в графе
                if (mod, tmod, kind) not in edges:
                    edges.append((mod, tmod, kind))

    # JS frontend (ES-modules)
    for jsdir in js_dirs:
        for js in sorted((repo / jsdir).rglob("*.js")):
            rel = str(js.relative_to(repo))
            nodes.add(rel)
            for spec in js_imports(js):
                tgt = (js.parent / spec).resolve()
                try:
                    trel = str(tgt.relative_to(repo))
                except ValueError:
                    continue
                nodes.add(trel)
                if (rel, trel, "es-import") not in edges:
                    edges.append((rel, trel, "es-import"))

    # entry points (js без входящих es-import = грузится страницей напрямую)
    targets = {t for _, t, _ in edges}
    entries = sorted(n for n in nodes if n.endswith(".js") and n not in targets)

    graph = {
        "meta": {"tool": "scripts/codegraph.py", "py_dir": PY_DIR, "js_dirs": list(JS_DIRS)},
        "nodes": sorted(nodes),
        "edges": [{"from": a, "to": b, "kind": k} for a, b, k in sorted(edges)],
        "js_entry_points": entries,
    }
    return graph


def report(graph) -> str:
    lines = [f"Узлов: {len(graph['nodes'])}, ребер: {len(graph['edges'])}"]
    indeg = {}
    for e in graph["edges"]:
        indeg[e["to"]] = indeg.get(e["to"], 0) + 1
    lines.append("Самые зависимые модули (входящих ребер):")
    for mod, n in sorted(indeg.items(), key=lambda x: -x[1])[:5]:
        lines.append(f"  {mod}: {n}")
    if graph["js_entry_points"]:
        lines.append("JS entry points (нет входящих импортов): " + ", ".join(graph["js_entry_points"]))
    orphans = [n for n in graph["nodes"] if not any(e["to"] == n or e["from"] == n for e in graph["edges"])]
    if orphans:
        lines.append("Изолированные узлы (без ребер): " + ", ".join(orphans))
    return "\n".join(lines)


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    repo = Path(sys.argv[1]).resolve()
    py_dirs, _ = config_for(repo)
    if not any((repo / d).is_dir() for d in py_dirs):
        print(f"CODEGRAPH-ERROR: py-каталоги не найдены: {py_dirs}")
        return 2
    graph = build(repo)
    out = repo / "codegraph.json"
    out.write_text(json.dumps(graph, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"codegraph.json записан: {out}")
    print(report(graph))
    return 0


if __name__ == "__main__":
    sys.exit(main())
