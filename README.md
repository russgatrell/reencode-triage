# reencode-triage: find the videos worth re-encoding

A video library fills drives fast, but re-encoding everything wastes days of processing on files that barely shrink. This tool scans a library, estimates how much space each file would free if re-encoded to HEVC or AV1, tests the best candidates for real, and only replaces originals you've approved.

By **Russ Gatrell**. It's a side project that applies the same habit I use in finance data work: measure first, then act on what the numbers say.

## What it does

- **Scan:** probes every video with `ffprobe` (in parallel, cached in SQLite, so re-runs are instant) and records codec, resolution, bitrate, HDR and stream sizes.
- **Estimate and rank:** predicts the size after re-encoding from a bits-per-pixel model and ranks files by *absolute* space saved, not by percent. Files below a minimum saving are filtered out.
- **Sample:** test-encodes short clips spread through each top candidate and scores them with VMAF. It searches for the highest compression that stays close to each file's own best quality. Estimates are then replaced with measurements.
- **Encode, compare, replace:** fully re-encodes a file (copying audio, subtitles and chapters untouched), checks predicted against actual, and swaps in the new file only when you approve, keeping a backup if you ask for one.
- **Web interface:** a Gradio front end with a job queue that is saved in SQLite. Reloading the page, or even a browser crash, loses nothing; queued jobs keep running.

## Quick start

Requires Python 3.10+ and [FFmpeg](https://ffmpeg.org/) built with `libvmaf`, on your PATH.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

python app.py                     # web interface (or double-click run_ui.bat on Windows)
```

Command line:

```bash
python reencode_triage.py scan "D:\Videos"     # probe and cache every file, write the ranked report
python reencode_triage.py report               # re-rank from the cache without probing
python reencode_triage.py sample --top 15      # real test encodes + VMAF for the top 15
python reencode_triage.py encode "Movie Name"  # full re-encode into ./output
python reencode_triage.py compare "Movie Name" # predicted vs. actual
python reencode_triage.py replace "Movie Name" --keep-backup
```

Useful options: `--target hevc|av1`, `--min-pct`, `--min-gb`, `--max-width 1920` (downscale, e.g. 4K to 1080p), `--preset`, `--crf`.

## Tuning

The estimation model is at the top of `reencode_triage.py`: target bits per pixel by resolution, the AV1 factor, savings caps for codecs that are already efficient, and the report's filters. The defaults aim for "visually fine" quality, roughly x265 CRF 23–26.

## License

MIT. See [LICENSE](LICENSE).
