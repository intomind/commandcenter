"""Everything the application shows is American English, identifiers included.

    python3 tests/test_american.py

The rule, 2026-08-19: the app uses only American English, and British
spellings had crept in everywhere. It should not have to be said twice, so
this is the saying, mechanized. A British identifier is the same mistake as
a British label, one layer down.

WHAT IS NOT CHECKED: `captures/`. A recording is the record of what happened and is never
edited to fix a spelling, which is also why the parameter label map
still carries the old `centre_s` key: three saccade captures were
recorded under it and they still have to render a label.
"""
from __future__ import annotations
import pathlib, re, sys, traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- the rule
#
# FAMILIES, NOT A WORD LIST. A list only catches the words somebody already
# thought of, and the next British spelling to arrive will be one nobody
# listed. So each family is matched by its shape.
#
# The -ise family cannot be matched by shape alone, because noise, promise,
# exercise, precise and forty more are English. It is matched broadly and then
# filtered against the stems in _FINE, which is the right way round: a false
# alarm is a person reading one line, and a miss is a British spelling that
# ships.

#: -our, matched as a prefix so colours, coloured and behavioural all land.
_OUR = ("behaviour|colour|favour|flavour|honour|humour|labour|neighbour"
        "|odour|rumour|armour|endeavour|harbour|parlour|saviour|savour"
        "|splendour|valour|vapour|vigour")

#: -re, likewise: centres, centred, millimetres. `centring` loses the e, so it
#: is spelled out.
_RE = ("centre|centring|metre|litre|fibre|theatre|calibre|spectre|sombre"
       "|lustre|manoeuvre|sceptre|mitre|ochre|meagre|millimetre|centimetre"
       "|kilometre|millilitre|micrometre|nanometre")

#: The rest, with a free tail. The `(?!l)` ones are British single-l words
#: whose American spelling doubles it, so fulfil is caught and fulfill is not.
_ODD = ("analogue|catalogue|grey|learnt|spelt|dreamt|whilst|amongst"
        "|artefact|aluminium|sulphur|tyre|mould|moult|smoulder|storey"
        "|draught|plough|kerb|aeroplane|moustache|pyjamas|cheque|gaol"
        "|whisky|sceptic|speciality|marvellous|jewellery|judgement"
        "|defence|offence|pretence|licence|practise"
        # programmed and programmer are American too. Only the noun
        # and its plural are British.
        "|programme(?![dr])"
        "|travelled|travelling|traveller|modelled|modelling|modeller"
        "|signalled|signalling|levelled|levelling|fuelled|fuelling"
        "|labelled|labelling|cancelled|cancelling|unlabelled|unravelled"
        "|fulfil(?!l)|skilful|wilful|instil(?!l)|distil(?!l)|enrol(?!l)"
        "|appal(?!l)|instalment")

# STARTS AFTER ANYTHING THAT IS NOT A LETTER, not at a word boundary. An
# underscore IS a word character, so `\b` walked straight past `wire_colours`
# and `n_centres`. A British identifier is the same mistake as a British
# label, one layer down, and it is the layer that is easy to miss.
_PLAIN = re.compile(r"(?<![a-zA-Z])(?:" + _OUR + "|" + _RE + "|" + _ODD
                    + r")\w*", re.IGNORECASE)

#: analyse and friends, but NOT `analyses`, which is the plural of analysis
#: and spelled the same on both sides.
_ANALYSE = re.compile(r"(?<![a-zA-Z])analys(?:e|ed|ing|er|ers)(?![a-z])",
                      re.IGNORECASE)

#: Broad -ise catch, filtered by _FINE. The tail is `(?![a-z])` rather than a
#: word boundary so `test_quantisation_step` is still caught.
_ISE = re.compile(r"\b(\w{2,}is)(e|es|ed|ing|ation|ations|er|ers)(?![a-z])",
                  re.IGNORECASE)

#: Stems ending in -is that are English. Checked lower case and without the
#: ending, so raised, raising and raiser are all covered by `rais`.
_FINE = {
    "rais", "nois", "prais", "cruis", "bruis", "guis", "disguis", "precis",
    "concis", "promis", "premis", "demis", "surpris", "compris", "compromis",
    "enterpris", "exercis", "franchis", "disenfranchis", "enfranchis",
    "merchandis", "advertis", "supervis", "improvis", "revis", "devis",
    "advis", "misadvis", "aris", "ris", "sunris", "upris", "wis", "likewis",
    "otherwis", "clockwis", "anticlockwis", "counterclockwis", "paradis",
    "expertis", "chastis", "despis", "excis", "incis", "appris", "repris",
    "treatis", "valis", "anis", "mortis", "tortois", "porpois", "turquois",
    "apprais", "malais", "mayonnais", "televis", "circumcis", "pois",
    "reprais", "assis", "liais", "cris", "iris", "trellis", "premis",
    # An arris is the edge where two surfaces meet. This is a CAD repository.
    "arris",
}


def british(line: str):
    """The first British spelling in a line, or None."""
    for pat in (_PLAIN, _ANALYSE):
        m = pat.search(line)
        if m:
            return m.group(0)
    for m in _ISE.finditer(line):
        # An identifier carries its English word in its LAST part:
        # `resolve_advised` is advised, and `_digitise` is not any of these.
        # Compared part by part rather than with endswith, because `organis`
        # ends with `anis` and would have been let through.
        stem = m.group(1).lower().split("_")[-1]
        # An English word keeps its prefix: `unsupervised` is supervised.
        # Stripped from an explicit list rather than by shape, because
        # `disguis` is itself English and `organis` is not `re` + `ganis`.
        for pre in ("un", "re", "non", "pre", "mis", "over", "under", "self"):
            if stem.startswith(pre) and stem[len(pre):] in _FINE:
                stem = stem[len(pre):]
                break
        # Anything -wise is English: clockwise, piecewise, blockwise,
        # elementwise. No British -ise word ends in `wis`.
        if stem.endswith("wis"):
            continue
        if stem not in _FINE:
            return m.group(0)
    return None

#: Trees that are not ours to spell, or that hold records rather than source.
SKIP_PARTS = {".git", "__pycache__", ".venv", "captures", "results"}

LOOK_AT = (".py", ".md", ".txt", ".json", ".html", ".js", ".css", ".sh", ".c",
           ".h", ".rs", ".toml", ".cfg", ".conf", ".yml", ".yaml", ".ini")

#: Whole files that are records rather than prose. What was written down at
#: the time is what was written down at the time.
SKIP_FILES = set()

#: Lines allowed to say it, each because it names an old recorded key that
#: still has to be readable. Matched on the whole stripped line.
ALLOWED = (
    "centre_s:'Time back at center (s)',",
    "and was `centre_s` before it, and three captures were recorded under the",
)


def offenders(root: pathlib.Path) -> list:
    out = []
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in LOOK_AT:
            continue
        rel = f.relative_to(root)
        if any(p in SKIP_PARTS for p in rel.parts):
            continue
        if str(rel) in SKIP_FILES:
            continue
        if rel.name == "test_american.py":
            continue                       # it has to spell them to find them
        try:
            text = f.read_text()
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if line.strip() in ALLOWED:
                continue
            hit = british(line)
            if hit:
                out.append(f"{rel}:{i}: {hit}  |  {line.strip()[:70]}")
    return out


def test_no_british_spelling_anywhere():
    bad = offenders(ROOT)
    assert not bad, (f"{len(bad)} British spellings:\n  "
                     + "\n  ".join(bad[:25]))


def test_the_guard_can_actually_see_one():
    """A checker that matches nothing passes forever. This is the proof that
    it is looking, and every line here was a real miss at some point."""
    for bad in ("the colour of the centre", "normalising the analogue signal",
                "a grey neighbour, unrecognised", "def _digitise(x):",
                "def test_quantisation_step():", "centring the fibre",
                "a labelled 5 millimetre programme", "fulfil the licence",
                "analysed and catalogued", "mould in the storey"):
        assert british(bad) is not None, bad


def test_the_guard_does_not_cry_about_words_that_are_the_same():
    """`analysis` and `analyses` are spelled the same on both sides, and an
    -ise blanket eats noise, precise, promise, exercise and forty more. Every
    word here was a false alarm the first time this was written."""
    for ok in ("analysis", "analyses", "noise", "precise", "promise",
               "premise", "exercise", "otherwise", "raise", "raised", "wise",
               "concise", "expertise", "supervise", "supervisor", "improvise",
               "characteristic", "realistic", "realism", "specialist",
               "organism", "organist", "emphasis", "basis", "crisis",
               "crises", "iris", "advertisement", "praiseworthy",
               "fulfillment", "instill", "distill", "enroll", "installment",
               "dialogue", "moisture", "revision", "precision"):
        assert british(ok) is None, ok


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = []
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as e:
            failed.append(t.__name__)
            print(f"  FAIL  {t.__name__}: {e}")
            if "-v" in sys.argv:
                traceback.print_exc()
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
