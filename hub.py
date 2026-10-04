"""IntoMind Command Center: live view, experiment runner, integrated analysis.

    python3 hub.py     then open http://localhost:8080

You choose and start experiments from the page. Every run persists raw ADC
counts, the device's own configuration and event markers to captures/ before any
analysis touches it. The same analysis library backs this page and the
tools/*.py CLIs, so a number seen here is the number seen there.

This application asks the device what it is and renders that. It holds no
register map, no table of specific hardware and no second control path: the
instrument API is the only way it reaches a device, and what that device
declares is what appears on screen.
"""
import asyncio, json, sys, pathlib, time, traceback
from urllib.parse import urlsplit

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import numpy as np
import context
from aiohttp import web, WSMsgType
from intomind import Session
from intomind.client import Refused, display_names, scan as ble_scan
from intomind import analysis as A
from intomind import experiments as X
from intomind import instrument as I
from intomind import protocol as P
from intomind import export as EXPORT
from intomind import audio

HERE = context.bind()


def link_action(connected: bool) -> str:
    """The header button's label, and the ONLY source of it.

    "Disconnect" while a link is live, "Connect" otherwise, and Connect
    lists the devices heard so a person picks one: nothing connects by
    itself. Messages that tell the user to press it compose their text from
    here, and the page renders the button from `status.link_action`, so a
    message never names a button that is not on screen.
    """
    return "Disconnect" if connected else "Connect"

# ---------------------------------------------------------------------------
# What the device can do is the device's own answer.
#
# `Device.profile()` composes it from the capability bits and the rate mask the
# device reported over the protocol, so hardware we do not make describes itself
# the same way and gets the same interface. The page renders from this and from
# nothing else: there is no list of gains, rates or modes in this application or
# in the page it serves.


def profile_for(dev) -> dict:
    """What this device can do, as the device itself answers.

    An object with no profile gets no controls rather than a plausible-looking
    default. A control that cannot do anything is worse than an absent one, and
    a default set of them would describe the first device this app met and
    quietly misdescribe every other.
    """
    ask = getattr(dev, "profile", None)
    return dict(ask()) if callable(ask) else dict(vendor=None, hardware=None,
                                                  controls={})


# Which of a device's controls this application knows how to apply, keyed by
# the control name the profile uses and valued by the field `actual_state`
# reports it under. This is a property of THIS APPLICATION, not of any device:
# the library offers a setter for each of these and none for anything else.
#
# A device may declare a control that is not here, and the page still shows its
# value. It shows it read-only, because offering a dropdown that cannot reach
# the device is a control that does nothing.
SETTABLE = {"gain": "gain", "sample_rate": "rate",
            "signal_source": "signal_source", "leadoff": "leadoff",
            # The chain the device runs on its own signal, and the one
            # choice every level of the page gets: which mains to notch.
            "processing": "processing", "mains": "mains",
            # The status lamp's level, kept by the device across power cycles.
            "indicator": "indicator",
            # 1.3: the device's name and adjective, kept by the device and
            # advertised; and how often its model describes a window, set
            # through the control the device calls model_cadence and read
            # back as the interval it reports.
            "name": "name", "adjective": "adjective",
            "model_cadence": "model_interval"}

#: Why the device refuses to run its two AI models together (1.3): the
#: generator behind the synthetic signal, or the foundation model behind
#: embeddings and predictions, never both.
ONE_MODEL = ("the device runs one AI model at a time: the generator behind the synthetic "
             "signal, or the foundation model behind embeddings and predictions")

#: 1.4: the device does not record while it is plugged in. Said when a
#: stream is refused on USB power, and when the device ends one because it
#: was plugged in.
UNPLUG_TO_RECORD = "The device is plugged in, and it does not record on USB power. Unplug it to record."
USB_STOPPED = ("The device was plugged in, so it stopped recording: it does not record "
               "on USB power. Unplug it to record.")

#: The notch bands a region's mains needs, fundamental and second harmonic.
MAINS_REGIONS = {"50": (50,), "60": (60,), "both": (50, 60)}


def _pairs(stages) -> list:
    return [(s.kind, tuple(s.params)) for s in stages]


def _region_bands(hz, rate_sps=None) -> list:
    """A region's mains bands the rate can represent: all of them when the
    rate is not known."""
    return P.mains_bands(hz, rate_sps)


def _mains_of(stages, rate_sps=None) -> str:
    """Which mains a chain's notch bands remove: "50", "60", "both", "none",
    or "custom" for bands that are not a region's, judged by the bands the
    rate can represent."""
    bands = sorted(tuple(s.params) for s in stages if s.kind == P.STAGE_KINDS["notch"])
    if not bands:
        return "none"
    fifty = sorted(tuple(s.params) for s in _region_bands(50, rate_sps))
    sixty = sorted(tuple(s.params) for s in _region_bands(60, rate_sps))
    if bands == fifty:
        return "50"
    if bands == sixty:
        return "60"
    if bands == sorted(fifty + sixty):
        return "both"
    return "custom"


def _stages_from_spec(spec) -> list:
    """A chain from the page's description of one, or from a list of the
    protocol's own stage records."""
    if isinstance(spec, list):
        return [P.Stage(int(s["kind"]), tuple(int(p) for p in s.get("params", []))) for s in spec]
    if not isinstance(spec, dict):
        raise RuntimeError("a chain is 'default', 'natural', a description, or a list of stages")
    stages = []
    hp = spec.get("highpass_hz")
    if hp is not None and hp != "" and float(hp) > 0:
        stages.append(P.highpass(float(hp)))
    for lo, hi in spec.get("notches") or []:
        stages.append(P.notch(float(lo), float(hi)))
    lp = spec.get("lowpass_hz", "auto")
    if lp == "auto":
        stages.append(P.lowpass())
    elif lp is not None and lp != "" and str(lp).lower() != "none":
        stages.append(P.lowpass(float(lp)))
    return stages


class Hub:
    def __init__(self):
        self.session = Session()
        self.dev = None
        self.rec = X.Recorder()
        self.clients: set = set()
        self.task = None
        self.ctx = None
        self.running = None
        self.live = False
        # "Not connected" and "connecting" are different states, and offering
        # Connect for a connection that is already in flight is a lie.
        self.connecting = False
        # What the last scan heard, by address, in the order heard: the list
        # a person picks from. Nothing is connected that was not picked. With
        # two devices on the air, the first to answer is not the one meant.
        self.heard: dict = {}
        self.scanning = False
        self._stop_scan: asyncio.Event | None = None
        self._scan_task = None
        # Lead-off detection is OFF until someone asks for it, because finding
        # out whether an electrode is attached means pushing current through
        # it. With it off every lead-off bit reads 0, which looks exactly like
        # perfect contact, so contact then reads "unknown" and never "ok".
        self.contact_enabled = False
        self._applied: dict = {}
        self._batch = [[], []]
        self._loff: list = []      # lead-off bits per sample; contact comes from here
        self._contact = [False, False]
        self._gain = None
        self._rate = None
        self._cfg_err = None
        self._resync = None
        # The model's last output, and the head that produced it. Held so a
        # page that opens mid-session is not blank until the next window.
        self.prediction: dict | None = None

    def _contact_n(self) -> int:
        """How many contact indicators there are: the device's channel count.

        Two was the bench instrument's, written as a constant back when it was
        the only instrument. On a wider device that constant does not shrink the
        display, it stops reporting the electrodes past the second.
        """
        return I.contact_channels(self.dev) if self.dev else len(self._contact)

    def _contact_unknown(self) -> list:
        return [None] * self._contact_n()

    async def emit(self, msg: dict):
        text = json.dumps(msg)
        for ws in list(self.clients):
            try:
                await ws.send_str(text)
            except Exception:
                self.clients.discard(ws)

    def on_sample(self, dev, s):
        self.rec.on_sample(dev, s)
        # Width follows the device (two channels on one, four on another),
        # discovered from the first sample rather than assumed.
        if len(self._batch) != len(s.uv):
            self._batch = [[] for _ in s.uv]
        for lst, v in zip(self._batch, s.uv):
            lst.append(v)
        self._loff.append(getattr(s, "leadoff", 0) or 0)

    def on_prediction(self, dev, pred):
        """One window's model output, as the device produced it.

        Kept as the device stated it, including the flags that say the window
        was not clean. A prediction over a window with a gap in it is still a
        prediction the device made, and hiding the flag would make it look
        better than it is.
        """
        self.prediction = dict(
            head_slot=pred.head_slot, head_id=pred.head_id, index=pred.index,
            outputs=[float(x) for x in pred.outputs],
            window_samples=pred.window_samples,
            gap_in_window=pred.gap_in_window,
            leadoff_in_window=pred.leadoff_in_window,
            duty_reduced=pred.duty_reduced,
            # 1.3: a window of generated signal describes no one, and the
            # device says so. Kept so the page can say it too.
            input_source=pred.input_source)
        # bleak notifies from its own thread, so the send is scheduled onto
        # the loop rather than awaited here.
        try:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    self.emit({"type": "prediction", **self.prediction})))
        except Exception:
            pass

    def on_packet(self, dev, p):
        self.rec.on_packet(dev, p)
        # Each packet header carries the gain and rate its samples were
        # converted at, so the trace is labeled by the wire rather than by
        # what the host last asked for.
        self._gain, self._rate = p.gain, p.sample_rate_hz

    def another_listener(self) -> bool:
        """Whether another program on this computer is listening to the
        device now. The library drops the copies it causes, so the stream
        and any recording are intact either way."""
        return bool(self.connected and getattr(self.dev, "another_listener", False))

    async def _note_listener(self):
        """Say so when another program starts or stops listening, once each
        way, never once a packet."""
        now = self.another_listener()
        if now != getattr(self, "_listener_said", False):
            self._listener_said = now
            if now:
                await self.emit({"type": "note", "text": "another program on this computer is also "
                                 "listening to the device; its copies are dropped"})
            await self.emit(self.status())

    async def flusher(self):
        while True:
            await asyncio.sleep(0.08)
            await self._note_listener()
            if self._batch[0]:
                b, self._batch = self._batch, [[] for _ in self._batch]
                lo, self._loff = self._loff, []
                # Contact comes from the device's own lead-off detection, never
                # from the DC offset. A floating input drifts above any DC
                # threshold you pick, so the old heuristic called electrodes on
                # a table "on skin". See instrument.contact_from_leadoff.
                # Decoded at the device's width and in the packing ITS packet
                # contract uses. Two contracts put different bits in the same
                # byte, so the wrong one reports the wrong electrodes rather
                # than failing.
                if lo and self.contact_enabled:
                    self._contact = I.contact_from_leadoff(
                        lo, n_channels=self._contact_n())
                elif not self.contact_enabled:
                    self._contact = self._contact_unknown()   # not "on"

                dc = [float(np.mean(x)) if x else 0.0 for x in b]
                await self.emit({"type": "data",
                                 **{f"ch{i + 1}": x for i, x in enumerate(b)},
                                 "dc": dc, "contact": list(self._contact),
                                 "gain": self._gain, "rate": self._rate})

    @property
    def connected(self) -> bool:
        return bool(self.dev and self.dev.connected)

    def _on_disconnect(self, _dev):
        """A dropped link aborts the run and can never yield a finalized
        capture. Scheduled onto the loop: bleak calls us from its thread."""
        self.live = False
        if self.ctx:
            self.ctx.stop.set()
        try:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._announce_drop()))
        except Exception:
            pass

    def _on_usb_stop(self, _dev):
        """1.4: the device ended its stream because it was plugged in.
        Onto the loop, like a dropped link."""
        try:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._usb_stopped()))
        except Exception:
            pass

    async def _usb_stopped(self):
        """The live view stops, and a run in progress ends there and keeps
        what it recorded, the way a person's stop does: the samples up to
        the device's stop are whole. Then the reason, in words."""
        self.live = False
        if self.ctx:
            self.ctx.stop.set()
        await self.emit({"type": "error", "text": USB_STOPPED})
        await self.emit(self.status())

    async def _start(self, **kw):
        """Start the stream. On USB power the device refuses, and the
        refusal is said in words."""
        try:
            await self.dev.start(**kw)
        except Refused as e:
            if e.status == P.STATUS_USB_POWER:
                raise RuntimeError(UNPLUG_TO_RECORD) from e
            raise

    async def _announce_drop(self):
        await self.emit({"type": "error",
                         "text": "The link to the device dropped, so the run was "
                                 f"aborted. Press {link_action(self.connected)}."})
        await self.emit(self.status())

    # --- the devices on the air, and the one a person picks ---------------

    def devices(self) -> list:
        """What the scan heard, for the page: each device's own name,
        numbered by this host when two share one, and its address."""
        found = list(self.heard.values())
        labels = display_names(found)
        return [dict(address=d.address, name=d.name or "", label=labels[d.address]) for d in found]

    async def _emit_devices(self):
        await self.emit({"type": "devices", "scanning": self.scanning, "devices": self.devices()})

    def start_scan(self, seconds: float = 15.0):
        """Listen for devices in the background, listing each as it answers.
        Connects nothing."""
        if not self.scanning:
            self._scan_task = asyncio.create_task(self.scan(seconds))

    async def scan(self, seconds: float = 15.0):
        if self.scanning:
            return
        self._loop = asyncio.get_running_loop()
        self.heard = {}
        self.scanning = True
        self._stop_scan = asyncio.Event()
        await self._emit_devices()

        def heard(d) -> None:
            self.heard[d.address] = d
            self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._emit_devices()))
        try:
            await ble_scan(seconds, on_device=heard, stop=self._stop_scan)
        finally:
            self.scanning = False
            self._stop_scan = None
            await self._emit_devices()

    async def connect(self, address: str):
        """Connect the device a person picked from the scan, and only it."""
        if self.running:
            raise RuntimeError(f"{self.running} is running")
        if self.connected:
            raise RuntimeError("a device is connected: disconnect it first")
        if self.connecting:
            raise RuntimeError("a connection is already being made")
        bd = self.heard.get(address)
        if bd is None:
            raise RuntimeError("that device was not heard by the last scan: scan again and pick it from the list")
        # The radio stops listening before it connects.
        if self._stop_scan is not None:
            self._stop_scan.set()
        if self._scan_task is not None:
            await asyncio.gather(self._scan_task, return_exceptions=True)
        self._loop = asyncio.get_running_loop()
        self.connecting = True
        await self.emit(self.status())
        try:
            return await self._connect(bd)
        finally:
            self.connecting = False
            await self.emit(self.status())

    async def _connect(self, bd):
        self.session = Session()
        await self.session.connect(bd)
        self.dev = self.session.devices[0]
        self.dev._on_disconnect = self._on_disconnect
        self.dev.on_sample = self.on_sample
        self.dev.on_packet = self.on_packet
        self.dev.on_prediction = self.on_prediction
        self.dev.on_usb_stop = self._on_usb_stop
        self.prediction = None
        await self.quiesce()
        # A single TIME_SYNC leaves skew pinned at 1.0 and no error bar, and
        # nothing ever started `resync_loop`, so the clock map was one point
        # for the life of the process.
        q = await self.dev.calibrate_clock()
        print(f"clock: {q['n_syncs']} syncs, skew {q['skew']:.9f}, "
              f"residual {q['residual_ms']:.3f} ms, "
              f"half-RTT <= {q['max_half_rtt_ms']:.2f} ms")
        if self._resync is None or self._resync.done():
            self._resync = asyncio.create_task(self.session.resync_loop())
        # Read the instrument's actual configuration NOW. Without this the hub
        # connects knowing nothing and reports every setting as unknown.
        await self.refresh_config()
        # Lead-off detection is NOT enabled here, and must not be.
        #
        # It works by pushing a small current into every electrode. That is
        # fine as an on-demand check, and it is the only way a device can
        # *know* an electrode is attached. It is not fine to leave running
        # during acquisition: it perturbs the very signal the electrode is
        # measuring, and on a marginal electrode it drives the input to a rail
        # and flattens the channel, which was observed here as one dead trace.
        #
        # Leaving it on by default changed the instrument's behavior underneath
        # a baseline recording that exists to characterize the instrument. Off
        # by default. Checked on demand, or turned on deliberately.
        #
        # A connected, awake device is streaming. There is no "start live view":
        # that control changed nothing on the device, it only hid the picture.
        try:
            await self.set_live(True)
        except Exception as e:
            print(f"could not start the live stream: {e}")
        return self.dev

    async def check_contact(self, seconds: float = 1.0) -> dict:
        """Check electrode contact, once, on demand.

        The firmware refuses a lead-off change while the device is streaming,
        so the stream is stopped around each toggle and put back afterwards.
        The trace blinks for about a second, which is the cost of an explicit
        check the operator asked for.

        This check always leaves the current off again, whatever it was
        before. It is pushed through the very electrodes that are measuring
        microvolts, so it disturbs the signal, and on a marginal electrode it
        drives the input to a rail and flattens the channel. An operator who
        wants it running continuously turns it on as a setting, where the page
        says what that costs.
        """
        was_live = self.live or bool(getattr(self.dev, "streaming", False))

        async def _leadoff(on: bool):
            # The firmware refuses the change while streaming, so the stream
            # comes down first. Stopping an already-stopped device is an error
            # the device reports, and that is what made every check after the
            # first one fail.
            if getattr(self.dev, "streaming", False):
                try:
                    await self.dev.stop()
                except Exception:
                    pass
                await asyncio.sleep(0.05)
            await self.dev.set_leadoff(on)

        try:
            await _leadoff(True)
            self.contact_enabled = True
            self._loff = []
            await self._start(samples_per_packet=25)
            await asyncio.sleep(seconds)
            loff = list(self._loff)
        finally:
            try:
                await _leadoff(False)
            except Exception:
                pass
            self.contact_enabled = False
            self._contact = self._contact_unknown()
            if was_live:                     # put the trace back
                try:
                    await self.set_live(True)
                except Exception:
                    pass

        if not loff:
            return dict(ok=False, reason="no samples arrived during the check")
        on = I.contact_from_leadoff(loff, n_channels=self._contact_n())
        await self.emit(self.status())
        return dict(ok=True, channels=[bool(x) for x in on], n=len(loff))


    async def set_contact_detection(self, on: bool):
        """Turn lead-off detection on or off, and leave it there.

        Continuous detection is the only way to see an electrode fall off
        mid-recording, and it costs a small current through every electrode.
        Both facts are true at once, so this is the operator's call and the
        page says what it costs rather than deciding for them.
        """
        await self.dev.set_leadoff(on)
        self.contact_enabled = bool(on)
        if not on:
            self._contact = self._contact_unknown()
        await self.emit(self.status())

    async def disconnect(self):
        """Release the link deliberately.

        The counterpart to connect(). One BLE client at a time is the
        instrument's contract, so a user who wants to hand the device to another
        tool had no way to release it short of killing the hub. The page asks
        before it lets go, and a recording refuses it.
        """
        if self.running:
            raise RuntimeError(f"{self.running} is running")
        if self._resync:
            self._resync.cancel()
            self._resync = None
        try:
            if self.dev:
                await self.dev.disconnect()
        except Exception:
            pass
        self.dev = None
        await self.emit(self.status())

    async def quiesce(self):
        """Force a known state on connect.

        The device half lives in the library (`instrument.quiesce`) because
        every application that connects needs exactly the same thing and a
        second copy would drift. What stays here is this application's own
        view state, which the library has no notion of.
        """
        from intomind.instrument import quiesce as _quiesce
        await _quiesce(self.dev)
        self.contact_enabled = False
        self._contact = self._contact_unknown()

    async def refresh_config(self) -> dict:
        """The single config read, from the device.

        The status characteristic is the whole configuration answer under this
        contract, so this is one read with no second path behind it. It runs at
        connect and after every apply, which is what makes the page's values
        the instrument's values rather than the last thing it was asked for.
        """
        if not self.connected:
            return {}
        try:
            cfg = await self.dev.refresh_config()
            self._cfg_err = None
            return cfg
        except Exception as e:
            self._cfg_err = f"{type(e).__name__}: {e}"
            return dict(getattr(self.dev, "config", {}) or {})

    async def set_live(self, on: bool):
        """Stream for the plot without recording. rec.armed stays False, so
        nothing reaches disk. A live view must never masquerade as a capture."""
        # The flag is not the truth: the device is. check_contact stops the
        # hardware directly, so `live` can say True while nothing is streaming.
        # Trusting the flag here is what left the trace dead after a check.
        if not self.dev:
            return
        streaming = bool(getattr(self.dev, "streaming", False))
        if on == self.live and streaming == on:
            return
        if on and self.running:
            raise RuntimeError(f"{self.running} is running")
        self.rec.armed = False
        if on:
            await self._start(samples_per_packet=25)
        else:
            try:
                await self.dev.stop()
            except Exception:
                pass
        self.live = on

    def actual_state(self) -> dict:
        """The instrument's configuration, as the device reported it.

        Never what the UI last requested, never what a protocol defaulted to.
        `refresh_config` fills this from the status characteristic at connect
        and after every apply.
        """
        cfg = dict(getattr(self.dev, "config", {}) or {}) if self.connected else {}
        st = self._reported(cfg)
        # THE READ WINS OVER THE WIRE. Each packet header carries the gain and
        # rate it was converted at, which is the device's own answer but only
        # while packets are arriving: on a stopped device it is whatever the
        # last one said, however long ago. The status read is current, so the
        # wire only fills a field no read has answered yet.
        if st["gain"] is None and self._gain is not None:
            st["gain"] = self._gain
        if st["rate"] is None and self._rate is not None:
            st["rate"] = self._rate
        # Anything still missing after a read is reported as unknown, never
        # guessed. _applied only backfills a field the read has not answered.
        for k, v in self._applied.items():
            if st.get(k) is None:
                st[k] = v
        return st

    def state_name(self) -> str:
        if not self.connected:
            return "connecting" if self.connecting else "disconnected"
        if self.running or self.live:
            return "streaming"
        return "connected"

    def capabilities(self) -> dict:
        """What the connected device can do, for the page to render from.

        The controls are the device's own profile, verbatim. The rest is what
        the device said about itself in Device Info. Absent when nothing is
        connected: the page then shows an offline, analysis-only surface.
        """
        if not self.dev:
            return {}
        info = getattr(self.dev, "info", None)
        prof = profile_for(self.dev)
        # The device's controls, each marked with whether THIS APPLICATION can
        # apply it. The device's own description is passed through untouched
        # and this is added beside it, so the page can state a value it cannot
        # change instead of offering a dropdown that reaches nothing.
        controls = {k: dict(v, settable=k in SETTABLE)
                    for k, v in (prof.get("controls") or {}).items()}
        return dict(
            device=self.dev.name, vendor=prof.get("vendor"),
            # The adapter owns the transport, so it is the one that says what
            # is carrying the samples.
            transport=dict(getattr(self.dev, "ADAPTER", {})).get("transport"),
            hardware=prof.get("hardware"),
            channels=getattr(info, "channels", None),
            adc_bits=getattr(info, "adc_bits", None),
            vref_uv=getattr(info, "vref_uv", None),
            tick_hz=getattr(info, "tick_hz", None),
            rates=list(getattr(info, "rates", []) or []),
            fw=getattr(info, "firmware_string", None),
            proto=getattr(info, "protocol_string", None),
            device_id=getattr(info, "device_id", None),
            controls=controls,
        )

    def status(self) -> dict:
        return dict(type="status", connected=self.connected,
                    connecting=self.connecting,
                    scanning=self.scanning, devices=self.devices(),
                    another_listener=self.another_listener(),
                    link_action=link_action(self.connected),
                    device=self.dev.name if self.dev else None,
                    fw=self.dev.info.firmware_string if self.connected else None,
                    running=self.running, live=self.live,
                    state=self.state_name(), actual=self.actual_state(),
                    caps=self.capabilities(),
                    contact_detection="on" if self.contact_enabled else "off",
                    prediction=self.prediction,
                    embeddings=self._embeddings_status(),
                    config_err=self._cfg_err)

    async def configure(self, settings: dict) -> dict:
        """Configure the instrument, now, independent of any run.

        Device state is a property of the instrument, not of a protocol. It is
        applied immediately, read back from the device, and compared against
        what was asked for. The live view and any subsequent recording
        therefore always agree.

        A control this application has no way to apply is refused by name
        rather than accepted and dropped. The page already renders it
        read-only, so this is the case where a request was made by hand.
        """
        if self.running:
            raise RuntimeError(f"{self.running} is running")
        if not self.connected:
            raise RuntimeError("the link to the device is down")
        unknown = [k for k in settings if k not in SETTABLE]
        if unknown:
            raise RuntimeError(
                f"this application cannot set {', '.join(sorted(unknown))}. "
                f"It can set {', '.join(sorted(SETTABLE))}")
        applied = {}
        # The device takes a configuration only between streams, so the live
        # view is paused for the change and resumed after it. A recording is
        # never interrupted: it was refused above.
        was_live = self.live
        if was_live:
            await self.set_live(False)
        try:
            applied.update(await self._apply_settings(settings))
        finally:
            if was_live:
                await self.set_live(True)
        self._applied.update({SETTABLE[k]: v for k, v in applied.items()
                              if k not in ("processing", "mains")})
        cfg = await self.refresh_config()
        # Verify against the device's own read-back, and against the READ
        # rather than against `actual_state`, which backfills an unanswered
        # field with what was requested. Checking the backfill would compare
        # the request against itself and pass every time. The chain verifies
        # itself in `_apply_chain`, against the chain read back.
        reported = self._reported(cfg)
        problems = [f"asked for {k}={v!r} and the device reports "
                    f"{reported[SETTABLE[k]]!r}"
                    for k, v in applied.items()
                    if k not in ("processing", "mains")
                    and reported[SETTABLE[k]] is not None
                    and reported[SETTABLE[k]] != v]
        await self.emit(self.status())
        if problems:
            raise RuntimeError("; ".join(problems))
        return applied

    async def _apply_settings(self, settings: dict) -> dict:
        applied = {}
        # Lead-off first, because it goes through the path that also clears the
        # cached contact verdict.
        if "leadoff" in settings:
            on = str(settings["leadoff"]).lower() in ("on", "true", "1")
            await self.set_contact_detection(on)
            applied["leadoff"] = on
        if "gain" in settings:
            await self.dev.set_gain(int(settings["gain"]))
            applied["gain"] = int(settings["gain"])
        if "sample_rate" in settings:
            await self.dev.set_rate(int(settings["sample_rate"]))
            applied["sample_rate"] = int(settings["sample_rate"])
        if "signal_source" in settings:
            try:
                await self.dev.set_mode(P.MODES[settings["signal_source"]])
            except RuntimeError as e:
                # The device's own refusal is the authority; this puts it in words.
                if settings["signal_source"] == "synthetic" and "status 3" in str(e):
                    raise RuntimeError(f"{ONE_MODEL}. Turn predictions off, and let any "
                                       "embeddings collection finish, first") from e
                raise
            applied["signal_source"] = settings["signal_source"]
        if "processing" in settings or "mains" in settings:
            applied.update(await self._apply_chain(settings))
        if "indicator" in settings:
            level = str(settings["indicator"])
            if level not in P.INDICATOR_LEVELS:
                raise RuntimeError(f"the light's level is one of {list(P.INDICATOR_LEVELS)}, not {level!r}")
            await self.dev.set_indicator(level)
            back = await self.dev.indicator()
            if back != level:
                raise RuntimeError(f"asked for the light at {level} and the device reports {back}")
            applied["indicator"] = level
        if "name" in settings or "adjective" in settings:
            # Both parts go together: the device composes them as one name,
            # and the composition has to fit what the air carries.
            current_name, current_adjective = await self.dev.get_name()
            name = str(settings.get("name", current_name)).strip(" ")
            adjective = str(settings.get("adjective", current_adjective)).strip(" ")
            if not P.name_fits(name, adjective):
                raise RuntimeError(
                    f"{P.compose_name(name, adjective)!r} is longer than the {P.NAME_MAX_COMPOSED} bytes "
                    "the device's name may be. Shorten the name or the adjective.")
            await self.dev.set_name(name, adjective)
            applied["name"], applied["adjective"] = name, adjective
        if "model_cadence" in settings:
            seconds = int(settings["model_cadence"])
            mi = await self.dev.model_interval()
            if seconds != 0 and seconds < mi.minimum_s:
                raise RuntimeError(f"the model needs at least {mi.minimum_s} seconds between windows on this device; 0 is every window")
            await self.dev.set_model_interval(seconds)
            applied["model_cadence"] = seconds
        return applied

    # --- the light, the converter's registers, embeddings (1.2) --------------

    async def identify(self, seconds: int = 5) -> dict:
        """Blink the light so this device can be told from others."""
        if not (self.connected and self.dev.can("indicator")):
            raise RuntimeError("this device has no light a host can address")
        await self.dev.identify(int(seconds))
        return dict(seconds=int(seconds))

    async def registers(self) -> dict:
        """The converter's registers, raw and in words. Read only, and only
        while the converter is idle: the device answers busy otherwise, so
        the live view is paused around the read."""
        if not (self.connected and self.dev.can("converter_registers")):
            raise RuntimeError("this device does not offer its converter's registers")
        was_live = self.live and getattr(self.dev, "streaming", False)
        if was_live:
            await self.dev.stop()
        try:
            regs = await self.dev.converter_registers()
        finally:
            if was_live:
                await self._start()
        return dict(family=regs.family, family_code=regs.family_code, first=regs.first,
                    values=regs.values.hex(), words=regs.describe())

    def _embeddings_status(self) -> dict | None:
        job = getattr(self, "embeddings_job", None)
        return dict(job) if job else None

    async def collect_embeddings(self, windows: int, form: str) -> dict:
        """Gather whole windows of the encoder's output from the live
        stream and write them to a file in the captures directory, as a
        background job the status reports on. The live view must be
        running: the device sends embeddings only while it streams."""
        if not (self.connected and self.dev.can("embeddings")):
            raise RuntimeError("this device does not send its encoder's output")
        if form not in P.EMBEDDING_FORMS or form == "off":
            raise RuntimeError("the form is window, tokens, or both")
        windows = int(windows)
        if not 1 <= windows <= 500:
            raise RuntimeError("ask for 1 to 500 windows")
        if self.actual_state().get("signal_source") == "synthetic":
            raise RuntimeError(f"{ONE_MODEL}. Switch the signal source back to the electrodes first")
        if not (self.live and getattr(self.dev, "streaming", False)):
            raise RuntimeError("start the live view first: the device sends embeddings only while it streams")
        if self.running:
            raise RuntimeError(f"{self.running} is running")
        job = getattr(self, "embeddings_job", None)
        if job and job.get("running"):
            raise RuntimeError("a collection is already running")
        self.embeddings_job = dict(running=True, target=windows, got=0, form=form, file=None, error=None)
        asyncio.get_running_loop().create_task(self._collect_embeddings(windows, form))
        await self.emit(self.status())
        return dict(self.embeddings_job)

    async def _collect_embeddings(self, windows: int, form: str):
        job = self.embeddings_job
        got: list = []

        def take(_dev, w):
            got.append(w)
            job["got"] = len(got)
            asyncio.get_running_loop().create_task(self.emit(self.status()))

        try:
            previous = self.dev.on_embedding
            await self.dev.set_embeddings(form, on_window=take)
            try:
                deadline = asyncio.get_running_loop().time() + 30.0 * windows + 60.0
                while len(got) < windows and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.25)
            finally:
                self.dev.on_embedding = previous
                try:
                    await self.dev.set_embeddings("off")
                except Exception:                    # noqa: BLE001
                    pass
            if not got:
                raise RuntimeError("no whole window arrived")
            info = await self.dev.model_info()
            name = f"embeddings-{time.strftime('%Y%m%d-%H%M%S')}.json"
            path = pathlib.Path(A.CAPTURES) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(dict(
                form=form, channels=self.dev.info.channels,
                tokens_per_channel=info.tokens_per_channel, encoder_id=info.encoder_id,
                embed_scale=P.EMBED_SCALE,
                windows=[dict(index=w.index, device_time=w.device_time, window_samples=w.window_samples,
                              input_source=w.input_source, gap_in_window=w.gap_in_window,
                              duty_reduced=w.duty_reduced, leadoff_in_window=w.leadoff_in_window,
                              embedding=w.embedding, tokens=w.tokens) for w in got[:windows]])))
            job.update(running=False, got=len(got[:windows]), file=name)
        except Exception as e:                       # noqa: BLE001
            job.update(running=False, error=f"{type(e).__name__}: {e}")
        await self.emit(self.status())

    async def _apply_chain(self, settings: dict) -> dict:
        """The chain on the device's signal path, and the mains region.

        `processing` is "default", "natural", or a description: a high-pass
        corner or none, a low-pass corner, "auto", or none, and the notch
        bands. `mains` keeps the rest of the chain and replaces its notch
        bands with the region's. Each is read back from the device and
        compared to what was asked, because a chain the device did not take
        is a recording that says the wrong thing about itself.
        """
        can = getattr(self.dev, "can", None)
        if not (callable(can) and self.dev.can("pipeline")):
            raise RuntimeError("this device runs no processing chain")
        applied = {}
        if "processing" in settings:
            spec = settings["processing"]
            if spec == "default":
                await self.dev.restore_pipeline_default()
                state = await self.dev.pipeline()
                if state.origin != "default":
                    raise RuntimeError("asked for the device's default chain and the device reports a host's")
                applied["processing"] = "default"
            elif spec == "natural":
                await self.dev.clear_pipeline()
                state = await self.dev.pipeline()
                if state.stages:
                    raise RuntimeError("asked for the natural signal and the device reports a chain")
                applied["processing"] = "natural"
            else:
                stages = _stages_from_spec(spec)
                await self.dev.set_pipeline(stages)
                state = await self.dev.pipeline()
                if _pairs(state.stages) != _pairs(stages):
                    raise RuntimeError(f"asked for {P.describe_chain(stages)} and the device reports {P.describe_chain(state.stages)}")
                applied["processing"] = [s.as_dict() for s in stages]
        if "mains" in settings:
            region = str(settings["mains"])
            if region not in MAINS_REGIONS:
                raise RuntimeError(f"the mains region is one of {list(MAINS_REGIONS)}, not {region!r}")
            current = await self.dev.pipeline()
            notch = P.STAGE_KINDS["notch"]
            highpass = [s for s in current.stages if s.kind == P.STAGE_KINDS["highpass"]]
            lowpass = [s for s in current.stages if s.kind == P.STAGE_KINDS["lowpass"]]
            others = [s for s in current.stages if s.kind not in (notch, P.STAGE_KINDS["highpass"], P.STAGE_KINDS["lowpass"])]
            # Only the bands the rate can represent: at 250 samples a second
            # a region's higher harmonics are refused by the device.
            rate = (await self.dev.refresh_config()).get("rate_sps")
            bands = [b for hz in MAINS_REGIONS[region] for b in _region_bands(hz, rate)]
            stages = highpass + bands + others + lowpass
            await self.dev.set_pipeline(stages)
            state = await self.dev.pipeline()
            if _mains_of(state.stages, rate) != region:
                raise RuntimeError(f"asked for the {region} Hz mains and the device reports {_mains_of(state.stages, rate)}")
            applied["mains"] = region
        return applied

    @staticmethod
    def _reported(cfg: dict) -> dict:
        """The device's configuration read, under the names this application
        reports it by. The chain is the device's own words; the mains region
        is read off the chain's notch bands."""
        proc = cfg.get("processing")
        stages = [P.Stage(s["kind"], tuple(s["params"])) for s in (proc or {}).get("stages", [])]
        return dict(gain=cfg.get("gain"), rate=cfg.get("rate_sps"),
                    signal_source=cfg.get("mode"), leadoff=cfg.get("leadoff"),
                    processing=proc, mains=_mains_of(stages, cfg.get("rate_sps")) if proc else None,
                    indicator=cfg.get("indicator"),
                    name=cfg.get("name"), adjective=cfg.get("adjective"),
                    composed_name=cfg.get("composed_name"),
                    model_interval=cfg.get("model_interval_s"),
                    model_interval_min=cfg.get("model_interval_min_s"))

    # --- the model, on a device that carries one -----------------------------

    async def model(self) -> dict:
        """The device's model, its head slots, and which one is selected.

        A device that does not claim a model answers that it has none, and the
        page renders nothing rather than an empty panel.
        """
        if not (self.connected and self.dev.can("model")):
            return dict(supported=False)
        info = await self.dev.model_info()
        out = dict(supported=True, state=info.state, ready=info.ready,
                   predictions_on=info.predictions_on,
                   encoder_id=info.encoder_id,
                   weights_version=".".join(str(x) for x in info.weights_version),
                   active_head=info.active_head, heads=[],
                   embedding=self.dev.info.model_embed_dim,
                   slots=self.dev.info.head_slots,
                   synthetic=self.dev.can("synthetic"))
        if self.dev.can("model_cadence"):
            # 1.3: what the device measured and holds. A device before 1.3
            # sends none of it, and a zero read from a message that ended
            # before these fields is not a measurement, so it is left out.
            out.update(pass_ms=info.pass_ms, interval_s=info.interval_s,
                       generator=info.generator)
        if self.dev.can("heads"):
            active, heads = await self.dev.heads()
            out["active_head"] = active
            out["heads"] = [dict(slot=h.slot, state=h.state, name=h.name,
                                 outputs=h.out_dim, head_id=h.head_id,
                                 usable=h.usable,
                                 # 1.3: a head trained beside another encoder
                                 # is said, and still offered.
                                 encoder_id=h.encoder_id,
                                 trained_beside_this_encoder=h.trained_beside(info.encoder_id))
                            for h in heads]
        return out

    async def select_head(self, slot: int) -> dict:
        await self.dev.select_head(int(slot))
        return await self.model()

    async def set_predictions(self, on: bool) -> dict:
        if on and self.actual_state().get("signal_source") == "synthetic":
            raise RuntimeError(f"{ONE_MODEL}. Switch the signal source back to the electrodes first")
        await self.dev.set_predictions(bool(on))
        if not on:
            self.prediction = None
        await self.emit(self.status())
        return await self.model()

    async def prepare_run(self, spec: dict, params: dict, tier: str,
                          overrides: dict) -> dict:
        """What the recording captures about the instrument -- it changes
        nothing.

        An experiment NEVER sets instrument settings. The settings the user
        configured, within their control tier, are exactly what the recording
        uses. This reads the actual configuration from the device and stamps it
        into the manifest so the capture is self-describing. It does not apply,
        correct, or override anything.

        The enforced mechanism remains for a genuinely unsafe precondition
        (it refuses the run), but it is not a setting the experiment imposes.
        """
        I.check_enforced(spec, params)

        await self.refresh_config()
        actual = self.actual_state()

        cfg = dict(actual)
        cfg.update(
            tier=tier,
            preregistered_analysis=spec["prereg"] or None,
            effective_settings=I.effective_settings(spec, actual, {}, params),
            # What the instrument was set to when the run armed, named for what
            # it is. There is no register read behind this and the manifest
            # must not imply one.
            config_before=dict(actual),
        )
        return cfg

    async def begin(self, name: str, params: dict, tier: str,
                    overrides: dict) -> dict:
        """Gate the run *inside the request*, so a refusal reaches the browser.

        Arming happens here and the recording body is spawned afterwards. If this
        raises, nothing was armed and `running` is cleared.
        """
        spec = X.REGISTRY[name]
        # The stream is NOT stopped here. A connected device streams, the trace
        # draws whatever arrives, and a run only decides whether those samples
        # are also written to disk. "Experiments own the stream" was a concept I
        # invented, and it is deleted.
        self.running = name
        try:
            return await self.prepare_run(spec, params, tier, overrides)
        except Exception as e:
            self.running = None
            # The run never armed, so the stream is ours again. A refused run
            # must not leave the operator staring at a dead trace: the device is
            # still connected and still streaming, and the picture must say so.
            try:
                await self.set_live(True)
            except Exception:
                pass
            await self.emit({"type": "error", "text": f"{type(e).__name__}: {e}"})
            await self.emit(self.status())
            raise

    async def execute(self, name: str, params: dict, cfg: dict):
        spec = X.REGISTRY[name]
        # EVERYTHING is inside the try, so `running` is cleared no matter where
        # it fails -- including RunCtx construction and the first emit. A crash
        # before the try used to leave `running` stuck at the experiment name,
        # and then every later run was refused with "<name> is running".
        try:
            self.ctx = X.RunCtx(self.emit, cfg)
            await self.emit({"type": "started", "name": name, "params": params,
                             "cfg": cfg})
            labels = await spec["fn"](self.dev, self.rec, self.emit, params, self.ctx)
            await self.emit({"type": "done", "name": name, "labels": labels})
        except Exception as e:
            traceback.print_exc()
            await self.emit({"type": "error", "text": f"{type(e).__name__}: {e}"})
        finally:
            self.running = None
            self.ctx = None
            try:
                await self.refresh_config()
            except Exception:
                pass
            # The run owned the stream. Hand it back to the live trace.
            try:
                await self.set_live(True)
            except Exception:
                pass
            await self.emit(self.status())


hub = Hub()


async def body_of(r) -> dict:
    """The request's JSON object, or a refusal.

    A missing field or a body that is not JSON is the caller's mistake and
    should read as one. Letting it raise out of a handler turns it into a
    server error and a stack trace in the log, which says the server broke
    when it did not.
    """
    try:
        body = await r.json()
    except Exception:
        raise web.HTTPBadRequest(text="this route takes a JSON object")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="this route takes a JSON object")
    return body


def field(body: dict, name: str):
    """One required field, refused by name when it is not there."""
    if name not in body:
        raise web.HTTPBadRequest(text=f"{name} is required")
    return body[name]


async def api_experiments(_r):
    return web.json_response([dict(name=v["name"], doc=v["doc"],
                                   params=v["params"],
                                   enforced=v["enforced"],
                                   prereg=v["prereg"])
                              for v in X.REGISTRY.values()])


async def api_analyses(_r):
    return web.json_response([dict(name=k, doc=v["doc"],
                                   needs_events=v["needs_events"],
                                   params=v.get("params", []))
                              for k, v in A.ANALYSES.items()])


async def api_captures(_r):
    return web.json_response(await asyncio.to_thread(A.list_captures))


async def api_status(_r):
    return web.json_response(hub.status())


async def api_run(r):
    """Start a run, or explain in structured form why it may not start.

    A run records at the instrument's CURRENT settings and never changes them,
    so there is no "this protocol needs a different setting" negotiation. The
    only refusals are structural: the enforced mechanism (unsafe precondition),
    an unknown experiment, or the instrument being busy/down.
    """
    body = await body_of(r)
    name = field(body, "name")
    if name not in X.REGISTRY:
        raise web.HTTPBadRequest(text=f"unknown experiment {name}")
    # Self-heal a stale flag: if the previous run's task has finished, `running`
    # cannot still be true. Belt-and-suspenders on top of execute()'s finally,
    # so a wedged flag can never permanently block the Run button.
    if hub.running and (hub.task is None or hub.task.done()):
        hub.running = None
    if hub.running:
        raise web.HTTPConflict(text=f"{hub.running} is running")
    if not hub.connected:
        raise web.HTTPConflict(
            text=f"The link to the device is down. Press {link_action(False)}.")

    tier = body.get("tier", "basic")
    if tier not in I.TIERS:
        raise web.HTTPBadRequest(text=f"tier must be one of {I.TIERS}")
    params = dict(body.get("params", {}))
    if body.get("consent"):
        params["consent"] = body["consent"]

    try:
        cfg = await hub.begin(name, params, tier,
                              body.get("overrides") or {})
    except I.EnforcedViolation as e:
        return web.json_response({"error": "enforced", "text": str(e)},
                                 status=403)
    except Exception as e:
        raise web.HTTPConflict(text=f"{type(e).__name__}: {e}")

    hub.task = asyncio.create_task(hub.execute(name, params, cfg))
    return web.json_response({"ok": True, "cfg": _jsonable_cfg(cfg)})


def _jsonable_cfg(cfg: dict) -> dict:
    return json.loads(json.dumps(cfg, default=str))


async def api_tone(r):
    body = await body_of(r)
    # The cue text is handed to the player as one argv element, so nothing
    # ever parses it as a command. What does still parse it is an option
    # reader: a value beginning with a hyphen is taken for a flag rather than
    # for words to say, and the cue silently does something other than cue.
    text = str(body.get("text", "Test cue"))
    if text.startswith("-"):
        raise web.HTTPBadRequest(text="a cue cannot start with a hyphen")
    audio.play(body.get("tone", "none"), text)
    return web.json_response(audio.available())


async def api_live(r):
    body = await body_of(r)
    try:
        await hub.set_live(bool(body.get("on")))
    except Exception as e:
        raise web.HTTPConflict(text=str(e))
    await hub.emit(hub.status())
    return web.json_response({"live": hub.live})


async def api_config(r):
    """Change settings the device declared.

    The body's keys are control names from `caps.controls`, and a name this
    application has no setter for is refused by name rather than accepted and
    dropped.
    """
    body = await body_of(r)
    try:
        applied = await hub.configure(dict(body))
    except Exception as e:
        raise web.HTTPConflict(text=f"{type(e).__name__}: {e}")
    return web.json_response({"applied": applied, "status": hub.status()})


async def api_model(_r):
    """The device's model and its head slots, or that it has none."""
    try:
        return web.json_response(await hub.model())
    except Exception as e:
        raise web.HTTPConflict(text=f"{type(e).__name__}: {e}")


async def api_select_head(r):
    body = await body_of(r)
    if body.get("slot") is None:
        raise web.HTTPBadRequest(text="no slot given")
    try:
        return web.json_response(await hub.select_head(int(body["slot"])))
    except Exception as e:
        raise web.HTTPConflict(text=f"{type(e).__name__}: {e}")


async def api_predictions(r):
    body = await body_of(r)
    try:
        return web.json_response(await hub.set_predictions(bool(body.get("on"))))
    except Exception as e:
        raise web.HTTPConflict(text=f"{type(e).__name__}: {e}")


async def api_identify(r):
    """Blink the light on the connected device for a few seconds."""
    body = await body_of(r)
    if not hub.connected:
        raise web.HTTPConflict(text="The link to the device is down.")
    seconds = body.get("seconds", 5)
    try:
        seconds = int(seconds)
        if not 0 <= seconds <= P.IDENTIFY_MAX_SECONDS:
            raise ValueError
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text=f"seconds is a whole number from 0 to {P.IDENTIFY_MAX_SECONDS}")
    try:
        return web.json_response(await hub.identify(seconds))
    except Exception as e:
        raise web.HTTPConflict(text=f"{type(e).__name__}: {e}")


async def api_registers(_r):
    """The converter's registers, raw and in words, for debugging."""
    if not hub.connected:
        raise web.HTTPConflict(text="The link to the device is down.")
    if hub.running:
        raise web.HTTPConflict(text=f"{hub.running} is running")
    try:
        return web.json_response(await hub.registers())
    except Exception as e:
        raise web.HTTPConflict(text=f"{type(e).__name__}: {e}")


async def api_embeddings(r):
    """Collect whole windows of the encoder's output from the live stream."""
    body = await body_of(r)
    if not hub.connected:
        raise web.HTTPConflict(text="The link to the device is down.")
    try:
        return web.json_response(await hub.collect_embeddings(body.get("windows", 1), str(body.get("form", "window"))))
    except Exception as e:
        raise web.HTTPConflict(text=f"{type(e).__name__}: {e}")


async def api_disconnect(_r):
    try:
        await hub.disconnect()
    except Exception as e:
        raise web.HTTPConflict(text=f"{type(e).__name__}: {e}")
    return web.json_response(hub.status())


async def api_delete(r):
    """Delete a recording and everything that belongs to it.

    Delete means delete. Captures are written read-only so nothing edits them
    in place, but that is tamper-evidence, not a reason to keep a spoiled run
    forever: a botched recording left in the corpus reads as valid to every
    later analysis.
    """
    body = await body_of(r)
    labels = body.get("labels") or []
    removed, errors = [], []
    for label in labels:
        if not _safe_label(label):
            errors.append(f"{label}: refused")
            continue
        gone = []
        for ext in (".npz", ".meta.json", ".events.json", ".sha256"):
            p = A.CAPTURES / f"{label}{ext}"
            try:
                if p.exists():
                    p.unlink()
                    gone.append(ext)
            except Exception as e:
                errors.append(f"{label}{ext}: {e}")
        if gone:
            removed.append(dict(label=label, files=gone))
        else:
            errors.append(f"{label}: nothing to delete")
    if removed:
        await hub.emit({"type": "note",
                        "text": "deleted " + ", ".join(r["label"] for r in removed)})
    return web.json_response({"deleted": removed, "errors": errors})


# Every export goes through the library's exporter. This application writes no
# file format of its own: a second writer is a second set of rules about what a
# capture means, and the two would drift.
EXPORTS = HERE / "exports"


async def api_formats(_r):
    """The formats the library writes. The page offers these and no others, so
    a format added there appears here without an edit."""
    return web.json_response([dict(name=f, suffix=EXPORT.SUFFIX[f])
                              for f in EXPORT.FORMATS])


async def api_export(r):
    """Write selected captures out, and report what each one cost.

    The exporter measures whether a format returned this capture's samples to
    the count they were recorded at, per file, so the summary states a fact
    about this export rather than a claim about the format.
    """
    body = await body_of(r)
    labels = body.get("labels") or []
    fmt = str(body.get("format") or "").strip().lower()
    if not labels:
        raise web.HTTPBadRequest(text="no captures selected")
    if fmt not in EXPORT.FORMATS:
        raise web.HTTPBadRequest(
            text=f"unknown format {fmt!r}. Known formats: "
                 f"{', '.join(EXPORT.FORMATS)}")
    EXPORTS.mkdir(exist_ok=True)
    out, errs = [], []
    for label in labels:
        if not _safe_label(label):
            errs.append(f"{label}: refused")
            continue
        try:
            out.append(await asyncio.to_thread(
                EXPORT.export, label, fmt, EXPORTS))
        except Exception as e:
            errs.append(f"{label}: {type(e).__name__}: {e}")
    if not out:
        raise web.HTTPBadRequest(text="; ".join(errs))
    return web.json_response({"exported": out, "errors": errs})


def _safe_label(label: str) -> bool:
    """A label names one capture, so it is one path component and never a
    path. This is the only thing standing between a label and the filesystem."""
    return bool(label) and "/" not in label and "\\" not in label \
        and not label.startswith(".")


async def api_contact_check(_r):
    """Check electrode contact once, on demand. The current is on only for the
    duration of the check."""
    if not hub.connected:
        raise web.HTTPConflict(text="The link to the device is down.")
    if not hub.dev.can("leadoff"):
        raise web.HTTPConflict(
            text="this device does not detect lead-off, so contact cannot be "
                 "checked")
    if hub.running:
        raise web.HTTPConflict(text=f"{hub.running} is running")
    try:
        return web.json_response(await hub.check_contact())
    except Exception as e:
        raise web.HTTPConflict(text=f"{type(e).__name__}: {e}")


async def api_download(r):
    """Send an exported file to the browser.

    Export used to write a file into a folder on the server and stop there. On
    this machine the server IS the user's machine, so it looked like it worked.
    Hosted, the user has no filesystem there and nothing ever reaches them.
    """
    label, fmt = r.match_info["label"], r.match_info["fmt"]
    if fmt not in EXPORT.FORMATS:
        raise web.HTTPBadRequest(text=f"unknown format {fmt!r}")
    if not _safe_label(label):
        raise web.HTTPBadRequest(text="a label is one name, not a path")
    name = f"{label}{EXPORT.SUFFIX[fmt]}"
    path = EXPORTS / name
    if not path.exists():
        raise web.HTTPNotFound(text=f"{name} has not been produced")
    return web.FileResponse(path, headers={
        "Content-Disposition": f'attachment; filename="{name}"'})


async def api_scan(_r):
    """Start listening for devices. They reach the page as they answer."""
    hub.start_scan()
    return web.json_response(dict(scanning=True, devices=hub.devices()))


async def api_connect(r):
    """Connect the one device the person picked."""
    body = await r.json()
    try:
        await hub.connect(str(body.get("address", "")))
    except Exception as e:
        raise web.HTTPConflict(text=f"{type(e).__name__}: {e}")
    return web.json_response(hub.status())


async def api_stop(_r):
    if hub.ctx:
        hub.ctx.stop.set()
        return web.json_response({"ok": True})
    return web.json_response({"ok": False, "reason": "nothing running"})


async def api_marker(r):
    body = await body_of(r)
    if not hub.ctx:
        raise web.HTTPConflict(text="no experiment running")
    e = hub.ctx.marker(body.get("name", "mark"))
    await hub.emit({"type": "marker", "event": e, "count": len(hub.ctx.events)})
    return web.json_response(e)


# Analyses are pure: the same capture, analysis and parameters always give the
# same answer, and a capture is immutable once written. So a result computed
# once need never be computed again, which is what lets the page analyze on
# selection instead of on a button press. Keyed by mtime too, so the one case
# where a label is reused (a deleted recording's name) cannot serve a stale hit.
_ANA_CACHE: dict = {}
_ANA_CACHE_MAX = 64


def _ana_key(which, labels, kw):
    stamp = []
    for lb in labels:
        p = A.CAPTURES / f"{lb}.npz"
        stamp.append((lb, p.stat().st_mtime_ns if p.exists() else 0))
    return json.dumps([which, stamp, sorted(kw.items())], default=str)


async def api_analyze(r):
    body = await body_of(r)
    which, labels = field(body, "analysis"), field(body, "labels")
    if which not in A.ANALYSES:
        raise web.HTTPBadRequest(text=f"unknown analysis {which}")
    # An analysis reaches a capture by joining its label to a path, exactly as
    # delete, export and download do, so it gets the same rule they get.
    if not all(_safe_label(label) for label in labels):
        raise web.HTTPBadRequest(text="a label is one name, not a path")
    spec = A.ANALYSES[which]
    allowed = {p["name"] for p in spec.get("params", [])}
    kw = {k: v for k, v in (body.get("params") or {}).items() if k in allowed}
    # 0 and "" disable a stage of an analysis filter, so they are kept rather
    # than coerced back to the default.
    key = _ana_key(which, labels, kw)
    hit = _ANA_CACHE.get(key)
    if hit is not None:
        return web.json_response({**hit, "_cached": True})
    try:
        if which == "psd" and len(labels) > 1:
            out = await asyncio.to_thread(lambda: A.compare(labels, **kw))
        else:
            out = await asyncio.to_thread(lambda: spec["fn"](labels[0], **kw))
        out["_analysis"] = which
        out["_params"] = kw
    except Exception as e:
        traceback.print_exc()
        raise web.HTTPBadRequest(text=f"{type(e).__name__}: {e}")
    payload = json.loads(json.dumps(out, default=float))
    if len(_ANA_CACHE) >= _ANA_CACHE_MAX:
        _ANA_CACHE.pop(next(iter(_ANA_CACHE)))
    _ANA_CACHE[key] = payload
    return web.json_response(payload)


async def ws_handler(r):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(r)
    hub.clients.add(ws)
    await ws.send_str(json.dumps(hub.status()))
    try:
        async for m in ws:
            if m.type == WSMsgType.ERROR:
                break
    finally:
        hub.clients.discard(ws)
    return ws


async def index(_r):
    return web.Response(text=(HERE / "hub.html").read_text(),
                        content_type="text/html")


async def api_docs(r):
    """The Command Center's own documentation, served by the Command Center.

    Two audiences, kept apart because they share almost nothing: operating the
    app, and extending it. Documentation for the device itself and for the
    device API is published separately -- neither is this application's to
    serve.
    """
    name = r.match_info["page"]
    if name not in ("user", "dev", "_style.css"):
        raise web.HTTPNotFound(text=f"no such documentation page: {name}")
    if name.endswith(".css"):
        return web.Response(text=(HERE / "docs" / name).read_text(),
                            content_type="text/css")
    return web.Response(text=(HERE / "docs" / f"{name}.html").read_text(),
                        content_type="text/html")


# The hub answers only its own machine. A browser carries a POST or a
# WebSocket from any open web page to 127.0.0.1, and WebSockets are not
# subject to the same-origin policy at all, so without this guard a foreign
# page could start runs, delete captures, or read the live stream. Host names
# who the browser thinks it is talking to, so a rebound DNS name fails it.
# Origin names which page asked, so a foreign page fails it. curl and the
# tools send a bare local Host and no Origin, and keep working.
LOCAL = {"localhost:8080", "127.0.0.1:8080", "[::1]:8080"}


@web.middleware
async def same_machine_only(request, handler):
    if request.headers.get("Host") not in LOCAL:
        raise web.HTTPForbidden(text="this hub answers only its own machine")
    origin = request.headers.get("Origin")
    if origin is not None and urlsplit(origin).netloc not in LOCAL:
        raise web.HTTPForbidden(text="this hub answers only its own pages")
    return await handler(request)


async def main():
    app = web.Application(middlewares=[same_machine_only])
    app.add_routes([
        web.get("/", index),
        web.get("/docs/{page}", api_docs),
        web.get("/ws", ws_handler),
        web.get("/api/experiments", api_experiments),
        web.get("/api/analyses", api_analyses),
        web.get("/api/captures", api_captures),
        web.get("/api/status", api_status),
        web.get("/api/model", api_model),
        web.get("/api/formats", api_formats),
        web.post("/api/run", api_run),
        web.post("/api/live", api_live),
        web.post("/api/scan", api_scan),
        web.post("/api/connect", api_connect),
        web.post("/api/disconnect", api_disconnect),
        web.post("/api/delete", api_delete),
        web.post("/api/contact_check", api_contact_check),
        web.post("/api/config", api_config),
        web.post("/api/head", api_select_head),
        web.post("/api/predictions", api_predictions),
        web.post("/api/identify", api_identify),
        web.get("/api/registers", api_registers),
        web.post("/api/embeddings", api_embeddings),
        web.post("/api/export", api_export),
        web.get("/api/download/{fmt}/{label}", api_download),
        web.post("/api/tone", api_tone),
        web.post("/api/stop", api_stop),
        web.post("/api/marker", api_marker),
        web.post("/api/analyze", api_analyze),
        web.static("/assets", HERE / "assets"),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 8080).start()
    asyncio.create_task(hub.flusher())
    # Nothing connects by itself. The page lists the devices on the air and
    # connects the one picked; until then it serves captures and analyses.
    print("hub at http://localhost:8080: open it and pick your device")
    await hub.emit(hub.status())
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
