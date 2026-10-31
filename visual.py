"""Native VISUAL service: Playwright audit + multimodal review (no Factory)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import verify as verify_policy
import native_util
from router import RouteRequest
from state import ProjectState, Task


def playwright_audit(root: Path, logs_dir: Path, page: str = "index.html",
                     width: int = 1024, height: int = 768,
                     wait_ms: int = 1500) -> dict[str, Any]:
    dom = native_util.dom_contract_check(root)
    report: dict[str, Any] = {"verdict": "FAIL", "console_errors": [],
                              "console_all": [], "page_errors": [],
                              "failed_requests": [], "http_errors": [],
                              "dom_mismatches": dom, "screenshot": ""}
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        report["verdict"] = "UNAVAILABLE"
        report["note"] = f"playwright indisponível: {exc}"
        return report
    server, port = native_util.serve(root)
    if server is None:
        report["verdict"] = "UNAVAILABLE"
        report["note"] = "não consegui subir servidor HTTP local"
        return report
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(viewport={"width": width, "height": height})
            pg = ctx.new_page()
            pg.on("console", lambda m: report["console_all"].append(
                {"type": m.type, "text": m.text, "where": str(m.location)}))
            pg.on("pageerror", lambda e: report["page_errors"].append(str(e)))
            pg.on("requestfailed", lambda r: report["failed_requests"].append(
                {"url": r.url, "failure": (r.failure or "")}))
            pg.on("response", lambda r: report["http_errors"].append(
                {"url": r.url, "status": r.status})
                  if r.status >= 400 and "favicon" not in r.url else None)
            pg.goto(f"http://127.0.0.1:{port}/{page}", wait_until="load", timeout=15000)
            pg.wait_for_timeout(wait_ms)
            logs_dir.mkdir(parents=True, exist_ok=True)
            png = logs_dir / "ui_screenshot.png"
            pg.screenshot(path=str(png), full_page=True)
            report["screenshot"] = str(png)
            ctx.close()
            browser.close()
    except Exception as exc:
        report["note"] = f"playwright falhou: {type(exc).__name__}: {exc}"
    finally:
        server.shutdown()
        server.server_close()
    report["console_errors"] = [c for c in report["console_all"]
                                if c["type"] in ("error", "assert")]
    problems = (report["page_errors"] or report["console_errors"]
                or report["failed_requests"] or report["http_errors"]
                or report["dom_mismatches"])
    if not report.get("note") and not problems:
        report["verdict"] = "PASS"
    elif not report.get("note"):
        report["verdict"] = "FAIL"
    return report


class VisualService:
    def __init__(self, settings: Any, pool: Any, emit: Any, run_id: str = "",
                 artifact_dir: str | Path | None = None):
        self.settings = settings
        self.pool = pool
        self.emit = emit
        self.run_id = run_id
        self.artifact_dir = Path(artifact_dir).expanduser() if artifact_dir else None

    def _artifacts(self, root: Path) -> Path:
        return (self.artifact_dir or (root / ".crew" / "runs" / (self.run_id or "latest")))

    def step_visual(self, project: ProjectState, workspace_root: str | Path) -> bool:
        root = Path(workspace_root).expanduser().resolve()
        if not (root / "index.html").is_file():
            project.visual = {"verdict": verify_policy.NA, "note": "sem index.html"}
            return True
        if not self.settings.visual_check:
            project.visual = {"verdict": verify_policy.NA, "note": "visual_check off"}
            return True
        report = playwright_audit(root, self._artifacts(root) / "screenshots")
        png = report.get("screenshot")
        vision: dict[str, Any] = {"verdict": verify_policy.NA}
        if png:
            vision = self._review(project, Path(png))
            report["vision"] = vision
            if vision.get("verdict") == verify_policy.FAIL and vision.get("objections"):
                old = vision
                self._fix(project, root, vision)
                report = playwright_audit(root, self._artifacts(root) / "screenshots")
                report["vision_retry"] = old
                if report.get("screenshot"):
                    vision = self._review(project, Path(report["screenshot"]))
                    report["vision"] = vision
        project.visual = report
        v = verify_policy.normalize_verdict(vision.get("verdict", verify_policy.NA))
        audit = verify_policy.normalize_verdict(report.get("verdict"))
        audit_ok = audit == verify_policy.PASS
        ok = audit_ok and v in (verify_policy.PASS, verify_policy.NA)
        if audit == verify_policy.NA and not png:
            ok = True
        if v == verify_policy.UNVERIFIED or audit == verify_policy.UNVERIFIED:
            ok = bool(self.settings.allow_unverified) and audit != verify_policy.FAIL
        self.emit("VISUAL", f"audit {report.get('verdict')} · vision {v}")
        return ok

    def _review(self, project: ProjectState, png: Path) -> dict[str, Any]:
        try:
            img = base64.b64encode(png.read_bytes()).decode()
        except Exception as exc:
            return {"verdict": verify_policy.UNVERIFIED, "note": str(exc)}
        acceptance = project.requirements.acceptance_texts()
        prompt = f"""ACCEPTANCE CRITERIA:
{chr(10).join(f'- {a}' for a in acceptance) or '- UI matches goal'}

GOAL: {project.goal}

Check this screenshot. STRICT JSON:
{{"verdict":"PASS"|"FAIL","objections":[{{"claim":str,"evidence":str,"fix":str}}]}}"""
        request = RouteRequest(role="visual", required_capabilities=frozenset({"vision"}),
                               run_id=self.run_id or "", tag="VISUAL")
        routed = self.pool.router.route(request)
        if not routed.candidates:
            return {"verdict": verify_policy.UNVERIFIED,
                    "note": "sem modelo multimodal"}
        from langchain_core.messages import HumanMessage, SystemMessage
        try:
            raw = self.pool.complete(routed.chain, [
                SystemMessage("You output valid JSON only."),
                HumanMessage(content=[{"type": "text", "text": prompt},
                                      {"type": "image_url",
                                       "image_url": {"url": f"data:image/png;base64,{img}"}}]),
            ], meta={"run": self.run_id or None, "tag": "VISUAL"})
            data = native_util.json_or_raise(raw, "vision")
            verdict = verify_policy.normalize_verdict(data.get("verdict"))
            if verdict not in (verify_policy.PASS, verify_policy.FAIL):
                return {"verdict": verify_policy.UNVERIFIED,
                        "note": f"vision verdict inválido: {data.get('verdict')!r}"}
            data["verdict"] = verdict
            if routed.candidates:
                data["vision"] = routed.candidates[0].ref.to_dict()
            return data
        except Exception as exc:
            return {"verdict": verify_policy.UNVERIFIED, "note": str(exc)[:300]}

    def _fix(self, project: ProjectState, root: Path, verdict: dict[str, Any]) -> None:
        objections = json.dumps(verdict.get("objections", []), ensure_ascii=False)[:4000]
        request = RouteRequest(role="fixer", complexity="high",
                               run_id=self.run_id or "", tag="DEV")
        routed = self.pool.router.route(request)
        if not routed.candidates:
            return
        for task in [t for t in project.tasks if t.target_file.endswith((".css", ".html", ".js"))][:3]:
            path = (root / task.target_file).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                continue
            if not path.is_file():
                continue
            lang = path.suffix.lstrip(".") or "text"
            prompt = f"""GOAL: {project.goal}
FILE: {task.target_file}
UI OBJECTIONS:
{objections}
CURRENT CONTENT:
```{lang}
{path.read_text(encoding='utf-8')[:12000]}
```
Fix ONLY the objections. Output the COMPLETE file in one fenced block."""
            try:
                raw = self.pool.complete(routed.chain, [
                    self._msg("SystemMessage", "You fix UI defects exactly."),
                    self._msg("HumanMessage", prompt),
                ], meta={"run": self.run_id or None, "tag": "DEV", "task": task.id})
                code = native_util.one_block(raw, lang)
                if code.strip():
                    path.write_text(code.rstrip("\n") + "\n", encoding="utf-8")
            except Exception:
                continue

    @staticmethod
    def _msg(kind: str, text: str) -> Any:
        from langchain_core.messages import HumanMessage, SystemMessage
        return SystemMessage(text) if kind == "SystemMessage" else HumanMessage(text)
