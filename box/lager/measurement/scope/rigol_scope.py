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

    def _daemon_url(self) -> str:
        from lager.measurement.scope import routing
        return routing.daemon_url_for(self._record)

    def _instrument_key(self) -> str:
        return "rigol:%s" % (self.address or self.netname)

    def stream_start(self, *args, **kwargs):
        from lager.measurement.scope import routing
        if not routing.has_live_view(self._record):
            # The trace streams from the HDMI output; without a card the
            # daemon would never publish a frame, and the stream would wait
            # out its timeout to say nothing useful.
            raise UnsupportedScopeFeature(
                "no HDMI capture card is paired with this scope, so it cannot "
                "stream; pair one with `lager nets set-capture`. Single "
                "captures and measurements work without it")
        return super().stream_start(*args, **kwargs)

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
