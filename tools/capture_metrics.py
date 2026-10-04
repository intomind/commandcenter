#!/usr/bin/env python3
"""Per-channel metrics for every capture, computed from the raw files.

This exists because a number was typed into a document by hand, and it was the
*good channel* of two. Prose once advertised "60 Hz line, RLD on: 95" as a
property of the instrument. That was ch2. In the same recording ch1 read 814,
and forty minutes later 30 011. ch1 had been broken since 2026-07-10 and the
prose could not show it.

So no metric of a capture is written by a human again. This regenerates
`captures/METRICS.md` from the `.npz` files, per channel, always both channels,
directly from what the captures actually say.

    python3 tools/capture_metrics.py            # print the table
    python3 tools/capture_metrics.py --write    # regenerate captures/METRICS.md
    python3 tools/capture_metrics.py --check    # exit 1 if METRICS.md is stale
"""
from __future__ import annotations

import pathlib as _pl, sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import context; context.bind()   # where this installation's captures are
import argparse
import json
import pathlib
import sys

import numpy as np
from scipy import signal as sg

from intomind import provenance as prov   # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
CAPTURES = ROOT / "captures"
OUT = CAPTURES / "METRICS.md"

BAND = (5.0, 45.0)  # in-band, as every RLD and noise figure in this repo uses


def metrics(npz: pathlib.Path) -> dict | None:
    """DC, in-band power and mains PSD, per channel. None if unreadable."""
    meta_path = npz.with_suffix("").with_suffix(".meta.json")
    if not meta_path.exists():
        meta_path = npz.parent / (npz.stem + ".meta.json")
    if not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text())
    d = np.load(npz)
    if "counts" not in d.files:
        return None
    fs = meta.get("fs_effective") or meta.get("fs")
    gain = meta.get("gain")
    vref = meta.get("vref_uv")
    bits = meta.get("adc_bits", 24)
    if not (fs and gain and vref):
        return None
    lsb = 2 * vref / (gain * 2 ** bits)
    counts = np.asarray(d["counts"], dtype=float)
    if counts.ndim != 2 or counts.shape[1] < 512:
        return None
    mains = float(meta.get("mains_hz") or 60.0)

    row = {
        "capture": npz.stem,
        "gain": gain,
        "fs": round(float(fs), 1),
        "n": int(counts.shape[1]),
        "revision": meta.get("board_revision") or meta.get("revision") or "?",
        "mains_hz": mains,
        "channels": [],
    }
    for ch in range(counts.shape[0]):
        x = counts[ch] * lsb
        dc_mv = float(x.mean()) / 1000.0
        f, p = sg.welch(x - x.mean(), fs=fs, nperseg=min(4096, len(x)))
        sel = (f >= BAND[0]) & (f < BAND[1])
        row["channels"].append({
            "ch": ch + 1,
            "dc_mv": round(dc_mv, 2),
            "band_uv2": round(float(np.trapezoid(p[sel], f[sel])), 3),
            "mains_uv2_hz": round(float(p[int(np.argmin(abs(f - mains)))]), 1),
        })
    return row


def collect() -> tuple[list[dict], list[tuple[str, str]]]:
    """(rows, excluded). An ABORTED run is not a recording and is never analyzed.

    This table used to include them, silently, in the same rows as real data: a run
    whose link died mid-capture, whose stop was never acknowledged and whose tail may
    be missing, rendered indistinguishable from one that completed. `provenance.verify`
    has always known the difference; this file simply never asked. Excluded runs
    are LISTED, not dropped -- a table that quietly omits things reads as "everything
    is here" when it is not.
    """
    rows, excluded = [], []
    for npz in sorted(CAPTURES.glob("*.npz")):
        why = prov.aborted_reason(npz.stem)
        if why:
            excluded.append((npz.stem, why))
            continue
        r = metrics(npz)
        if r:
            rows.append(r)
    return rows, excluded


def render(rows: list[dict], excluded: list[tuple[str, str]]) -> str:
    out = [
        "# Capture metrics — GENERATED, DO NOT EDIT",
        "",
        "Regenerate with `python3 tools/capture_metrics.py --write`. Every figure is",
        "**per channel**: the defect that hid for four days was a channel-averaged",
        "claim in prose.",
        "",
        "`band` = in-band power 5–45 Hz (µV²). `mains` = PSD at the site's mains",
        "frequency (µV²/Hz). `dc` = mean offset (mV), the best single evidence an",
        "electrode is on skin.",
        "",
        "| capture | rev | gain | ch | dc (mV) | band (µV²) | mains (µV²/Hz) |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        for c in r["channels"]:
            out.append(
                f"| `{r['capture']}` | {r['revision']} | {r['gain']} | {c['ch']} "
                f"| {c['dc_mv']} | {c['band_uv2']} | {c['mains_uv2_hz']} |"
            )
    out.append("")
    out.append("## Excluded — ABORTED, never finalized, not analyzed")
    out.append("")
    if excluded:
        out.append("An aborted run's link died before its stop was acknowledged, so its")
        out.append("tail may be missing and its length cannot be trusted. It is not a")
        out.append("recording. It is listed here so that its absence above is visible.")
        out.append("")
        out.append("| capture | why |")
        out.append("|---|---|")
        for label, why in excluded:
            out.append(f"| `{label}` | {why} |")
    else:
        out.append("None.")
    out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    rows, excluded = collect()
    text = render(rows, excluded)
    if a.write:
        OUT.write_text(text)
        print(f"wrote {OUT.relative_to(ROOT)}")
        return 0
    if a.check:
        if not OUT.exists():
            print(f"FAIL: {OUT.relative_to(ROOT)} does not exist. "
                  f"Run tools/capture_metrics.py --write.")
            return 1
        if OUT.read_text() != text:
            print(f"FAIL: {OUT.relative_to(ROOT)} disagrees with the captures. "
                  f"Run tools/capture_metrics.py --write.")
            return 1
        print("capture metrics: current")
        return 0
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
