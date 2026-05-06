# Firmware Binaries

This directory is a placeholder for firmware binaries used by the debug/flash integration tests.

Firmware binaries are not included in the open-source release. To run flash tests, place your own firmware files here:

- `.elf` files (e.g., for ARM targets)
- `.hex` files (e.g., for Nordic or Renesas targets)

Then update the test scripts in `test/integration/communication/` to reference your filenames.

<!-- Copyright 2024-2026 Lager Data -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
