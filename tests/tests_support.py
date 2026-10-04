"""Shared fakes. Kept out of the test modules so every suite uses one device.

The fake carries a REAL `protocol.DeviceInfo` and borrows the real
`Device.profile` and `Device.can`, so what the tests render from is the
library's own answer rather than a second description of a device that could
drift from it. A capability the contract adds shows up here as soon as a test
sets its bit.
"""
from __future__ import annotations
import numpy as np

from intomind import protocol as P
from intomind.client import Device

FS = 500.0

#: Everything the first device to speak this contract claims. Tests that want
#: a narrower device pass their own set of names.
FULL = ("battery_voltage", "leadoff", "test_signal", "input_short", "update",
        "model", "model_ready", "heads", "pipeline",
        "indicator", "converter_registers", "embeddings")


def caps(*names) -> int:
    return sum(P.CAPABILITIES[n] for n in names)


def device_info(*, channels=4, capabilities=None, rates=(250, 500, 1000),
                embed=64, slots=4, capabilities_high=0) -> P.DeviceInfo:
    """A DeviceInfo as the protocol defines it, not as a test imagines it."""
    bit = {sps: b for b, sps in P.RATE_BY_INFO_BIT.items()}
    return P.DeviceInfo(
        protocol=(1, 0), firmware=(1, 4, 1), channels=channels, adc_bits=24,
        tick_hz=32768, vref_uv=2_420_000,
        capabilities=caps(*(FULL if capabilities is None else capabilities)),
        supported_rates=sum(1 << bit[r] for r in rates),
        device_id="00112233445566", hardware=(1, 2, 0),
        model_embed_dim=embed, head_slots=slots, head_max_outputs=8,
        capabilities_high=capabilities_high)


class FakeDev:
    """A device that answers for itself, as the contract requires."""

    ADAPTER = dict(name="intomind.ble", version="1", first_party=True,
                   transport="bluetooth-le")

    connected = True
    name = "IntoMind-FAKE"

    # The device's own answers, so what the hub renders is what the library
    # composes rather than what this file decided a device looks like.
    profile = Device.profile
    can = Device.can

    def __init__(self, dc: float = 1500.0, gaps: int = 0, leadoff: int = 0x00,
                 info: P.DeviceInfo | None = None):
        self.on_sample = None
        self.on_packet = None
        self.on_prediction = None
        self.dc, self.gaps = dc, gaps
        # The lead-off byte the device would report. 0x00 = every electrode on.
        # Contact is decoded from this, never from `dc`.
        self.leadoff = leadoff
        self.streaming = False
        self.commands: list = []
        self.gaps_announced = 0
        self.info = info if info is not None else device_info()
        # What the device is set to, from the device. Empty until a read.
        self.config: dict = {}
        self.predictions_on = False
        self.active_head = 0
        # The chain on the device's signal path: its own default at first.
        self.chain = list(P.default_chain(500))
        self.chain_origin = "default"
        self._heads = [P.Head(0, "valid", 3, "aabbccdd11223344", "focus"),
                       P.Head(1, "empty", 0, "0" * 16, "")]
        # 1.2: the light, the registers, the encoder's output.
        self.indicator_level = "reserved"
        self.identified: list = []
        # 1.3: the name and the adjective the device keeps, and its cadence.
        self.device_name = ("", "")
        self.interval_s, self.interval_min_s = 0, 4
        self.embeddings_form = "off"
        self.on_embedding = None

    # --- configuration -----------------------------------------------------
    async def refresh_config(self):
        self.config.setdefault("gain", 12)
        self.config.setdefault("rate_sps", 500)
        self.config.setdefault("mode", "normal")
        self.config.setdefault("leadoff", False)
        if self.can("pipeline"):
            self.config["processing"] = dict(
                origin=self.chain_origin, stages=[s.as_dict() for s in self.chain],
                description=P.describe_chain(self.chain, self.config["rate_sps"]))
        if self.can("indicator"):
            self.config["indicator"] = self.indicator_level
        if self.can("model_cadence"):
            self.config["model_interval_s"] = self.interval_s
            self.config["model_interval_min_s"] = self.interval_min_s
        if self.can("device_name"):
            self.config["name"], self.config["adjective"] = self.device_name
            self.config["composed_name"] = P.compose_name(*self.device_name)
        return dict(self.config)

    # --- 1.3: the name and the cadence -------------------------------------
    async def get_name(self):
        if not self.can("device_name"):
            raise RuntimeError("this device does not claim the device_name capability")
        return self.device_name

    async def set_name(self, name, adjective):
        if not self.can("device_name"):
            raise RuntimeError("this device does not claim the device_name capability")
        if not P.name_fits(name, adjective):
            raise ValueError("does not fit")
        self.device_name = (name, adjective)
        self.commands.append((P.OPCODES["set_name"], P.encode_name_parts(name, adjective)))

    async def model_interval(self):
        if not self.can("model_cadence"):
            raise RuntimeError("this device does not claim the model_cadence capability")
        return P.ModelInterval(self.interval_s, self.interval_min_s)

    async def set_model_interval(self, seconds):
        if not self.can("model_cadence"):
            raise RuntimeError("this device does not claim the model_cadence capability")
        if seconds != 0 and seconds < self.interval_min_s:
            raise RuntimeError("op 0x88 -> status 1")
        self.interval_s = int(seconds)
        self.commands.append((P.OPCODES["set_model_interval"], int(seconds)))

    # --- the processing chain ----------------------------------------------
    def _chain_op(self):
        if not self.can("pipeline"):
            raise RuntimeError("this device does not claim the pipeline capability")
        if self.streaming:
            raise RuntimeError("op -> status 3")   # busy, as the device answers

    async def pipeline(self):
        if not self.can("pipeline"):
            raise RuntimeError("this device does not claim the pipeline capability")
        return P.PipelineState(self.chain_origin, list(self.chain))

    async def set_pipeline(self, stages):
        self._chain_op()
        P.check_chain(stages, self.config.get("rate_sps", 500))
        self.chain, self.chain_origin = list(stages), "host"
        self.commands.append((P.OPCODES["set_pipeline"], P.encode_chain(stages)))

    async def clear_pipeline(self):
        self._chain_op()
        self.chain, self.chain_origin = [], "host"
        self.commands.append((P.OPCODES["clear_pipeline"], None))

    async def restore_pipeline_default(self):
        self._chain_op()
        self.chain = list(P.default_chain(self.config.get("rate_sps", 500)))
        self.chain_origin = "default"
        self.commands.append((P.OPCODES["restore_pipeline_default"], None))

    async def set_gain(self, gain):
        if gain not in P.GAIN_BY_CODE:
            raise ValueError(f"this device has no gain {gain}x")
        self.config["gain"] = int(gain)

    async def set_rate(self, sps):
        self.config["rate_sps"] = int(sps)

    async def set_mode(self, mode):
        # The device runs one model at a time (1.3): no generator while the
        # foundation model is in use.
        if int(mode) == P.MODES["synthetic"] and (
                getattr(self, "predictions_on", False)
                or getattr(self, "embeddings_form", "off") not in (None, "off")):
            raise RuntimeError("op 0x20 -> status 3")
        self.config["mode"] = P.MODE_BY_CODE[int(mode)]

    async def set_leadoff(self, on):
        if not self.can("leadoff"):
            raise RuntimeError("this device does not detect lead-off")
        self.commands.append((P.OPCODES["set_leadoff"], 1 if on else 0))
        self.config["leadoff"] = bool(on)

    # --- the light, the registers, embeddings (1.2) --------------------------
    async def indicator(self):
        if not self.can("indicator"):
            raise RuntimeError("this device does not claim the indicator capability")
        return self.indicator_level

    async def set_indicator(self, level):
        if not self.can("indicator"):
            raise RuntimeError("this device does not claim the indicator capability")
        if level not in P.INDICATOR_LEVELS:
            raise ValueError(level)
        self.indicator_level = level
        self.commands.append((P.OPCODES["set_indicator"], P.INDICATOR_LEVELS[level]))

    async def identify(self, seconds=5):
        if not self.can("indicator"):
            raise RuntimeError("this device does not claim the indicator capability")
        self.identified.append(int(seconds))
        self.commands.append((P.OPCODES["identify"], int(seconds)))

    async def converter_registers(self):
        if not self.can("converter_registers"):
            raise RuntimeError("this device does not claim the converter_registers capability")
        if self.streaming:
            raise RuntimeError("op -> status 3")
        values = bytes.fromhex("3c95d1e00065656565" + "00" * 11 + "0f200000")
        return P.ConverterRegisters(P.CONVERTER_FAMILIES[1], 1, 0, values)

    async def set_embeddings(self, form, on_window=None):
        if not self.can("embeddings"):
            raise RuntimeError("this device does not claim the embeddings capability")
        self.embeddings_form = form
        if on_window is not None:
            self.on_embedding = on_window
        self.commands.append((P.OPCODES["set_embeddings"], P.EMBEDDING_FORMS[form]))
        # A device that streams answers with a whole window at once here, so
        # a test sees the collection finish without waiting on a model.
        if form != "off" and self.streaming and self.on_embedding:
            w = P.EmbeddingWindow(index=1000, device_time=123456, window_samples=2000,
                                  encoder_id="1122334455667788", input_source="stream",
                                  embed_dim=4, gap_in_window=False, duty_reduced=False,
                                  leadoff_in_window=False,
                                  embedding=[1, 2, 3, 4] if form in ("window", "both") else None,
                                  tokens=[[1, 2, 3, 4]] * (self.info.channels * 20) if form in ("tokens", "both") else None)
            self.on_embedding(self, w)

    # --- the model ---------------------------------------------------------
    async def model_info(self):
        return P.ModelInfo("ready", self.active_head, self.predictions_on,
                           "1122334455667788", (1, 0, 0), tokens_per_channel=20,
                           pass_ms=3150, interval_s=self.interval_s,
                           generator=self.can("synthetic"))

    async def heads(self):
        return self.active_head, list(self._heads)

    async def select_head(self, slot):
        self.active_head = int(slot)

    async def set_predictions(self, on):
        if self.active_head is None:
            raise RuntimeError("no head is selected")
        self.predictions_on = bool(on)

    # --- the stream --------------------------------------------------------
    async def start(self, samples_per_packet: int = 25):
        self.streaming = True

    async def stop(self):
        # A device that is not streaming answers with an error, and
        # `Device.command` raises on any non-zero status. Model that.
        if not self.streaming:
            raise RuntimeError("stop: not streaming")
        self.streaming = False

    async def command(self, op, arg=None, timeout=6.0):
        self.commands.append((op, arg))
        return b""


def fake_sample(*uv, leadoff: int = 0):
    return type("S", (), dict(uv=tuple(uv), leadoff=leadoff))()


def fake_prediction(slot: int = 0, outputs=(0.9, 0.05, 0.05), **flags):
    return P.Prediction(
        head_slot=slot, head_id="aabbccdd11223344", index=1000,
        device_time=123456, window_samples=2000,
        gap_in_window=flags.get("gap", False),
        duty_reduced=flags.get("duty", False),
        leadoff_in_window=flags.get("leadoff", False),
        outputs=list(outputs),
        input_source=flags.get("source", "stream"))


class FakeCapture:
    """A capture shaped the way `export` reads one, so the export route can be
    tested with nothing on disk. `export` takes a `load=` for exactly this."""

    fs = FS
    lsb_uv = 0.0298
    repairs: list = []

    def __init__(self, n: int = 500, nch: int = 2):
        self.nch = nch
        self.counts = np.tile(np.arange(n, dtype=np.int64) % 997, (nch, 1))
        self.meta = dict(label="fake", n=n, fs_effective=self.fs)

    def uv(self, c):
        return self.counts[c] * self.lsb_uv
