"""P7.2c static project discovery and evidence-based architecture understanding."""

from __future__ import annotations

import ast
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional

import adoption
from state import ProjectState, utc_now

DISCOVERY_VERSION = "p7-2c/1"
TOOL = "crew-tui static discovery"

FRAMEWORK_HINTS = {
    "django": "Django", "flask": "Flask", "fastapi": "FastAPI",
    "react": "React", "vue": "Vue", "next": "Next.js", "express": "Express",
    "pytest": "pytest", "sqlalchemy": "SQLAlchemy", "prisma": "Prisma",
    "langchain": "LangChain", "pydantic": "Pydantic",
}
PACKAGE_MANAGERS = {
    "package.json": "npm/yarn/pnpm", "package-lock.json": "npm",
    "yarn.lock": "yarn", "pnpm-lock.yaml": "pnpm",
    "requirements.txt": "pip", "pyproject.toml": "python project",
    "Pipfile": "pipenv", "poetry.lock": "poetry", "Cargo.toml": "cargo",
    "go.mod": "go modules", "pom.xml": "maven", "build.gradle": "gradle",
}
BUILD_FILES = {"Makefile", "CMakeLists.txt", "Dockerfile",
               "docker-compose.yml", "tsconfig.json", "vite.config.js",
               "vite.config.ts", "next.config.js", "setup.py"}
CI_NAMES = {".github", ".gitlab-ci.yml", ".circleci", "azure-pipelines.yml",
            "Jenkinsfile", ".buildkite"}
LINT_NAMES = {"ruff.toml", ".ruff.toml", ".flake8", ".eslintrc",
              ".eslintrc.js", ".eslintrc.json", "eslint.config.js",
              "mypy.ini", ".pylintrc", "pyproject.toml", "tox.ini"}
DOC_NAMES = {"README.md", "README.rst", "README", "docs", "CONTRIBUTING.md",
             "ARCHITECTURE.md"}
TEXT_SUFFIX = {".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".json",
               ".toml", ".yaml", ".yml", ".md", ".txt", ".cfg", ".ini",
               ".html", ".css", ".go", ".rs", ".java", ".cs", ".rb", ".php",
               ".sh", ".sql"}


def _evidence(rel: str, line: int = 0, symbol: str = "") -> dict[str, Any]:
    out = {"file": rel}
    if line:
        out["line"] = line
    if symbol:
        out["symbol"] = symbol
    return out


def _finding(text: str, status: str = "OBSERVED",
             evidence: Optional[list[dict[str, Any]]] = None,
             reason: str = "") -> dict[str, Any]:
    return {"text": text, "status": status,
            "evidence": evidence or [], "reason": reason}


def _contains_any(text: str, words: Iterable[str]) -> list[str]:
    low = text.lower()
    return [w for w in words if w.lower() in low]


def _safe_rel(root: Path, path: Path) -> Optional[str]:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    return rel.as_posix()


def _read_text(root: Path, rel: str, max_chars: int) -> tuple[str, str]:
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return "", "outside-root"
    if path.is_symlink() or not path.is_file():
        return "", "not-regular"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return "", f"read-error: {exc}"
    if b"\x00" in raw[:4096]:
        return "", "binary"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "", "not-utf8"
    return text[:max_chars], "truncated" if len(text) > max_chars else ""


def _py_analysis(rel: str, text: str, module_index: dict[str, str]
                 ) -> tuple[dict[str, Any], list[dict[str, Any]],
                            list[dict[str, Any]], list[str]]:
    imports: list[dict[str, Any]] = []
    external: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    entry = False
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return ({"parse_error": str(exc), "symbols": [], "imports": []},
                [], [], [rel])
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                imports.append({"module": name, "line": node.lineno})
                target = module_index.get(name)
                if target:
                    edges.append({"from": rel, "to": target, "line": node.lineno,
                                  "kind": "import", "status": "OBSERVED",
                                  "target_mapping": "INFERRED"})
                else:
                    external.append({"module": name, "line": node.lineno})
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                name = f"{mod}.{alias.name}" if mod else alias.name
                imports.append({"module": name, "line": node.lineno})
                target = module_index.get(mod) or module_index.get(name)
                if target:
                    edges.append({"from": rel, "to": target, "line": node.lineno,
                                  "kind": "from-import", "status": "OBSERVED",
                                  "target_mapping": "INFERRED"})
                else:
                    external.append({"module": name, "line": node.lineno})
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            symbols.append({"name": node.name, "kind": kind,
                            "line": node.lineno,
                            "public": not node.name.startswith("_")})
        elif isinstance(node, ast.If):
            test = ast.unparse(node.test) if hasattr(ast, "unparse") else ""
            if "__name__" in test and "__main__" in test:
                entry = True
    return ({"imports": imports, "symbols": symbols, "entrypoint": entry},
            edges, [], [])


def _js_analysis(rel: str, text: str, module_index: dict[str, str]
                 ) -> tuple[dict[str, Any], list[dict[str, Any]],
                            list[dict[str, Any]]]:
    imports = []
    external = []
    edges = []
    for m in re.finditer(
            r"(?:import\s+[^;]*?from\s+|require\(\s*|export\s+[^;]*?from\s*)"
            r"[\"']([^\"']+)[\"']", text):
        name = m.group(1)
        line = text[:m.start()].count("\n") + 1
        imports.append({"module": name, "line": line})
        if name.startswith(".") or name.startswith("/"):
            target = module_index.get(name.lstrip("./"))
            if target:
                edges.append({"from": rel, "to": target, "line": line,
                              "kind": "import", "status": "OBSERVED",
                              "target_mapping": "INFERRED"})
            else:
                external.append({"module": name, "line": line,
                                 "note": "unresolved local import"})
        else:
            external.append({"module": name, "line": line})
    return {"imports": imports}, edges, external


def _build_module_index(files: Iterable[str]) -> dict[str, str]:
    index: dict[str, str] = {}
    for rel in files:
        if rel.endswith(".py"):
            noext = rel[:-3]
            dotted = noext.replace("/", ".")
            index.setdefault(dotted, rel)
            index.setdefault(noext.rsplit("/", 1)[-1], rel)
        elif rel.endswith((".js", ".mjs", ".ts", ".tsx", ".jsx")):
            noext = re.sub(r"\.(mjs|js|ts|tsx|jsx)$", "", rel)
            index.setdefault(noext.lstrip("./"), rel)
            index.setdefault(noext.rsplit("/", 1)[-1], rel)
    return index


def _component_name(rel: str) -> str:
    parts = rel.split("/")
    return parts[0] if len(parts) > 1 else rel


def _manifest_dependencies(text: str, rel: str) -> list[dict[str, Any]]:
    deps = []
    name = rel.rsplit("/", 1)[-1]
    if name == "package.json":
        try:
            data = json.loads(text)
        except Exception:
            return deps
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            for dep, version in (data.get(key) or {}).items():
                deps.append({"name": dep, "version": str(version),
                             "scope": key, "evidence": [_evidence(rel)]})
    elif name in ("requirements.txt", "Pipfile"):
        for line_no, line in enumerate(text.splitlines(), 1):
            clean = line.split("#", 1)[0].strip()
            if clean and not clean.startswith("-"):
                dep = re.split(r"[=<>!~ ]", clean, 1)[0]
                deps.append({"name": dep, "version": "",
                             "scope": "runtime",
                             "evidence": [_evidence(rel, line_no)]})
    elif name in ("pyproject.toml", "Cargo.toml", "go.mod", "pom.xml"):
        for line_no, line in enumerate(text.splitlines(), 1):
            clean = line.strip()
            m = re.match(r"[\"']?([A-Za-z0-9_.-]+)[\"']?\s*(?:=|\s)", clean)
            if m and not clean.startswith(("[", "#", "{")):
                deps.append({"name": m.group(1), "version": "",
                             "scope": "manifest",
                             "evidence": [_evidence(rel, line_no)]})
    return deps


def _mermaid_component(components: list[dict[str, Any]]) -> str:
    lines = ["flowchart TD"]
    for i, comp in enumerate(components[:20]):
        label = comp["name"].replace('"', "'")
        lines.append(f'  C{i}["{label}"]')
    return "\n".join(lines) if len(lines) > 1 else ""


def _mermaid_dependencies(edges: list[dict[str, Any]]) -> str:
    if not edges:
        return ""
    nodes: dict[str, str] = {}
    lines = ["flowchart LR"]
    for i, edge in enumerate(edges[:40]):
        for key in ("from", "to"):
            nodes.setdefault(edge[key], f"N{len(nodes)}")
        lines.append(f'  {nodes[edge["from"]]}["{edge["from"]}"] --> '
                     f'{nodes[edge["to"]]}["{edge["to"]}"]')
    return "\n".join(lines)


def analyze(root: str | Path, *,
            max_files: int = 2000,
            max_total_bytes: int = 64 * 1024 * 1024,
            max_file_bytes: int = 8 * 1024 * 1024,
            max_files_analyzed: int = 500,
            max_chars_per_file: int = 20000,
            max_total_chars: int = 2_000_000) -> dict[str, Any]:
    root = Path(root).expanduser().resolve()
    inventory = adoption.inspect_repository(
        root, max_files=max_files, max_total_bytes=max_total_bytes,
        max_file_bytes=max_file_bytes)
    files = sorted(inventory.get("file_sha256", {}).keys())
    module_index = _build_module_index(files)
    parsed: dict[str, Any] = {}
    analyzed: list[str] = []
    analysis_skipped: list[dict[str, str]] = []
    parse_errors: list[str] = []
    internal_edges: list[dict[str, Any]] = []
    external_imports: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    entrypoints: list[dict[str, Any]] = []
    todo_markers: list[dict[str, Any]] = []
    env_refs: list[dict[str, Any]] = []
    manifests: dict[str, str] = {}
    text_chars = 0
    for rel in files:
        if len(analyzed) >= max_files_analyzed or text_chars >= max_total_chars:
            analysis_skipped.append({"path": rel, "reason": "analysis_limit"})
            continue
        if Path(rel).suffix.lower() not in TEXT_SUFFIX:
            continue
        text, note = _read_text(root, rel, max_chars_per_file)
        if note.startswith(("read-error", "binary", "not-utf8", "outside")):
            analysis_skipped.append({"path": rel, "reason": note})
            continue
        if not text:
            analysis_skipped.append({"path": rel, "reason": note or "empty"})
            continue
        text_chars += len(text)
        analyzed.append(rel)
        parsed[rel] = {"note": note}
        if rel.endswith(".py"):
            info, edges, _external, errors = _py_analysis(rel, text, module_index)
            parsed[rel].update(info)
            internal_edges.extend(edges)
            external_imports.extend([{**item, "file": rel} for item in _external])
            symbols.extend([{**sym, "file": rel}
                            for sym in info.get("symbols", [])])
            parse_errors.extend(errors)
            if info.get("entrypoint"):
                entrypoints.append({"file": rel, "line": 1,
                                    "reason": "__main__ block"})
        elif rel.endswith((".js", ".mjs", ".ts", ".tsx", ".jsx")):
            info, edges, external = _js_analysis(rel, text, module_index)
            parsed[rel].update(info)
            internal_edges.extend(edges)
            external_imports.extend([{**item, "file": rel} for item in external])
        if rel.rsplit("/", 1)[-1] in adoption.CONFIG_NAMES:
            manifests[rel] = text
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(r"\b(TODO|FIXME|XXX|deprecated)\b", line, re.I):
                todo_markers.append({"file": rel, "line": i,
                                     "text": line.strip()[:240]})
            for m in re.finditer(
                    r"os\.environ(?:\.get)?\(?[\"']([A-Za-z_][A-Za-z0-9_]*)",
                    line):
                env_refs.append({"key": m.group(1), "file": rel, "line": i})
    dependencies = []
    package_deps = []
    for rel, text in manifests.items():
        package_deps.extend(_manifest_dependencies(text, rel))
    dep_names = {d["name"].lower() for d in package_deps}
    frameworks = []
    import_text = " ".join(i["module"].lower() for i in external_imports)
    for hint, label in FRAMEWORK_HINTS.items():
        if hint in dep_names or hint in import_text:
            frameworks.append({"name": label, "status": "OBSERVED",
                               "evidence": [e for d in package_deps
                                            if d["name"].lower() == hint
                                            for e in d["evidence"]]})
    package_managers = sorted({
        pm for name, pm in PACKAGE_MANAGERS.items()
        if any(rel.endswith(name) or rel == name for rel in files)
    })
    build_systems = sorted({f for f in BUILD_FILES if f in {
        rel.rsplit("/", 1)[-1] for rel in files}})
    ci_files = sorted({rel for rel in files
                       if any(part in CI_NAMES for part in rel.split("/"))})
    lint_files = sorted({rel for rel in files
                         if rel.rsplit("/", 1)[-1] in LINT_NAMES})
    doc_files = sorted({rel for rel in files
                        if rel.rsplit("/", 1)[-1] in DOC_NAMES or
                        rel.startswith("docs/")})
    test_files = sorted(inventory.get("tests") or [])
    configs = sorted(inventory.get("configurations") or [])
    components_map: dict[str, list[str]] = {}
    for rel in files:
        if Path(rel).suffix.lower() in (".py", ".js", ".mjs", ".ts", ".tsx",
                                        ".jsx", ".go", ".rs", ".java", ".cs"):
            components_map.setdefault(_component_name(rel), []).append(rel)
    components = []
    for name in sorted(components_map):
        paths = components_map[name]
        responsibilities = []
        if name in ("tests", "test"):
            responsibilities.append(_finding("Test code", "INFERRED",
                                             [_evidence(p) for p in paths[:3]]))
        elif name in ("docs", "doc"):
            responsibilities.append(_finding("Documentation", "INFERRED",
                                             [_evidence(p) for p in paths[:3]]))
        else:
            responsibilities.append(_finding(
                f"Source component containing {len(paths)} code file(s)",
                "INFERRED", [_evidence(p) for p in paths[:3]]))
        components.append({"name": name, "paths": paths[:50],
                           "file_count": len(paths),
                           "responsibilities": responsibilities})
    entry_names = {"main.py", "app.py", "run.py", "index.html", "server.py",
                   "manage.py"}
    for rel in files:
        if rel.rsplit("/", 1)[-1] in entry_names:
            entrypoints.append({"file": rel, "line": 1, "reason": "conventional entry file"})
    if "package.json" in manifests:
        try:
            pkg = json.loads(manifests["package.json"])
            main = pkg.get("main")
            if main:
                entrypoints.append({"file": str(main), "line": 1,
                                    "reason": "package.json main",
                                    "status": "OBSERVED"})
        except Exception:
            pass
    entrypoints = list({(e["file"], e.get("line", 0), e["reason"]): e
                        for e in entrypoints}.values())
    persistence = []
    persistence_hints = {
        "sqlalchemy": "SQLAlchemy", "sqlite3": "SQLite", "psycopg": "PostgreSQL",
        "pymongo": "MongoDB", "prisma": "Prisma", "redis": "Redis",
        "django.db": "Django ORM",
    }
    for hint, label in persistence_hints.items():
        evidence = [e for d in package_deps if hint in d["name"].lower()
                    for e in d.get("evidence", [])]
        evidence += [_evidence(item["file"], item.get("line", 0), hint)
                     for item in external_imports
                     if hint in item.get("module", "").lower()]
        if evidence:
            persistence.append(_finding(label, "OBSERVED", evidence[:5]))
    external_interfaces = []
    for dep in package_deps:
        if dep["name"].lower() in ("requests", "httpx", "axios", "fetch",
                                   "grpc", "graphql", "openai"):
            external_interfaces.append(_finding(
                f"Dependency {dep['name']} may establish external interfaces",
                "INFERRED", dep["evidence"]))
    env_keys = sorted({item["key"] for item in env_refs})
    for item in env_refs[:40]:
        external_interfaces.append(_finding(
            f"Environment variable referenced: {item['key']}", "OBSERVED",
            [_evidence(item["file"], item["line"], item["key"])]))
    quality = {
        "tests": [{"path": p, "status": "OBSERVED"} for p in test_files],
        "test_frameworks": [f["name"] for f in frameworks if f["name"] == "pytest"],
        "ci": [{"path": p, "status": "OBSERVED"} for p in ci_files],
        "linting": [{"path": p, "status": "OBSERVED"} for p in lint_files],
        "documentation": [{"path": p, "status": "OBSERVED"} for p in doc_files],
        "markers": todo_markers[:100],
    }
    risks = []
    if not test_files:
        risks.append(_finding("No test files were observed", "INFERRED",
                              reason="absence of tests is a potential risk"))
    if not ci_files:
        risks.append(_finding("No CI workflow was observed", "INFERRED",
                              reason="absence of CI is a potential risk"))
    if todo_markers:
        risks.append(_finding(f"{len(todo_markers)} TODO/FIXME/deprecated marker(s)",
                              "OBSERVED", [_evidence(m["file"], m["line"])
                                            for m in todo_markers[:10]]))
    if not inventory.get("hash_complete", False):
        risks.append(_finding("Repository hash coverage is incomplete",
                              "UNKNOWN", reason="bounded or unreadable files"))
    preserve = []
    for entry in entrypoints[:20]:
        preserve.append(_finding(f"Entry point: {entry['file']}", "OBSERVED",
                                 [_evidence(entry["file"], entry.get("line", 0))]))
    for sym in symbols[:40]:
        if sym.get("public"):
            preserve.append(_finding(
                f"Public {sym['kind']} {sym['name']}", "OBSERVED",
                [_evidence(sym["file"], sym.get("line", 0), sym["name"])]))
    for item in external_interfaces[:40]:
        preserve.append(_finding(item["text"], item["status"], item["evidence"]))
    unresolved = []
    if not entrypoints:
        unresolved.append("No application entry point was identified.")
    unresolved.append("Runtime behaviour was not executed or observed dynamically.")
    if not inventory.get("hash_complete", False):
        unresolved.append("Analysis used bounded/incomplete file coverage.")
    if env_keys:
        unresolved.append("Environment variable values and deployment context are unknown.")
    if not test_files:
        unresolved.append("No executable test evidence was available.")
    conflicting = []
    for exp in entrypoints:
        if exp["file"] not in files and not exp["file"].startswith("commands."):
            conflicting.append(_finding(
                f"Declared entry point is absent: {exp['file']}", "CONFLICTING",
                [_evidence("package.json", 1, "main")]))
    classifications = {"OBSERVED": 0, "INFERRED": 0, "UNKNOWN": 0,
                       "CONFLICTING": 0}
    def count(item: dict[str, Any]) -> None:
        st = str(item.get("status") or "UNKNOWN")
        classifications[st] = classifications.get(st, 0) + 1
    for group in (components, external_interfaces, persistence, risks, preserve,
                  conflicting):
        for item in group:
            if "status" in item:
                count(item)
            for sub in item.get("responsibilities", []) if isinstance(item, dict) else []:
                count(sub)
    overview = {
        "project_name": root.name,
        "top_level_directories": sorted({
            rel.split("/", 1)[0] for rel in files if "/" in rel
        }),
        "top_level_files": sorted([rel for rel in files if "/" not in rel])[:40],
        "entry_points": entrypoints,
        "source_languages": inventory.get("languages", {}),
        "file_count": inventory.get("files_total_regular", 0),
    }
    dossier = {
        "schema": DISCOVERY_VERSION,
        "generated_at": utc_now(),
        "repository": {
            "root": str(root),
            "head": inventory.get("git_head", ""),
            "branch": inventory.get("git_branch", ""),
            "dirty": inventory.get("git_dirty", []),
        },
        "analysis_scope": {
            "static_only": True,
            "runtime_execution": "none",
            "dependency_installation": "none",
            "files_total": inventory.get("files_total_regular", 0),
            "files_analyzed": len(analyzed),
            "text_chars": text_chars,
            "limits": {
                "max_files_inventory": max_files,
                "max_total_bytes": max_total_bytes,
                "max_file_bytes": max_file_bytes,
                "max_files_analyzed": max_files_analyzed,
                "max_chars_per_file": max_chars_per_file,
                "max_total_chars": max_total_chars,
            },
            "inventory_complete": inventory.get("inventory_complete", False),
            "hash_complete": inventory.get("hash_complete", False),
            "hash_coverage": inventory.get("hash_coverage", 0.0),
            "analysis_skipped": analysis_skipped[:100],
            "parse_errors": parse_errors,
            "file_hashes": inventory.get("file_sha256", {}),
        },
        "overview": overview,
        "inventory": inventory,
        "technology_stack": {
            "languages": inventory.get("languages", {}),
            "frameworks": frameworks,
            "package_managers": package_managers,
            "build_systems": build_systems,
            "dependencies": package_deps[:300],
        },
        "components": components,
        "dependencies": {
            "internal": internal_edges[:200],
            "external_imports": external_imports[:200],
        },
        "entry_points": entrypoints,
        "execution_flows": [
            {"entry": entry["file"], "status": "INFERRED",
             "steps": [], "evidence": [_evidence(entry["file"], entry.get("line", 0))]}
            for entry in entrypoints[:20]
        ],
        "external_interfaces": external_interfaces[:100],
        "persistence": persistence,
        "quality_signals": quality,
        "risks": risks,
        "preserve": preserve,
        "unresolved_questions": unresolved,
        "conflicts": conflicting,
        "analysis_limitations": analysis_skipped[:100],
        "epistemic": classifications,
        "diagrams": {
            "component": _mermaid_component(components),
            "dependency": _mermaid_dependencies(internal_edges),
        },
        "provenance": {"tool": TOOL, "version": DISCOVERY_VERSION,
                       "model": None, "mode": "static"},
        "chestertons_fence": {
            "understand_before_changing": True,
            "justify_before_removing": True,
            "verify_after_changing": True,
            "unfamiliar_is_not_defective": True,
            "preserve_existing_behaviour": True,
        },
    }
    return dossier


def _markdown(dossier: dict[str, Any]) -> str:
    lines = [f"# Project Understanding Dossier — {dossier['overview']['project_name']}", ""]
    def add(title: str, body: Any) -> None:
        lines.extend([f"## {title}", "", body, ""])
    add("Project overview", json.dumps(dossier["overview"], ensure_ascii=False,
                                       indent=2))
    inv = dossier["inventory"]
    add("Repository inventory",
        f"- regular files: {inv.get('files_total_regular', 0)}\n"
        f"- hashed: {inv.get('hashed_count', 0)}\n"
        f"- hash complete: {inv.get('hash_complete')}\n"
        f"- coverage: {inv.get('hash_coverage')}\n"
        f"- excluded dirs: {', '.join(inv.get('excluded_directories') or [])}")
    tech = dossier["technology_stack"]
    add("Technology stack",
        f"- languages: {tech['languages']}\n"
        f"- frameworks: {[f['name'] for f in tech['frameworks']]}\n"
        f"- package managers: {tech['package_managers']}\n"
        f"- build systems: {tech['build_systems']}")
    add("Application entry points",
        "\n".join(f"- {e['file']} ({e['reason']})" for e in dossier["entry_points"]) or "- UNKNOWN")
    add("Architecture overview",
        "\n".join(f"- {c['name']} — {c['file_count']} file(s)" for c in dossier["components"]) or "- UNKNOWN")
    add("Major components and responsibilities",
        json.dumps(dossier["components"], ensure_ascii=False, indent=2))
    add("Dependency relationships",
        f"- internal edges: {len(dossier['dependencies']['internal'])}\n"
        f"- external imports: {len(dossier['dependencies']['external_imports'])}")
    add("Execution and data flows",
        "\n".join(f"- {f['entry']} ({f['status']})" for f in dossier["execution_flows"]) or "- UNKNOWN")
    add("External interfaces",
        "\n".join(f"- {i['text']} ({i['status']})" for i in dossier["external_interfaces"]) or "- UNKNOWN")
    add("Persistence model",
        "\n".join(f"- {i['text']} ({i['status']})" for i in dossier["persistence"]) or "- UNKNOWN")
    add("Existing tests and quality signals",
        json.dumps(dossier["quality_signals"], ensure_ascii=False, indent=2))
    add("Potential technical risks",
        "\n".join(f"- {r['text']} ({r['status']})" for r in dossier["risks"]) or "- none observed")
    add("Existing behaviour requiring preservation",
        "\n".join(f"- {p['text']}" for p in dossier["preserve"]) or "- none observed")
    add("Unresolved questions",
        "\n".join(f"- {q}" for q in dossier["unresolved_questions"]) or "- none")
    add("Analysis limitations",
        "\n".join(f"- {lim.get('path')}: {lim.get('reason')}" for lim in dossier["analysis_limitations"]) or "- none")
    add("Source evidence references",
        f"- analysis generated at {dossier['generated_at']}\n"
        f"- repository HEAD: {dossier['repository']['head']}\n"
        f"- files analyzed: {dossier['analysis_scope']['files_analyzed']}")
    add("Diagrams",
        "### Components\n\n```mermaid\n" + (dossier["diagrams"]["component"] or "") +
        "\n```\n\n### Dependencies\n\n```mermaid\n" +
        (dossier["diagrams"]["dependency"] or "") + "\n```")
    return "\n".join(lines)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
    except Exception:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def discover_project(state: ProjectState, store: Any, *,
                     refresh: bool = True,
                     **limits: Any) -> dict[str, Any]:
    existing = state.extensions.get("understanding") or {}
    if not refresh and existing.get("summary"):
        return {"summary": existing["summary"], "cached": True,
                "metadata": existing}
    root = Path(state.project_root).expanduser().resolve()
    dossier = analyze(root, **limits)
    if state.requirements.objective:
        dossier["requested_functionality"] = {
            "source": "RequirementContract",
            "objective": state.requirements.objective,
            "acceptance": state.requirements.acceptance_texts(),
            "status": "REQUESTED",
        }
    try:
        import brief as brief_flow
        blockers = brief_flow.blockers(state) if brief_flow.has_brief(state) else []
    except Exception:
        blockers = []
    if blockers:
        dossier["unresolved_questions"].extend(blockers)
    dossier["unresolved_questions"].append(
        "Documentation/implementation consistency was not semantically verified.")
    out_dir = Path(store.project_state_path(root)).parent / "understanding"
    json_path = out_dir / "dossier.json"
    md_path = out_dir / "dossier.md"
    store.atomic_write_json(json_path, dossier) if hasattr(store, "atomic_write_json") else _write_text_atomic(
        json_path, json.dumps(dossier, ensure_ascii=False, indent=2))
    _write_text_atomic(md_path, _markdown(dossier))
    compact = {
        "project": dossier["overview"]["project_name"],
        "languages": dossier["technology_stack"]["languages"],
        "frameworks": [f["name"] for f in dossier["technology_stack"]["frameworks"]],
        "entry_points": [e["file"] for e in dossier["entry_points"]],
        "components": [c["name"] for c in dossier["components"]],
        "risks": [r["text"] for r in dossier["risks"]],
        "unresolved": list(dossier["unresolved_questions"]),
        "epistemic": dossier["epistemic"],
        "analysis_scope": {
            "files_total": dossier["analysis_scope"]["files_total"],
            "files_analyzed": dossier["analysis_scope"]["files_analyzed"],
            "hash_complete": dossier["analysis_scope"]["hash_complete"],
        },
    }
    meta = {
        "generated_at": dossier["generated_at"],
        "head": dossier["repository"]["head"],
        "dossier_path": str(json_path),
        "markdown_path": str(md_path),
        "summary": compact,
        "scope": dossier["analysis_scope"],
        "tool": dossier["provenance"],
        "freshness": "FRESH",
    }
    state.extensions["understanding"] = meta
    store.save_project(state)
    return {"summary": compact, "metadata": meta, "dossier": dossier,
            "cached": False}


def load_dossier(state: ProjectState) -> Optional[dict[str, Any]]:
    path = (state.extensions.get("understanding") or {}).get("dossier_path")
    if not path or not Path(path).is_file():
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def freshness(state: ProjectState, root: Optional[str | Path] = None) -> dict[str, Any]:
    dossier = load_dossier(state)
    meta = state.extensions.get("understanding") or {}
    if dossier is None:
        return {"status": "UNKNOWN", "reason": "no dossier",
                "changed": [], "new": [], "removed": [], "head_changed": False}
    base_root = Path(root or state.project_root).expanduser().resolve()
    limits = dossier.get("analysis_scope", {}).get("limits", {})
    current = adoption.inspect_repository(
        base_root,
        max_files=int(limits.get("max_files_inventory", 2000)),
        max_total_bytes=int(limits.get("max_total_bytes", 64 * 1024 * 1024)),
        max_file_bytes=int(limits.get("max_file_bytes", 8 * 1024 * 1024)))
    old_hashes = dossier.get("analysis_scope", {}).get("file_hashes", {})
    new_hashes = current.get("file_sha256", {})
    changed = sorted(rel for rel, sha in old_hashes.items()
                     if rel in new_hashes and new_hashes[rel] != sha)
    removed = sorted(rel for rel in old_hashes if rel not in new_hashes)
    new_files = sorted(rel for rel in new_hashes if rel not in old_hashes)
    head_changed = bool(meta.get("head") and current.get("git_head")
                        and meta["head"] != current["git_head"])
    complete = (dossier.get("analysis_scope", {}).get("hash_complete", False)
                and current.get("hash_complete", False))
    if not complete:
        status = "UNKNOWN"
        reason = "analysis or current hash coverage is incomplete"
    elif changed or removed or new_files:
        status = "STALE"
        reason = "repository contents changed since analysis"
    else:
        status = "FRESH"
        reason = "bounded analysed files match"
    result = {"status": status, "reason": reason,
              "changed": changed[:100], "new": new_files[:100],
              "removed": removed[:100], "head_changed": head_changed,
              "current_head": current.get("git_head", "")}
    meta["last_freshness"] = result
    state.extensions["understanding"] = meta
    return result


def section(state: ProjectState, name: str) -> Any:
    dossier = load_dossier(state)
    if dossier is None:
        return None
    return dossier.get(name)


def summary_for_agent(state: ProjectState, max_chars: int = 4000) -> str:
    dossier = load_dossier(state)
    meta = state.extensions.get("understanding") or {}
    if not dossier:
        return "No project understanding dossier is available."
    s = dossier["overview"]
    tech = dossier["technology_stack"]
    risks = [r["text"] for r in dossier["risks"]]
    unresolved = dossier["unresolved_questions"]
    text = (
        f"Project: {s.get('project_name')} | languages={tech.get('languages')} | "
        f"frameworks={[f['name'] for f in tech.get('frameworks', [])]}\n"
        f"Entry points: {[e['file'] for e in dossier['entry_points']]}\n"
        f"Components: {[c['name'] for c in dossier['components']]}\n"
        f"Risks: {risks[:6]}\nUnresolved: {unresolved[:6]}\n"
        f"Freshness: {meta.get('freshness', 'UNKNOWN')} | "
        f"analyzed={dossier['analysis_scope']['files_analyzed']}/"
        f"{dossier['analysis_scope']['files_total']}")
    return text[:max_chars]
