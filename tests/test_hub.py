"""What the hub and its page must be true about.

    python3 tests/test_hub.py

No device, no BLE, no network. Three things are checked here that nothing else
can check: that the runner arms a recording without touching the instrument,
that the page renders from what the device declared rather than from a list
kept in the page, and that the server answers only its own machine.
"""
from __future__ import annotations
import asyncio, inspect, json, pathlib, re, shutil, subprocess, sys, types, \
       tempfile, traceback, warnings

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import hub                                        # noqa: E402
from intomind import analysis as A                # noqa: E402
from intomind import experiments as X             # noqa: E402
from intomind import export as EXPORT             # noqa: E402
from intomind import instrument as I              # noqa: E402
from intomind import protocol as P                # noqa: E402
from intomind import provenance as prov           # noqa: E402
from html.parser import HTMLParser                # noqa: E402
from tests_support import (FULL, FakeDev, device_info,  # noqa: E402
                           fake_prediction, fake_sample)

HUB_HTML = (pathlib.Path(__file__).resolve().parent.parent
            / "hub.html").read_text()
HUB_PY = (pathlib.Path(__file__).resolve().parent.parent
          / "hub.py").read_text()
DOCS = pathlib.Path(__file__).resolve().parent.parent / "docs"
ROOT = pathlib.Path(__file__).resolve().parent.parent


def make_hub(*, dc=1500.0, info=None, config=None):
    h = hub.Hub.__new__(hub.Hub)     # skip __init__: no Session, no radio
    h.dev = FakeDev(dc=dc, info=info)
    h.dev.config = dict(config if config is not None else
                        dict(gain=12, rate_sps=500, mode="normal",
                             leadoff=False))
    h.running = None
    h.live = False
    h.connecting = False
    h._applied = {}
    h.ctx = None
    h._gain = 12
    h._rate = 500
    h._cfg_err = None
    h.prediction = None
    h.heard = {}
    h.scanning = False
    h._stop_scan = None
    h._scan_task = None
    h.contact_enabled = False
    h._contact = [None, None]
    h._batch = [[], []]
    h._loff = []
    h.clients = set()
    h.emitted = []

    async def emit(m):
        h.emitted.append(m)
    h.emit = emit
    return h


CONSENT = {"who": "ada", "granted_utc": "2026-07-10T06:00:00Z"}


def run(coro):
    return asyncio.run(coro)


class Req:
    """The two things a route reads off a request."""

    def __init__(self, body=None, **match):
        self._body = body or {}
        self.match_info = match
        self.headers = {}

    async def json(self):
        return self._body


def body_of(response):
    return json.loads(response.body)


def refused(fn, *a, **kw):
    """The status a route refused with, or None if it did not refuse."""
    try:
        run(fn(*a, **kw))
    except hub.web.HTTPException as e:
        return e.status
    return None


# ------------------------------------------- experiments never set settings
def test_a_run_arms_without_configuring_the_instrument():
    """The settings the user set are the settings they get at Record. A run
    reads the configuration and records at it. It issues no configuration
    command of its own."""
    h = make_hub()
    h.dev.commands.clear()
    cfg = run(h.prepare_run(X.REGISTRY["berger"], {}, "basic", {}))
    assert h.dev.commands == [], f"the run configured the device: {h.dev.commands}"
    assert cfg["config_before"]["gain"] == 12


def test_the_manifest_records_the_configuration_the_device_reported():
    """Whatever the instrument is set to at Record is what the capture
    records, read back from the device rather than remembered."""
    h = make_hub(config=dict(gain=6, rate_sps=250, mode="test", leadoff=True))
    cfg = run(h.prepare_run(X.REGISTRY["berger"], {}, "advanced", {}))
    assert cfg["gain"] == 6 and cfg["rate"] == 250
    assert cfg["signal_source"] == "test"
    assert cfg["effective_settings"]["gain"] == 6


def test_the_manifest_never_claims_a_register_read():
    """There are no registers in this contract, so nothing here may write a
    key that says there were. A manifest carrying `regs_before` from this
    application would be claiming a read that did not happen."""
    h = make_hub()
    cfg = run(h.prepare_run(X.REGISTRY["berger"], {}, "basic", {}))
    assert "regs_before" not in cfg and "regs" not in cfg
    assert "rld" not in cfg, "the bias drive setting is not part of this contract"


def test_the_run_model_has_no_advice_machinery():
    """The advised/resolve_advised auto-apply is gone, root and branch."""
    assert not hasattr(I, "resolve_advised")
    assert not hasattr(I, "AdviceViolation")
    for name, spec in X.REGISTRY.items():
        assert "advised" not in spec and "free" not in spec, \
            f"{name} still declares setting opinions: {spec}"


def test_enforcement_mechanism_still_refuses_a_declared_precondition():
    """check_enforced is retained: an unsafe precondition still refuses. It
    refuses the run, and it never reconfigures the device."""
    try:
        I.check_enforced({"name": "synthetic",
                          "enforced": {"consent": "required"}}, {})
    except I.EnforcedViolation:
        return
    raise AssertionError("check_enforced no longer refuses a declared precondition")


def test_registry_declares_nothing_enforced_per_run():
    """The per-run consent gates are gone. No protocol declares an enforced
    setting now, and the real guard is that runs start only from here.
    `check_enforced` remains as a mechanism, tested above."""
    for name, spec in X.REGISTRY.items():
        assert not spec["enforced"], \
            f"{name} still declares an enforced per-run setting: {spec['enforced']}"


def test_no_experiment_declares_a_gate_that_nothing_evaluates():
    """Every experiment used to carry a `gates=[...]` list and NOTHING EVER
    EVALUATED IT. prepare_run never read it, and the API shipped it to a
    browser that rendered an empty div. A safety mechanism that does not run,
    taking credit for catching failures it cannot catch, is a lie a reader
    will believe.

    If gates ever come back, they come back with something that RUNS them."""
    for name, spec in X.REGISTRY.items():
        assert "gates" not in spec, (
            f"{name} declares gates {spec.get('gates')!r}. Nothing evaluates "
            f"gates. Either wire them into prepare_run and prove it with a "
            f"test, or do not declare them.")


def test_a_clean_run_records_its_preregistered_plan():
    h = make_hub()
    cfg = run(h.prepare_run(X.REGISTRY["berger"], {"consent": CONSENT},
                            "basic", {}))
    assert cfg["preregistered_analysis"]["test"] == "wilcoxon_paired_blocks"
    assert cfg["effective_settings"]["gain"] == 12


def test_there_is_no_preflight_and_nothing_can_refuse_a_run():
    """Nobody asked for the seven checks and they were the root of the
    trouble: a probe on every Run, a report panel, override machinery, and a
    contact test that pushed current through the electrodes while they were
    measuring. All of it is gone. Press Run and it records."""
    for dead in ("preflight", "take_probe", "Probe", "gate_contact",
                 "gate_rld_regulating", "gate_mains_below", "GateReport"):
        assert not hasattr(I, dead), f"{dead} is back"
    assert not hasattr(hub, "GateFailure"), "a run can be refused again"


# ---------------------------------------------------------------- the dead layer
def test_the_register_layer_is_gone_and_nothing_stubs_it():
    """The register level is not part of this contract. A stub that answers
    with an empty dict is worse than nothing: it reports a device with no
    settings, which is a different claim from having no register access."""
    for dead in ("_refresh_regs_usb", "refresh_filter", "set_filters",
                 "enable_contact_detection"):
        assert not hasattr(hub.Hub, dead), f"Hub.{dead} survived"
    for dead in ("api_regs", "api_filter", "GENERIC_PROFILE", "DEVICE_PROFILES"):
        assert not hasattr(hub, dead), f"{dead} survived"
    for dead in ("intomind.shell", "decode_state", "RLD_PRESETS", "set_rld",
                 "get_regs", "get_rldout", "get_filters", "has_register_read",
                 "has_bias_control", "has_configurable_filters", "_regs"):
        assert dead not in HUB_PY, f"hub.py still reaches for {dead}"


def test_nothing_in_the_page_displays_the_layer_that_was_cut():
    """Deleting a route and leaving the control that called it produces a
    button that fails. The panel goes with the route."""
    for dead in ("dfHp", "dfLp", "dfBands", "dfApply", "dfAddBand", "dfSummary",
                 "regionTabs", "syncFilterPanel", "renderFilterSummary",
                 "/api/filter", "/api/regs", "bias_drive", "cfgRld",
                 "m.shell", "m.regs", "asleep"):
        assert dead not in HUB_HTML, f"the page still carries {dead}"


def test_there_is_one_control_path_and_no_console_behind_it():
    """A second control path is a second answer about what the instrument is
    set to, and the two drift. The link is the only one."""
    h = make_hub()
    assert not hasattr(h, "shell")
    assert "shell" not in json.dumps(h.status()), \
        "the status still advertises a console"
    assert "Shell" not in HUB_PY and "shell" not in HUB_PY, \
        "hub.py still knows about a console"


# ------------------------------------------- capabilities are the device's answer
def test_the_app_holds_no_table_of_specific_hardware():
    """Capabilities are the device's own answer, not a lookup in the
    application. An app that keeps a table of specific hardware cannot be
    extended by code it does not ship."""
    class Anon:
        pass
    gen = hub.profile_for(Anon())
    assert gen["controls"] == {}, \
        "an object that declares nothing is given controls it never claimed"


def test_the_controls_the_page_gets_are_the_devices_own_profile():
    """`capabilities()` passes the device's profile through. Anything it
    composed itself would be this application describing hardware again."""
    h = make_hub()
    caps = h.capabilities()
    declared = h.dev.profile()["controls"]
    assert set(caps["controls"]) == set(declared), \
        "the application added or dropped a control the device declared"
    for k, v in declared.items():
        got = dict(caps["controls"][k])
        # Only this application's own answer is added, and nothing the device
        # said is changed on the way through.
        assert got.pop("settable") in (True, False)
        assert got == v, f"{k} was rewritten on the way to the page"
    assert set(caps["controls"]) >= {"gain", "sample_rate", "signal_source",
                                     "leadoff", "predictions"}
    assert caps["channels"] == 4 and caps["rates"] == [250, 500, 1000]
    assert caps["transport"] == "bluetooth-le"


def test_a_narrower_device_gets_a_narrower_control_set():
    """The proof that the profile is read rather than assumed: a device that
    claims nothing optional offers gain and rate and not one control more."""
    h = make_hub(info=device_info(channels=2, capabilities=(), rates=(250,)))
    caps = h.capabilities()
    assert set(caps["controls"]) == {"gain", "sample_rate"}
    assert caps["controls"]["sample_rate"]["options"] == [250]
    assert caps["channels"] == 2


def test_the_page_holds_no_gain_rate_or_mode_list():
    """The page renders whatever the device reported. A list of gains, rates
    or modes in the page describes one device and misdescribes the rest."""
    script = HUB_HTML[HUB_HTML.index("<script>"):]
    for dead in ("[1,2,4,6,8,12,24]", "[250,500,1000]", "GAIN_OPTIONS",
                 "RATE_OPTIONS", "'normal','test','short'"):
        assert dead not in script, f"the page carries a fixed list: {dead}"
    r = script[script.index("function renderControls"):]
    r = r[:r.index("\nasync function applyConfig")]
    assert "Object.keys(ctl)" in r, \
        "renderControls iterates over something other than the device's controls"
    assert "ctl[k].options" in r, "the options are not the device's options"
    assert "supported!==false" in r, \
        "a control the device says it does not support would still render"


def test_a_control_with_no_options_is_not_rendered_as_a_select():
    """`predictions` is a panel, not a dropdown. Rendering it as a select
    would produce an empty one, which is a control that does nothing."""
    r = HUB_HTML[HUB_HTML.index("function renderControls"):]
    r = r[:r.index("\nasync function applyConfig")]
    assert "Array.isArray(ctl[k].options)" in r, \
        "a control with no options would render as an empty select"


def test_the_mode_decides_what_may_change_and_never_what_may_be_seen():
    """Recording with a test signal, or with current flowing through the
    electrodes, is exactly the thing a reader has to be able to find out. A
    mode that hid it would let a recording misrepresent itself to the person
    who made it."""
    r = HUB_HTML[HUB_HTML.index("function renderControls"):]
    r = r[:r.index("\nasync function applyConfig")]
    assert "const editable=k=>{" in r, \
        "the mode still decides visibility rather than editability"
    assert "for(const k of names){" in r, \
        "some controls are filtered out of the panel entirely"
    body = r[r.index("for(const k of names){"):]
    assert "if(!editable(k)){" in body and "class=kv" in body, \
        "a control that cannot be changed here is not stated here either"


def test_a_setting_the_device_does_not_report_leaves_no_panel_behind():
    """A panel for something the device cannot do reads as a fault in the
    device. Contact and the model are both declared, so both are conditional."""
    assert "id=contactCol hidden" in HUB_HTML
    assert "id=modelCol hidden" in HUB_HTML
    assert "$('#contactCol').hidden=!(caps.controls||{}).leadoff" in HUB_HTML


def test_the_trace_is_labeled_from_the_packet_the_library_actually_decodes():
    """The hub read `packet.rate` off a packet that carries `sample_rate_hz`,
    which is an AttributeError on the first notification and therefore a trace
    with no rate and a window that is not the number of seconds it says. Built
    from the protocol's own decoder so the field cannot drift again."""
    h = make_hub()
    h.rec = X.Recorder()
    # Packed with the protocol's own struct, so a header change breaks this
    # test rather than passing over a packet nothing can decode.
    raw = P._DATA.pack(P.PACKET_EEG, 0, 0, 1000, 12345, 1, 0,
                       P.CODE_BY_GAIN[12], P.CODE_BY_RATE[500])
    pkt = P.decode_packet(raw + b"\x00\x01\x02" * 4, channels=4)
    h.on_packet(h.dev, pkt)
    assert h._gain == 12 and h._rate == 500
    assert h.actual_state()["rate"] == 500


def test_configuration_is_verified_against_the_devices_own_read_back():
    """A write the device rounded or refused has to surface at the moment it
    is applied, not in a manifest that says something the instrument was not."""
    h = make_hub()
    applied = run(h.configure(dict(gain=6, sample_rate=250,
                                   signal_source="test")))
    assert applied == dict(gain=6, sample_rate=250, signal_source="test")
    assert h.actual_state()["gain"] == 6 and h.actual_state()["rate"] == 250

    async def deaf(_g):
        pass                           # accepts the write, changes nothing
    h.dev.set_gain = deaf
    try:
        run(h.configure(dict(gain=24)))
    except RuntimeError as e:
        assert "device reports" in str(e)
        return
    raise AssertionError("a setting the device did not take was reported applied")


def test_a_mains_region_at_250_carries_only_the_bands_the_rate_can_represent():
    """At 250 samples a second a region's higher harmonics sit above what
    the rate can represent, and the device refuses them. The region is the
    bands that fit: 48 to 52 and 98 to 102 Hz for 50 Hz mains, 58 to 62 Hz
    for 60 Hz."""
    h = make_hub(config=dict(rate_sps=250))
    run(h.dev.refresh_config())
    for region, bands in (("50", [(480, 520), (980, 1020)]), ("60", [(580, 620)])):
        assert run(h.configure(dict(mains=region))) == dict(mains=region)
        st = h.actual_state()
        assert st["mains"] == region
        notches = [tuple(s["params"]) for s in st["processing"]["stages"] if s["name"] == "notch"]
        assert notches == bands, (region, notches)


def test_the_mains_region_and_the_chain_are_applied_read_back_and_the_live_view_resumes():
    """The device runs the chain and refuses a change while streaming, so
    the hub pauses the live view, applies, resumes, and reads back what the
    device says it runs. Never a raw-or-filtered flag: the words say what
    the signal is."""
    h = make_hub()
    run(h.dev.refresh_config())
    assert h.actual_state()["mains"] == "both", "the device's default notches both mains"
    assert h.actual_state()["processing"]["origin"] == "default"

    h.live = True
    h.dev.streaming = True
    h.rec = types.SimpleNamespace(armed=False)
    applied = run(h.configure(dict(mains="60")))
    assert applied == dict(mains="60")
    assert h.live and h.dev.streaming, "the live view resumed"
    st = h.actual_state()
    assert st["mains"] == "60"
    names = [s["name"] for s in st["processing"]["stages"]]
    assert names == ["highpass", "notch", "notch", "notch", "lowpass"], names
    assert st["processing"]["description"].startswith("high-pass 0.5 Hz, notch 58 to 62 Hz")

    applied = run(h.configure(dict(processing=dict(highpass_hz=1.0, lowpass_hz=40, notches=[[48, 52]]))))
    assert [s["name"] for s in applied["processing"]] == ["highpass", "notch", "lowpass"]
    assert h.actual_state()["processing"]["description"] == "high-pass 1 Hz, notch 48 to 52 Hz, low-pass 40 Hz"
    assert h.actual_state()["mains"] == "custom"

    assert run(h.configure(dict(processing="natural"))) == dict(processing="natural")
    assert h.actual_state()["processing"]["description"] == "natural signal"
    assert h.actual_state()["mains"] == "none"
    assert run(h.configure(dict(processing="default"))) == dict(processing="default")
    assert h.actual_state()["processing"]["origin"] == "default"

    # A chain the device would refuse is refused in words, before the wire.
    try:
        run(h.configure(dict(processing=dict(highpass_hz=0.5, lowpass_hz=400))))
    except Exception as e:
        assert "nine tenths" in str(e), e
    else:
        raise AssertionError("a corner above Nyquist was applied")


def test_a_device_that_runs_no_chain_is_not_asked_for_one():
    h = make_hub(info=device_info(capabilities=tuple(c for c in FULL if c != "pipeline")))
    run(h.dev.refresh_config())
    assert h.actual_state()["processing"] is None and h.actual_state()["mains"] is None
    assert "processing" not in h.capabilities()["controls"]
    try:
        run(h.configure(dict(mains="50")))
    except RuntimeError as e:
        assert "no processing chain" in str(e)
    else:
        raise AssertionError("a device without a chain was sent one")
    assert h.dev.commands == []


def test_a_control_this_app_cannot_apply_is_stated_and_never_offered():
    """A device may declare a control the library has no setter for. Showing
    its value is honest and offering a dropdown that reaches nothing is not,
    so the server says which ones it can drive and the page reads that."""
    h = make_hub()
    ctl = h.capabilities()["controls"]
    assert ctl["gain"]["settable"] is True
    assert ctl["predictions"]["settable"] is False, \
        "predictions is a panel, and the settings list must not claim it"
    try:
        run(h.configure(dict(impedance_range="low")))
    except RuntimeError as e:
        assert "cannot set impedance_range" in str(e)
    else:
        raise AssertionError("a setting with no setter behind it was accepted")
    r = HUB_HTML[HUB_HTML.index("function renderControls"):]
    r = r[:r.index("\nasync function applyConfig")]
    assert "ctl[k].settable===false" in r, \
        "the page offers a control the server cannot apply"


# ---------------------------------------------------------------- the model
def test_a_device_with_no_model_answers_that_it_has_none():
    h = make_hub(info=device_info(capabilities=("leadoff",)))
    assert run(h.model()) == dict(supported=False)


def test_a_device_with_a_model_reports_its_heads():
    h = make_hub()
    m = run(h.model())
    assert m["supported"] is True and m["ready"] is True
    assert m["embedding"] == 64 and m["slots"] == 4
    slots = {x["slot"]: x for x in m["heads"]}
    assert slots[0]["name"] == "focus" and slots[0]["usable"] is True
    assert slots[1]["usable"] is False, "an empty slot is offered as selectable"


def test_a_head_can_be_selected_and_predictions_turned_on_and_off():
    h = make_hub()
    assert run(h.select_head(1))["active_head"] == 1
    assert run(h.set_predictions(True))["predictions_on"] is True
    assert run(h.set_predictions(False))["predictions_on"] is False
    assert h.prediction is None, "a stale prediction survived predictions off"


def test_a_prediction_carries_the_head_that_produced_it():
    """Two heads give different answers about the same four seconds, so an
    output without its head is not a result. The flags ride along too: a
    window with a gap in it produced that number and the page has to say so."""
    h = make_hub()
    h._loop = asyncio.new_event_loop()
    try:
        h.on_prediction(h.dev, fake_prediction(slot=2, gap=True))
    finally:
        h._loop.close()
    p = h.prediction
    assert p["head_slot"] == 2 and p["head_id"] == "aabbccdd11223344"
    assert p["outputs"] == [0.9, 0.05, 0.05]
    assert p["gap_in_window"] is True
    assert h.status()["prediction"]["head_slot"] == 2
    # Over generated signal, the device's own mark reaches the page.
    h._loop = asyncio.new_event_loop()
    try:
        h.on_prediction(h.dev, fake_prediction(slot=2, source="synthetic"))
    finally:
        h._loop.close()
    assert h.prediction["input_source"] == "synthetic"


def test_the_model_panel_appears_only_when_the_device_claims_a_model():
    """Rendered from the capability, not from whether a head happens to be
    present: a device with a model and no heads still has a model panel, and
    one without a model has none."""
    s = HUB_HTML[HUB_HTML.index("function syncModelPanel"):]
    s = s[:s.index("async function loadModel")]
    assert "controls||{}).predictions" in s, \
        "the model panel is shown on something other than the capability"
    assert "$('#modelCol').hidden=!has" in s

    assert "predictions" in make_hub().capabilities()["controls"]
    narrow = make_hub(info=device_info(capabilities=("leadoff",)))
    assert "predictions" not in narrow.capabilities()["controls"]


def test_the_prediction_readout_names_its_head_and_its_flags():
    s = HUB_HTML[HUB_HTML.index("function renderPrediction"):]
    s = s[:s.index("\n}")]
    assert "head_slot" in s and "head_id" in s
    for flag in ("gap_in_window", "leadoff_in_window", "duty_reduced", "input_source==='synthetic'"):
        assert flag in s, f"a prediction's {flag} is not shown"


def test_the_page_does_not_offer_to_train_a_head():
    """A head is trained from a recording by the library. Offering it here
    would be a second implementation of the thing that has to match the device
    exactly, so the page says where it happens instead."""
    assert "/api/train" not in HUB_HTML and "trainHead" not in HUB_HTML
    s = HUB_HTML[HUB_HTML.index("function renderModel"):]
    s = s[:s.index("function renderPrediction")]
    assert "instrument library" in s, \
        "the page does not say where a head comes from"


# ---------------------------------------------------------------- exports
def _exports_tmp():
    d = pathlib.Path(tempfile.mkdtemp(prefix="cc_exports_"))
    hub.EXPORTS = d
    return d


def _capture_on_disk(label="t_cc_export", n=1500, nch=2):
    """A real capture in a temporary captures directory, so the export route
    is exercised end to end rather than against a stub. The signal has a
    millivolt excursion on purpose: a fixture that sat at zero would make a
    16-bit container look lossless."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="cc_captures_"))
    prov.use_captures_dir(tmp / "captures")
    pathlib.Path(A.CAPTURES).mkdir(parents=True, exist_ok=True)
    fs, vref, gain, bits = 500.0, 2_420_000, 12, 24
    lsb = (2.0 * vref) / (gain * (1 << bits))
    t = np.arange(n) / fs
    counts = np.stack([
        ((1500.0 * (c + 1) + np.linspace(0, 8000.0, n)
          + 20.0 * np.sin(2 * np.pi * 10 * t)) / lsb).astype(np.int64)
        for c in range(nch)])
    idx = np.arange(n, dtype=np.int64)
    np.savez_compressed(
        pathlib.Path(A.CAPTURES) / f"{label}.npz", counts=counts, index=idx,
        device_ticks=idx, host_time=idx / fs,
        leadoff=np.zeros(n, np.uint8), gap_before=np.zeros(n, bool))
    (pathlib.Path(A.CAPTURES) / f"{label}.meta.json").write_text(json.dumps(
        dict(label=label, n=n, fs_effective=fs, vref_uv=vref, gain=gain,
             adc_bits=bits, device_name="fixture")))
    prov.write_checksums(label)
    return label, tmp


def test_every_format_the_library_writes_is_offered():
    """The page offers what the library can write, fetched rather than listed
    in the page, so a format it gains is not one the page has to be told."""
    offered = body_of(run(hub.api_formats(Req())))
    assert [x["name"] for x in offered] == list(EXPORT.FORMATS)
    assert len(EXPORT.FORMATS) == 6, "six formats, and the page offers all six"
    assert "let FORMATS=[]" in HUB_HTML and "api('/api/formats')" in HUB_HTML


def test_the_export_route_writes_through_the_library_and_nothing_else():
    """One exporter. A second writer here would be a second set of rules about
    what a capture means, and the two would drift."""
    src = inspect.getsource(hub.api_export)
    assert "EXPORT.export" in src
    for own in ("np.savez", "csv.writer", "open(", "write_text"):
        assert own not in src, f"api_export writes a file itself: {own}"
    assert "EDF." not in HUB_PY, "the hub still calls a format writer directly"


def test_an_export_reports_what_it_measured_on_the_file_it_wrote():
    """Every format answers with the same keys plus a measurement of what it
    cost on THIS capture, and the route passes that through unchanged. A
    24-bit container returns the counts and 16-bit EDF+ does not, and both say
    so with a number rather than a claim about the format in general."""
    label, tmp = _capture_on_disk()
    out = _exports_tmp()
    try:
        summaries = {}
        for fmt in EXPORT.FORMATS:
            r = body_of(run(hub.api_export(
                Req({"labels": [label], "format": fmt}))))
            assert r["errors"] == [], r["errors"]
            s = r["exported"][0]
            summaries[fmt] = s
            assert (out / f"{label}{EXPORT.SUFFIX[fmt]}").exists(), \
                f"{fmt} reported success and wrote no file"
            for key in ("label", "path", "format", "channels", "samples",
                        "sample_rate_hz", "lossy", "counts_recovered"):
                assert key in s, f"the {fmt} summary has no {key}"
        assert summaries["bdf"]["counts_recovered"] is True, \
            "24 bits did not return a 24-bit record"
        assert summaries["npz"]["counts_recovered"] is True
        assert summaries["edf"]["lossy"] is True, \
            "EDF+ is 16 bits over a 24-bit record and has to say so"
        assert summaries["edf"]["max_error_uv"] > summaries["bdf"]["max_error_uv"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


def test_the_export_cli_uses_the_same_exporter_as_the_page():
    """A file written from the command line and a file written from the page
    have to be the same file, which they are not if two writers produce them.
    The CLI also has to be refused by the same provenance gate."""
    cli = ROOT / "tools" / "export.py"
    assert cli.exists(), "the export CLI is gone"
    src = cli.read_text()
    assert "from intomind import export" in src and "export.export(" in src
    assert "from intomind import edf" not in src, \
        "the CLI writes a format directly, behind the exporter's refusal"
    assert "export.FORMATS" in src, "the CLI offers a list of its own"
    assert not (ROOT / "tools" / "export_edf.py").exists(), \
        "a CLI named for one format still exports only that format"


def test_the_page_states_the_measured_cost_rather_than_a_claim():
    s = HUB_HTML[HUB_HTML.index("function exportSummary"):]
    s = s[:s.index("async function doExport")]
    assert "counts_recovered" in s and "max_error_uv" in s
    assert "lsb_uv" in s, "an error with no LSB beside it is not a scale"
    assert "omitted" in s, "an array that was not written would go unmentioned"


def test_an_unknown_format_is_refused_with_the_ones_that_exist():
    assert refused(hub.api_export, Req({"labels": ["x"], "format": "wav"})) == 400
    assert refused(hub.api_export, Req({"labels": [], "format": "npz"})) == 400


def test_a_label_is_a_name_and_never_a_path():
    """The only thing between a label from a browser and the filesystem."""
    for bad in ("../secret", "a/b", "..\\b", ".hidden", ""):
        assert not hub._safe_label(bad), f"{bad!r} was accepted as a label"
    assert hub._safe_label("berger_2026-08-19")
    assert refused(hub.api_download,
                   Req(fmt="npz", label="../../etc/passwd")) == 400
    assert refused(hub.api_download, Req(fmt="wav", label="a")) == 400


def test_a_download_comes_from_the_exports_directory_not_the_captures():
    """A capture is checksummed and frozen and an export is regenerable. A
    directory holding both invites a ledger that counts the wrong files."""
    src = inspect.getsource(hub.api_download)
    assert "EXPORTS" in src and "A.CAPTURES" not in src


# ---------------------------------------------------------------- lead-off
def test_contact_current_never_runs_in_the_background():
    """The test current is pushed through the very electrodes that are
    measuring microvolts. On a marginal electrode it rails the input and
    flattens the channel. It runs for an explicit check, or when the operator
    turns it on, and never by default."""
    h = hub.Hub.__new__(hub.Hub)
    h.contact_enabled = False
    assert h.contact_enabled is False
    src = inspect.getsource(hub.Hub.connect) + inspect.getsource(hub.Hub._connect)
    assert "set_leadoff(True)" not in src and "set_leadoff(1)" not in src, \
        "lead-off is switched on at connect again"
    assert "set_leadoff" in inspect.getsource(hub.Hub.check_contact)


def test_quiesce_clears_leadoff_on_connect():
    """A known state has to be made known, not assumed.

    Lead-off lives in the device and survives the host restarting, the app
    reconnecting and the link dropping. Contact detection was enabled at
    connect once, and deleting that line did NOT undo what it had written, so
    the current kept flowing into every electrode for days. The host asserts
    the state it wants at connect rather than inheriting whatever the last
    session left behind.

    This test is the reason that can never silently regress again.
    """
    h = hub.Hub.__new__(hub.Hub)
    h.dev = FakeDev()
    h.running = None
    h.live = False
    h.clients = set()
    h.contact_enabled = True         # as a previous session left it
    h._contact = [True, True]        # and a stale cached verdict
    h._applied = {}
    h._loff = []

    async def emit(m): pass
    h.emit = emit

    h.dev.streaming = True           # a stale stream from an earlier process
    run(h.quiesce())

    assert (P.OPCODES["set_leadoff"], 0) in h.dev.commands, \
        "quiesce() did not turn lead-off OFF. The device is still pushing " \
        "current into every electrode, and no amount of host-side code " \
        "deletion undoes a write to the device."
    assert h.contact_enabled is False, "stale contact-detection flag survived connect"
    # One indicator per channel the device has, all of them unknown. Two was
    # the first instrument's count, written here as a constant, which on a
    # wider device stopped reporting the electrodes past the second.
    assert h._contact == [None] * h.dev.info.channels, \
        "a cached contact verdict survived connect"


def test_a_contact_check_leaves_the_stream_running():
    """The check must stop the device, because the firmware refuses the change
    while streaming, and then put it back. It did not: `set_live` trusted the
    `live` flag, which still said True while the hardware was stopped, so it
    returned without restarting anything and the trace stayed dead."""
    h = make_hub()
    h.rec = X.Recorder()
    h.live = True
    h.dev.streaming = False          # the device is stopped, as a check leaves it
    run(h.set_live(True))
    assert h.dev.streaming is True, "the stream was not restarted after a check"
    assert h.live is True


def test_contact_can_be_checked_twice_and_the_stream_survives_both():
    """Two failures, both real, both from trusting a flag over the hardware.

    1. The check stops the device, but `set_live` believed the `live` flag
       (still True) and returned without restarting: the trace stayed dead.
    2. The next check then called stop() on an already-stopped device, which
       the device reports as an error, so every check after the first blew up.
    """
    h = make_hub()
    h.rec = X.Recorder()
    h.live = True
    h.dev.streaming = True

    stops, orig_stop = [], h.dev.stop

    async def counting_stop():
        stops.append(h.dev.streaming)      # was it actually streaming?
        await orig_stop()
    h.dev.stop = counting_stop

    # feed the lead-off bits the device would report while the check runs
    import hub as _hubmod
    real_sleep = _hubmod.asyncio.sleep

    async def patched(sec):
        if sec == 1.0:
            h._loff.extend([0x00] * 200)
        await real_sleep(0)
    _hubmod.asyncio.sleep = patched
    try:
        r1 = run(h.check_contact())
        assert h.dev.streaming is True, "stream dead after the first check"
        r2 = run(h.check_contact())
        assert h.dev.streaming is True, "stream dead after the second check"
    finally:
        _hubmod.asyncio.sleep = real_sleep

    assert r1["ok"] and r2["ok"], "a repeat check failed"
    assert all(stops), "stop() was called on a device that was not streaming"
    assert h.contact_enabled is False, "the check left the current on"


def test_the_light_is_a_setting_the_device_confirms_and_identify_is_an_act():
    """The light's level goes through configure like any setting, is read
    back from the device, and shows in the reported configuration; identify
    is a route of its own, refused without a link or on a device without a
    light."""
    h = make_hub()
    applied = run(h.configure(dict(indicator="verbose")))
    assert applied["indicator"] == "verbose"
    assert h.dev.indicator_level == "verbose"
    assert (P.OPCODES["set_indicator"], 2) in h.dev.commands
    run(h.dev.refresh_config())
    assert hub.Hub._reported(h.dev.config)["indicator"] == "verbose"
    try:
        run(h.configure(dict(indicator="loud")))
    except RuntimeError as e:
        assert "level" in str(e)
    else:
        raise AssertionError("a level the contract does not define was applied")
    assert run(h.identify(3)) == dict(seconds=3)
    assert h.dev.identified == [3]
    # A device without a light is not asked.
    h2 = make_hub(info=device_info(capabilities=("battery_voltage",)))
    try:
        run(h2.identify(3))
    except RuntimeError:
        pass
    else:
        raise AssertionError("a device without a light was asked to identify")
    hub.hub.dev = None
    hub.hub.running = None
    assert refused(hub.api_identify, Req(dict(seconds=5))) == 409
    hub.hub.dev = h.dev
    assert refused(hub.api_identify, Req(dict(seconds=99))) == 400
    hub.hub.dev = None


def test_the_registers_are_read_in_words_with_the_live_view_paused():
    """The device answers busy while it converts, so the read stops the
    stream and restarts it; the words are the library's, not the page's."""
    h = make_hub()
    h.live = True
    h.dev.streaming = True
    r = run(h.registers())
    assert r["family_code"] == 1 and len(bytes.fromhex(r["values"])) == 24
    assert r["words"]["data_rate_sps"] == 500
    assert r["words"]["channels"][0]["input"] == "test signal"
    assert h.dev.streaming is True, "the live view was not restarted after the read"
    h2 = make_hub(info=device_info(capabilities=("battery_voltage",)))
    try:
        run(h2.registers())
    except RuntimeError:
        pass
    else:
        raise AssertionError("a device without register reading was asked")


def test_embeddings_are_collected_from_the_live_stream_into_a_file():
    """The device sends embeddings only while it streams, so the collection
    needs the live view; it runs as a job the status reports, turns the form
    off when done, and writes what arrived beside the captures."""
    h = make_hub()
    h.live = False
    try:
        run(h.collect_embeddings(1, "both"))
    except RuntimeError as e:
        assert "live view" in str(e)
    else:
        raise AssertionError("a collection started without a stream")
    h.live = True
    h.dev.streaming = True
    tmp = tempfile.mkdtemp()
    before = A.CAPTURES
    A.CAPTURES = pathlib.Path(tmp)
    try:
        async def go():
            job = await h.collect_embeddings(1, "both")
            assert job["running"] is True and job["target"] == 1
            for _ in range(40):
                await asyncio.sleep(0.05)
                if not h.embeddings_job["running"]:
                    break
            return dict(h.embeddings_job)
        job = run(go())
        assert job["error"] is None, job
        assert job["got"] == 1, "the fake device answers one window per request"
        assert h.dev.embeddings_form == "off", "the form was left on"
        written = json.loads((pathlib.Path(tmp) / job["file"]).read_text())
        assert written["tokens_per_channel"] == 20 and written["channels"] == 4
        assert written["windows"][0]["embedding"] == [1, 2, 3, 4]
        assert len(written["windows"][0]["tokens"]) == 80
        assert h.status()["embeddings"]["file"] == job["file"]
    finally:
        A.CAPTURES = before
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_device_that_cannot_detect_leadoff_is_not_offered_a_contact_check():
    """The route refuses rather than sending an operation the device never
    claimed, which would come back as an unsupported-opcode error."""
    hub.hub.dev = FakeDev(info=device_info(capabilities=()))
    hub.hub.running = None
    try:
        assert refused(hub.api_contact_check, Req()) == 409
    finally:
        hub.hub.dev = None


def test_the_recorder_records_the_streamed_signal():
    """The device streams ONE signal. The recorder records exactly what
    arrives on it, and there is no second stream to reconcile."""
    h = make_hub()
    h.rec = X.Recorder()
    h.rec.armed = True
    seen = []
    h.rec.on_sample = lambda d, s: seen.append(s.uv)
    h.on_sample(h.dev, fake_sample(1.5, -2.5))
    assert seen == [(1.5, -2.5)], "the recorder did not record the streamed sample"


# ---------------------------------------------------------------- the page
def test_the_cue_box_is_hidden_when_nothing_is_cued():
    """An unlabeled box holding one em dash reads as broken."""
    assert "<div id=cue class=hidden></div>" in HUB_HTML
    assert "$('#cue').textContent='—'" not in HUB_HTML


def test_the_button_label_has_exactly_one_source():
    """Messages said "Press Reconnect" while the button read "Connect"."""
    assert "def link_action" in HUB_PY
    body = "\n".join(l for l in HUB_PY.splitlines()
                     if "Press " in l and not l.strip().startswith("#"))
    assert "link_action" in body, "a message still hard-codes the button name"
    assert "m.link_action" in HUB_HTML, "the page does not render the served label"


def test_nothing_connects_by_itself():
    """Ruled 2026-09-28: none of the programs connects automatically; they
    list the devices on the air and connect the one picked. With two devices
    in a room the first to answer is not the one meant."""
    main_src = HUB_PY[HUB_PY.index("async def main("):]
    assert "hub.connect(" not in main_src, "the hub connects on start"
    assert "discover_and_connect" not in HUB_PY, "something still connects whatever answers first"
    assert '"/api/reconnect"' not in HUB_PY
    assert hub.link_action(True) == "Disconnect" and hub.link_action(False) == "Connect"
    h = make_hub()
    h.dev.connected = False
    try:
        run(h.connect("EE:AE:00:00:00:09"))
    except RuntimeError as e:
        assert "not heard" in str(e)
    else:
        raise AssertionError("a device no scan heard was connected")


def test_the_list_names_each_device_and_numbers_twins():
    from types import SimpleNamespace as NS
    h = make_hub()
    h.heard = {a: NS(address=a, name=n) for a, n in [
        ("AA:00:00:00:00:01", "Ada's IntoMind One"), ("AA:00:00:00:00:02", "IntoMind One"),
        ("AA:00:00:00:00:03", "Ada's IntoMind One")]}
    assert [d["label"] for d in h.devices()] == ["Ada's IntoMind One", "IntoMind One", "Ada's IntoMind One 2"]
    assert h.status()["devices"][2]["address"] == "AA:00:00:00:00:03"


def test_the_page_lists_devices_as_text_and_connects_only_the_one_clicked():
    hostile = '"><img src=x onerror=alert(1)>'
    status = dict(connected=False, connecting=False, link_action="Connect", scanning=True,
                  devices=[dict(address="AA:00:00:00:00:01", name="IntoMind One", label="IntoMind One"),
                           dict(address="AA:00:00:00:00:02", name=hostile, label=hostile)],
                  caps={}, actual={})
    out = render_page({"scenario": "picker", "status": status,
                       "routes": {**PAGE_ROUTES, "/api/scan": {"scanning": True, "devices": []},
                                  "/api/connect": {}},
                       "click": "AA:00:00:00:00:02"})
    assert out["picker_open"], "a page opened with nothing connected does not list the devices"
    assert out["link"]["text"] == "Connect"
    markup = out["list"]["html"]
    assert "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;" in markup
    assert _Rendered(markup).handlers == [] and "img" not in _Rendered(markup).tags
    assert out["state"]["text"] == "Listening for more"
    bodies = [c for c in out["clicked"] if c["url"] == "/api/connect"]
    assert len(bodies) == 1 and json.loads(bodies[0]["body"]) == {"address": "AA:00:00:00:00:02"}


def test_another_program_listening_is_said_once_each_way():
    """Measured: the system's Bluetooth service sends each notification once
    per program subscribed. The library drops the copies; the page says why
    the device is being heard twice, and stops saying it when it stops."""
    h = make_hub()
    h.dev.another_listener = True
    assert h.status()["another_listener"] is True
    run(h._note_listener())
    run(h._note_listener())
    notes = [m for m in h.emitted if m.get("type") == "note" and "another program" in m.get("text", "")]
    assert len(notes) == 1, "said once as it starts, not once a packet"
    h.dev.another_listener = False
    run(h._note_listener())
    assert h.emitted[-1]["type"] == "status" and h.emitted[-1]["another_listener"] is False
    s = HUB_HTML[HUB_HTML.index("function onStatus"):]
    assert "#listenNote" in s[:s.index("\n}")] and "id=listenNote" in HUB_HTML


def test_disconnect_asks_first():
    s = HUB_HTML[HUB_HTML.index("$('#linkBtn').onclick"):]
    s = s[:s.index("};")]
    assert "confirm(" in s and "/api/disconnect" in s


def test_there_is_a_disconnect_and_it_is_reachable():
    assert "async def api_disconnect" in HUB_PY
    assert '"/api/disconnect"' in HUB_PY
    assert "async def disconnect" in HUB_PY
    assert "id=linkBtn" in HUB_HTML


def test_delete_deletes():
    """Not a void flag, not a rename: the files go."""
    assert "async def api_delete" in HUB_PY
    assert '"/api/delete"' in HUB_PY
    d = HUB_PY[HUB_PY.index("async def api_delete"):]
    d = d[:d.index("\n\n\n")] if "\n\n\n" in d else d
    assert ".unlink()" in d, "api_delete does not remove anything"
    for ext in (".npz", ".meta.json", ".events.json", ".sha256"):
        assert ext in d, f"api_delete leaves {ext} behind"


def test_analysis_is_chosen_from_the_recording():
    assert "EXP2ANA" in HUB_HTML and "autoPickAnalysis" in HUB_HTML
    for exp in ("saccade", "blink", "jaw_clench", "berger"):
        assert exp in HUB_HTML[HUB_HTML.index("const EXP2ANA"):][:400], \
            f"{exp} has no analysis mapping"


def test_summary_and_psd_run_with_every_analysis():
    """They are the standard read of any recording, not alternatives to the
    experiment-specific one, and the PSD is what keeps the chart from being
    blank."""
    h = HUB_HTML[HUB_HTML.index("async function runAnalysis"):]
    h = h[:h.index("$('#analyze').onclick")]
    assert "analysis:'summary'" in h and "analysis:'psd'" in h
    assert "appendStandard" in h


def test_no_analysis_falls_back_to_raw_json():
    """Every analysis in the registry has a renderer, and JSON is not a
    result."""
    r = HUB_HTML[HUB_HTML.index("function render(r)"):]
    r = r[:r.index("function summaryTable")]
    for name in A.ANALYSES:
        key = "psd" if name == "psd" else name
        assert f"_analysis==='{key}'" in r or key == "psd", \
            f"{name} has no renderer and would dump JSON"


def test_export_offers_data_analysis_or_both():
    assert "id=expWhat" in HUB_HTML
    for w in ("data", "both", "analysis"):
        assert f'data-w={w}' in HUB_HTML, f"export tab missing: {w}"


def test_the_dashboard_does_not_scroll_as_a_page():
    """It is a dashboard, not a document. The viewport holds it exactly and
    each panel scrolls inside itself, so the header and footer never leave the
    screen."""
    css = HUB_HTML[:HUB_HTML.index("</style>")]
    assert "height:100vh" in css and "overflow:hidden" in css, \
        "the body no longer pins itself to the viewport"
    assert "min-height:calc(100vh" not in css, \
        "main is sized taller than the space left for it again"
    i = css.index("body{")
    body = css[i:css.index("header,", i)]
    assert "display:flex" in body and "flex-direction:column" in body
    main = css[css.index("main{display:grid"):]
    main = main[:main.index("section{")]
    assert "flex:1 1 auto" in main and "min-height:0" in main, \
        "main cannot shrink, so it will push the footer off the bottom"


def test_selecting_a_recording_analyses_it():
    """Pressing a button to see the obvious next thing is an extra step.
    Selection is the request. Debounced, and served from a cache keyed by
    capture and parameters, so a long recording is not recomputed per click."""
    assert "function scheduleAnalyze" in HUB_HTML
    sel = HUB_HTML[HUB_HTML.index("function setSel()"):]
    sel = sel[:sel.index("/* Pressing a button")]
    assert "scheduleAnalyze()" in sel, "selecting a recording does not analyze it"
    assert "setTimeout" in HUB_HTML[HUB_HTML.index("function scheduleAnalyze"):][:300], \
        "scheduleAnalyze does not debounce"
    assert "_ANA_CACHE" in HUB_PY and "_ana_key" in HUB_PY, \
        "there is no analysis cache, so every click recomputes"
    assert "st_mtime_ns" in HUB_PY, "the cache key ignores the file, so it can go stale"


def test_a_mismatched_analysis_warns_but_a_general_one_does_not():
    """Warn only when the recording HAS an expected analysis and a different
    one is chosen. A general recording has no expected analysis, so any choice
    is legitimate and silence is correct."""
    n = HUB_HTML[HUB_HTML.index("function anaNote()"):]
    n = n[:n.index("function setSel()")]
    assert "if(!want){" in n and "return}" in n, \
        "anaNote warns even when the recording has no expected analysis"
    assert "unlikely" in n, "no warning text for a mismatched analysis"
    assert "'warn'" in n, "the mismatch is not marked as a warning"


def test_ui_text_carries_no_em_dashes():
    """One bare em dash was already a bug. They are not a style here."""
    body = HUB_HTML[HUB_HTML.index("</style>"):]
    body = body[:body.index("<script>")]
    assert "—" not in body, "an em dash is back in the page markup"
    note = HUB_HTML[HUB_HTML.index("function anaNote()"):]
    note = note[:note.index("function setSel()")]
    assert "—" not in note, "the analysis note still uses an em dash"


# ---------------------------------------------------------------- the docs
def test_the_command_center_serves_its_own_documentation():
    """Two audiences, kept apart: operating the app, and extending it. Device
    and API documentation are published separately and are not this
    application's to serve."""
    for page in ("user.html", "dev.html", "_style.css"):
        assert (DOCS / page).exists(), f"missing documentation file: {page}"
    assert "async def api_docs" in HUB_PY
    assert '"/docs/{page}"' in HUB_PY, "the documentation is not routed"
    assert 'href="/docs/user"' in HUB_HTML and 'href="/docs/dev"' in HUB_HTML, \
        "the footer does not link to the documentation"


def test_the_documented_endpoints_are_the_endpoints_that_exist():
    """A documented route that is not routed is a promise the app breaks, and
    a route nobody documented is one nobody can find."""
    dev = (DOCS / "dev.html").read_text()
    routed = {r for r in re.findall(r'web\.(?:get|post)\("(/[^"{]*)"', HUB_PY)
              if r.startswith("/api") or r == "/ws"}
    documented = set(re.findall(r"<code>(?:GET|POST) (/[^<\s]+)</code>", dev))
    documented = {d for d in documented if not d.endswith("}")}
    assert documented <= routed, f"documented but not routed: {documented - routed}"
    assert routed <= documented, f"routed but not documented: {routed - documented}"


def test_the_documentation_describes_no_control_the_app_no_longer_has():
    """Documentation for a deleted control sends a user looking for a panel
    that is not there."""
    for page in ("user.html", "dev.html"):
        text = (DOCS / page).read_text().lower()
        for dead in ("bias drive", "notch band", "signal filters",
                     "mains supply", "/api/filter"):
            assert dead not in text, f"{page} still documents {dead!r}"


def test_the_docs_carry_no_em_dashes_or_prose_semicolons():
    """House style. Code samples keep their own punctuation."""
    for page in ("user.html", "dev.html"):
        text = (DOCS / page).read_text()
        assert "—" not in text, f"{page} contains an em dash"
        prose = re.sub(r"<pre>.*?</pre>", "", text, flags=re.S)
        prose = prose.replace("&copy;", "").replace("&nbsp;", "")
        assert ";" not in prose, f"{page} contains a semicolon in prose"


def _private_words():
    """What nothing published may name: unreleased devices and projects,
    part numbers, register names, people, private repositories.

    The words are themselves what they protect, so they are not written in
    this repository: they live on the maintainers' machines, in the file
    INTOMIND_PRIVATE_WORDS names or ~/.config/intomind/private-words.tsv,
    shared with the library, the SDK and the documentation. One pattern per
    line, then what it is, "fold" or "exact", and an example the check must
    catch, separated by tabs. Without the file this fails and says why: a
    check that passes where it could not look is not a check."""
    import os
    path = pathlib.Path(os.environ.get("INTOMIND_PRIVATE_WORDS",
                                       "~/.config/intomind/private-words.tsv")).expanduser()
    assert path.is_file(), (f"the private word list is not on this machine ({path}), so what "
                            "this repository may publish cannot be checked. Set INTOMIND_PRIVATE_WORDS to it.")
    out = []
    for line in path.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            pattern, why, case, example = line.split("\t")
            out.append(((("(?i)" if case == "fold" else "") + pattern), why, example))
    return out


#: Patterns that give nothing away by being written down.
PUBLIC_WORDS = [(r"(?i)[\w.+-]+@gmail\.com", "a personal address", "write to someone@gmail.com"),
                (r"(?i)/home/[a-z]", "a path on somebody's machine", "/home/someone/project")]


def test_nothing_published_names_what_it_must_not():
    """Every tracked file is published, so every tracked file is read: a part
    number or a person in a source comment ships as far as one on a page.
    The file list comes from git, because a list typed here goes stale the
    first time a file is added. This file writes the public patterns'
    made-up examples, so it is held to the private words alone."""
    private = _private_words()
    listed = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                            text=True, check=True).stdout.split()
    assert len(listed) > 20, "git listed almost nothing, so this read almost nothing"
    problems = []
    for rel in listed:
        try:
            text = (ROOT / rel).read_text()
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        patterns = private if rel == "tests/test_hub.py" else PUBLIC_WORDS + private
        for pattern, why, _example in patterns:
            for m in re.finditer(pattern, text):
                if m.group(0).lower() == "loff_statp":
                    continue          # the contract's own field name
                problems.append(f"{rel} names {m.group(0)!r}, which is {why}")
    assert not problems, "\n  " + "\n  ".join(sorted(set(problems)))


def test_the_guard_can_see_a_name():
    """A check that has never been seen to fail is not a check: every
    pattern catches its own example."""
    for pattern, why, example in PUBLIC_WORDS + _private_words():
        assert re.search(pattern, example), f"{example!r} went unseen by the pattern for {why}"


# ---- came here in the 2026-08-19 split: this reads tools/mark_montage.py,
# which is a capture tool and followed the captures here.
def test_backfill_refuses_captures_whose_prose_disagrees():
    """The guard that stops a montage being pooled across a change of
    electrodes. Checked against the tool's own comparison, not a
    reimplementation."""
    sys.path.insert(0, str(ROOT / "tools"))
    import importlib
    mm = importlib.import_module("mark_montage")
    assert mm._norm("ch1 =  a;\n ch2 = b") == mm._norm("ch1 = a; ch2 = b")
    assert mm._norm("ch1 = a") != mm._norm("ch1 = occipital")


def test_the_hub_answers_only_its_own_machine():
    """A web page anywhere can make the browser POST to 127.0.0.1, and a
    WebSocket handshake is not subject to the same-origin policy at all. The
    middleware is the whole defense. A wrong Host is a rebound DNS name and a
    foreign Origin is another page's script, and neither may reach a handler."""
    class H:
        def __init__(self, **h):
            self.headers = h

    async def handler(r):
        return "reached"

    async def verdict(req):
        try:
            return await hub.same_machine_only(req, handler)
        except hub.web.HTTPForbidden:
            return "forbidden"

    assert run(verdict(H(Host="localhost:8080"))) == "reached"
    assert run(verdict(H(Host="127.0.0.1:8080",
                         Origin="http://localhost:8080"))) == "reached"
    # DNS rebinding: the attacker's name resolves here, and Host carries it
    assert run(verdict(H(Host="evil.example:8080"))) == "forbidden"
    # a foreign page's fetch or WebSocket carries its own Origin
    assert run(verdict(H(Host="localhost:8080",
                         Origin="https://evil.example"))) == "forbidden"
    # a sandboxed or file:// page sends the literal string "null"
    assert run(verdict(H(Host="localhost:8080", Origin="null"))) == "forbidden"
    assert run(verdict(H())) == "forbidden"


# ------------------------------------------------- the page as an attack surface
#
# A capture's metadata is a file on disk, and a server error message quotes
# back whatever was asked for. Both are drawn into the page, and text drawn
# into markup IS markup: a script there runs inside this page's own origin,
# where `same_machine_only` has already said yes. It can call /api/delete, it
# can call /api/run, which starts a recording on somebody wearing electrodes,
# and it can read the stream off /ws. The same-machine guard is the whole
# defense against a page on the internet, and it is no defense at all against
# a payload that came in through the front door.
#
# These run hub.html's OWN script, so what is checked is the renderer that
# ships rather than a description of it that could stay right while the page
# goes wrong.

def _node() -> str:
    """Where node is.

    A hard requirement, not a skip. The page's renderers are JavaScript, and a
    check that quietly does not run is not a check.
    """
    exe = shutil.which("node") or shutil.which("nodejs")
    if exe:
        return exe
    nvm = sorted(pathlib.Path.home().glob(".nvm/versions/node/*/bin/node"))
    if nvm:
        return str(nvm[-1])
    raise AssertionError(
        "node is not on PATH, so the page's own renderers cannot be run and "
        "these checks cannot answer. Install node or put it on PATH.")


#: What the page fetches on start-up. A scenario overrides the one it is about.
PAGE_ROUTES = {"/api/experiments": [], "/api/formats": [], "/api/analyses": [],
               "/api/status": {}, "/api/captures": []}


def render_page(scenario: dict) -> dict:
    """Run the page's own renderers over `scenario` and report what they made."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(scenario, f)
        path = f.name
    try:
        r = subprocess.run([_node(), str(ROOT / "tests" / "page_render.js"), path],
                           capture_output=True, text=True, timeout=120)
    finally:
        pathlib.Path(path).unlink(missing_ok=True)
    assert r.stdout, f"the page render harness said nothing: {r.stderr}"
    out = json.loads(r.stdout)
    assert "harness_error" not in out, out["harness_error"]
    assert r.returncode == 0, f"the page render harness failed: {r.stderr}"
    return out


class _Rendered(HTMLParser):
    """What a browser would actually build out of a rendered string.

    A substring check answers whether a payload is present. This answers the
    question that matters: whether the browser would make an ELEMENT out of
    it, and whether any attribute on it is a handler. An escaped payload still
    contains the letters `onerror=`, harmlessly, as text.
    """

    def __init__(self, markup):
        super().__init__()
        self.tags, self.handlers = [], []
        self.feed(markup)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for name, _value in attrs:
            if name.startswith("on"):
                self.handlers.append((tag, name))


def test_a_hostile_capture_manifest_renders_as_text_and_never_as_markup():
    """Anything that can write to the captures directory can write a
    `.meta.json`, and its fields go straight into the recordings list. A
    board revision of `"><img src=x onerror=...>` used to execute there."""
    caps = [dict(label='berger"><img src=x onerror=alert(1)>',
                 board_revision='"><img src=x onerror=alert(2)>',
                 rld_preset="<svg onload=alert(3)>",
                 n="<b>9</b>", gain="<i>12</i>", fs_effective=500.0,
                 integrity="ok", has_events=True, exploratory=True)]
    out = render_page({"scenario": "captures",
                       "routes": {**PAGE_ROUTES, "/api/captures": caps}})
    markup = out["caps"]["html"]
    assert markup, "the recordings list rendered nothing at all"
    built = _Rendered(markup)
    assert built.handlers == [], f"the list carries event handlers: {built.handlers}"
    assert set(built.tags) <= {"div", "span", "br", "small"}, \
        f"the list built tags this page never writes: {sorted(set(built.tags))}"
    # Escaped, not merely absent: the name still has to be readable.
    assert "&quot;&gt;&lt;img src=x onerror=alert(2)&gt;" in markup, \
        "the board revision did not survive as text"
    assert "&lt;svg onload=alert(3)&gt;" in markup, \
        "the bias preset did not survive as text"
    assert markup.count("berger&quot;&gt;&lt;img src=x onerror=alert(1)&gt;") == 2, \
        "the label is not escaped in both its attribute and its text"


def test_a_server_error_reaches_the_log_and_the_toast_as_text():
    """`unknown experiment {name}` and `{type(e).__name__}: {e}` both quote the
    caller's own string back at it, and every route funnels them into the log
    and the toast."""
    msg = "unknown experiment <img src=x onerror=alert(1)>'\"&"
    out = render_page({"scenario": "error_message", "text": msg,
                       "routes": PAGE_ROUTES})
    assert out["log"]["html"] == "", "the log is still assembled as markup"
    assert out["toast"]["html"] == "", "the toast is still assembled as markup"
    logged = [c["text"] for c in out["log_children"] if c["tag"] == "SPAN"]
    assert logged == [msg], f"the log did not carry the message as text: {logged}"
    shown = [c["text"] for c in out["toast_children"] if c["tag"] == "SPAN"]
    assert msg in shown, f"the toast body is not the message as text: {shown}"


def test_a_marker_name_can_never_become_a_handler():
    """A marker name arrives over the wire and used to be written into
    `onclick="mark('...')"`, where a quote in it is code. The button is built
    and the click closes over the name, so there is nothing to break out of."""
    names = ["rest", "');alert(1);//", 'x" onmouseover="alert(2)',
             "<img src=x onerror=alert(3)>"]
    out = render_page({"scenario": "markers", "names": names,
                       "routes": {**PAGE_ROUTES, "/api/marker": {"ok": True}}})
    assert out["markers"]["html"] == "", "the marker row is still written as markup"
    assert [b["text"] for b in out["buttons"]] == names, \
        "a marker name did not survive as its own text"
    for b in out["buttons"]:
        assert b["inline_handlers"] == [], f"{b['text']!r} carries an inline handler"
        assert b["listeners"] == ["click"], f"{b['text']!r} has no bound click"
    for name, posts in zip(names, out["clicked"]):
        assert [p["url"] for p in posts] == ["/api/marker"], \
            f"clicking {name!r} did not mark"
        assert json.loads(posts[0]["body"]) == {"name": name}, \
            "the marker the server is told about is not the one on the button"


def test_a_protocol_parameter_cannot_break_out_of_its_attribute():
    """The protocol form puts a parameter's default into `value="..."` and its
    name into a data attribute. Both come from the server."""
    exp = dict(name="evil", doc="d", enforced=[], prereg={}, params=[
        dict(name="seconds", type="number", default='1" onfocus="alert(1)'),
        dict(name="tone_start", type="select", default="beep_low",
             choices=["beep_low", 'x" onmouseover="alert(2)']),
        dict(name="note", type="textarea",
             default="</textarea><img src=x onerror=alert(3)>"),
        dict(name="markers", type="text",
             default='a"><img src=x onerror=alert(4)>')])
    out = render_page({"scenario": "params", "experiment": "evil",
                       "routes": {**PAGE_ROUTES, "/api/experiments": [exp]}})
    both = out["params"]["html"] + out["marker_edit"]["html"]
    assert both, "the protocol form rendered nothing at all"
    built = _Rendered(both)
    assert built.handlers == [], f"the form carries event handlers: {built.handlers}"
    assert set(built.tags) <= {"label", "input", "select", "option", "span",
                               "textarea", "button"}, \
        f"the form built tags this page never writes: {sorted(set(built.tags))}"
    assert both.count("</textarea>") == 1, \
        "a default closed the textarea it was supposed to sit in"
    assert "&lt;/textarea&gt;&lt;img src=x onerror=alert(3)&gt;" in both
    assert 'data-preview="tone_start"' in both, \
        "the cue preview button no longer carries its parameter as data"


def test_the_page_builds_no_handler_out_of_data():
    """An inline handler assembled from a value is that value compiled. No
    amount of escaping makes one safe, so the page builds none."""
    built = re.findall(r'\bon[a-z]+\s*=\s*"[^"]*\$\{[^"]*"', HUB_HTML)
    assert not built, f"a handler is still built from data: {built}"
    for dead in ('onclick="mark(', 'onclick="preview('):
        assert dead not in HUB_HTML, f"the page still writes {dead}...)"


def test_the_page_carries_a_content_security_policy():
    """Its own origin is the only place the page loads from or reaches. The
    script and the styles are inline, so they still need 'unsafe-inline': what
    the policy buys is that a payload which did land cannot carry what it
    found to another origin."""
    m = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">',
                  HUB_HTML)
    assert m, "the page declares no Content-Security-Policy"
    policy = {d.split()[0]: d.split()[1:]
              for d in (x.strip() for x in m.group(1).split(";")) if d}
    assert policy.get("default-src") == ["'self'"]
    assert policy.get("connect-src") == ["'self'"], \
        "the page is allowed to reach an origin that is not its own"
    assert "'unsafe-eval'" not in m.group(1)
    assert "*" not in m.group(1), "a wildcard source is not a policy"
    for named in ("script-src", "style-src", "img-src"):
        assert named in policy, f"{named} is left to fall back to default-src"
    assert HUB_HTML.index("Content-Security-Policy") < HUB_HTML.index("<style>"), \
        "the policy is declared after the first thing it governs"


def test_the_read_path_refuses_a_capture_name_nobody_could_have_written():
    """A capture directory is a directory, and a `.meta.json` in it can be
    written by something other than the recorder. The label came off the
    filename with no rule applied, and `verify` then raised on it, so one
    unreadable name cost the caller every other capture in the collection."""
    before = prov.CAPTURES
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="cc_hostile_"))
    try:
        prov.use_captures_dir(tmp / "captures")
        here = pathlib.Path(A.CAPTURES)
        (here / "good.meta.json").write_text('{"n": 10}')
        (here / '"><img src=x onerror=alert(1)>.meta.json').write_text('{"n": 1}')
        (here / ".hidden.meta.json").write_text('{"n": 1}')
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            got = A.list_captures()
        assert [c["label"] for c in got] == ["good"], \
            f"a name the library would refuse on the way in was read back: {got}"
        assert any("skipped" in str(w.message) for w in seen), \
            "files were skipped and the caller was never told"
        assert any("2 file(s)" in str(w.message) for w in seen), \
            "the number of skipped files was not reported"
    finally:
        prov.use_captures_dir(before)
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_analysis_route_applies_the_label_rule_the_others_apply():
    """Delete, export and download all check a label before joining it to a
    path. An analysis joins it to a path too."""
    for bad in ("../../etc/passwd", ".hidden", "a/b"):
        assert refused(hub.api_analyze,
                       Req({"analysis": "summary", "labels": [bad]})) == 400, \
            f"{bad!r} was accepted as a capture to analyze"
    for name in ("api_delete", "api_export", "api_download", "api_analyze"):
        assert "_safe_label" in inspect.getsource(getattr(hub, name)), \
            f"{name} joins a label to a path without the rule"


def test_a_cue_cannot_be_read_as_an_option():
    """The cue text goes to the player as one argv element, so nothing parses
    it as a command. What does read it is an option reader, and a value
    starting with a hyphen is taken for a flag rather than words to say."""
    assert refused(hub.api_tone,
                   Req({"tone": "speech", "text": "-x --and-then-some"})) == 400
    assert refused(hub.api_tone, Req({"tone": "none", "text": "ready"})) is None


def test_a_stream_refused_on_usb_power_says_to_unplug():
    """1.4: the device does not stream from the electrodes while plugged in.
    The page says so in words, not as a status number, and does not go live."""
    import types
    from intomind.client import Refused
    h = make_hub()
    h.rec = types.SimpleNamespace(armed=False)

    async def refuse(samples_per_packet=25):
        raise Refused(0x01, 7)
    h.dev.start = refuse
    try:
        run(h.set_live(True))
    except RuntimeError as e:
        assert str(e) == hub.UNPLUG_TO_RECORD, str(e)
    else:
        raise AssertionError("a start refused on USB power went live")
    assert not h.live


def test_the_device_ending_its_stream_on_usb_power_stops_the_view_and_the_run():
    """1.4: plugged in, the device ends its stream. The live view stops, a
    run ends where a person's stop would end it, keeping what it recorded,
    and the page says why."""
    import types
    h = make_hub()
    h.live = True
    h.ctx = types.SimpleNamespace(stop=asyncio.Event())
    run(h._usb_stopped())
    assert not h.live
    assert h.ctx.stop.is_set(), "the run ends"
    assert {"type": "error", "text": hub.USB_STOPPED} in h.emitted
    assert h.emitted[-1]["type"] == "status"


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
    if failed:
        print("failed:", ", ".join(failed))
    return 1 if failed else 0



def _hub_1_3():
    """A device that claims everything 1.3 adds."""
    from tests_support import caps, FULL
    info = device_info(capabilities=list(FULL) + ["model_cadence"],
                       capabilities_high=sum(P.CAPABILITIES_HIGH.values()))
    return make_hub(info=info)


def test_the_name_is_set_as_two_parts_composed_within_the_air_and_read_back():
    h = _hub_1_3()
    applied = run(h.configure(dict(name="Ada", adjective="Blue")))
    assert applied == dict(name="Ada", adjective="Blue")
    st = h.actual_state()
    assert (st["name"], st["adjective"], st["composed_name"]) == ("Ada", "Blue", "Ada's Blue IntoMind One")
    # Too long for the air is refused here, with the composition named.
    try:
        run(h.configure(dict(name="Beatrice", adjective="Purple")))
    except RuntimeError as e:
        assert "29" in str(e) and "Beatrice's Purple IntoMind One" in str(e)
    else:
        raise AssertionError("a name the air cannot carry was sent")
    # One part alone changes one part.
    run(h.configure(dict(adjective="")))
    assert h.actual_state()["composed_name"] == "Ada's IntoMind One"
    # The controls the page renders come from the device's profile.
    ctl = h.dev.profile()["controls"]
    assert ctl["name"]["max_bytes"] == 29 and ctl["name"]["product"] == "IntoMind One"
    assert "synthetic" in ctl["signal_source"]["options"]


def test_the_models_cadence_is_set_and_bounded_by_the_devices_minimum():
    h = _hub_1_3()
    # The control the device declares is the setting's name, so the page
    # renders it as one this application can apply.
    ctl = h.capabilities()["controls"]
    assert ctl["model_cadence"]["settable"] and ctl["name"]["settable"]
    assert run(h.configure(dict(model_cadence=30))) == dict(model_cadence=30)
    assert h.actual_state()["model_interval"] == 30 and h.actual_state()["model_interval_min"] == 4
    try:
        run(h.configure(dict(model_cadence=2)))
    except RuntimeError as e:
        assert "at least 4" in str(e)
    else:
        raise AssertionError("an interval below the device's minimum was sent")
    assert run(h.configure(dict(model_cadence=0))) == dict(model_cadence=0)
    m = run(h.model())
    assert m["pass_ms"] == 3150 and m["interval_s"] == 0 and m["generator"] and m["synthetic"]


def test_the_two_models_are_never_asked_for_together():
    """The generator or the foundation model, never both (ruled 2026-09-28).
    The device refuses; the hub says why, in words."""
    h = _hub_1_3()
    h.dev.active_head = 1
    run(h.set_predictions(True))
    try:
        run(h.configure(dict(signal_source="synthetic")))
    except RuntimeError as e:
        assert "one AI model at a time" in str(e) and "predictions off" in str(e)
    else:
        raise AssertionError("the generator was started beside the foundation model")
    run(h.set_predictions(False))
    assert run(h.configure(dict(signal_source="synthetic"))) == dict(signal_source="synthetic")
    for ask in (lambda: h.set_predictions(True), lambda: h.collect_embeddings(1, "window")):
        try:
            run(ask())
        except RuntimeError as e:
            assert "one AI model at a time" in str(e)
        else:
            raise AssertionError("the foundation model was asked for while the generator ran")


def test_a_device_without_the_claims_gets_no_name_or_cadence_panel_and_no_such_request():
    h = make_hub()
    ctl = h.dev.profile()["controls"]
    assert "name" not in ctl and "model_cadence" not in ctl
    assert "synthetic" not in ctl["signal_source"]["options"]
    for ask in (dict(name="Ada"), dict(model_cadence=30)):
        try:
            run(h.configure(ask))
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"{ask} went to a device that never claimed it")
    assert not any(op in (P.OPCODES["set_name"], P.OPCODES["set_model_interval"]) for op, _ in h.dev.commands)
    # Nor does its model report carry fields its model info never sent.
    m = run(h.model())
    assert m["supported"] and not {"pass_ms", "interval_s", "generator"} & set(m)


def _status_1_3(**actual):
    controls = dict(
        name=dict(supported=True, parts=["name", "adjective"], product="IntoMind One",
                  max_bytes=29, settable=True),
        model_cadence=dict(supported=True, unit="seconds", settable=True),
        predictions=dict(supported=True, ready=True, head_slots=4, embedding=76, settable=False))
    return dict(connected=True, device="IntoMind One",
                caps=dict(device="IntoMind One", channels=4, fw="1.3.1", controls=controls),
                actual=actual)


MODEL_1_3 = dict(supported=True, state="ready", embedding=76, encoder_id="e2f9b60f411f0e73",
                 active_head=None, ready=True, predictions_on=False,
                 pass_ms=3150, interval_s=0, generator=True,
                 heads=[dict(slot=0, state="ready", name="sex", outputs=1, head_id="aa",
                             usable=True, encoder_id="e2f9b60f411f0e73",
                             trained_beside_this_encoder=True),
                        dict(slot=1, state="ready", name="age<b>", outputs=1, head_id="bb",
                             usable=True, encoder_id="1111111111111111",
                             trained_beside_this_encoder=False)])


def _empty_slot(slot):
    return dict(slot=slot, state="empty", name="", outputs=0, head_id="0000000000000000",
                usable=False, encoder_id="0000000000000000", trained_beside_this_encoder=None)


def _model_panel(heads, active_head):
    status = _status_1_3(name="Ada", adjective="Blue", composed_name="Ada's Blue IntoMind One",
                         signal_source="electrodes", model_interval=0, model_interval_min=4)
    model = {**MODEL_1_3, "heads": heads, "active_head": active_head}
    out = render_page({"scenario": "device_status", "status": status,
                       "routes": {**PAGE_ROUTES, "/api/model": model, "/api/config": {"applied": {}}}})
    return out["model"]["html"]


def test_the_model_panel_lists_the_heads_on_the_device_and_never_an_empty_slot():
    """The device lists every slot it has, five on the IntoMind One, and an
    empty slot is not a head. With none selected the list says so, rather
    than showing a head as if it were running, which also could not then be
    chosen, because choosing what is shown changes nothing."""
    age = dict(slot=2, state="valid", name="age", outputs=1, head_id="cc", usable=True,
               encoder_id="e2f9b60f411f0e73", trained_beside_this_encoder=True)
    slots = [_empty_slot(0), _empty_slot(1), age, _empty_slot(3), _empty_slot(4)]
    m = _model_panel(slots, None)
    assert "slot 2: age" in m and "1 outputs" in m
    assert not any(f"slot {s}:" in m for s in (0, 1, 3, 4)), m
    assert "empty" not in m and "unnamed" not in m
    assert ">none selected</option>" in m and "No heads on the device" not in m
    # Once a head runs, the list shows it and offers no placeholder.
    m = _model_panel(slots, 2)
    assert "none selected" not in m and "selected" in m.split("slot 2: age")[0].rsplit("<option", 1)[1]
    # A device whose every slot is empty has no heads, and says how to add one.
    m = _model_panel([_empty_slot(s) for s in range(5)], None)
    assert "No heads on the device" in m and "id=headSel" not in m


def test_the_page_names_the_device_within_the_air_and_says_when_its_signal_is_synthetic():
    status = _status_1_3(name="Ada", adjective="Blue", composed_name="Ada's Blue IntoMind One",
                         signal_source="synthetic", model_interval=0, model_interval_min=4)
    status["prediction"] = dict(head_slot=0, head_id="aa", index=0, outputs=[0.5], window_samples=2000,
                                gap_in_window=False, leadoff_in_window=False, duty_reduced=False,
                                input_source="synthetic")
    out = render_page({"scenario": "device_status", "status": status,
                       "routes": {**PAGE_ROUTES, "/api/model": MODEL_1_3,
                                  "/api/config": {"applied": {}}},
                       "typed": [["Beatrice", "Purple"], [" Cy ", ""], ["José", "Green"]]})
    assert out["name_col_hidden"] is False
    assert (out["name_in"], out["adj_in"]) == ("Ada", "Blue")
    assert out["preview"]["text"] == "Ada's Blue IntoMind One · 23 of 29 bytes"
    # The device's own current name heads the device column, escaped.
    assert "Ada&#39;s Blue IntoMind One" in out["devinfo"]["html"]
    # Said above the trace, in words.
    assert out["source"]["text"].startswith("Synthetic signal: generated on the device")
    assert "hidden" not in out["source"]["className"]
    too_long, trimmed, accented = out["typed"]
    # Too long for the air: counted, said, and never sent.
    assert too_long["preview"]["text"].startswith("Beatrice's Purple IntoMind One · 30 of 29 bytes")
    assert too_long["disabled"] and too_long["fetches"] == []
    assert "warn" in too_long["preview"]["className"]
    # Spaces at the ends are not part of a name.
    assert json.loads(trimmed["fetches"][0]["body"]) == {"name": "Cy", "adjective": ""}
    # Bytes, not letters: é is two, so 25 letters are 26 bytes.
    assert accented["preview"]["text"] == "José's Green IntoMind One · 26 of 29 bytes"
    assert not accented["disabled"]
    # The model panel: what the device measured, its cadence, its generator,
    # and a head trained beside another encoder, offered and marked.
    m = out["model"]["html"]
    assert "3.15 s" in m and "every window it can" in m and "on the device" in m
    assert "id=cadIn" in m and "at least 4 s between windows" in m
    assert "Trained beside another encoder" in m and "age&lt;b&gt;" in m
    assert _Rendered(m).handlers == []
    # An output over generated signal is shown marked, never as a prediction about anyone.
    assert "generated signal, describes no one" in out["predict"]["html"]
    assert "hidden" not in out["predict"]["className"]


def test_a_device_without_the_claims_gets_no_name_panel_and_no_source_note():
    status = _status_1_3(signal_source="normal")
    status["caps"]["controls"] = dict(gain=dict(supported=True, options=[1, 24], settable=True))
    out = render_page({"scenario": "device_status", "status": status, "routes": PAGE_ROUTES})
    assert out["name_col_hidden"] is True
    assert out["source"]["text"] == "" and "hidden" in out["source"]["className"]


if __name__ == "__main__":
    sys.exit(main())
