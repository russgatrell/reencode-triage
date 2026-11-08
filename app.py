"""Gradio front end for reencode_triage: scan folders, tune files, re-encode them, approve the results.

Everything you click is queued (jobs.py) and runs one at a time in a background worker; the page just
shows the queue's state, so reloading the browser (or a browser crash) loses nothing.

Run:  .venv\\Scripts\\python.exe app.py      (or double-click run_ui.bat)
"""
import os
from pathlib import Path

import gradio as gr

import jobs
import reencode_triage as rt

HEADERS = ["#", "Saved GB", "% saved", "Size GB", "Codec", "Res", "Output", "bpp", "HDR",
           "Basis", "VMAF", "CRF", "Est. encode", "Path"]
QUEUE_HEADERS = ["#", "Job", "File", "Status", "Progress", "Elapsed"]
PENDING_HEADERS = ["Saved GB", "% saved", "Original GB", "Re-encode GB", "Path"]


def norm_mw(max_width):
    return int(max_width) if max_width and int(max_width) > 0 else None


def clean(folder):
    return (folder or "").strip().strip('"')


def as_list(selected):
    if not selected:
        return []
    return [selected] if isinstance(selected, str) else list(selected)


def est_label(r):
    """'48 min' when measured on this file's own tune clips, '~48 min' when it's the preset's machine average."""
    if not r["enc_s"]:
        return ""
    return ("" if r["enc_basis"] == "tuned" else "~") + jobs.fmt_duration(r["enc_s"])


def build_table(folder, min_pct, min_gb, max_width, preset):
    _, keep = rt.rows("hevc", float(min_pct), float(min_gb), 0, norm_mw(max_width), preset)
    out = [r for r in keep
           if (not folder or rt.under(r["path"], os.path.abspath(folder))) and os.path.exists(r["path"])]
    return [
        [i, round(r["saved_gb"], 3), round(r["pct_saved"]), round(r["size_gb"], 3), r["codec"], r["res"],
         r["out_res"], round(r["bpp"], 3), r["hdr"], r["basis"], r["vmaf"], r["crf"], est_label(r), r["path"]]
        for i, r in enumerate(out, 1)
    ]


def choices(rows):
    return gr.update(choices=[r[-1] for r in rows])


# ---- queueing actions ------------------------------------------------------------------------------

def report_queued(kind, ids, total):
    if total and not [i for i in ids if i]:
        gr.Warning(f"Nothing queued: those {kind} jobs are already in the queue.")
    elif total:
        skipped = total - len(ids)
        gr.Info(f"Queued {len(ids)} {kind} job(s)" + (f" ({skipped} already queued)" if skipped else ""))
    return jobs.queue_rows()


def q_scan(folder, workers):
    folder = clean(folder)
    if not folder or not Path(folder).exists():
        gr.Warning(f"Folder not found: {folder!r}")
        return jobs.queue_rows()
    return report_queued("scan", [jobs.enqueue("scan", folder, {"workers": int(workers)})], 1)


def need_files(selected):
    files = as_list(selected)
    if not files:
        gr.Warning("Select one or more files first (click table rows).")
    return files


def q_tune(selected, max_width, preset, clips, secs, rel_drop):
    files = need_files(selected)
    params = {"preset": preset, "clips": int(clips), "secs": int(secs), "rel_drop": float(rel_drop),
              "max_width": norm_mw(max_width)}
    return report_queued("tune", [jobs.enqueue("tune", f, params) for f in files], len(files))


def q_encode(selected, max_width, preset, crf_override):
    files = need_files(selected)
    crf = int(crf_override or 0)
    ids = []
    for f in files:  # est_s feeds the queue's total-time estimate in the status line
        params = {"preset": preset, "crf": crf, "max_width": norm_mw(max_width),
                  "est_s": rt.estimate_for(f, preset, use_preset=bool(crf), max_width=norm_mw(max_width))}
        ids.append(jobs.enqueue("encode", f, params))
    return report_queued("encode", ids, len(files))


def q_compare(selected, full_vmaf):
    files = need_files(selected)
    return report_queued("compare", [jobs.enqueue("compare", f, {"full_vmaf": bool(full_vmaf)}) for f in files],
                         len(files))


def q_replace(approved, keep_backup, confirm):
    files = as_list(approved)
    if not files:
        gr.Warning("Select the re-encodes to approve first.")
    elif not confirm:
        gr.Warning("Tick the confirmation box first: this replaces the original files.")
    else:
        return report_queued("replace", [jobs.enqueue("replace", f, {"keep_backup": bool(keep_backup)})
                                         for f in files], len(files))
    return jobs.queue_rows()


def do_refresh(folder, min_pct, min_gb, max_width, preset):
    folder = clean(folder)
    if folder and Path(folder).exists():
        n = rt.prune_missing(Path(folder))
        if n:
            gr.Info(f"Removed {n} stale entries (files no longer on disk)")
    rows = build_table(folder, min_pct, min_gb, max_width, preset)
    pend = jobs.pending_reencodes()
    return rows, choices(rows), pend, choices(pend)


def do_cancel_job(job_id):
    gr.Info(jobs.cancel(int(job_id or 0)))
    return jobs.queue_rows()


def do_stop():
    cur = jobs.CURRENT["id"]
    gr.Info(jobs.cancel(cur) if cur else "nothing is running")
    return jobs.queue_rows()


def do_clear_queued():
    gr.Info(f"Cancelled {jobs.cancel_queued()} queued job(s)")
    return jobs.queue_rows()


def do_retry():
    gr.Info(f"Re-queued {jobs.retry_failed()} interrupted/failed job(s)")
    return jobs.queue_rows()


def do_clear_finished():
    gr.Info(f"Removed {jobs.clear_finished()} finished job(s) from the list")
    return jobs.queue_rows()


# ---- selection -------------------------------------------------------------------------------------

def toggle(current, evt: gr.SelectData):
    """Clicking a row adds its file (the last column) to the selection, or removes it if already there."""
    row = evt.row_value
    if not row:
        return current
    path = row[-1]
    cur = as_list(current)
    return [p for p in cur if p != path] if path in cur else cur + [path]


def select_all(folder, min_pct, min_gb, max_width, preset):
    return [r[-1] for r in build_table(clean(folder), min_pct, min_gb, max_width, preset)]


def pick_job(evt: gr.SelectData):
    row = evt.row_value
    return int(row[0]) if row else 0


# ---- live view (page load + timer) -----------------------------------------------------------------

def view(view_id, folder, min_pct, min_gb, max_width, preset, st):
    """Everything on screen that comes from the job queue. Parts that haven't changed are skipped so
    the log keeps its scroll position and the tables keep their selection."""
    st = dict(st or {})

    def fresh(key, sig):
        if st.get(key) == sig:
            return False
        st[key] = sig
        return True

    qrows = jobs.queue_rows()
    q_out = qrows if fresh("q", repr(qrows)) else gr.skip()
    jid, log_text = jobs.get_log(int(view_id or 0))
    log_out = log_text if fresh("log", (jid, log_text)) else gr.skip()
    status_out = jobs.status_line()
    cand_out = cand_choices = pend_out = pend_choices = gr.skip()
    if fresh("tok", jobs.change_token()):  # a job finished (or first load): rebuild the tables
        rows = build_table(clean(folder), min_pct, min_gb, max_width, preset)
        pend = jobs.pending_reencodes()
        cand_out, cand_choices, pend_out, pend_choices = rows, choices(rows), pend, choices(pend)
    return q_out, log_out, status_out, cand_out, cand_choices, pend_out, pend_choices, st


# ---- layout ----------------------------------------------------------------------------------------

with gr.Blocks(title="Re-encode triage") as demo:
    gr.Markdown("## Re-encode triage\nScan a folder, pick files, tune their CRF on short clips, re-encode them "
                "into `output/`, then approve the ones you like to replace the originals. Jobs queue up and run "
                "in the background, one at a time; you can close or reload this page at any time.")
    with gr.Row():
        folder = gr.Textbox(label="Folder to scan", value=r"Z:\Movie", scale=4)
        scan_btn = gr.Button("Queue scan", variant="primary", scale=1)
        refresh_btn = gr.Button("Refresh tables", scale=1)
    with gr.Accordion("Settings", open=True):
        with gr.Row():
            min_pct = gr.Slider(0, 80, value=10, step=1, label="Show files saving at least (%)")
            min_gb = gr.Slider(0, 5, value=0, step=0.05, label="...and at least (GB)")
            max_width = gr.Number(value=0, precision=0, label="Downscale wider than (px, 0 = off; 1920 = 4K→1080p)")
            workers = gr.Slider(1, 12, value=4, step=1, label="Scan threads")
        with gr.Row():
            preset = gr.Dropdown(["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"],
                                 value="fast", label="x265 preset (tune + encode)")
            clips = gr.Slider(1, 6, value=3, step=1, label="Test clips per file")
            secs = gr.Slider(5, 30, value=10, step=1, label="Seconds per clip")
            rel_drop = gr.Slider(1, 8, value=3.0, step=0.5, label="Allowed VMAF drop vs best case")
    table = gr.Dataframe(headers=HEADERS, interactive=False, wrap=False, max_height=360,
                         label="Candidates (click rows to select; sorted by estimated GB saved)")
    with gr.Row():
        selected = gr.Dropdown(label="Selected files (click table rows to add/remove, or pick here)",
                               choices=[], multiselect=True, allow_custom_value=True, scale=6)
        all_btn = gr.Button("Select all shown", scale=1)
        none_btn = gr.Button("Clear selection", scale=1)
    with gr.Row():
        tune_btn = gr.Button("1. Queue tune")
        crf_override = gr.Number(value=0, precision=0, label="CRF override (0 = use tuned)")
        encode_btn = gr.Button("2. Queue re-encode → output/")
        full_vmaf = gr.Checkbox(label="Full-film VMAF (slow)", value=False)
        compare_btn = gr.Button("3. Queue compare")

    gr.Markdown("### Re-encodes awaiting approval")
    pending_table = gr.Dataframe(headers=PENDING_HEADERS, interactive=False, wrap=False, max_height=240,
                                 label="Finished re-encodes in output/ whose original is still in place "
                                       "(click rows to select)")
    approve = gr.Dropdown(label="Approved (replace the original with its re-encode)", choices=[],
                          multiselect=True, allow_custom_value=True)
    with gr.Row():
        keep_backup = gr.Checkbox(label="Keep the original as .bak instead of deleting it", value=False)
        confirm = gr.Checkbox(label="I've reviewed these re-encodes; replace the originals", value=False)
        replace_btn = gr.Button("4. Queue replace originals", variant="stop")
    gr.Markdown("Replacing checks the re-encode first (readable, same duration and track counts, smaller), copies "
                "it next to the original under a temp name, and only then removes the original. The result is "
                "`<original name>.mkv` in the original's folder.")

    gr.Markdown("### Job queue")
    status = gr.Textbox(label="Status", interactive=False)
    queue_table = gr.Dataframe(headers=QUEUE_HEADERS, interactive=False, wrap=False, max_height=260,
                               label="Jobs (click a row to show its log and pick it for Cancel)")
    with gr.Row():
        view_id = gr.Number(value=0, precision=0, label="Log of job # (0 = running / latest)")
        cancel_btn = gr.Button("Cancel job #")
        stop_btn = gr.Button("Stop running encode", variant="stop")
        clear_q_btn = gr.Button("Cancel all queued")
        retry_btn = gr.Button("Retry interrupted/failed")
        clear_f_btn = gr.Button("Clear finished list")
    log = gr.Textbox(label="Log", lines=16, max_lines=16, autoscroll=True, interactive=False)

    state = gr.State({})
    common = [folder, min_pct, min_gb, max_width, preset]
    view_in = [view_id] + common + [state]
    view_out = [queue_table, log, status, table, selected, pending_table, approve, state]

    scan_btn.click(q_scan, [folder, workers], queue_table)
    refresh_btn.click(do_refresh, common, [table, selected, pending_table, approve])
    table.select(toggle, selected, selected)
    pending_table.select(toggle, approve, approve)
    all_btn.click(select_all, common, selected)
    none_btn.click(lambda: [], None, selected)
    tune_btn.click(q_tune, [selected, max_width, preset, clips, secs, rel_drop], queue_table)
    encode_btn.click(q_encode, [selected, max_width, preset, crf_override], queue_table)
    compare_btn.click(q_compare, [selected, full_vmaf], queue_table)
    replace_btn.click(q_replace, [approve, keep_backup, confirm], queue_table)
    queue_table.select(pick_job, None, view_id)
    cancel_btn.click(do_cancel_job, view_id, queue_table)
    stop_btn.click(do_stop, None, queue_table)
    clear_q_btn.click(do_clear_queued, None, queue_table)
    retry_btn.click(do_retry, None, queue_table)
    clear_f_btn.click(do_clear_finished, None, queue_table)

    demo.load(view, view_in, view_out)
    gr.Timer(2).tick(view, view_in, view_out)

if __name__ == "__main__":
    # no fixed port: Gradio takes 7860 or the next free one, so several local apps can coexist (set PORT to pin one)
    port = int(os.environ["PORT"]) if os.environ.get("PORT") else None
    demo.queue(default_concurrency_limit=None).launch(
        server_name="127.0.0.1", server_port=port,
        inbrowser=not os.environ.get("NO_BROWSER"), prevent_thread_lock=True)
    jobs.start_worker()  # after the server is up: a resumed job redirects stdout, which would break startup
    demo.block_thread()
