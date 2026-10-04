"""V2 validation: ocular dipole. Thin CLI over intomind.analysis.saccade.

    python3 tools/saccade.py <label>

PASS requires: ch1/ch2 polarity inverted for both directions, sign reversing
with direction, deflection >= 50 uV, and a null sham.
"""
import pathlib as _pl, sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import context; context.bind()   # where this installation's captures are
import sys, pathlib
from intomind import analysis as A

r = A.saccade(sys.argv[1])
print(f"== {r['label']}   analysis v{r['analysis_version']}   "
      f"sha {r['capture_sha256'][:16]}…")
for cond, d in r["conditions"].items():
    chs = "  ".join(f"ch{i+1} {c['deflection_uv']:+8.1f} uV (snr {c['snr']:5.1f})"
                    for i, c in enumerate(d["channels"]))
    corr = d.get("ch1_ch2_corr")
    print(f"  {cond:<6} n={d['n_trials']:<3} {chs}   corr={corr:+.3f}"
          if corr is not None else f"  {cond:<6} n={d['n_trials']:<3} {chs}")
print("\n  criteria:")
for k, v in r["criteria"].items():
    print(f"    {'PASS' if v else 'FAIL'}  {k}")
print(f"\n  VERDICT: {r['verdict']}")
if r.get("failed_criteria"):
    print("  failed:", ", ".join(r["failed_criteria"]))
