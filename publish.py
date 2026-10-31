"""Recoverable multi-file publish with journal and conflict detection."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from workspace import PublishPlan, plan_publish, sha256_file


class PublishError(Exception):
    pass


class PublishConflict(PublishError):
    pass


class PublishManager:
    def __init__(self, project_root: str | Path, workspace_root: str | Path,
                 state_dir: str | Path, origin_hashes: dict[str, str]):
        self.project = Path(project_root).expanduser().resolve()
        self.workspace = Path(workspace_root).expanduser().resolve()
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.origin = dict(origin_hashes or {})
        self.root = self.state_dir / "publish"

    # -- paths -------------------------------------------------------------- #

    def _new_dir(self) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = self.root / f"{stamp}-{os.getpid()}"
        n = 1
        while path.exists():
            path = self.root / f"{stamp}-{os.getpid()}-{n}"
            n += 1
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _journal(self, path: Path) -> Path:
        return path / "journal.json"

    def _write_journal(self, path: Path, data: dict[str, Any]) -> None:
        tmp = self._journal(path).with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self._journal(path))

    # -- conflict/publish --------------------------------------------------- #

    def publish(self) -> dict[str, Any]:
        plan = plan_publish(self.project, self.workspace, self.origin)
        if plan.conflicts:
            raise PublishConflict("; ".join(plan.conflicts))
        pending = [e for e in plan.entries if e.action != "skip"]
        run_dir = self._new_dir()
        entries: list[dict[str, Any]] = []
        for entry in pending:
            backup = None
            if entry.action == "replace":
                src = self.project / entry.rel
                backup = run_dir / "backup" / entry.rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, backup)
            entries.append({"rel": entry.rel, "action": entry.action,
                            "old_sha": entry.old_sha, "new_sha": entry.new_sha,
                            "backup": str(backup) if backup else None,
                            "status": "pending"})
        journal = {"id": run_dir.name, "status": "publishing",
                   "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "entries": entries}
        self._write_journal(run_dir, journal)
        try:
            for item in journal["entries"]:
                self._apply(item)
                item["status"] = "done"
                self._write_journal(run_dir, journal)
            journal["status"] = "done"
            self._write_journal(run_dir, journal)
            return {"id": run_dir.name, "status": "done",
                    "entries": len(entries), "path": str(run_dir)}
        except Exception as exc:
            self._rollback(journal, run_dir)
            journal["status"] = "rolled_back"
            journal["error"] = str(exc)[:300]
            self._write_journal(run_dir, journal)
            if isinstance(exc, PublishError):
                raise
            raise PublishError(str(exc)) from exc

    def _apply(self, item: dict[str, Any]) -> None:
        src = self.workspace / item["rel"]
        dest = self.project / item["rel"]
        current = sha256_file(dest) if dest.is_file() else ""
        if item["action"] == "replace" and current != item["old_sha"]:
            raise PublishConflict(f"alteração concorrente: {item['rel']}")
        if item["action"] == "create" and current:
            raise PublishConflict(f"ficheiro criado entretanto: {item['rel']}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.",
                                        suffix=".p6tmp", dir=str(dest.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out, src.open("rb") as inp:
                shutil.copyfileobj(inp, out)
                out.flush()
                os.fsync(out.fileno())
            if sha256_file(tmp) != item["new_sha"]:
                raise PublishError(f"hash divergente: {item['rel']}")
            os.replace(tmp, dest)
        finally:
            try:
                tmp.unlink()
            except Exception:
                pass

    def _rollback(self, journal: dict[str, Any], run_dir: Path) -> None:
        for item in reversed(journal["entries"]):
            rel = item["rel"]
            dest = self.project / rel
            backup = Path(item["backup"]) if item.get("backup") else None
            try:
                if backup and backup.is_file():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup, dest)
                elif (item["action"] == "create" and dest.is_file()
                      and sha256_file(dest) == item.get("new_sha")):
                    dest.unlink()
            except Exception:
                pass
            item["status"] = "rolled_back"

    # -- recovery ----------------------------------------------------------- #

    def recover(self) -> Optional[dict[str, Any]]:
        if not self.root.is_dir():
            return None
        journals = sorted(self.root.glob("*/journal.json"), reverse=True)
        for jpath in journals:
            try:
                data = json.loads(jpath.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("status") != "publishing":
                continue
            run_dir = jpath.parent
            for item in reversed(data.get("entries", [])):
                dest = self.project / item["rel"]
                current = sha256_file(dest) if dest.is_file() else ""
                backup = Path(item["backup"]) if item.get("backup") else None
                try:
                    if backup and backup.is_file():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(backup, dest)
                    elif item["action"] == "create" and current == item["new_sha"]:
                        dest.unlink()
                except Exception:
                    pass
                item["status"] = "rolled_back"
            data["status"] = "rolled_back"
            self._write_journal(run_dir, data)
            return {"id": run_dir.name, "status": "rolled_back"}
        return None
