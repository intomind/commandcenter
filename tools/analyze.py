"""Summarize a capture. Thin CLI over intomind.analysis.summary.

    python3 tools/analyze.py <label> [<label> ...]
"""
import pathlib as _pl, sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import context; context.bind()   # where this installation's captures are
import sys, pathlib

from intomind import analysis as A


def show(label):
    r = A.summary(label)
    m = r["meta"]
    print(f"== {label}  fs={r['fs']:.3f}  gain={m['gain']}  "
          f"mode={m.get('mode','?')}  rld={m.get('rld_preset','?')}  "
          f"n={r['n']}  contiguous={r['kept_frac']*100:.1f}%")
    for i, c in enumerate(r["channels"]):
        print(f"  ch{i+1}: DC={c['dc_uv']:+10.1f} uV  ac_rms={c['ac_rms_uv']:9.2f} uV  "
              f"pp/rms={c['pp_over_rms']:5.2f}  sat={c['saturation_pct']:.2f}%")
        print(f"        PSD 5-45={c['psd_5_45']:9.4f}  100-200={c['psd_100_200']:9.4f} "
              f" uV^2/Hz   rms(1-45)={c['rms_1_45_uv']:8.2f} uV")
        print(f"        slope 2-40={c['slope_2_40']:+5.2f}"
              f"{'  << white, not EEG' if abs(c['slope_2_40']) < 0.3 else ''}"
              f"   peak={c['peak_hz']:.2f} Hz   PSD@60Hz={c['psd_60']:.3f}")
        print("        bands %: " + "  ".join(
            f"{k}={v:.1f}" for k, v in c["bands"].items()))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        for c in A.list_captures():
            print(f"  {c['label']:<45} n={c['n']:<7} gain={c['gain']}")
        sys.exit(0)
    for lb in sys.argv[1:]:
        show(lb); print()
