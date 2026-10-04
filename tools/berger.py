"""Berger (eyes closed vs open). Thin CLI over intomind.analysis.berger.

    python3 tools/berger.py <label> [--ppmax 150] [--epoch 2] [--pooled]

The default test is a paired, block-level Wilcoxon. Absolute band power is the
honest number: relative alpha can rise purely because its 4-30 Hz denominator
fell. Both are printed.
"""
import pathlib as _pl, sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import context; context.bind()   # where this installation's captures are
import sys, argparse, pathlib

from intomind import analysis as A

ap = argparse.ArgumentParser()
ap.add_argument("label")
ap.add_argument("--ppmax", type=float, default=150.0)
ap.add_argument("--epoch", type=float, default=2.0)
ap.add_argument("--pooled", action="store_true",
                help="also run the pseudoreplicated pooled-epoch test")
a = ap.parse_args()

r = A.berger(a.label, epoch_s=a.epoch, ppmax=a.ppmax,
             allow_pseudoreplication=a.pooled)

print(f"== {r['label']}   analysis v{r['analysis_version']}   "
      f"{r['n_reps']} reps   block={r['block']}s")
print(f"   capture sha256 {r['capture_sha256'][:16]}…   "
      f"reject pp>{a.ppmax} uV, epoch {a.epoch}s")
mc = r["multiple_comparisons"]
print(f"   {mc['n_tests']} tests -> Bonferroni threshold p < {mc['bonferroni_alpha']:.4f}")

for i, c in enumerate(r["channels"]):
    print(f"\n  --- ch{i+1} ---")
    for b in c["blocks"]:
        print(f"    rep{b['rep']} {b['cond']:>6}: abs={b['alpha_abs']:8.3f}  "
              f"rel={b['alpha_rel']:6.2f}%   epochs {b['clean']}/{b['total']}")
    for tag in ("absolute", "relative"):
        s = c.get(tag, {})
        if "closed" not in s:
            print(f"    {tag}: {s.get('note', 'no data')}")
            continue
        star = " *" if s["p"] < mc["bonferroni_alpha"] else ""
        print(f"    {tag:<9} closed={s['closed']:8.3f} open={s['open']:8.3f} "
              f"ratio={s['ratio']:.3f}  wins {s['wins']}/{s['n_blocks']}  "
              f"p={s['p']:.4f} (floor {s['p_floor']:.4f})  dz={s['cohens_dz']:+.2f}{star}")
    if c.get("denominator_artifact"):
        print(f"    !! {c['interpretation']}")
    elif c.get("reference_band_ratio"):
        print(f"    reference band ratio {c['reference_band_ratio']:.3f}")
    if "pooled_epochs" in c:
        pe = c["pooled_epochs"]
        print(f"    pooled    p={pe['p']:.4f}   !! {pe['warning']}")
    sl, pk = c.get("slope_2_40"), c.get("alpha_peak_ratio")
    if sl is not None:
        print(f"    1/f slope {sl:+.2f}"
              f"{'   << WHITE — not EEG' if abs(sl) < 0.3 else ''}")
    if pk is not None:
        print(f"    alpha peak {pk:.2f}x over 1/f at {c['alpha_peak_hz']:.2f} Hz"
              f"   {'(real peak)' if c['has_alpha_peak'] else '(NO real peak)'}")

if r["pseudoreplicated"]:
    print("\n!! This run included a pooled-epoch test. Its p-value is inflated.")
