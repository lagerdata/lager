# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Collection rules for cli/tests/.

test_box_lager_imports.py is a standalone import-verification REPORT, not a
pytest suite. It contains no assert statements at all: its 16 `test_*`
functions record outcomes into a module-level RESULTS dict via log_pass() /
log_fail() and print a summary. Collected by pytest they pass unconditionally
-- including for modules that do not exist -- so gating on them would add 16
checks that cannot fail. It is excluded here rather than deleted because it
remains useful when run directly:

    python cli/tests/test_box_lager_imports.py

Its syntax is still covered by the repo-wide compile check. Converting it into
a real pytest suite means deciding what to do with the root-level import
aliases it probes (lager.supply, lager.uart, lager.scope, ...), most of which
went away with the same consolidation that removed lager.adc/dac/gpio -- see
cli/tests/test_io_imports.py. That is a follow-up, not a CI change.
"""

collect_ignore = ["test_box_lager_imports.py"]
