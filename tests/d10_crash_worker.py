#!/usr/bin/env python3
"""D10 subprocess worker: real process termination at durable boundaries.

Usage: d10_crash_worker.py MODE KIND BASE_DIR
MODE: build-confirm | build-consume | integrate-fix | signoff-approve |
      signoff-reject | publication-pending
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_p7_pipeline_adapters import (FakeVerifier, PipelinePool, StateManager,
                                      _make_adapter, make_state, settings)
import engine


def responder(ref, messages):
    blob = "\n".join(str(getattr(m, "content", m)) for m in messages)
    if "handover memory" in blob:
        return json.dumps({"what_it_is": "demo", "how_to_run": ["python app.py"]})
    if "fix the runtime/integration defects" in blob:
        return ("### FILE: app.py\n```python\nprint('fixed')\n```\n")
    return "```python\nprint('ok')\n```"


def responder_integration_failure(ref, messages):
    blob = "\n".join(str(getattr(m, "content", m)) for m in messages)
    if "handover memory" in blob:
        return json.dumps({"what_it_is": "demo", "how_to_run": ["python app.py"]})
    if "fix the runtime/integration defects" in blob:
        return ("### FILE: app.py\n```python\nprint('fixed')\n```\n")
    return "```python\nraise RuntimeError('boom')\n```"


def make_run(kind, base, complexity="high", registry_mode="payg_first",
             reply=None):
    project = base / "project"
    project.mkdir(parents=True, exist_ok=True)
    ws = base / "ws"
    cfg = settings(base)
    cfg.max_verify_loops = 1
    st = make_state(project, ws, complexity)
    st.phase = "BUILD"
    pool = PipelinePool({}, registry_mode=registry_mode,
                        responder=reply or responder)
    adapter = _make_adapter(kind, cfg, pool, st, FakeVerifier())
    store = StateManager(cfg.workspace_dir, cfg.log_dir)
    store.save_project(st)
    eng = engine.WorkflowEngine(st, store, adapter, emit=lambda *a: None)
    eng.apply_pool(pool)
    return {"project": project, "ws": ws, "cfg": cfg, "state": st,
            "pool": pool, "adapter": adapter, "store": store, "engine": eng}


def drive(run, stop_phase=None):
    for _ in range(12):
        phase = run["state"].phase
        if stop_phase and phase == stop_phase:
            return
        result = run["engine"].step()
        if result in ("paused", "failed", "done"):
            return
    raise SystemExit(90)


def main() -> None:
    mode, kind, base_raw = sys.argv[1:4]
    base = Path(base_raw)
    if mode in ("build-confirm", "build-consume"):
        run = make_run(kind, base, complexity="high",
                       registry_mode="payg_first")
        if mode == "build-confirm":
            run["pool"].crash_after_confirm = True
            run["pool"].crash_after_confirm_exit = True
        else:
            run["engine"].call_ledger.mark_consumed = lambda call_id: os._exit(72)
        run["engine"].step()
        raise SystemExit(91)
    if mode == "integrate-fix":
        run = make_run(kind, base, complexity="medium", registry_mode="full",
                       reply=responder_integration_failure)
        for _ in range(10):
            if run["state"].phase == "INTEGRATE":
                if kind == "native":
                    run["adapter"].integration._revalidate = (
                        lambda *a, **k: os._exit(73))
                else:
                    run["adapter"].factory._revalidate_integration = (
                        lambda *a, **k: os._exit(73))
            result = run["engine"].step()
            if result in ("paused", "failed", "done"):
                raise SystemExit(92)
        raise SystemExit(93)
    if mode in ("signoff-approve", "signoff-reject", "publication-pending"):
        run = make_run(kind, base, complexity="medium", registry_mode="full")
        drive(run)
        if run["state"].phase != "SIGNOFF":
            raise SystemExit(94)
        if mode == "signoff-approve":
            run["engine"].approve_signoff("d10", "real process boundary")
            os._exit(74)
        if mode == "signoff-reject":
            run["engine"].reject_signoff("d10", "rejected in process")
            os._exit(75)
        run["engine"].approve_signoff("d10", "publication recovery")
        journal = run["project"] / ".crew" / "publish" / "d10-pending"
        journal.mkdir(parents=True, exist_ok=True)
        (journal / "journal.json").write_text("{invalid", encoding="utf-8")
        os._exit(76)
    raise SystemExit(99)


if __name__ == "__main__":
    main()
