"""Small resident scheduler for Orchestra-next."""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

from orchestra import attention, child_runs, db, dsh, scheduler, supervise

log = logging.getLogger("orchestra.daemon")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging() -> None:
    """One line per notable event on stderr; launchd points stderr at logs/daemon.log."""
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stderr, force=True)

DEFAULT_INTERVAL = 1.0


def _alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def recover(con) -> list[int]:
    recovered = []
    for run in con.execute("SELECT * FROM runs WHERE status IN ('starting','running')"):
        if _alive(run["dsh_pid"]):
            continue
        with con:
            if run["strategy"] == "goal" and run["dsh_session_id"] and run["resume_count"] < 1:
                con.execute("UPDATE runs SET status='queued',resume_count=resume_count+1,dsh_pid=NULL,error='resident process disappeared',updated_at=? WHERE id=?", (db.now(), run["id"]))
                db.record_control(con, actor="orchestra", action="run.recover", outcome="queued", target_type="run", target_id=run["id"])
                recovered.append(int(run["id"]))
            else:
                attention.open_request(con, run["id"], kind="alert", prompt="Resident DSH process disappeared and cannot be replayed safely")
    return recovered


def tick(con=None, *, launcher=supervise.spawn_supervisor) -> dict:
    owned = con is None
    con = con or db.connect()
    try:
        children_resumed = child_runs.settle(con)
        recovered = recover(con)
        decision = scheduler.admit(con)
        launched = []
        for run_id in decision["admitted"]:
            launcher(run_id)
            launched.append(run_id)
        if launched or recovered or children_resumed:
            log.info("tick: launched=%s recovered=%s children_resumed=%s", launched, recovered, children_resumed)
        return {"recovered": recovered, "children_resumed": children_resumed, "launched": launched, **decision}
    finally:
        if owned:
            con.close()


def run(interval=DEFAULT_INTERVAL, *, once=False, preflight=True) -> int:
    configure_logging()
    if preflight:
        dsh.check_profile()
        dsh.cached_catalog(os.getcwd(), refresh=True)
    if once:
        tick()
        return 0
    from orchestra import http
    stop = threading.Event()
    server = threading.Thread(target=http.serve, args=(stop,), daemon=True)
    server.start()
    log.info("daemon started: pid=%s interval=%ss", os.getpid(), interval)
    try:
        while True:
            try:
                tick()
            except Exception:  # a bad tick must not kill the daemon
                log.exception("tick failed")
            time.sleep(max(0.1, interval))
    except KeyboardInterrupt:
        return 0
    finally:
        stop.set()
        server.join(timeout=2)
