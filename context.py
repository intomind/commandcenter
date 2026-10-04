"""Where this installation's world is, stated once.

The library talks to whatever device is connected from wherever it is
installed, so it discovers none of this and does not try: an application says
where its recordings live, which source tree is doing the recording, and what
is known about the setup. Anything not declared is recorded as unknown.

Every entry point into this application imports this first. Before the
2026-08-19 split these were inferred from the library's own location inside one
device's repository, which is how recordings from one device came to carry
another device's electrode description.
"""
import pathlib

from intomind import provenance as prov

HERE = pathlib.Path(__file__).resolve().parent


def bind() -> pathlib.Path:
    prov.use_captures_dir(HERE / "captures")
    prov.use_repo(HERE)
    # THE SETUP, ONLY IF THIS INSTALLATION HAS ONE TO DECLARE.
    #
    # The Command Center is not a bench. It connects to whatever device is in
    # front of it, so it knows nothing about board revisions, mains frequency
    # or what is on anyone's head, and it asserts none of it. Someone running
    # it on a described bench can drop that bench's site.json here and it will
    # be recorded; with no file, a capture says "unknown", which is true.
    site = HERE / "config" / "site.json"
    if site.exists():
        prov.use_site(site)
    return HERE
