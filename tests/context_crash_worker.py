"""Real process termination at managed request durable boundaries."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_p72c3_core import make_run, activate, prepare
from calls import CallLedger


def main():
    base, boundary = sys.argv[1:]
    run = make_run(base)
    activate(run)
    _, messages = prepare(run)
    if boundary == "prepared":
        os._exit(73)
    run.pool.real_exit = True
    if boundary == "result":
        original = CallLedger._write_result
        def persisted_result(ledger, *args, **kwargs):
            original(ledger, *args, **kwargs)
            os._exit(73)
        CallLedger._write_result = persisted_result
    if boundary == "submitted":
        run.route.candidates[0].handle = run.pool.handle("crash")
    elif boundary in ("confirmed", "attempt"):
        run.pool.crash = boundary
    run.core.invoke(run.pool, run.route, "build-t-1", messages)
    if boundary == "applied":
        CallLedger.mark_consumed = lambda *a: os._exit(73)
        run.core.commit("build-t-1", lambda state, result: state.integration.update(text=result.text))
    raise SystemExit("boundary not reached")


if __name__ == "__main__":
    main()
