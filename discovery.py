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

try:  # Python 3.11+; 3.10 reports TOML manifests as unresolved instead of guessing.
    import tomllib as _tomllib
except Exception:  # pragma: no cover - exercised on Python 3.10 CI
    _tomllib = None

DISCOVERY_VERSION = "p7-2c/2"
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

# Dependency manifests we attempt to interpret. Files that are recognised but
# cannot be interpreted reliably are reported under dependencies.unresolved_formats.
MANIFEST_NAMES = {
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "requirements.txt", "Pipfile", "Pipfile.lock", "poetry.lock",
    "pyproject.toml", "setup.py", "setup.cfg", "Cargo.toml", "Cargo.lock",
    "go.mod", "go.sum", "pom.xml", "build.gradle",
}
TEST_CONFIG_NAMES = {
    "pytest.ini", "tox.ini", ".coveragerc", "noxfile.py", "conftest.py",
    "jest.config.js", "jest.config.ts", "vitest.config.js",
    "vitest.config.ts", ".mocharc.json", ".mocharc.yml", "karma.conf.js",
}
# Source languages with a recorded structural-extraction method.
STRUCTURE_METHODS = {
    ".py": ("Python", "python-ast"),
    ".js": ("JavaScript", "regex-imports"),
    ".mjs": ("JavaScript", "regex-imports"),
    ".ts": ("TypeScript", "regex-imports"),
    ".tsx": ("TypeScript", "regex-imports"),
    ".jsx": ("JavaScript", "regex-imports"),
}
# Source-like extensions for which no structural extraction is attempted.
UNSUPPORTED_SOURCE_SUFFIXES = {
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".m", ".mm", ".swift",
    ".kt", ".kts", ".scala", ".clj", ".ex", ".exs", ".erl", ".hrl",
    ".lua", ".pl", ".pm", ".r", ".jl", ".dart", ".vue", ".svelte",
    ".fs", ".fsx", ".vb", ".asm", ".s", ".sol", ".zig", ".nim", ".cr",
}
# Markup/stylesheets are inventoried but are not source-code modules and must
# never be reported as "unsupported source code".
NON_SOURCE_SUFFIXES = {".html", ".css"}
# Source-code suffixes recognised by the project inventory (adoption language
# detection) plus the explicit source-like set, excluding markup/style. Any of
# these not in STRUCTURE_METHODS has no structural extractor. Deriving this from
# adoption.LANG_BY_SUFFIX keeps the classification complete when the inventory
# later learns new languages.
SOURCE_SUFFIXES = (set(STRUCTURE_METHODS)
                   | (set(adoption.LANG_BY_SUFFIX) - NON_SOURCE_SUFFIXES)
                   | UNSUPPORTED_SOURCE_SUFFIXES)


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
                            "public": not node.name.startswith("_"),
                            "extraction_method": "python-ast"})
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


def _porcelain_path(line: str) -> str:
    """Best-effort repository-relative path from a ``git status --porcelain`` line."""
    s = line.rstrip("\n")
    if len(s) > 3:
        s = s[3:]
    s = s.strip()
    if " -> " in s:
        s = s.split(" -> ", 1)[1]
    return s.strip().strip('"')


def _parse_requirement(rel: str, line: str, line_no: int) -> Optional[dict[str, Any]]:
    clean = line.split("#", 1)[0].strip()
    if not clean or clean.startswith("-"):
        return None
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.\-]*)", clean)
    if not m:
        return None
    version = ""
    vm = re.search(r"[=<>!~]=?\s*([^\s,;#]+)", clean)
    if vm:
        version = vm.group(1)
    return {"name": m.group(1), "version": version, "scope": "runtime",
            "evidence": [_evidence(rel, line_no)]}


def _requirements_deps(rel: str, text: str) -> tuple[list[dict[str, Any]], str]:
    deps: list[dict[str, Any]] = []
    unparsed = 0
    for line_no, line in enumerate(text.splitlines(), 1):
        clean = line.split("#", 1)[0].strip()
        if not clean or clean.startswith("-"):
            continue
        parsed = _parse_requirement(rel, line, line_no)
        if parsed is None:
            unparsed += 1
        else:
            deps.append(parsed)
    return deps, (f"{unparsed} unparsed line(s)" if unparsed else "")


def _toml_manifest_deps(rel: str, name: str,
                        text: str) -> tuple[list[dict[str, Any]], str]:
    if _tomllib is None:
        return [], "toml parser unavailable"
    try:
        data = _tomllib.loads(text)
    except Exception as exc:
        return [], f"invalid TOML: {exc}"
    if not isinstance(data, dict):
        return [], "TOML root is not a table"
    deps: list[dict[str, Any]] = []
    if name == "pyproject.toml":
        project = data.get("project")
        if isinstance(project, dict):
            for spec in project.get("dependencies") or []:
                if isinstance(spec, str):
                    parsed = _parse_requirement(rel, spec, 0)
                    if parsed:
                        deps.append(parsed)
            optional = project.get("optional-dependencies")
            if isinstance(optional, dict):
                for scope, specs in optional.items():
                    if isinstance(specs, list):
                        for spec in specs:
                            if isinstance(spec, str):
                                parsed = _parse_requirement(rel, spec, 0)
                                if parsed:
                                    parsed["scope"] = f"optional:{scope}"
                                    deps.append(parsed)
        poetry = (data.get("tool") or {}).get("poetry") if isinstance(
            data.get("tool"), dict) else None
        if isinstance(poetry, dict):
            for key in ("dependencies", "dev-dependencies"):
                block = poetry.get(key)
                if isinstance(block, dict):
                    for dep, version in block.items():
                        if str(dep).lower() == "python":
                            continue
                        deps.append({"name": str(dep),
                                     "version": str(version),
                                     "scope": key,
                                     "evidence": [_evidence(rel)]})
        build = data.get("build-system")
        if isinstance(build, dict):
            for spec in build.get("requires") or []:
                if isinstance(spec, str):
                    parsed = _parse_requirement(rel, spec, 0)
                    if parsed:
                        parsed["scope"] = "build"
                        deps.append(parsed)
    elif name == "Pipfile":
        for key in ("packages", "dev-packages"):
            block = data.get(key)
            if isinstance(block, dict):
                for dep, version in block.items():
                    deps.append({"name": str(dep), "version": str(version),
                                 "scope": key, "evidence": [_evidence(rel)]})
    else:  # Cargo.toml
        for key in ("dependencies", "dev-dependencies", "build-dependencies"):
            block = data.get(key)
            if isinstance(block, dict):
                for dep, version in block.items():
                    deps.append({"name": str(dep), "version": str(version),
                                 "scope": key, "evidence": [_evidence(rel)]})
    return deps, ""


def _go_manifest_deps(rel: str, text: str) -> tuple[list[dict[str, Any]], str]:
    deps: list[dict[str, Any]] = []
    in_block = False
    for line_no, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith("require ("):
            in_block = True
            continue
        if in_block and s == ")":
            in_block = False
            continue
        if s.startswith("require ") and not in_block:
            s = s[len("require "):].strip()
        elif not in_block:
            continue
        if not s or s.startswith("//"):
            continue
        parts = s.split()
        if parts and re.match(r"^[A-Za-z0-9._/\-]+$", parts[0]):
            deps.append({"name": parts[0],
                         "version": parts[1] if len(parts) > 1 else "",
                         "scope": "module",
                         "evidence": [_evidence(rel, line_no)]})
    return deps, ""


def _parse_manifest(rel: str, text: str) -> tuple[list[dict[str, Any]], str]:
    """Return (declared dependencies, unresolved reason for this manifest)."""
    name = rel.rsplit("/", 1)[-1]
    if name == "package.json":
        try:
            data = json.loads(text)
        except Exception as exc:
            return [], f"invalid JSON: {exc}"
        if not isinstance(data, dict):
            return [], "package.json is not an object"
        deps: list[dict[str, Any]] = []
        for key in ("dependencies", "devDependencies", "peerDependencies",
                    "optionalDependencies"):
            block = data.get(key)
            if isinstance(block, dict):
                for dep, version in block.items():
                    deps.append({"name": str(dep), "version": str(version),
                                 "scope": key, "evidence": [_evidence(rel)]})
        return deps, ""
    if name == "requirements.txt":
        return _requirements_deps(rel, text)
    if name in ("pyproject.toml", "Cargo.toml", "Pipfile"):
        return _toml_manifest_deps(rel, name, text)
    if name == "go.mod":
        return _go_manifest_deps(rel, text)
    # Recognised manifest whose format is not interpreted reliably (locks,
    # setup.py/setup.cfg, Maven/Gradle). Reported explicitly, never guessed.
    return [], "format not interpreted"


def _project_description(root: Path, files: Iterable[str],
                         manifests: dict[str, str]
                         ) -> tuple[dict[str, Any], str]:
    """First README paragraph (OBSERVED) or package.json description, else UNKNOWN."""
    file_set = set(files)
    for rel in ("README.md", "README.rst", "README", "readme.md"):
        if rel not in file_set:
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        para: list[tuple[int, str]] = []
        for line_no, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s:
                if para:
                    break
                continue
            if not para and s.startswith(("#", "!", "[", "```", "<", "|", "=")):
                continue
            para.append((line_no, s))
            if len(" ".join(p[1] for p in para)) > 400:
                break
        if para:
            description = " ".join(p[1] for p in para)[:400]
            return ({"value": description, "status": "OBSERVED",
                     "evidence": [_evidence(rel, para[0][0])]}, "")
    pkg = manifests.get("package.json")
    if pkg:
        try:
            data = json.loads(pkg)
        except Exception:
            data = None
        if isinstance(data, dict):
            desc = data.get("description")
            if isinstance(desc, str) and desc.strip():
                return ({"value": desc.strip()[:400], "status": "OBSERVED",
                         "evidence": [_evidence("package.json")]}, "")
    return ({"value": "", "status": "UNKNOWN", "evidence": [],
             "reason": "no README paragraph or metadata description observed"}, "")


def _structure_section(files: list[str], symbols: list[dict[str, Any]],
                       test_files: Iterable[str]) -> dict[str, Any]:
    test_set = set(test_files)
    modules: list[dict[str, Any]] = []
    unsupported: set[str] = set()
    for rel in files:
        suffix = Path(rel).suffix.lower()
        if suffix in STRUCTURE_METHODS:
            language, method = STRUCTURE_METHODS[suffix]
            modules.append({"path": rel, "language": language,
                            "extraction_method": method, "status": "OBSERVED"})
        elif suffix in SOURCE_SUFFIXES:
            # Source language recognised without a structural extractor.
            unsupported.add(suffix)
    classes: list[dict[str, Any]] = []
    functions: list[dict[str, Any]] = []
    interfaces: list[dict[str, Any]] = []
    for sym in sorted(symbols, key=lambda x: (x.get("file", ""),
                                              x.get("line", 0), x.get("name", ""))):
        rec = {"name": sym.get("name", ""), "file": sym.get("file", ""),
               "line": sym.get("line", 0), "public": bool(sym.get("public")),
               "status": "OBSERVED", "extraction_method": sym.get(
                   "extraction_method", "python-ast")}
        if sym.get("kind") == "class":
            classes.append(rec)
        else:
            functions.append(rec)
        if rec["public"] and rec["file"] not in test_set:
            interfaces.append({"name": rec["name"], "kind": sym.get("kind", ""),
                               "file": rec["file"], "line": rec["line"],
                               "status": "INFERRED",
                               "extraction_method": rec["extraction_method"],
                               "note": "public surface; semantic contract not verified"})
    return {"modules": modules[:500],
            "classes": classes[:500],
            "functions": functions[:500],
            "interfaces": interfaces[:500],
            "unsupported_source_suffixes": sorted(unsupported)}


def _tests_section(inventory: dict[str, Any], files: list[str],
                   dep_names: set[str]) -> dict[str, Any]:
    test_files = sorted(inventory.get("tests") or [])
    directories = sorted({rel.rsplit("/", 1)[0] if "/" in rel else "."
                          for rel in test_files})
    configuration: list[dict[str, Any]] = []
    for rel in sorted(files):
        name = rel.rsplit("/", 1)[-1]
        if name in TEST_CONFIG_NAMES:
            configuration.append({"path": rel, "status": "OBSERVED"})
    frameworks = sorted(name for name in (
        "pytest", "jest", "vitest", "mocha", "jasmine", "junit", "rspec")
        if name in dep_names)
    return {"files": test_files,
            "directories": directories,
            "configuration": configuration,
            "frameworks": frameworks,
            "execution_status": "NOT_RUN"}


def _evidence_items(groups: Iterable[Any], cap: int = 200
                    ) -> list[dict[str, Any]]:
    """Flatten findings into a deterministic, id-stable evidence index."""
    items: list[dict[str, Any]] = []
    confidence = {"OBSERVED": "high", "INFERRED": "medium",
                  "UNKNOWN": "low", "CONFLICTING": "low"}
    for group in groups:
        for finding in group:
            if not isinstance(finding, dict):
                continue
            status = str(finding.get("status") or "UNKNOWN")
            for ev in finding.get("evidence") or []:
                if len(items) >= cap:
                    return items
                items.append({
                    "id": f"ev-{len(items) + 1:04d}",
                    "classification": status,
                    "subject": finding.get("text", ""),
                    "claim": finding.get("text", ""),
                    "source_path": ev.get("file", ""),
                    "line_start": ev.get("line") or None,
                    "line_end": ev.get("line") or None,
                    "symbol": ev.get("symbol", ""),
                    "extraction_method": "static-analysis",
                    "confidence": confidence.get(status, "low"),
                    "limitations": finding.get("reason", ""),
                })
    return items


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
    truncated_manifests: set[str] = set()
    text_chars = 0
    for rel in files:
        if len(analyzed) >= max_files_analyzed or text_chars >= max_total_chars:
            analysis_skipped.append({"path": rel, "reason": "analysis_limit"})
            continue
        base_name = rel.rsplit("/", 1)[-1]
        if (Path(rel).suffix.lower() not in TEXT_SUFFIX
                and base_name not in MANIFEST_NAMES):
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
        if base_name in MANIFEST_NAMES:
            manifests[rel] = text
            if note == "truncated":
                truncated_manifests.add(rel)
            manifests[rel] = text
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(r"\b(TODO|FIXME|XXX|deprecated)\b", line, re.I):
                todo_markers.append({"file": rel, "line": i,
                                     "text": line.strip()[:240]})
            for m in re.finditer(
                    r"os\.environ(?:\.get)?\(?[\"']([A-Za-z_][A-Za-z0-9_]*)",
                    line):
                env_refs.append({"key": m.group(1), "file": rel, "line": i})
    package_deps: list[dict[str, Any]] = []
    unresolved_formats: list[dict[str, Any]] = []
    for rel in sorted(manifests):
        if rel in truncated_manifests:
            unresolved_formats.append({"path": rel,
                                       "reason": "truncated (analysis limit)"})
            continue
        parsed_deps, unresolved = _parse_manifest(rel, manifests[rel])
        package_deps.extend(parsed_deps)
        if unresolved:
            unresolved_formats.append({"path": rel, "reason": unresolved})
    package_deps.sort(key=lambda d: (d["name"].lower(), d.get("scope", "")))
    dep_names = {d["name"].lower() for d in package_deps}
    # Framework evidence: a declared dependency or an exact top-level import
    # segment. Substring matching over arbitrary text is deliberately avoided.
    import_by_pkg: dict[str, list[dict[str, Any]]] = {}
    for item in external_imports:
        module = str(item.get("module") or "")
        top = re.split(r"[./]", module, maxsplit=1)[0].lower()
        if top:
            import_by_pkg.setdefault(top, []).append(
                _evidence(item.get("file", ""), item.get("line", 0)))
    frameworks = []
    for hint, label in FRAMEWORK_HINTS.items():
        dep_evidence = [e for d in package_deps
                        if d["name"].lower() == hint for e in d["evidence"]]
        import_evidence = import_by_pkg.get(hint, [])
        evidence = dep_evidence + import_evidence
        if evidence:
            frameworks.append({"name": label, "status": "OBSERVED",
                               "evidence": evidence[:10],
                               "detected_via": ("declared-dependency"
                                                if dep_evidence else "import")})
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
    name_value, name_source, name_evidence = root.name, "directory-name", []
    pkg_text = manifests.get("package.json")
    if pkg_text:
        try:
            pkg_data = json.loads(pkg_text)
        except Exception:
            pkg_data = None
        if isinstance(pkg_data, dict):
            pkg_name = pkg_data.get("name")
            if isinstance(pkg_name, str) and pkg_name.strip():
                name_value = pkg_name.strip()
                name_source = "package.json"
                name_evidence = [_evidence("package.json")]
    if name_source == "directory-name":
        py_text = manifests.get("pyproject.toml")
        if py_text and _tomllib is not None:
            try:
                pdata = _tomllib.loads(py_text)
            except Exception:
                pdata = None
            proj = pdata.get("project") if isinstance(pdata, dict) else None
            if isinstance(proj, dict):
                proj_name = proj.get("name")
                if isinstance(proj_name, str) and proj_name.strip():
                    name_value = proj_name.strip()
                    name_source = "pyproject.toml"
                    name_evidence = [_evidence("pyproject.toml")]
    description, _ = _project_description(root, files, manifests)
    structure = _structure_section(files, symbols,
                                   inventory.get("tests") or [])
    tests_section = _tests_section(inventory, files, dep_names)
    files_total = inventory.get("files_total_regular", 0)
    coverage = {
        "files_observed": files_total,
        "files_hashed": inventory.get("hashed_count", 0),
        "files_analysed": len(analyzed),
        "files_skipped": len(analysis_skipped),
        "analysis_coverage": (round(len(analyzed) / files_total, 4)
                              if files_total else 0.0),
        "hash_coverage": inventory.get("hash_coverage", 0.0),
        "denominator": "files_total_regular",
    }
    dirty_lines = inventory.get("git_dirty") or []
    dirty_paths = sorted({_porcelain_path(line) for line in dirty_lines
                          if line.strip()})
    status_available = bool(inventory.get("git_status_available"))
    unresolved_paths = {item["path"] for item in unresolved_formats}
    manifest_report = [
        {"path": rel,
         "status": ("unresolved" if rel in unresolved_paths else "interpreted")}
        for rel in sorted(manifests)
    ]
    component_findings = [resp for comp in components
                          for resp in (comp.get("responsibilities") or [])]
    evidence_items = _evidence_items([
        component_findings, external_interfaces, persistence, risks,
        preserve, conflicting,
    ])
    dossier = {
        "schema": DISCOVERY_VERSION,
        "generated_at": utc_now(),
        "project": {
            "root": str(root),
            "name": {"value": name_value, "source": name_source,
                     "status": "OBSERVED", "evidence": name_evidence},
            "description": description,
        },
        "repository": {
            "root": str(root),
            "git_available": bool(inventory.get("git_available")),
            "head": inventory.get("git_head", ""),
            "branch": inventory.get("git_branch", ""),
            "dirty": (bool(dirty_lines) if status_available else None),
            "dirty_paths": dirty_paths if status_available else [],
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
            "test_frameworks": tests_section["frameworks"],
            "dependencies": package_deps[:300],
        },
        "components": components,
        "structure": structure,
        "dependencies": {
            "internal": internal_edges[:200],
            "external_imports": external_imports[:200],
            "manifests": manifest_report,
            "declared_dependencies": package_deps[:300],
            "unresolved_formats": unresolved_formats,
        },
        "entry_points": entrypoints,
        "execution_flows": [
            {"entry": entry["file"], "status": "INFERRED",
             "steps": [], "evidence": [_evidence(entry["file"], entry.get("line", 0))]}
            for entry in entrypoints[:20]
        ],
        "external_interfaces": external_interfaces[:100],
        "persistence": persistence,
        "tests": tests_section,
        "evidence": {
            "coverage": coverage,
            "classifications": classifications,
            "items": evidence_items,
        },
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
    project = dossier.get("project") or {}
    description = (project.get("description") or {})
    add("Project description",
        f"- name: {(project.get('name') or {}).get('value', 'UNKNOWN')} "
        f"({(project.get('name') or {}).get('source', 'unknown')})\n"
        f"- description: {description.get('value') or 'UNKNOWN'} "
        f"({description.get('status', 'UNKNOWN')})")
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
        f"- external imports: {len(dossier['dependencies']['external_imports'])}\n"
        f"- manifests: {[m['path'] for m in dossier['dependencies']['manifests']]}\n"
        f"- unresolved formats: "
        f"{[u['path'] for u in dossier['dependencies']['unresolved_formats']]}")
    structure = dossier.get("structure") or {}
    add("Source structure",
        f"- modules: {len(structure.get('modules', []))}\n"
        f"- classes: {len(structure.get('classes', []))}\n"
        f"- functions: {len(structure.get('functions', []))}\n"
        f"- interfaces (public surface, inferred): "
        f"{len(structure.get('interfaces', []))}\n"
        f"- unsupported suffixes: "
        f"{structure.get('unsupported_source_suffixes', [])}")
    tests = dossier.get("tests") or {}
    add("Tests",
        f"- files: {len(tests.get('files', []))}\n"
        f"- directories: {tests.get('directories', [])}\n"
        f"- frameworks: {tests.get('frameworks', [])}\n"
        f"- configuration: {[c['path'] for c in tests.get('configuration', [])]}\n"
        f"- execution: {tests.get('execution_status', 'UNKNOWN')}")
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
        f"- files analyzed: {dossier['analysis_scope']['files_analyzed']}\n"
        f"- evidence items: {len((dossier.get('evidence') or {}).get('items', []))}\n"
        f"- coverage: {json.dumps((dossier.get('evidence') or {}).get('coverage', {}))}")
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
