#!/usr/bin/env python3
"""D12 subprocess publisher-crash worker.

Usage: d12_crash_worker.py MODE KIND BASE_DIR
MODE: before-journal | after-journal | after-publishing | after-backup |
      partial-apply | before-commit | after-commit | rollback |
      rollback-impossible | normal
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_p7_pipeline_adapters import D11PublicationPipelineTests


OLD_A = "old-a\n"
OLD_B = "old-b\n"
NEW_A = "new-a\n"
NEW_B = "new-b\n"


def fault_for(mode):
    def fault(name, info):
        rel = info.get("rel")
        if mode == "before-journal" and name == "before_journal":
            os._exit(81)
        if mode == "after-journal" and name == "after_journal":
            os._exit(82)
        if mode == "after-publishing" and name == "after_publishing":
            os._exit(83)
        if mode == "after-backup" and name == "after_backup" \
                and rel == "app.py":
            os._exit(84)
        if mode == "partial-apply" and name == "after_apply" \
                and rel == "app.py":
            os._exit(85)
        if mode == "before-commit" and name == "before_commit":
            os._exit(86)
        if mode == "after-commit" and name == "after_commit":
            os._exit(87)
        if mode in ("rollback", "rollback-impossible"):
            if name == "before_apply" and rel == "util.py":
                raise RuntimeError("induce rollback")
            if name == "during_rollback" and rel == "app.py":
                os._exit(88 if mode == "rollback" else 89)
    return fault


def main() -> None:
    mode, kind, base_raw = sys.argv[1:4]
    base = Path(base_raw)
    case = D11PublicationPipelineTests("runTest")
    run = case._base(files={"app.py": OLD_A, "util.py": OLD_B}, base=base)
    eng = case._approved(
        kind, run, content=NEW_A,
        workspace_files={"app.py": NEW_A, "util.py": NEW_B})
    if mode != "normal":
        eng["adapter"].publication_fault = fault_for(mode)
    result = eng["engine"].step()
    if mode == "normal":
        if result != "ok":
            raise SystemExit(90)
        os._exit(0)
    raise SystemExit(91)


if __name__ == "__main__":
    main()
