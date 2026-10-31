"""Recoverable per-file publish with journal, path safety and rollback."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional

from workspace import (
    IgnoreRules, PublishEntry, WorkspaceManifest, plan_publish, sha256_file,
)

STATUS_PREPARED = "PREPARED"
STATUS_PUBLISHING = "PUBLISHING"
STATUS_ROLLING_BACK = "ROLLING_BACK"
STATUS_RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
STATUS_COMMITTED = "COMMITTED"
STATUS_ROLLED_BACK = "ROLLED_BACK"
STATUS_REVALIDATION = "REVALIDATION_REQUIRED"

PENDING_STATES = {STATUS_PREPARED, STATUS_PUBLISHING, STATUS_ROLLING_BACK,
                  "prepared", "publishing", "rolling_back"}


class PublishError(Exception):
    pass


class PublishConflict(PublishError):
    pass


class PublishBusy(PublishError):
    pass


class RecoveryRequired(PublishError):
    pass


class ProjectLock:
    """Advisory exclusive lock for one project's publish operation."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a+")
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0, os.SEEK_END)
                if self._fh.tell() == 0:
                    self._fh.write("\0")
                    self._fh.flush()
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception as exc:
            self._fh.close()
            raise PublishBusy(f"publicação já em curso: {self.path}") from exc
        return self

    def __exit__(self, *exc):
        try:
            if self._fh is not None:
                if os.name == "nt":
                    import msvcrt
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                self._fh.close()
        except Exception:
            pass
        self._fh = None
        return False


def _safe_rel(rel: str) -> str:
    rel = str(rel or "").replace("\\", "/").strip()
    if not rel or rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        raise PublishError(f"caminho inválido: {rel!r}")
    path = PurePosixPath(rel)
    if any(part in ("", ".", "..") for part in path.parts):
        raise PublishError(f"caminho inseguro: {rel!r}")
    return str(path)


def _safe_path(root: Path, rel: str) -> Path:
    rel = _safe_rel(rel)
    root = root.resolve()
    current = root
    for part in PurePosixPath(rel).parts:
        current = current / part
        if current.is_symlink():
            raise PublishError(f"symlink no caminho de destino: {current}")
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PublishError(f"caminho escapa da raiz: {rel!r}") from exc
    return target


def _fsync_dir(path: Path) -> None:
    """Persist a directory entry after replace on POSIX. Windows has no portable
    directory fsync; process-death recovery still applies there."""
    if os.name == "nt":
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atom_write_from(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.parent.is_symlink():
        raise PublishError(f"diretório symlink: {dest.parent}")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.",
                                    suffix=".p6tmp", dir=str(dest.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, src.open("rb") as inp:
            shutil.copyfileobj(inp, out)
            out.flush()
            os.fsync(out.fileno())
        if sha256_file(tmp) != sha256_file(src):
            raise PublishError(f"cópia divergente: {dest}")
        os.replace(tmp, dest)
        _fsync_dir(dest.parent)
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass


class PublishManager:
    def __init__(self, project_root: str | Path, workspace_root: str | Path,
                 state_dir: str | Path, origin_hashes: dict[str, str], *,
                 rules: Optional[IgnoreRules] = None,
                 deletions: Iterable[Any] = (),
                 manifest: Optional[WorkspaceManifest] = None,
                 fault: Optional[Any] = None):
        self.project = Path(project_root).expanduser().resolve()
        self.workspace = Path(workspace_root).expanduser().resolve()
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.origin = dict(origin_hashes or {})
        self.rules = rules or IgnoreRules.from_project(self.project)
        self.deletions = list(deletions or ())
        self.manifest = manifest
        self.fault = fault
        self.root = self.state_dir / "publish"
        self.marker = self.root / "RECOVERY_REQUIRED.json"
        self.lock_path = self.state_dir / "publish.lock"

    # -- journal helpers ---------------------------------------------------- #

    def _checkpoint(self, name: str, **info: Any) -> None:
        """Test seam for crash injection; a BaseException simulates a crash."""
        if self.fault is not None:
            self.fault(name, info)

    def _new_dir(self) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = self.root / f"{stamp}-{os.getpid()}"
        n = 1
        while path.exists():
            path = self.root / f"{stamp}-{os.getpid()}-{n}"
            n += 1
        path.mkdir(parents=True, exist_ok=False)
        return path

    @staticmethod
    def _journal(path: Path) -> Path:
        return path / "journal.json"

    def _write_journal(self, path: Path, data: dict[str, Any]) -> None:
        target = self._journal(path)
        fd, tmp_name = tempfile.mkstemp(prefix=".journal.", suffix=".tmp",
                                        dir=str(path))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
            _fsync_dir(path)
        finally:
            try:
                tmp.unlink()
            except Exception:
                pass

    def _mark_recovery(self, journal_id: str, reason: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.marker.write_text(json.dumps(
            {"journal": journal_id, "reason": reason,
             "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            ensure_ascii=False, indent=2), encoding="utf-8")

    def _clear_marker(self) -> None:
        try:
            self.marker.unlink()
        except FileNotFoundError:
            pass

    def recovery_required(self) -> Optional[dict[str, Any]]:
        if self.marker.is_file():
            try:
                return json.loads(self.marker.read_text(encoding="utf-8"))
            except Exception:
                return {"reason": "marcador de recuperação ilegível"}
        for jpath in sorted(self.root.glob("*/journal.json"), reverse=True):
            try:
                data = json.loads(jpath.read_text(encoding="utf-8"))
            except Exception:
                self._mark_recovery(jpath.parent.name, "journal inválido")
                return {"journal": jpath.parent.name, "reason": "journal inválido"}
            status = str(data.get("status") or "")
            if status.upper() in (STATUS_RECOVERY_REQUIRED,
                                  STATUS_REVALIDATION):
                return {"journal": data.get("id"), "reason": status}
        return None

    # -- validation --------------------------------------------------------- #

    def _validate_journal(self, run_dir: Path,
                          data: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(data, dict):
            raise PublishError("journal inválido")
        entries = data.get("entries")
        if not isinstance(entries, list):
            raise PublishError("journal sem entries")
        out: list[dict[str, Any]] = []
        for item in entries:
            if not isinstance(item, dict):
                raise PublishError("entrada de journal inválida")
            rel = _safe_rel(item.get("rel"))
            action = str(item.get("action") or "")
            if action not in ("create", "replace", "delete", "skip"):
                raise PublishError(f"action inválida: {action!r}")
            backup = item.get("backup")
            if backup:
                b = Path(str(backup))
                expected_root = (run_dir / "backup").resolve()
                b = b.resolve() if b.is_absolute() else (run_dir / b).resolve()
                try:
                    b.relative_to(expected_root)
                except ValueError as exc:
                    raise PublishError(f"backup fora da pasta: {backup}") from exc
                item["backup"] = str(b)
            generated = item.get("generated")
            if generated:
                g = Path(str(generated))
                expected_root = (run_dir / "generated").resolve()
                g = g.resolve() if g.is_absolute() else (run_dir / g).resolve()
                try:
                    g.relative_to(expected_root)
                except ValueError as exc:
                    raise PublishError(f"generated fora da pasta: {generated}") from exc
                item["generated"] = str(g)
            item["rel"] = rel
            out.append(item)
        return out

    # -- public API --------------------------------------------------------- #

    def publish(self) -> dict[str, Any]:
        with ProjectLock(self.lock_path):
            if self.recovery_required() is not None:
                raise RecoveryRequired("publicação bloqueada: recuperação pendente")
            recovered = self._recover_locked()
            if recovered and recovered.get("status") in (STATUS_RECOVERY_REQUIRED,
                                                          STATUS_REVALIDATION):
                raise RecoveryRequired("publicação bloqueada: recuperação pendente")
            return self._publish_locked()

    def _publish_locked(self) -> dict[str, Any]:
        plan = plan_publish(self.project, self.workspace, self.origin,
                            rules=self.rules,
                            approved_deletions=self.deletions,
                            manifest=self.manifest)
        if plan.conflicts:
            raise PublishConflict("; ".join(plan.conflicts))
        pending = [e for e in plan.entries if e.action != "skip"]
        if not pending:
            return {"id": "", "status": STATUS_COMMITTED, "entries": 0}
        for entry in pending:
            _safe_path(self.project, entry.rel)
            _safe_path(self.workspace, entry.rel)
        run_dir = self._new_dir()
        self._checkpoint("after_run_dir", run_dir=run_dir.name)
        entries: list[dict[str, Any]] = []
        for entry in pending:
            backup = None
            generated = None
            if entry.action in ("replace", "delete"):
                src = self.project / entry.rel
                backup = run_dir / "backup" / entry.rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                if not src.is_file():
                    raise PublishConflict(f"original desapareceu: {entry.rel}")
                self._checkpoint("before_backup", rel=entry.rel)
                shutil.copy2(src, backup)
                if sha256_file(backup) != entry.old_sha:
                    raise PublishConflict(f"original mudou: {entry.rel}")
                self._checkpoint("after_backup", rel=entry.rel)
            if entry.action in ("create", "replace"):
                src = self.workspace / entry.rel
                generated = run_dir / "generated" / entry.rel
                generated.parent.mkdir(parents=True, exist_ok=True)
                self._checkpoint("before_generated", rel=entry.rel)
                shutil.copy2(src, generated)
                if sha256_file(generated) != entry.new_sha:
                    raise PublishError(f"artefacto gerado mudou: {entry.rel}")
                self._checkpoint("after_generated", rel=entry.rel)
            entries.append({"rel": entry.rel, "action": entry.action,
                            "old_sha": entry.old_sha, "new_sha": entry.new_sha,
                            "backup": str(backup) if backup else None,
                            "generated": str(generated) if generated else None,
                            "status": "pending", "error": ""})
        self._checkpoint("before_journal", run_dir=run_dir.name)
        journal = {"id": run_dir.name, "status": STATUS_PREPARED,
                   "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "manifest": plan.manifest.to_dict() if plan.manifest else None,
                   "entries": entries}
        self._write_journal(run_dir, journal)
        self._checkpoint("after_journal", run_dir=run_dir.name)
        journal["status"] = STATUS_PUBLISHING
        self._write_journal(run_dir, journal)
        self._checkpoint("after_publishing", run_dir=run_dir.name)
        try:
            for item in journal["entries"]:
                item["status"] = "applying"
                self._write_journal(run_dir, journal)
                self._checkpoint("before_apply", rel=item["rel"])
                self._apply(item)
                item["status"] = "done"
                self._write_journal(run_dir, journal)
                self._checkpoint("after_apply", rel=item["rel"])
            self._checkpoint("before_commit", run_dir=run_dir.name)
            journal["status"] = STATUS_COMMITTED
            self._write_journal(run_dir, journal)
            self._clear_marker()
            self._checkpoint("after_commit", run_dir=run_dir.name)
            return {"id": run_dir.name, "status": STATUS_COMMITTED,
                    "entries": len(entries), "path": str(run_dir)}
        except Exception as exc:
            journal["status"] = STATUS_ROLLING_BACK
            self._write_journal(run_dir, journal)
            self._checkpoint("before_rollback", run_dir=run_dir.name)
            ok = self._rollback(journal, run_dir)
            self._checkpoint("after_rollback", run_dir=run_dir.name, ok=ok)
            journal["status"] = STATUS_ROLLED_BACK if ok else STATUS_RECOVERY_REQUIRED
            journal["error"] = str(exc)[:500]
            self._write_journal(run_dir, journal)
            if not ok:
                self._mark_recovery(run_dir.name, str(exc)[:500])
            if isinstance(exc, PublishError):
                raise
            raise PublishError(str(exc)) from exc

    def _apply(self, item: dict[str, Any]) -> None:
        rel = item["rel"]
        dest = _safe_path(self.project, rel)
        current = sha256_file(dest) if dest.is_file() and not dest.is_symlink() else ""
        if item["action"] in ("replace", "delete"):
            if current != item["old_sha"]:
                raise PublishConflict(f"alteração concorrente: {rel}")
        elif item["action"] == "create" and current:
            raise PublishConflict(f"ficheiro criado entretanto: {rel}")
        if item["action"] == "delete":
            dest.unlink()
        else:
            src = Path(item["generated"])
            if not src.is_file() or sha256_file(src) != item["new_sha"]:
                raise PublishError(f"artefacto gerado em falta/divergente: {rel}")
            _atom_write_from(src, dest)
        new_current = sha256_file(dest) if dest.is_file() else ""
        expected = item["new_sha"] if item["action"] != "delete" else ""
        if new_current != expected:
            raise PublishError(f"hash pós-escrita divergente: {rel}")

    # -- recovery ----------------------------------------------------------- #

    def recover(self) -> Optional[dict[str, Any]]:
        with ProjectLock(self.lock_path):
            return self._recover_locked()

    def _recover_locked(self) -> Optional[dict[str, Any]]:
        if not self.root.is_dir():
            return None
        last: Optional[dict[str, Any]] = None
        for jpath in sorted(self.root.glob("*/journal.json"), reverse=True):
            run_dir = jpath.parent
            try:
                data = json.loads(jpath.read_text(encoding="utf-8"))
            except Exception:
                self._mark_recovery(run_dir.name, "journal inválido")
                raise RecoveryRequired(f"journal inválido: {jpath}; preservado")
            try:
                entries = self._validate_journal(run_dir, data)
            except PublishError as exc:
                self._mark_recovery(run_dir.name, str(exc))
                raise RecoveryRequired(str(exc)) from exc
            data["entries"] = entries
            status = str(data.get("status") or "").upper()
            if status == STATUS_RECOVERY_REQUIRED:
                self._mark_recovery(run_dir.name, data.get("error") or "pendente")
                raise RecoveryRequired(f"journal {run_dir.name} requer recuperação")
            if status == STATUS_REVALIDATION:
                return {"id": run_dir.name, "status": STATUS_REVALIDATION}
            if status not in (STATUS_PREPARED, STATUS_PUBLISHING, STATUS_ROLLING_BACK):
                last = {"id": run_dir.name, "status": status or "UNKNOWN"}
                continue
            data["status"] = STATUS_ROLLING_BACK
            self._write_journal(run_dir, data)
            self._checkpoint("before_recovery_rollback", run_dir=run_dir.name)
            ok = self._rollback(data, run_dir)
            self._checkpoint("after_recovery_rollback", run_dir=run_dir.name,
                             ok=ok)
            data["status"] = STATUS_ROLLED_BACK if ok else STATUS_RECOVERY_REQUIRED
            self._write_journal(run_dir, data)
            if not ok:
                self._mark_recovery(run_dir.name,
                                    data.get("error") or "rollback falhou")
                raise RecoveryRequired(f"rollback incompleto: {run_dir.name}")
            last = {"id": run_dir.name, "status": STATUS_ROLLED_BACK}
        return last

    def _rollback(self, journal: dict[str, Any], run_dir: Path) -> bool:
        ok = True
        for item in reversed(journal.get("entries", [])):
            rel = item["rel"]
            self._checkpoint("during_rollback", rel=rel)
            try:
                dest = _safe_path(self.project, rel)
                current = sha256_file(dest) if dest.is_file() and not dest.is_symlink() else ""
                action = item["action"]
                old = item.get("old_sha", "")
                new = item.get("new_sha", "")
                if action in ("replace", "delete"):
                    if current == old:
                        item["status"] = "rolled_back"
                        continue
                    if action == "replace" and current == new:
                        backup = Path(item["backup"]) if item.get("backup") else None
                        if backup and backup.is_file() and sha256_file(backup) == old:
                            _atom_write_from(backup, dest)
                            item["status"] = "rolled_back"
                            continue
                        item["status"] = "recovery_required"
                        item["error"] = "backup ausente/corrompido"
                        ok = False
                        continue
                    if action == "delete" and not current:
                        backup = Path(item["backup"]) if item.get("backup") else None
                        if backup and backup.is_file() and sha256_file(backup) == old:
                            _atom_write_from(backup, dest)
                            item["status"] = "rolled_back"
                            continue
                        item["status"] = "recovery_required"
                        item["error"] = "backup ausente/corrompido"
                        ok = False
                        continue
                    # third version: preserve human content and block
                    item["status"] = "recovery_required"
                    item["error"] = "versão humana/terceira detectada"
                    ok = False
                elif action == "create":
                    if not current:
                        item["status"] = "rolled_back"
                        continue
                    if current == new:
                        dest.unlink()
                        item["status"] = "rolled_back"
                        continue
                    item["status"] = "recovery_required"
                    item["error"] = "versão humana detectada"
                    ok = False
                else:
                    item["status"] = "rolled_back"
            except Exception as exc:
                item["status"] = "recovery_required"
                item["error"] = str(exc)[:300]
                ok = False
        return ok

    # -- orphan run dirs ---------------------------------------------------- #

    def _orphans_locked(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for child in sorted(self.root.iterdir()):
            if not child.is_dir() or child.is_symlink():
                continue
            if (child / "journal.json").is_file():
                continue
            files = 0
            size = 0
            for path in child.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    files += 1
                    try:
                        size += path.stat().st_size
                    except OSError:
                        pass
            out.append({"id": child.name, "path": str(child),
                        "files": files, "bytes": size})
        return out

    def orphans(self) -> list[dict[str, Any]]:
        """Run dirs without a journal: never applied, safe to inspect/clean."""
        with ProjectLock(self.lock_path):
            return self._orphans_locked()

    def clean_orphans(self, ids: Optional[Iterable[str]] = None,
                      *, dry_run: bool = False) -> dict[str, Any]:
        """Remove orphan run dirs only when explicitly named (or all if None)."""
        with ProjectLock(self.lock_path):
            orphans = {o["id"]: o for o in self._orphans_locked()}
            selected = sorted(orphans) if ids is None else [
                _safe_rel(i) for i in ids]
            missing = [i for i in selected if i not in orphans]
            removed: list[str] = []
            if not dry_run:
                for oid in selected:
                    path = (self.root / oid).resolve()
                    try:
                        path.relative_to(self.root.resolve())
                    except ValueError:
                        continue
                    shutil.rmtree(path)
                    removed.append(oid)
            return {"orphans": [orphans[i] for i in selected if i in orphans],
                    "removed": removed, "dry_run": bool(dry_run),
                    "missing": missing}

    # -- conflict resolution ------------------------------------------------ #

    def list_issues(self) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        for jpath in sorted(self.root.glob("*/journal.json"), reverse=True):
            try:
                data = json.loads(jpath.read_text(encoding="utf-8"))
            except Exception:
                issues.append({"journal": jpath.parent.name, "error": "invalid"})
                continue
            if str(data.get("status", "")).upper() in (STATUS_RECOVERY_REQUIRED,
                                                       STATUS_REVALIDATION):
                issues.append({"journal": data.get("id"),
                               "status": data.get("status"),
                               "entries": data.get("entries")})
        return {"issues": issues, "orphans": self._orphans_locked()}

    def resolve(self, journal_id: str, rel: str, choice: str,
                manual_file: Optional[str] = None) -> dict[str, Any]:
        with ProjectLock(self.lock_path):
            run_dir = self.root / _safe_rel(journal_id)
            jpath = self._journal(run_dir)
            if not jpath.is_file():
                raise PublishError(f"journal não encontrado: {journal_id}")
            try:
                data = json.loads(jpath.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RecoveryRequired(
                    f"journal inválido: {journal_id}; preservado") from exc
            entries = self._validate_journal(run_dir, data)
            data["entries"] = entries
            item = next((e for e in entries if e["rel"] == _safe_rel(rel)), None)
            if item is None:
                raise PublishError(f"entrada não encontrada: {rel}")
            dest = _safe_path(self.project, item["rel"])
            if choice == "human":
                item["choice"] = "human"
                item["status"] = "resolved_human"
            elif choice == "original":
                backup = Path(item["backup"]) if item.get("backup") else None
                if not backup or not backup.is_file():
                    raise RecoveryRequired("backup original ausente")
                _atom_write_from(backup, dest)
                item["choice"] = "original"
                item["status"] = "resolved_original"
            elif choice == "generated":
                generated = Path(item["generated"]) if item.get("generated") else None
                if not generated or not generated.is_file():
                    raise RecoveryRequired("artefacto gerado ausente")
                _atom_write_from(generated, dest)
                item["choice"] = "generated"
                item["status"] = "resolved_generated"
            elif choice == "manual":
                if not manual_file or not Path(manual_file).is_file():
                    raise PublishError("manual_file inexistente")
                _atom_write_from(Path(manual_file), dest)
                item["choice"] = "manual"
                item["status"] = "resolved_manual"
            else:
                raise PublishError(f"choice inválido: {choice}")
            unresolved = [e for e in entries
                          if e.get("status") == "recovery_required"]
            non_tested = [e for e in entries
                          if e.get("choice") in ("human", "original", "manual")]
            if unresolved:
                data["status"] = STATUS_RECOVERY_REQUIRED
                self._mark_recovery(run_dir.name, "entradas por resolver")
            elif non_tested:
                data["status"] = STATUS_REVALIDATION
                self._mark_recovery(run_dir.name, "revalidação necessária")
            else:
                data["status"] = STATUS_COMMITTED
                self._clear_marker()
            self._write_journal(run_dir, data)
            return {"journal": run_dir.name, "status": data["status"]}
