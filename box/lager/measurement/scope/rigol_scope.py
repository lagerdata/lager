# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Scope operations for a Rigol MSO5000 net, over the oscilloscope daemon.

The same driver as a PicoScope's -- the daemon hides the make -- plus what a
Rigol has and a PicoScope does not: autoscale and trigger coupling, and raw
SCPI for everything else the scope offers. The daemon holds the scope's only
connection, so raw SCPI goes through it too.
"""
from __future__ import annotations

from lager.measurement.scope.picoscope import PicoScope, UnsupportedScopeFeature

_TRIGGER_COUPLINGS = {
    "dc": "DC",
    "ac": "AC",
    "lf_reject": "LFReject",
    "low_freq_rej": "LFReject",
    "lfreject": "LFReject",
    "hf_reject": "HFReject",
    "high_freq_rej": "HFReject",
    "hfreject": "HFReject",
}


class RigolScope(PicoScope):
    def __init__(self, address=None, pin=None, channel=None, netname=None,
                 record=None, **kwargs):
        super().__init__(address=address, pin=pin, channel=channel,
                         netname=netname, **kwargs)
        self._record = dict(record or {})
        if address and not self._record.get("address"):
            self._record["address"] = address
        self._record.setdefault("instrument", "Rigol_MSO5000")
        self._paired = None

    @property
    def client(self):
        from lager.measurement.scope import routing
        # The hardware service keeps one driver per scope for as long as it
        # runs, and `lager nets set-capture` changes the pairing under it. A
        # new pairing restarts the daemon on a new port, so the connection
        # follows it instead of answering from the old daemon until a restart.
        self._record = self._saved_record()
        paired = routing.capture_of(self._record)
        if self._client is not None and paired != self._paired:
            self.close()
        self._paired = paired
        return super().client

    def _saved_record(self) -> dict:
        if not self.netname:
            return self._record
        try:
            from lager.nets.net import Net
            for saved in Net.get_local_nets():
                if saved.get("name") == self.netname:
                    record = {k: v for k, v in self._record.items() if k != "capture"}
                    record.update(saved)
                    return record
        except Exception:
            pass
        return self._record

    def _daemon_url(self) -> str:
        from lager.measurement.scope import routing
        return routing.daemon_url_for(self._record)

    def _command(self, name, **params):
        from lager.measurement.scope import daemon_client
        try:
            return super()._command(name, **params)
        except daemon_client.ScopeDaemonUnavailable:
            # The daemon restarted on a new port since the last command (the
            # hardware service keeps this driver): look it up again, once.
            self.close()
            return super()._command(name, **params)

    def _instrument_key(self) -> str:
        return "rigol:%s" % (self.address or self.netname)

    def _require_live_view(self):
        from lager.measurement.scope import routing
        if not routing.has_live_view(self._saved_record()):
            # The trace streams from the HDMI output; without a card the
            # daemon would never publish a frame, and the stream would wait
            # out its timeout to say nothing useful.
            raise UnsupportedScopeFeature(
                "no HDMI capture card is paired with this scope, so it cannot "
                "stream; pair one with `lager nets set-capture`. Single "
                "captures and measurements work without it")

    def stream_start(self, *args, **kwargs):
        self._require_live_view()
        return super().stream_start(*args, **kwargs)

    def stream_frames(self, *args, **kwargs):
        self._require_live_view()
        return super().stream_frames(*args, **kwargs)

    def stream_capture(self, *args, **kwargs):
        self._require_live_view()
        return super().stream_capture(*args, **kwargs)

    def scpi(self, command: str):
        """Send one SCPI command; a query returns the scope's reply."""
        return self._command("Scpi", scpi=str(command)).get("reply")

    def autoscale(self):
        self.scpi(":AUToscale")
        # Autoscale takes seconds and returns at once; ask until it is done.
        self.scpi("*OPC?")
        return {"status": "autoscaled"}

    def set_trigger_coupling(self, coupling):
        token = _TRIGGER_COUPLINGS.get(str(coupling).strip().lower())
        if token is None:
            raise ValueError(
                "trigger coupling %r is not one of %s"
                % (coupling, ", ".join(sorted(_TRIGGER_COUPLINGS))))
        self.scpi(":TRIGger:COUPling %s" % token)
        return {"trigger_coupling": token}

    def get_trigger_coupling(self):
        return self.scpi(":TRIGger:COUPling?")
