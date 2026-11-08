"""Find video files where re-encoding would reclaim the most disk space.

Ranking is by estimated *absolute* bytes saved (not percent), with a minimum
percent-saved floor so marginal re-encodes are filtered out.

Usage:
  python reencode_triage.py scan   Z:\\            # probe files (cached), write report
  python reencode_triage.py report                  # re-rank from cache, no probing
  python reencode_triage.py sample --top 15         # real test-encode of top candidates
                                                    # to replace heuristics with measurements
"""
import argparse
import csv
import json
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
# REENCODE_DB / REENCODE_OUTPUT let tests run against a scratch cache and output folder
DB_PATH = Path(os.environ.get("REENCODE_DB", HERE / "probe_cache.sqlite"))
REPORT_CSV = HERE / "reencode_report.csv"

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts", ".m2ts", ".wmv", ".mpg", ".mpeg", ".flv"}

# --- Estimation model (tweak these) -----------------------------------------
# Target bits-per-pixel-per-frame for HEVC at roughly "visually fine" quality
# (about x265 CRF 23-26). Smaller frames need more bits per pixel.
# (max_megapixels, bpp)
HEVC_BPP = [(0.45, 0.070), (1.0, 0.055), (2.2, 0.040), (9.0, 0.028), (99, 0.022)]
AV1_FACTOR = 0.80          # AV1 target = HEVC target * this
# Codecs that are already efficient: re-encoding saves less and costs more quality.
EFFICIENT = {"hevc", "av1", "vp9"}
# For efficient sources, a re-encode rarely beats ~35% reduction even if bitrate is high.
EFFICIENT_MAX_REDUCTION = 0.35
DOWNSCALE_MAX_REDUCTION = 0.88  # cap when the encode also reduces resolution (e.g. 4K -> 1080p)
MAX_REDUCTION = 0.75       # cap on heuristic savings for older codecs (h264, mpeg2, ...)
# Hard filters for the report
MIN_PCT_SAVED = 25.0
MIN_GB_SAVED = 0.20
# Optional absolute VMAF floor (0 = off). Grainy/dark sources score low even when nearly transparent,
# so the default judges each file against its own best-quality ceiling instead (see REL_DROP).
# If set: measured files whose test-encode scores below this VMAF are dropped from candidates.
# ~95+ is generally indistinguishable, 90-93 is "slightly soft if you look". HDR/DV files
# are exempt from the floor because VMAF is trained on SDR content.
MIN_VMAF = 0.0  # absolute floor is off by default; the sampler judges quality relative to each file's own ceiling
# -----------------------------------------------------------------------------


_schema_ready = set()


def db():
    # the UI's worker thread, its status polling and the CLI all share this file, so wait out brief locks
    con = sqlite3.connect(DB_PATH, timeout=30)
    if str(DB_PATH) in _schema_ready:
        return con
    try:
        con.execute("PRAGMA journal_mode=WAL")  # readers (UI polling) don't block the worker's writes
    except sqlite3.OperationalError:
        pass  # another process holds the file; stay in the current mode
    con.execute(
        "CREATE TABLE IF NOT EXISTS probe (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, info TEXT)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS sample2 (path TEXT PRIMARY KEY, size INTEGER, ratio REAL, "
        "vmaf REAL, vmaf_min REAL, note TEXT)"
    )
    for col in ("crf INTEGER", "max_width INTEGER", "enc_mpix_s REAL"):
        try:
            con.execute(f"ALTER TABLE sample2 ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass  # column already exists
    # UI job queue (see jobs.py) and the log of originals swapped for their re-encodes
    con.execute(
        "CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, path TEXT, params TEXT, "
        "status TEXT, created REAL, started REAL, finished REAL, progress TEXT, log TEXT)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS replaced (path TEXT, final_path TEXT, old_bytes INTEGER, new_bytes INTEGER, at REAL)"
    )
    # one row per finished full encode: megapixels pushed through x265 and wall seconds (calibrates time estimates)
    con.execute(
        "CREATE TABLE IF NOT EXISTS encode_stats (path TEXT, preset TEXT, mpix REAL, seconds REAL, crf INTEGER, at REAL)"
    )
    con.commit()
    _schema_ready.add(str(DB_PATH))
    return con


def run_probe(path: Path):
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=120)
        return json.loads(r.stdout) if r.returncode == 0 else None
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return None


def num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def summarize(data, size):
    """Reduce raw ffprobe output to the fields the estimator needs."""
    streams = data.get("streams", [])
    vids = [s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    if not vids:
        return None
    v = vids[0]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    fmt = data.get("format", {})
    duration = num(fmt.get("duration")) or num(v.get("duration"))
    if duration <= 0:
        return None

    def fps_of(s):
        for key in ("avg_frame_rate", "r_frame_rate"):
            try:
                n, d = map(float, s.get(key, "0/1").split("/"))
                if d and n / d > 1:
                    return n / d
            except ValueError:
                pass
        return 24.0

    def stream_bytes(s):
        """Best-effort bytes for one stream: explicit bitrate, mkv stats tags, else None."""
        tags = {k.upper(): val for k, val in s.get("tags", {}).items()}
        if "NUMBER_OF_BYTES" in tags:
            return num(tags["NUMBER_OF_BYTES"])
        br = num(s.get("bit_rate"))
        if br:
            return br * duration / 8
        if "BPS" in tags:
            return num(tags["BPS"]) * duration / 8
        return None

    audio_bytes, audio_known = 0.0, True
    for a in audio:
        b = stream_bytes(a)
        if b is None:
            b = 192_000 * duration / 8  # assume 192 kbps when unknown
            audio_known = False
        audio_bytes += b

    vbytes = stream_bytes(v)
    est_method = "stream"
    if vbytes is None:
        # container overhead ~1%; everything not audio is video (subs are tiny)
        vbytes = max(size * 0.99 - audio_bytes, size * 0.3)
        est_method = "size-minus-audio"

    bits = num(v.get("bits_per_raw_sample")) or (10 if "10" in v.get("pix_fmt", "") else 8)
    trc = v.get("color_transfer", "")
    hdr = {"smpte2084": "HDR10", "arib-std-b67": "HLG"}.get(trc, "")
    if any("dovi" in str(sd.get("side_data_type", "")).lower() or "dolby vision" in str(sd.get("side_data_type", "")).lower()
           for sd in v.get("side_data_list", [])):
        hdr = "DV" if not hdr else hdr + "+DV"
    return {
        "hdr": hdr,
        "color": {k: v.get(k, "") for k in ("color_primaries", "color_transfer", "color_space")},
        "codec": v.get("codec_name", "unknown"),
        "width": int(v.get("width", 0)),
        "height": int(v.get("height", 0)),
        "fps": round(fps_of(v), 3),
        "bit_depth": int(bits),
        "interlaced": v.get("field_order", "progressive") not in ("progressive", "unknown", ""),
        "duration": duration,
        "video_bytes": vbytes,
        "audio_bytes": audio_bytes,
        "audio_codecs": ",".join(sorted({a.get("codec_name", "?") for a in audio})),
        "vbytes_method": est_method,
        "audio_known": audio_known,
    }


def target_bpp(width, height):
    mp = width * height / 1e6
    for limit, bpp in HEVC_BPP:
        if mp <= limit:
            return bpp
    return HEVC_BPP[-1][1]


def out_dims(info, max_width=None):
    """Output (w, h, scaled?) after an optional width cap (aspect kept, even dimensions)."""
    w, h = info["width"], info["height"]
    if max_width and w > max_width:
        return max_width, int(round(h * max_width / w / 2) * 2), True
    return w, h, False


def estimate(info, target="hevc", measured_ratio=None, max_width=None):
    """Return (new_video_bytes, saved_bytes). measured_ratio overrides the heuristic."""
    cur = info["video_bytes"]
    if measured_ratio is not None:
        new = cur * measured_ratio
    else:
        w, h, scaled = out_dims(info, max_width)
        bpp = target_bpp(w, h) * (AV1_FACTOR if target == "av1" else 1.0)
        want = w * h * info["fps"] * bpp * info["duration"] / 8
        new = min(cur, want)
        max_reduction = EFFICIENT_MAX_REDUCTION if info["codec"] in EFFICIENT else MAX_REDUCTION
        if scaled:
            max_reduction = DOWNSCALE_MAX_REDUCTION
        new = max(new, cur * (1 - max_reduction))
    return new, max(0.0, cur - new)


def scan(root: Path, workers: int):
    con = db()
    # rows probed before HDR detection existed lack the "hdr" key and get re-probed
    cached = {r[0]: (r[1], r[2]) for r in con.execute("SELECT path, size, mtime FROM probe WHERE info LIKE '%\"hdr\"%'")}
    files = []
    for p in root.rglob("*"):
        if p.suffix.lower() in VIDEO_EXTS and p.is_file():
            st = p.stat()
            if cached.get(str(p)) != (st.st_size, st.st_mtime):
                files.append((p, st.st_size, st.st_mtime))
    print(f"{len(files)} new/changed files to probe under {root}")

    def work(item):
        p, size, mtime = item
        data = run_probe(p)
        info = summarize(data, size) if data else None
        return p, size, mtime, info

    done = 0
    with ThreadPoolExecutor(workers) as ex:
        for p, size, mtime, info in ex.map(work, files):
            con.execute(
                "INSERT OR REPLACE INTO probe VALUES (?,?,?,?)",
                (str(p), size, mtime, json.dumps(info) if info else None),
            )
            done += 1
            if done % 25 == 0:
                con.commit()
            print(f"  probed {done}/{len(files)}: {p.name[:60]}", end="\r", flush=True)
    con.commit()
    print()
    prune_missing(root)


def under(path, root):
    """True if path is inside root (separator-aware, case-insensitive on Windows)."""
    r = os.path.normcase(str(root)).rstrip("\\/") + os.sep
    return os.path.normcase(str(path)).startswith(r)


def prune_missing(root=None):
    """Forget cached probes/samples for files that no longer exist (re-encoded to another extension,
    deleted, moved). Skipped when everything under root looks missing, which usually means the share
    is unmounted rather than emptied."""
    con = db()
    paths = [p for (p,) in con.execute("SELECT path FROM probe") if root is None or under(p, root)]
    gone = [p for p in paths if not os.path.exists(p)]
    if not gone:
        return 0
    if len(gone) == len(paths) and len(paths) > 3:
        print(f"All {len(paths)} cached files under {root or 'the cache'} look missing; "
              f"not pruning (is the drive mounted?)")
        return 0
    con.executemany("DELETE FROM probe WHERE path=?", [(p,) for p in gone])
    con.executemany("DELETE FROM sample2 WHERE path=?", [(p,) for p in gone])
    con.commit()
    print(f"Removed {len(gone)} stale entries (files no longer on disk)")
    return len(gone)


def probe_into_cache(p: Path):
    """Probe one file and store it in the cache (used after a file is replaced)."""
    st = p.stat()
    data = run_probe(p)
    info = summarize(data, st.st_size) if data else None
    con = db()
    con.execute("INSERT OR REPLACE INTO probe VALUES (?,?,?,?)",
                (str(p), st.st_size, st.st_mtime, json.dumps(info) if info else None))
    con.commit()


MEAS_COLS = "ratio, vmaf, vmaf_min, crf, max_width, enc_mpix_s, note"


def throughputs(con):
    """{preset: (Mpixels/s, source)} for x265 on this machine: real finished encodes if there are any
    at that preset, otherwise the median speed of the tune clips."""
    tp = {}
    for preset, mp, secs in con.execute("SELECT preset, SUM(mpix), SUM(seconds) FROM encode_stats GROUP BY preset"):
        if secs and secs > 0:
            tp[preset] = (mp / secs, "history")
    speeds = {}
    for note, v in con.execute("SELECT note, enc_mpix_s FROM sample2 WHERE enc_mpix_s IS NOT NULL"):
        speeds.setdefault((note or "").split("/")[0], []).append(v)
    for preset, vs in speeds.items():
        tp.setdefault(preset, (statistics.median(vs), "samples"))
    return tp


def encode_seconds(info, m, max_width, preset, tp):
    """(seconds, basis) for a full re-encode. A tuned file uses its own measured clip speed ("tuned");
    otherwise the machine's average for the preset ("history" / "samples"); else (None, "")."""
    ow, oh, _ = out_dims(info, m[4] if m else max_width)
    mpix = ow * oh * info["fps"] * info["duration"] / 1e6
    if m and m[5]:
        return mpix / m[5], "tuned"
    got = tp.get(preset)
    return (mpix / got[0], got[1]) if got else (None, "")


def estimate_for(path, preset, use_preset=False, max_width=None):
    """Estimated encode seconds for one cached file. The tuned preset wins unless use_preset (a CRF override)."""
    con = db()
    row = con.execute("SELECT info FROM probe WHERE path=?", (path,)).fetchone()
    if not row or not row[0]:
        return None
    m = con.execute(f"SELECT {MEAS_COLS} FROM sample2 WHERE path=?", (path,)).fetchone()
    if m and m[6] and not use_preset:
        preset = m[6].split("/")[0]
    return encode_seconds(json.loads(row[0]), m, max_width, preset, throughputs(con))[0]


def rows(target, min_pct, min_gb, min_vmaf=MIN_VMAF, max_width=None, preset=None):
    """preset: x265 preset assumed for time estimates of files that haven't been tuned."""
    con = db()
    meas = {r[0]: r[1:] for r in con.execute(f"SELECT path, {MEAS_COLS} FROM sample2")}
    tp = throughputs(con)
    out = []
    for path, size, info_json in con.execute("SELECT path, size, info FROM probe"):
        if not info_json:
            continue
        info = json.loads(info_json)
        m = meas.get(path)
        new, saved = estimate(info, target, m[0] if m else None, max_width)
        enc_s, enc_basis = encode_seconds(info, m, max_width, (m[6].split("/")[0] if m and m[6] else None) or preset,
                                          tp)
        cur = info["video_bytes"]
        pct = 100 * saved / cur if cur else 0
        out.append(
            {
                "saved_gb": saved / 1024**3,
                "pct_saved": pct,
                "size_gb": size / 1024**3,
                "codec": info["codec"],
                "res": f"{info['width']}x{info['height']}",
                "out_res": "{}x{}".format(*(out_dims(info, m[4] if m else max_width)[:2])),
                "fps": info["fps"],
                "video_mbps": cur * 8 / info["duration"] / 1e6,
                "bpp": cur * 8 / info["duration"] / max(1, info["width"] * info["height"] * info["fps"]),
                "bit_depth": info["bit_depth"],
                "hdr": info.get("hdr", ""),
                "interlaced": info["interlaced"],
                "audio": info["audio_codecs"],
                "basis": "measured" if m else "heuristic",
                "vmaf": round(m[1], 1) if m and m[1] is not None else "",
                "vmaf_min": round(m[2], 1) if m and m[2] is not None else "",
                "crf": m[3] if m and m[3] is not None else "",
                "bytes_basis": info["vbytes_method"],
                "enc_s": enc_s,
                "enc_basis": enc_basis,
                "path": path,
            }
        )
    out.sort(key=lambda r: r["saved_gb"], reverse=True)

    def ok(r):
        if r["pct_saved"] < min_pct or r["saved_gb"] < min_gb:
            return False
        # quality floor applies to measured SDR files; HDR/DV scores aren't trustworthy
        return not (r["vmaf"] != "" and not r["hdr"] and r["vmaf"] < min_vmaf)

    return out, [r for r in out if ok(r)]


def report(target, min_pct, min_gb, top, min_vmaf=MIN_VMAF, max_width=None):
    allrows, keep = rows(target, min_pct, min_gb, min_vmaf, max_width)
    if not allrows:
        print("Nothing in cache. Run: scan <folder>")
        return
    with open(REPORT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(allrows[0].keys()))
        w.writeheader()
        for r in keep:
            w.writerow({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})
    total = sum(r["size_gb"] for r in allrows)
    print(f"{len(allrows)} videos, {total:.1f} GB total. {len(keep)} candidates "
          f"(>= {min_pct:.0f}% and >= {min_gb} GB saved, VMAF >= {min_vmaf:.0f} where measured) -> {REPORT_CSV}")
    print(f"Estimated total reclaimable: {sum(r['saved_gb'] for r in keep):.1f} GB (target={target})")
    hdr_n = sum(1 for r in keep if r["hdr"])
    if hdr_n:
        print(f"NOTE: {hdr_n} candidates are HDR/Dolby Vision. A plain re-encode drops Dolby Vision "
              f"metadata and VMAF is unreliable for HDR; review those by eye.")
    print(f"\n{'#':>3} {'Saved GB':>8} {'%':>4} {'Size GB':>8} {'Codec':<6} {'Res':<10} {'Mbps':>6} {'bpp':>6} "
          f"{'HDR':<7} {'Basis':<9} {'VMAF':>5} {'CRF':>3} {'Output':<10} File")
    cum = 0.0
    for i, r in enumerate(keep[:top], 1):
        cum += r["saved_gb"]
        name = Path(r["path"]).name
        print(f"{i:>3} {r['saved_gb']:>8.2f} {r['pct_saved']:>4.0f} {r['size_gb']:>8.2f} {r['codec']:<6} "
              f"{r['res']:<10} {r['video_mbps']:>6.2f} {r['bpp']:>6.3f} {r['hdr']:<7} {r['basis']:<9} "
              f"{str(r['vmaf']):>5} {str(r['crf']):>3} {r['out_res']:<10} {name[:45]}")
    print(f"\nTop {min(top, len(keep))} cumulative savings: {cum:.1f} GB")


def vmaf_score(dist: Path, ref: Path, threads=8):
    """Mean VMAF of dist vs ref (every frame; both files must hold the same frames)."""
    cmd = [
        "ffmpeg", "-hide_banner", "-i", str(dist), "-i", str(ref), "-lavfi",
        "[0:v]setpts=PTS-STARTPTS,format=yuv420p10le[d];[1:v]setpts=PTS-STARTPTS,format=yuv420p10le[r];"
        f"[d][r]libvmaf=n_threads={threads}",
        "-f", "null", "-",
    ]
    r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")
    m = re.search(r"VMAF score:\s*([0-9.]+)", r.stderr)
    return float(m.group(1)) if m else None


def source_window_bytes(src: Path, ss: float, t: float, tmp):
    """Source video bytes for a t-second window, from a stream copy's bytes-per-second.
    (Copy snaps to a keyframe, so scale by the copy's real duration rather than trusting t.)"""
    cp = Path(tmp) / "copy.mkv"
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(ss), "-t", str(t), "-i", str(src),
                        "-map", "0:v:0", "-an", "-sn", "-c:v", "copy", str(cp)], capture_output=True)
    try:
        if r.returncode or not cp.exists():
            return 0
        d = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(cp)],
                           capture_output=True, encoding="utf-8").stdout.strip()
        dur = float(d)
        return cp.stat().st_size / dur * t if dur > 0 else 0
    except ValueError:
        return 0
    finally:
        cp.unlink(missing_ok=True)


def encode_args(target, crf, preset):
    if target == "av1":
        return ["-c:v", "libsvtav1", "-crf", str(crf + 8), "-preset", "6", "-pix_fmt", "yuv420p10le"]
    return ["-c:v", "libx265", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1"]


def prepare_clips(p, info, tmp, secs, clips, max_width=None):
    """Decode each test window ONCE to a lossless intermediate (scaled if requested).
    Encoding and scoring both use that intermediate, so frames always line up (decoding the
    source separately for encode and score drifted on files with irregular timestamps).
    Returns [(intermediate_path, source_bytes_for_window), ...]."""
    t = min(secs, info["duration"] / (clips + 1))
    w, h, scaled = out_dims(info, max_width)
    clip_mpix = w * h * info["fps"] * t / 1e6  # what encoding one clip costs, for speed measurements
    vf = ["-vf", f"scale={w}:{h}:flags=lanczos"] if scaled else []
    out = []
    for k in range(clips):
        print(f"   preparing test clip {k + 1}/{clips}...", end="\r", flush=True)
        ss = max(0, info["duration"] * (k + 1) / (clips + 1) - t / 2)
        ref = Path(tmp) / f"ref{k}.mkv"
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(ss), "-t", str(t), "-i", str(p),
                            "-map", "0:v:0", "-an", "-sn"] + vf + ["-c:v", "ffv1", str(ref)], capture_output=True)
        src_bytes = source_window_bytes(p, ss, t, tmp)
        if r.returncode or not ref.exists() or not src_bytes:
            print("   clip failed:", r.stderr.decode(errors="replace")[-200:])
            ref.unlink(missing_ok=True)
            continue
        out.append((ref, src_bytes, clip_mpix))
    return out


def measure(refs, tmp, enc, info):
    """Encode every prepared clip with `enc`.
    Returns (size_ratio, mean_vmaf, worst_vmaf, encode_mpix_per_sec) or None."""
    col = info.get("color", {})
    tags = []
    for flag, key in (("-color_primaries", "color_primaries"), ("-color_trc", "color_transfer"),
                      ("-colorspace", "color_space")):
        if col.get(key) and col[key] != "unknown":
            tags += [flag, col[key]]
    ref_total = new_total = 0
    enc_secs = enc_mpix = 0.0
    scores = []
    for n_done, (ref, src_bytes, clip_mpix) in enumerate(refs, 1):
        print(f"   encoding + scoring clip {n_done}/{len(refs)}...", end="\r", flush=True)
        new = Path(tmp) / "new.mkv"
        t0 = time.time()
        b = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(ref)] + enc + tags + [str(new)],
                           capture_output=True)
        took = time.time() - t0
        if b.returncode or not new.exists():
            print("   encode failed:", b.stderr.decode(errors="replace")[-200:])
            continue
        enc_secs += took
        enc_mpix += clip_mpix
        ref_total += src_bytes
        new_total += new.stat().st_size
        v = vmaf_score(new, ref)
        if v is not None:
            scores.append(v)
        new.unlink(missing_ok=True)
    if not ref_total:
        return None
    return (min(new_total / ref_total, 1.0),
            sum(scores) / len(scores) if scores else None,
            min(scores) if scores else None,
            enc_mpix / enc_secs if enc_secs else None)


FROM_CLI = False            # set by main(); the UI gets UI-appropriate wording
CRF_RANGE = (16, 32)       # auto-tune search bounds (x265 CRF)
REL_DROP = 3.0              # accepted VMAF loss vs the file's own best-quality (CRF 16) encode
WORST_CLIP_FACTOR = 1.5     # worst clip may drop REL_DROP * this vs the ceiling's worst clip


def sample(target, top, secs, crf, preset, clips=4, min_vmaf=MIN_VMAF, tune=True, max_width=None,
           rel_drop=REL_DROP, only=None):
    """Test-encode short clips per candidate. With tune=True, binary-search the CRF for the
    highest value whose VMAF stays within rel_drop of that file's own CRF-16 ceiling
    (HDR/DV files use the fixed CRF; VMAF can't judge them). min_vmaf is an optional absolute floor."""
    con = db()
    # ignore the VMAF floor when choosing what to test (earlier single-CRF failures deserve a retune);
    # skip files that were already tuned
    if only:  # explicitly chosen file: no savings thresholds, and re-tune even if already tuned
        keep = [r for r in rows(target, 0, 0, 0, max_width)[1] if only.lower() in r["path"].lower()]
    else:
        keep = [r for r in rows(target, MIN_PCT_SAVED, MIN_GB_SAVED, 0, max_width)[1] if r["crf"] == ""]
    n = min(top, len(keep))
    with tempfile.TemporaryDirectory() as tmp:
        for i, r in enumerate(keep[:top], 1):
            p = Path(r["path"])
            info = json.loads(con.execute("SELECT info FROM probe WHERE path=?", (r["path"],)).fetchone()[0])
            hdr = info.get("hdr", "")
            note_extra = ""
            print(f"[{i}/{n}] {p.name[:70]}" + (f"  [{hdr}]" if hdr else ""), flush=True)

            for old in Path(tmp).glob("*.mkv"):
                old.unlink(missing_ok=True)
            refs = prepare_clips(p, info, tmp, secs, clips, max_width)
            if not refs:
                continue

            def attempt(c):
                res = measure(refs, tmp, encode_args(target, c, preset), info)
                if res:
                    v = f"VMAF {res[1]:.1f} (worst {res[2]:.1f})" if res[1] is not None else "VMAF n/a"
                    print(f"   crf {c}: {res[0] * 100:.0f}% of original, {v}", flush=True)
                return res

            if tune and not hdr:
                # Relative target: encode at the best-quality CRF first. Its VMAF is this file's ceiling
                # (grainy/dark sources score low even when nearly transparent). Then find the highest
                # CRF that stays within rel_drop points of that ceiling.
                lo_crf, hi_crf = CRF_RANGE
                ceil_res = attempt(lo_crf)
                if not ceil_res or ceil_res[1] is None:
                    continue
                ceil_mean, ceil_worst = ceil_res[1], ceil_res[2]
                need_mean = max(ceil_mean - rel_drop, min_vmaf)
                need_worst = ceil_worst - rel_drop * WORST_CLIP_FACTOR
                print(f"   ceiling VMAF {ceil_mean:.1f}; accepting mean >= {need_mean:.1f}, worst >= {need_worst:.1f}")
                best = (lo_crf, ceil_res)
                lo, hi = lo_crf + 1, hi_crf
                while lo <= hi:
                    mid = (lo + hi) // 2
                    res = attempt(mid)
                    if not res:
                        break
                    if res[1] is not None and res[1] >= need_mean and res[2] >= need_worst:
                        best = (mid, res)
                        lo = mid + 1
                    else:
                        hi = mid - 1
                chosen = best
                note_extra = f" ceil{ceil_mean:.1f}"
            else:
                res = attempt(crf)
                if not res:
                    continue
                chosen = (crf, res)

            c, (ratio, mean_v, min_v, mpix_s) = chosen
            con.execute("INSERT OR REPLACE INTO sample2 (path, size, ratio, vmaf, vmaf_min, note, crf, max_width, "
                        "enc_mpix_s) VALUES (?,?,?,?,?,?,?,?,?)",
                        (r["path"], p.stat().st_size, ratio, mean_v, min_v, f"{preset}/{clips}x{secs}s{note_extra}", c,
                         max_width if out_dims(info, max_width)[2] else None, mpix_s))
            con.commit()
            eta = ""
            if mpix_s:
                ow, oh, _ = out_dims(info, max_width)
                secs_est = ow * oh * info["fps"] * info["duration"] / 1e6 / mpix_s
                eta = f"; full encode ~{secs_est:.0f} s" if secs_est < 90 else f"; full encode ~{secs_est / 60:.0f} min"
            print(f"   -> chosen crf {c}: saves {100 - ratio * 100:.0f}% (heuristic said {r['pct_saved']:.0f}%){eta}")
    print("\nSamples saved; run `report` to re-rank with measured numbers." if FROM_CLI
          else "\nSamples saved; the candidates table now shows the measured numbers.")


OUTPUT_DIR = Path(os.environ.get("REENCODE_OUTPUT", HERE / "output"))


def find_cached(substr):
    con = db()
    hits = [r for r in con.execute("SELECT path, size, info FROM probe") if substr.lower() in r[0].lower() and r[2]]
    if len(hits) != 1:
        sys.exit(f"'{substr}' matched {len(hits)} files: " + "; ".join(Path(h[0]).name for h in hits[:5]))
    return hits[0][0], hits[0][1], json.loads(hits[0][2])


def plan_encode(substr, target, crf, preset, max_width):
    """Build the ffmpeg command for a full re-encode (video re-encoded, audio/subs/chapters copied)."""
    path, size, info = find_cached(substr)
    con = db()
    m = con.execute("SELECT ratio, vmaf, crf, note FROM sample2 WHERE path=?", (path,)).fetchone()
    if crf is None:
        if not m or m[2] is None:
            sys.exit("No tuned CRF cached for this file; run `sample --only ...` first or pass --crf")
        crf = m[2]
    if m and m[3] and preset is None:
        preset = m[3].split("/")[0]
    preset = preset or "medium"
    src = Path(path)
    OUTPUT_DIR.mkdir(exist_ok=True)
    dst = OUTPUT_DIR / (src.stem + ".reencoded.mkv")
    w, h, scaled = out_dims(info, max_width)
    vf = ["-vf", f"scale={w}:{h}:flags=lanczos"] if scaled else []
    cmd = (["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-map", "0", "-c", "copy"] + vf
           + encode_args(target, crf, preset)
           # drop size/bitrate stats tags copied from the source; they'd be wrong for the new video
           + ["-metadata:s:v", "NUMBER_OF_BYTES=", "-metadata:s:v", "BPS=", "-metadata:s:v", "DURATION=",
              "-metadata:s:v", "NUMBER_OF_FRAMES=", "-map_chapters", "0", str(dst)])
    # '-c:v ...' after '-c copy' overrides it for video only; audio/subs stay stream-copied
    return {"cmd": cmd, "src": src, "dst": dst, "crf": crf, "preset": preset, "w": w, "h": h,
            "duration": info["duration"], "fps": info["fps"]}


def full_encode(substr, target, crf, preset, max_width):
    """Re-encode one whole file into ./output."""
    plan = plan_encode(substr, target, crf, preset, max_width)
    print(f"Encoding {plan['src'].name}\n  crf {plan['crf']}, preset {plan['preset']}, "
          f"output {plan['w']}x{plan['h']}\n  -> {plan['dst']}")
    subprocess.run(plan["cmd"], check=True)
    print(f"Done: {plan['dst']}")


def copy_with_progress(src: Path, dst: Path, chunk=8 << 20):
    total = src.stat().st_size
    done = 0
    with open(src, "rb") as f, open(dst, "wb") as g:
        while True:
            b = f.read(chunk)
            if not b:
                break
            g.write(b)
            done += len(b)
            print(f"\r  copying {100 * done / total:5.1f}%  ({done / 1024**3:.2f} of {total / 1024**3:.2f} GB)",
                  end="", flush=True)
    print()


def check_reencode(src: Path, dst: Path):
    """Refuse to swap in a re-encode that looks incomplete or doesn't match the original."""
    orig = run_probe(src)
    new = run_probe(dst)
    if not new:
        sys.exit(f"{dst.name} can't be read by ffprobe (incomplete?)")
    oi = summarize(orig, src.stat().st_size) if orig else None
    ni = summarize(new, dst.stat().st_size)
    if not oi or not ni:
        sys.exit("couldn't read video details from the original or the re-encode")
    if src.stat().st_mtime > dst.stat().st_mtime:
        sys.exit("the original was modified after the re-encode was made; re-encode it again")
    if abs(ni["duration"] - oi["duration"]) > max(2.0, 0.005 * oi["duration"]):
        sys.exit(f"duration differs: original {oi['duration']:.0f}s vs re-encode {ni['duration']:.0f}s")
    count = lambda d, t: sum(1 for s in d["streams"] if s.get("codec_type") == t)
    for kind in ("audio", "subtitle"):
        if count(orig, kind) != count(new, kind):
            sys.exit(f"{kind} track count differs: original {count(orig, kind)} vs re-encode {count(new, kind)}")
    if ni["codec"] not in ("hevc", "av1"):
        sys.exit(f"re-encode video codec is {ni['codec']}, expected hevc/av1")
    if dst.stat().st_size >= src.stat().st_size:
        sys.exit("the re-encode isn't smaller than the original")


def replace_original(path, keep_backup=False):
    """Swap the approved re-encode in ./output in for the original, in the original's folder and name
    (extension becomes .mkv). The copy lands under a temp name first, so the original is only removed
    after a complete, size-checked copy exists."""
    src = Path(path)
    dst = OUTPUT_DIR / (src.stem + ".reencoded.mkv")
    if not src.exists():
        sys.exit(f"original not found: {src}")
    if not dst.exists():
        sys.exit(f"no finished re-encode for {src.name} in {OUTPUT_DIR}")
    final = src.with_suffix(".mkv")
    if final != src and final.exists():
        sys.exit(f"{final.name} already exists next to the original; not overwriting it")
    bak = src.with_name(src.name + ".bak")
    if keep_backup and bak.exists():
        sys.exit(f"{bak.name} already exists")
    print(f"Verifying {dst.name} against {src.name}...", flush=True)
    check_reencode(src, dst)
    old_bytes, new_bytes = src.stat().st_size, dst.stat().st_size
    tmp = final.with_name(final.name + ".reencode-tmp")
    try:
        copy_with_progress(dst, tmp)
        if tmp.stat().st_size != new_bytes:
            sys.exit("copy is the wrong size; original left untouched")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:
        if keep_backup:
            os.replace(src, bak)  # frees the name
        elif final != src:
            src.unlink()
        os.replace(tmp, final)  # same-name case: overwrites the original in one step
    except OSError as e:
        sys.exit(f"swap failed ({e}). The re-encode is safe at {tmp} and {dst}")
    dst.unlink(missing_ok=True)
    con = db()
    con.execute("DELETE FROM probe WHERE path=?", (str(src),))
    con.execute("DELETE FROM sample2 WHERE path=?", (str(src),))
    con.execute("INSERT INTO replaced VALUES (?,?,?,?,?)", (str(src), str(final), old_bytes, new_bytes, time.time()))
    con.commit()
    probe_into_cache(final)
    print(f"Replaced {src.name} -> {final.name}: {old_bytes / 1024**3:.2f} GB -> {new_bytes / 1024**3:.2f} GB, "
          f"reclaimed {(old_bytes - new_bytes) / 1024**3:.2f} GB"
          + (f"\nOriginal kept as {bak.name}" if keep_backup else ""))


def compare(substr, vmaf_full):
    """Predicted vs actual for a finished re-encode in ./output."""
    path, size, info = find_cached(substr)
    src = Path(path)
    dst = OUTPUT_DIR / (src.stem + ".reencoded.mkv")
    if not dst.exists():
        sys.exit(f"{dst} not found; run `encode` first")
    con = db()
    m = con.execute("SELECT ratio, vmaf, crf FROM sample2 WHERE path=?", (path,)).fetchone()
    cur_v = info["video_bytes"]
    # count real packets: size tags in the output can be stale copies from the source
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=size",
                        "-of", "csv=p=0", str(dst)], capture_output=True, encoding="utf-8")
    new = {"video_bytes": sum(int(x) for x in r.stdout.split() if x.strip().isdigit()), "vbytes_method": "packets"}
    actual_ratio = new["video_bytes"] / cur_v
    print(f"\nFile: {src.name}")
    print(f"  original total {size / 1024**3:.2f} GB  ->  re-encode total {dst.stat().st_size / 1024**3:.2f} GB "
          f"({100 * (1 - dst.stat().st_size / size):.0f}% saved overall)")
    print(f"  video stream: {cur_v / 1024**3:.2f} GB -> {new['video_bytes'] / 1024**3:.2f} GB "
          f"({new['vbytes_method']}), actual ratio {actual_ratio * 100:.0f}%")
    if m:
        print(f"  sampler predicted ratio {m[0] * 100:.0f}% (CRF {m[2]}, sampled VMAF {m[1]:.1f})")
        print(f"  prediction error: {100 * (actual_ratio - m[0]):+.0f} points of original video size")
    est_new, _ = estimate(info, "hevc", None, None)
    print(f"  old bpp heuristic predicted ratio {100 * est_new / cur_v:.0f}%")
    if vmaf_full:
        print("  scoring the whole film with VMAF (every frame; this takes a while)...", flush=True)
        w, h, scaled = out_dims(info, None)
        cmd = ["ffmpeg", "-hide_banner", "-i", str(dst), "-i", str(src), "-lavfi",
               "[0:v:0]setpts=PTS-STARTPTS,format=yuv420p10le[d];[1:v:0]setpts=PTS-STARTPTS,format=yuv420p10le[r];"
               "[d][r]libvmaf=n_threads=16", "-f", "null", "-"]
        r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")
        mm = re.search(r"VMAF score:\s*([0-9.]+)", r.stderr)
        print(f"  full-film VMAF: {mm.group(1) if mm else 'failed'}")


def main():
    global FROM_CLI
    FROM_CLI = True
    ap =argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--target", choices=["hevc", "av1"], default="hevc")
    common.add_argument("--min-pct", type=float, default=MIN_PCT_SAVED)
    common.add_argument("--min-gb", type=float, default=MIN_GB_SAVED)
    common.add_argument("--top", type=int, default=40)
    common.add_argument("--min-vmaf", type=float, default=MIN_VMAF)
    common.add_argument("--max-width", type=int, default=None, metavar="PX",
                        help="downscale anything wider than this (e.g. 1920 turns 4K into 1080p)")

    s = sub.add_parser("scan", parents=[common])
    s.add_argument("folder", type=Path)
    s.add_argument("--workers", type=int, default=6)
    sub.add_parser("report", parents=[common])
    sm = sub.add_parser("sample", parents=[common])
    sm.add_argument("--secs", type=int, default=15, help="length of each test clip")
    sm.add_argument("--clips", type=int, default=4, help="clips per file, spread through the runtime")
    sm.add_argument("--rel-drop", type=float, default=REL_DROP,
                    help="accepted VMAF loss vs the file's own best-quality encode")
    sm.add_argument("--crf", type=int, default=24, help="fixed CRF (used with --no-tune, and for HDR files)")
    sm.add_argument("--no-tune", action="store_true", help="test only --crf instead of searching for the best CRF")
    sm.add_argument("--preset", default="medium")
    sm.add_argument("--only", help="only tune files whose path contains this text")
    en = sub.add_parser("encode", parents=[common], help="fully re-encode one file into ./output")
    en.add_argument("name", help="text identifying the file (must match exactly one)")
    en.add_argument("--crf", type=int, default=None, help="default: the tuned CRF from `sample`")
    en.add_argument("--preset", default=None, help="default: the preset used when sampling")
    cp = sub.add_parser("compare", parents=[common], help="predicted vs actual for a finished re-encode")
    cp.add_argument("name")
    cp.add_argument("--full-vmaf", action="store_true", help="also score the entire film (slow)")

    rp = sub.add_parser("replace", parents=[common], help="swap an approved re-encode in for the original")
    rp.add_argument("name")
    rp.add_argument("--keep-backup", action="store_true", help="rename the original to .bak instead of deleting it")

    a = ap.parse_args()
    if a.cmd == "replace":
        replace_original(find_cached(a.name)[0], a.keep_backup)
    elif a.cmd == "scan":
        if not a.folder.exists():
            sys.exit(f"Not found: {a.folder}")
        scan(a.folder, a.workers)
        report(a.target, a.min_pct, a.min_gb, a.top, a.min_vmaf, a.max_width)
    elif a.cmd == "report":
        report(a.target, a.min_pct, a.min_gb, a.top, a.min_vmaf, a.max_width)
    elif a.cmd == "encode":
        full_encode(a.name, a.target, a.crf, a.preset, a.max_width)
    elif a.cmd == "compare":
        compare(a.name, a.full_vmaf)
    else:
        sample(a.target, a.top, a.secs, a.crf, a.preset, a.clips, a.min_vmaf, not a.no_tune, a.max_width,
               a.rel_drop, a.only)


if __name__ == "__main__":
    main()
