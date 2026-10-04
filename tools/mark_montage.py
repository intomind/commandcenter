#!/usr/bin/env python3
"""Assert the electrode montage of captures recorded before it was structured.

    python3 tools/mark_montage.py --list
    python3 tools/mark_montage.py --source "config/site.json electrode_montage, unchanged since 2026-07-14" --dry-run
    python3 tools/mark_montage.py --source "..."

Captures made before 2026-08-06 recorded the montage as one English sentence
and nothing else. The sentence is real evidence -- it was written by the person
wearing the electrodes -- so those captures can be described structurally after
the fact. What they cannot be is *indistinguishable* from captures that
recorded the structure themselves.

So this writes a SIDECAR (`captures/MONTAGE_BACKFILL.json`) and never touches a
capture. Three reasons, and the first is the one that matters:

  1. A capture's manifest is the record of what was known WHEN IT WAS RECORDED.
     Editing it would erase the difference between an observation and a later
     claim about the same thing, which is the difference `montage_for()`
     reports as `recorded` vs `backfilled`.
  2. The manifest is inside the capture's `.sha256`. Rewriting it breaks the
     integrity check that makes captures worth trusting.
  3. Finalized captures are chmod 444. Anything that needs to unfreeze them to
     do its job is doing the wrong job.

**`--source` is mandatory and is not decoration.** It is the artifact the
montage was read FROM. The tool refuses to invent one, exactly as the deleted
`mark_legacy.py` refused to write a board revision without a `--source` citing
the artifact it came from -- guessing a montage pools recordings across a
change of electrodes, and that is worse than a capture nobody can describe.

**Prose must agree.** A capture is only marked when its own
`electrode_montage` sentence matches the one the structured montage carries.
A capture whose sentence differs describes a different head and is refused by
name, never quietly skipped.
"""
from __future__ import annotations

import pathlib as _pl, sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import context; context.bind()   # where this installation's captures are
import argparse
import json
import pathlib
import sys
import time


from intomind import montage as M           # noqa: E402
from intomind import provenance as prov     # noqa: E402


def _norm(s: str | None) -> str:
    """Whitespace-insensitive comparison. Nothing else is normalized: two
    montage sentences that differ in wording describe two different beliefs
    about the head and must not be quietly reconciled."""
    return " ".join((s or "").split())


def survey() -> dict[str, list]:
    """Every capture, sorted by what is known about its montage."""
    out = dict(recorded=[], backfilled=[], eligible=[], prose_differs=[],
               no_prose=[])
    site_m = M.from_site(prov.site())
    want = _norm((site_m or {}).get("prose"))
    for p in sorted(prov.CAPTURES.glob("*.meta.json")):
        label = p.name[: -len(".meta.json")]
        try:
            meta = json.loads(p.read_text())
        except Exception:
            continue
        info = prov.montage_for(label, meta)
        if info["provenance"] == "recorded":
            out["recorded"].append(label)
        elif info["provenance"] == "backfilled":
            out["backfilled"].append(label)
        else:
            prose = _norm(meta.get("electrode_montage"))
            if not prose:
                out["no_prose"].append(label)
            elif want and prose == want:
                out["eligible"].append(label)
            else:
                out["prose_differs"].append((label, meta.get("electrode_montage")))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", help="the artifact this montage was read FROM. "
                                     "Required to write anything.")
    ap.add_argument("--list", action="store_true",
                    help="report what is known, write nothing")
    ap.add_argument("--dry-run", action="store_true",
                    help="show exactly what would be written")
    ap.add_argument("labels", nargs="*",
                    help="restrict to these captures (default: all eligible)")
    a = ap.parse_args()

    site_m = M.from_site(prov.site())
    if site_m is None:
        print("config/site.json declares no structured montage; nothing to "
              "assert from.", file=sys.stderr)
        return 2

    s = survey()
    print(f"recorded at capture time : {len(s['recorded'])}")
    print(f"already back-filled      : {len(s['backfilled'])}")
    print(f"eligible (prose matches) : {len(s['eligible'])}")
    print(f"prose differs -- REFUSED : {len(s['prose_differs'])}")
    print(f"no montage prose at all  : {len(s['no_prose'])}")
    for label, prose in s["prose_differs"]:
        print(f"  refused {label}: {prose!r}")
    for label in s["no_prose"]:
        print(f"  no prose {label}: nothing to check a montage against")

    if a.list:
        return 0
    if not a.source:
        print("\n--source is required: cite the artifact this montage was read "
              "from. A guessed montage pools recordings across a change of "
              "electrodes.", file=sys.stderr)
        return 2

    targets = [l for l in s["eligible"] if not a.labels or l in a.labels]
    unknown = [l for l in a.labels if l not in s["eligible"]]
    for l in unknown:
        print(f"  not eligible, skipped: {l}", file=sys.stderr)
    if not targets:
        print("\nnothing to write.")
        return 0

    stamped = dict(site_m)
    stamped["source"] = a.source
    entry = dict(montage=stamped, source=a.source,
                 marked_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    if a.dry_run:
        print(f"\nwould mark {len(targets)} capture(s):")
        for l in targets:
            print(f"  {l}")
        print(f"\nsource: {a.source}")
        return 0

    book = prov.montage_backfill()
    for l in targets:
        book[l] = entry
    prov.MONTAGE_BACKFILL.write_text(json.dumps(book, indent=2, sort_keys=True))
    print(f"\nmarked {len(targets)} capture(s) in "
          f"{prov.MONTAGE_BACKFILL.relative_to(prov.REPO)}")
    print("No capture was modified; every checksum still verifies.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
