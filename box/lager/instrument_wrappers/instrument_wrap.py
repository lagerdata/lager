# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from .keithley_defines import (
    OvercurrentProtectionError,
    OvervoltageProtectionError,
    OvertemperatureProtectionError,
    SenseLeadsReversedError,
)

class InstrumentError(Exception):
    pass

class InstrumentWrap:
    def __init__(self, instr):
        self.instr = instr
        self.timeout = 10_000

    def _check_errors(self, response):
        err = self.instr.query(":SYST:ERR?").strip()
        # Handle both "code,message" and single-value error responses
        if ',' in err:
            code, message = err.split(',', 1)
            code = int(code, 10)
        else:
            # If no comma, try to parse as integer code
            try:
                code = int(err, 10)
                message = "Unknown error"
            except ValueError:
                # Not an integer, treat whole string as error message
                code = -1
                message = err

        if code == 0:
            return response

        raise InstrumentError(code, message, response)

    def query(self, q, *, check_errors=True, raw=False):
        response = self.instr.query(q)
        if not raw:
            response = response.strip()
        if not check_errors:
            return response
        return self._check_errors(response)

    def write(self, cmd, *, check_errors=True):
        response = self.instr.write(cmd)
        if not check_errors:
            return response
        return self._check_errors(response)

    @property
    def timeout(self):
        return self.instr.timeout

    @timeout.setter
    def timeout(self, value):
        self.instr.timeout = value

class InstrumentWrapKeithley(InstrumentWrap):
    def __init__(self, instr):
        super(InstrumentWrapKeithley, self).__init__(instr)

    def get_questionable_instrument_event_register(self):
        return int(self.instr.query(":STATus:QUEStionable:INSTrument:ISUMmary:CONDition?"))

    def get_qie_errors(self):
        reg = self.get_questionable_instrument_event_register()
        if reg & (1 << 2) > 0:
            raise OvercurrentProtectionError()
        elif reg & (1 << 3) > 0:
            raise OvervoltageProtectionError()
        elif reg & (1 << 4) > 0:
            raise OvertemperatureProtectionError()
        elif reg & (1 << 5) > 0:
            raise SenseLeadsReversedError()

    def _check_errors(self, response):
        self.get_qie_errors()

        err = self.instr.query(":SYST:ERR?").strip()
        code, message = err.split(',', 1)
        code = int(code, 10)
        if code == 0:
            return response

        raise InstrumentError(code, message, response)