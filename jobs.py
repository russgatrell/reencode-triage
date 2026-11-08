"""Persistent job queue. One worker thread runs jobs one at a time; each job's state and log live in
SQLite, so the UI can be reloaded (or the browser can crash) and still show exactly where things are.

A server restart marks the job that was running as 'interrupted'; queued jobs carry on by themselves.
"""
import contextlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from pathlib import Path

import reencode_triage as rt

FINISHED = ("done", "error", "stopped", "cancelled", "interrupted")
WAKE = threading.Event()
CURRENT = {"id": None, "proc": None, "stop": False}


class LogBuffer:
    """File-like stdout sink for one job. A bare '\\r' rewrites the current line, so progress doesn't pile up.
    The text is copied into the jobs table at most once a second."""

    def __init__(self, job_id):
        self.job_id = job_id
        self.lines = [""]
        self.rewrite = False  # a '\r' was seen: the next text replaces the current line
        self.lock = threading.Lock()
        self.last_flush = 0.0

    def write(self, s):
        with self.lock:
            for part in re.split(r"(\r\n|\n|\r)", s):
                if part in ("\n", "\r\n"):
                    self.rewrite = False
                    self.lines.append("")
                elif part == "\r":
                    self.rewrite = True
                elif part:
                    if self.rewrite:
                        self.lines[-1] = ""
                        self.rewrite = False
                    self.lines[-1] += part
        self.flush_db()
        return len(s)

    def flush(self):
        pass

    def isatty(self):  # uvicorn/gradio probe sys.stdout while a job has it redirected
        return False

    def text(self):
        with self.lock:
            return "\n".join(self.lines[-500:])

    def last(self):
        with self.lock:
            for ln in reversed(self.lines):
                if ln.strip():
                    return ln.strip()
        return ""

    def flush_db(self, force=False):
        now = time.time()
        if not force and now - self.last_flush < 1.0:
            return
        self.last_flush = now
        try:
            with closing(rt.db()) as c:
                c.execute("UPDATE jobs SET log=?, progress=? WHERE id=?",
                          (self.text(), self.last()[:160], self.job_id))
                c.commit()
        except sqlite3.Error:
            pass  # a locked DB must never kill the job; the next flush catches up


# ---- runners (each takes the job's path and params dict; raising marks the job as failed) -------------

def run_scan(folder, p):
    rt.scan(Path(folder), int(p.get("workers", 4)))


def run_tune(path, p):
    # forget any earlier result so this file is tuned again from scratch
    with closing(rt.db()) as c:
        c.execute("DELETE FROM sample2 WHERE path=?", (path,))
        c.commit()
    rt.sample("hevc", 1, int(p["secs"]), 24, p["preset"], int(p["clips"]), rt.MIN_VMAF, True, p.get("max_width"),
              float(p["rel_drop"]), only=path)


def run_encode(path, p):
    crf = int(p.get("crf") or 0) or None
    plan = rt.plan_encode(path, "hevc", crf, p.get("preset") if crf else None, p.get("max_width"))
    # encode into a .part file and rename when finished, so output/ only ever holds complete re-encodes
    part = plan["dst"].with_name(plan["dst"].stem + ".part.mkv")
    cmd = plan["cmd"][:-1] + [str(part)]
    cmd = cmd[:2] + ["-loglevel", "warning", "-progress", "pipe:1", "-nostats"] + cmd[2:]
    print(f"Encoding {plan['src'].name}\ncrf {plan['crf']}, preset {plan['preset']}, "
          f"output {plan['w']}x{plan['h']}\n-> {plan['dst']}\n")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8",
                            errors="replace")
    CURRENT["proc"] = proc
    info = {}
    started = time.time()
    for line in proc.stdout:
        line = line.strip()
        if "=" not in line or " " in line.split("=")[0]:
            if line:
                print(line)  # ffmpeg warnings/errors
            continue
        k, v = line.split("=", 1)
        info[k] = v
        if k == "progress":
            try:
                done_s = int(info.get("out_time_us", "0")) / 1e6
            except ValueError:
                done_s = 0
            pct = min(100.0, 100 * done_s / plan["duration"])
            try:
                speed = float(info.get("speed", "0x").rstrip("x"))
            except ValueError:
                speed = 0
            eta = (plan["duration"] - done_s) / speed / 60 if speed > 0 else 0
            size_gb = int(info.get("total_size", "0") or 0) / 1024**3 if info.get("total_size", "N/A") != "N/A" else 0
            print(f"\r{pct:5.1f}%   {info.get('fps', '?')} fps   speed {info.get('speed', '?')}   "
                  f"{size_gb:.2f} GB so far   ETA {eta:.0f} min   (elapsed {(time.time() - started) / 60:.1f} min)",
                  end="", flush=True)
    proc.wait()
    CURRENT["proc"] = None
    if CURRENT["stop"]:
        part.unlink(missing_ok=True)
        print("\n\nStopped; partial output removed.")
        return
    if proc.returncode:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg exited with code {proc.returncode}")
    os.replace(part, plan["dst"])
    with closing(rt.db()) as c:  # real speed on this machine, used to calibrate time estimates
        c.execute("INSERT INTO encode_stats VALUES (?,?,?,?,?,?)",
                  (path, plan["preset"], plan["w"] * plan["h"] * plan["fps"] * plan["duration"] / 1e6,
                   time.time() - started, plan["crf"], time.time()))
        c.commit()
    print(f"\n\nDone: {plan['dst']}\nReview it, then approve it under 'Re-encodes awaiting approval'.")


def run_compare(path, p):
    rt.compare(path, bool(p.get("full_vmaf")))


def run_replace(path, p):
    rt.replace_original(path, bool(p.get("keep_backup")))


RUNNERS = {"scan": run_scan, "tune": run_tune, "encode": run_encode, "compare": run_compare,
           "replace": run_replace}


# ---- queue -----------------------------------------------------------------------------------------

def enqueue(kind, path, params=None):
    """Add a job; returns its id, or None if the same job is already queued/running."""
    with closing(rt.db()) as c:
        if c.execute("SELECT 1 FROM jobs WHERE kind=? AND path=? AND status IN ('queued','running')",
                     (kind, path)).fetchone():
            return None
        cur = c.execute("INSERT INTO jobs (kind, path, params, status, created) VALUES (?,?,?,?,?)",
                        (kind, path, json.dumps(params or {}), "queued", time.time()))
        c.commit()
    WAKE.set()
    return cur.lastrowid


def _finish(job_id, status):
    with closing(rt.db()) as c:
        c.execute("UPDATE jobs SET status=?, finished=? WHERE id=?", (status, time.time(), job_id))
        c.commit()


def _run(job_id, kind, path, params):
    log = LogBuffer(job_id)
    CURRENT.update(id=job_id, proc=None, stop=False)
    status = "done"
    with contextlib.redirect_stdout(log):
        try:
            RUNNERS[kind](path, params)
        except BaseException as e:  # find_cached/replace use sys.exit for user-facing errors
            print(f"\nERROR: {e}")
            status = "error"
    log.flush_db(force=True)
    if CURRENT["stop"]:
        status = "stopped"
    CURRENT.update(id=None, proc=None, stop=False)
    _finish(job_id, status)


def _loop():
    while True:
        with closing(rt.db()) as c:
            row = c.execute("SELECT id, kind, path, params FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if row:
                c.execute("UPDATE jobs SET status='running', started=? WHERE id=?", (time.time(), row[0]))
                c.commit()
        if not row:
            WAKE.wait(2)
            WAKE.clear()
            continue
        _run(row[0], row[1], row[2], json.loads(row[3] or "{}"))


def start_worker():
    with closing(rt.db()) as c:
        c.execute("UPDATE jobs SET status='interrupted', finished=?, progress='server stopped while this was running' "
                  "WHERE status='running'", (time.time(),))
        c.commit()
    threading.Thread(target=_loop, daemon=True, name="job-worker").start()


def cancel(job_id):
    """Cancel a queued job, or stop the running one if it is an encode."""
    with closing(rt.db()) as c:
        row = c.execute("SELECT status, kind FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return f"no job #{job_id}"
        status, kind = row
        if status == "queued":
            c.execute("UPDATE jobs SET status='cancelled', finished=? WHERE id=? AND status='queued'",
                      (time.time(), job_id))
            c.commit()
            return f"job #{job_id} cancelled"
    if status == "running" and CURRENT["id"] == job_id:
        p = CURRENT["proc"]
        if p and p.poll() is None:
            CURRENT["stop"] = True
            p.terminate()
            return f"job #{job_id}: stopping the encode"
        return f"job #{job_id} is a running {kind}; it can't be interrupted and will finish on its own"
    return f"job #{job_id} is already {status}"


def cancel_queued():
    with closing(rt.db()) as c:
        n = c.execute("UPDATE jobs SET status='cancelled', finished=? WHERE status='queued'",
                      (time.time(),)).rowcount
        c.commit()
    return n


def retry_failed():
    """Put interrupted/failed/stopped jobs back in the queue."""
    with closing(rt.db()) as c:
        n = c.execute("UPDATE jobs SET status='queued', started=NULL, finished=NULL, progress='', log='' "
                      "WHERE status IN ('interrupted','error','stopped')").rowcount
        c.commit()
    WAKE.set()
    return n


def clear_finished():
    with closing(rt.db()) as c:
        n = c.execute("DELETE FROM jobs WHERE status IN ('done','error','stopped','cancelled','interrupted')").rowcount
        c.commit()
    return n


# ---- views for the UI ------------------------------------------------------------------------------

def fmt_duration(s):
    return f"{s:.0f} s" if s < 90 else f"{s / 60:.0f} min" if s < 5400 else f"{s / 3600:.1f} h"


def _fmt_elapsed(started, finished):
    if not started:
        return ""
    s = (finished or time.time()) - started
    return f"{s:.0f} s" if s < 90 else f"{s / 60:.1f} min" if s < 5400 else f"{s / 3600:.1f} h"


def queue_rows(keep_finished=30):
    with closing(rt.db()) as c:
        jobs = c.execute("SELECT id, kind, path, status, started, finished, progress FROM jobs").fetchall()
    running = [j for j in jobs if j[3] == "running"]
    queued = sorted((j for j in jobs if j[3] == "queued"), key=lambda j: j[0])
    done = sorted((j for j in jobs if j[3] in FINISHED), key=lambda j: j[0], reverse=True)[:keep_finished]
    return [[j[0], j[1], j[2] if j[1] == "scan" else Path(j[2]).name, j[3], (j[6] or "")[:110],
             _fmt_elapsed(j[4], j[5])] for j in running + queued + done]


def get_log(job_id=0):
    """(id, text) for a chosen job, else the running one, else the most recently finished."""
    with closing(rt.db()) as c:
        if job_id:
            row = c.execute("SELECT id, kind, path, status, log FROM jobs WHERE id=?", (job_id,)).fetchone()
        else:
            row = (c.execute("SELECT id, kind, path, status, log FROM jobs WHERE status='running' LIMIT 1").fetchone()
                   or c.execute("SELECT id, kind, path, status, log FROM jobs WHERE status IN "
                                "('done','error','stopped','interrupted') ORDER BY finished DESC LIMIT 1").fetchone())
    if not row:
        return 0, "No jobs yet."
    return row[0], f"Job #{row[0]} - {row[1]} - {row[3]}: {Path(row[2]).name}\n\n{row[4] or ''}"


def status_line():
    with closing(rt.db()) as c:
        run = c.execute("SELECT id, kind, path, progress FROM jobs WHERE status='running' LIMIT 1").fetchone()
        n_queued = c.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
        rows_params = c.execute("SELECT params FROM jobs WHERE status='queued' AND kind='encode'").fetchall()
        n_rep, saved = c.execute("SELECT COUNT(*), COALESCE(SUM(old_bytes - new_bytes), 0) FROM replaced").fetchone()
    parts = []
    if run:
        parts.append(f"running #{run[0]} {run[1]}: {Path(run[2]).name[:50]} - {(run[3] or '')[:80]}")
    else:
        parts.append("idle")
    if n_queued:
        est = [json.loads(p or "{}").get("est_s") for (p,) in rows_params]
        known = [e for e in est if e]
        parts.append(f"{n_queued} queued" + (f" (encodes ~{fmt_duration(sum(known))}"
                                             + (", some unknown" if len(known) < len(est) else "") + ")"
                                             if known else ""))
    if n_rep:
        parts.append(f"reclaimed so far: {saved / 1024**3:.1f} GB from {n_rep} replaced file(s)")
    return " | ".join(parts)


def change_token():
    """Changes whenever a job finishes or the list is cleared; the UI refreshes its tables on a change."""
    with closing(rt.db()) as c:
        return c.execute("SELECT COALESCE(MAX(finished), 0), COUNT(*) FROM jobs WHERE status IN "
                         "('done','error','stopped','cancelled','interrupted')").fetchone()


def pending_reencodes():
    """Finished re-encodes in output/ paired with their originals: [saved GB, %, orig GB, new GB, original path]."""
    if not rt.OUTPUT_DIR.exists():
        return []
    outs = {p.name.lower(): p for p in rt.OUTPUT_DIR.glob("*.reencoded.mkv")}
    if not outs:
        return []
    rows = []
    with closing(rt.db()) as c:
        for (path,) in c.execute("SELECT path FROM probe"):
            out = outs.get((Path(path).stem + ".reencoded.mkv").lower())
            if out and os.path.exists(path):
                old, new = os.stat(path).st_size, out.stat().st_size
                rows.append([round((old - new) / 1024**3, 3), round(100 * (old - new) / old), round(old / 1024**3, 3),
                             round(new / 1024**3, 3), path])
    rows.sort(key=lambda r: r[0], reverse=True)
    return rows
