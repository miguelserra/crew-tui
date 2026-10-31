"""Shared native workflow helpers (no Factory)."""

from __future__ import annotations

import http.server
import json
import re
import socketserver
import threading
import time
from pathlib import Path
from typing import Any, Optional


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def json_or_raise(raw: str, what: str) -> dict[str, Any]:
    from main import extract_json_block
    try:
        data = json.loads(extract_json_block(raw))
    except Exception as exc:
        raise RuntimeError(f"{what}: JSON inválido ({exc})") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{what}: JSON inválido")
    return data


def one_block(raw: str, fence: str) -> str:
    from main import extract_code_blocks_typed
    blocks = [(lang, code) for lang, code in extract_code_blocks_typed(raw) if code]
    if not blocks:
        return ""
    if fence:
        for lang, code in blocks:
            if lang == fence or (fence == "javascript" and lang in ("js", "mjs")):
                return code
    return max(blocks, key=lambda b: len(b[1]))[1]


def multi_file_blocks(raw: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pattern = re.compile(r"###\s*FILE:\s*([^\n`]+)\s*\n(.*?)(?=###\s*FILE:|\Z)", re.DOTALL)
    for m in pattern.finditer(raw):
        rel = m.group(1).strip().strip("`")
        body = m.group(2)
        code = one_block(body, "") or body.strip()
        if rel and code:
            out.append((rel, code))
    return out


def format_report_errors(report: dict[str, Any]) -> str:
    lines: list[str] = []
    if report.get("note"):
        lines.append(f"NOTE: {report['note']}")
    for e in report.get("page_errors", []) or []:
        lines.append(f"PAGE ERROR: {e}")
    for c in report.get("console_errors", []) or []:
        lines.append(f"CONSOLE {c.get('type')}: {c.get('text')} ({c.get('where')})")
    for r in report.get("failed_requests", []) or []:
        lines.append(f"REQUEST FAILED: {r.get('url')} — {r.get('failure')}")
    for r in report.get("http_errors", []) or []:
        lines.append(f"HTTP {r.get('status')}: {r.get('url')}")
    for m in report.get("dom_mismatches", []) or []:
        lines.append(f"DOM MISMATCH ({m.get('kind')}): {m.get('missing')} "
                     f"referenced by {m.get('referenced_by')} but defined in {m.get('defined_in')}")
    if report.get("output"):
        lines.append("OUTPUT:\n" + str(report["output"])[:4000])
    return "\n".join(lines)


def dom_contract_check(root: Path) -> list[dict[str, str]]:
    html_files = [p for p in root.rglob("*.html")
                  if ".factory" not in p.parts and ".crew" not in p.parts]
    if not html_files:
        return []
    ids: set[str] = set()
    classes: set[str] = set()
    for h in html_files:
        txt = h.read_text(encoding="utf-8", errors="ignore")
        ids |= set(re.findall(r'\bid\s*=\s*["\']([^"\']+)["\']', txt))
        classes |= set(re.findall(r'\bclass\s*=\s*["\']([^"\']+)["\']', txt))
    refs: dict[str, set[str]] = {}
    for js in list(root.rglob("*.js")) + list(root.rglob("*.mjs")):
        if ".factory" in js.parts or ".crew" in js.parts:
            continue
        txt = js.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"getElementById\(\s*[\"']([^\"']+)[\"']", txt):
            refs.setdefault(m.group(1), set()).add(str(js.relative_to(root)))
        for m in re.finditer(r"querySelector(?:All)?\(\s*[\"']#([A-Za-z0-9_-]+)[\"']", txt):
            refs.setdefault(m.group(1), set()).add(str(js.relative_to(root)))
    css_refs: dict[str, set[str]] = {}
    for css in root.rglob("*.css"):
        if ".factory" in css.parts or ".crew" in css.parts:
            continue
        txt = css.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"#([A-Za-z][A-Za-z0-9_-]*)", txt):
            css_refs.setdefault(m.group(1), set()).add(str(css.relative_to(root)))
    out: list[dict[str, str]] = []
    for ident, where in refs.items():
        if ident not in ids:
            out.append({"kind": "id", "missing": f"#{ident}",
                        "referenced_by": ", ".join(sorted(where)),
                        "defined_in": ", ".join(str(h.relative_to(root)) for h in html_files)})
    for ident, where in css_refs.items():
        if re.fullmatch(r"[0-9a-fA-F]{3,8}", ident) or ident == "root" or ident[0].isdigit():
            continue
        if ident not in ids:
            out.append({"kind": "css-id", "missing": f"#{ident}",
                        "referenced_by": ", ".join(sorted(where)),
                        "defined_in": ", ".join(str(h.relative_to(root)) for h in html_files)})
    return out


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        pass


def serve(root: Path) -> tuple[Optional[socketserver.TCPServer], int]:
    handler = lambda *a, **kw: _QuietHandler(*a, directory=str(root), **kw)  # noqa: E731
    for port in range(8765, 8805):
        try:
            server = socketserver.TCPServer(("127.0.0.1", port), handler)
        except OSError:
            continue
        server.allow_reuse_address = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        time.sleep(0.15)
        return server, port
    return None, 0
