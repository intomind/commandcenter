"""Write captures out in any format the library offers.

    python3 tools/export.py <label> [<label> ...] [--format bdf] [--out DIR]
    python3 tools/export.py --all --format csv
    python3 tools/export.py --formats

Every format comes from `intomind.export`, which is the same exporter the
Command Center's page uses, so a file written here and a file written there are
the same file. Nothing in this repository writes a format of its own.

What a format costs is not asserted here. The exporter measures it on the file
it just wrote and this prints that: how many samples it holds, whether every
sample came back at the value it was recorded at, and the largest error if not.
A capture that fails its own checksum is refused rather than exported.
"""
from __future__ import annotations
import pathlib as _pl, sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import context; context.bind()   # where this installation's captures are
import argparse, pathlib

from intomind import analysis as A
from intomind import export

ap = argparse.ArgumentParser()
ap.add_argument("labels", nargs="*")
ap.add_argument("--all", action="store_true", help="every analysable capture")
ap.add_argument("--format", default="npz", help="one of " + ", ".join(export.FORMATS))
ap.add_argument("--formats", action="store_true", help="list the formats and stop")
ap.add_argument("--out", default=None, help="output directory")
a = ap.parse_args()

if a.formats:
    for name in export.FORMATS:
        print(f"  {name:<4} {export.SUFFIX[name]}")
    raise SystemExit(0)

if a.format not in export.FORMATS:
    ap.error(f"unknown format {a.format!r}. Known formats: "
             + ", ".join(export.FORMATS))

labels = a.labels
if a.all:
    labels = [c["label"] for c in A.list_captures()
              if c["integrity"] not in ("ABORTED", "FAILED")]
if not labels:
    ap.error("give at least one label, or --all")

out = pathlib.Path(a.out) if a.out else \
    pathlib.Path(__file__).resolve().parent.parent / "exports"
out.mkdir(parents=True, exist_ok=True)

ok = failed = 0
for label in labels:
    try:
        s = export.export(label, a.format, out)
    except Exception as e:
        failed += 1
        print(f"  SKIP  {label}: {type(e).__name__}: {e}")
        continue
    ok += 1
    cost = ("every count recovered" if s.get("counts_recovered")
            else "values were rounded")
    err = (f", largest error {s['max_error_uv']:.6g} uV against an LSB of "
           f"{s['lsb_uv']:.6g} uV" if "max_error_uv" in s else "")
    drop = (f", dropped {s['samples_dropped']} trailing samples"
            if s.get("samples_dropped") else "")
    print(f"  {pathlib.Path(s['path']).name}  {s['samples']} samples x "
          f"{s['channels']} channels @ {s['sample_rate_hz']:.4f} Hz  "
          f"{cost}{err}{drop}")
    for note in s.get("omitted") or []:
        print(f"      omitted: {note}")

print(f"\n{ok} exported, {failed} skipped  ->  {out}")
print("captures/<label>.npz remains the record. An export is regenerable.")
