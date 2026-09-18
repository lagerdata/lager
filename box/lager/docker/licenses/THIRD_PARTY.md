# Third-party software in the Lager Box image

This file is in the image at `/usr/share/licenses/lager/THIRD_PARTY.md`. Beside it
are `LICENSE` and `NOTICE` for Lager itself, and `pip/`, which holds the license
notices of every Python distribution in the image (`pip/INDEX.tsv` lists them).

`box/lager/docker/box.Dockerfile` installs software from three kinds of source:

- **Debian packages**, from the Debian archive of the base image. Each keeps its
  notice at `/usr/share/doc/<package>/copyright`. They are not listed one by one
  here. OpenOCD is listed because issue #532 names it.
- **Python distributions**, from PyPI. `pip/INDEX.tsv` records the license that
  each one declares, and `pip/<name>-<version>/` holds its notice files. They
  are not listed one by one here.
- **Everything else**: a vendor download, a source build, a toolchain installer.
  Nothing records these unless this file does. The table below lists every one.

`test/unit/box/test_box_image_notices.py` reads the Dockerfile and fails when it
finds a download of the third kind that has no row here, or a row whose pin no
longer matches the Dockerfile.

## What this file is not

The **Declared license** column records what the upstream project says about
itself. It is not a review. **Nothing in this file states that a component can
be redistributed in a public image.** That review is tracked in issue #532 and
is not complete. "Not reviewed" marks the rows where the terms are a vendor's
own and have not been read for that purpose.

## Components

| Component | Pin | How it is installed | Declared license | Notice in the image | Source |
|-----------|-----|---------------------|------------------|---------------------|--------|
| OpenOCD | Debian package version | `apt-get install openocd` | GPL-2.0-or-later | `/usr/share/doc/openocd/copyright` | Debian archive |
| Phidget22 library (`libphidget22`) | unpinned (latest at build) | Vendor apt repository, `apt-get install libphidget22` | Not reviewed. Vendor terms | `/usr/share/doc/libphidget22/copyright`, if the package ships one | `http://www.phidgets.com/debian`, key `https://www.phidgets.com/gpgkey/pubring.gpg` |
| Node.js and npm | `NODE_VERSION=20.18.1` | Official binary tarball, checked against `SHASUMS256.txt` | MIT. The tarball bundles other projects under their own licenses | `/usr/local/LICENSE` | `https://nodejs.org/dist/` |
| MCC Universal Library for Linux (`uldaq`, C library) | `--branch v1.2.1` | Source build | MIT | None. The source tree is removed after the build | `https://github.com/mccdaq/uldaq.git` |
| LabJack LJM | `LabJack-LJM_2024-06-10.zip` | Vendor installer. x64 only | Not reviewed. Proprietary vendor terms | None collected | `https://files.labjack.com/installers/LJM/Linux/x64/release/LabJack-LJM_2024-06-10.zip` |
| LabJack Exodriver (`liblabjackusb`) | `--branch v2.7.0` | Source build | MIT (X11) | None. The source tree is removed after the build | `https://github.com/labjack/exodriver.git` |
| pykush (YKUSH hub control) | commit `d83ee856b4d0ea961b63b531fe4f4c533f6962f6` | `pip install` from the repository | As declared in `pip/INDEX.tsv` | `pip/` | `git+https://github.com/Yepkit/pykush` |
| asusrouter | commit `8de97bfa8ffe3efa2f6d1ec30bb95187d13ab37a` | `pip install` from the repository | As declared in `pip/INDEX.tsv` | `pip/` | `git+https://github.com/Vaskivskyi/asusrouter.git` |
| Rust toolchain (rustup, cargo, rustc) | unpinned (latest at build). `--default-toolchain stable` | The rustup installer script | MIT OR Apache-2.0 | Under `/opt/rust`, as the toolchain ships them | `https://sh.rustup.rs` |
| defmt-print | `defmt-print@1.1.0`, `--locked` | `cargo install`. The build links many crates, each under its own license | MIT OR Apache-2.0 | None collected for the linked crates | crates.io |
| nrfutil | unpinned (latest at build) | Vendor binary | Not reviewed. Proprietary vendor terms | None collected | `https://developer.nordicsemi.com/.pc-tools/nrfutil/x64-linux/nrfutil` |
| nrfutil `nrf5sdk-tools` | unpinned (latest at build) | `nrfutil install nrf5sdk-tools`, downloaded at build | Not reviewed. Proprietary vendor terms | None collected | Nordic Semiconductor, through nrfutil |
| nrfutil `device` | unpinned (latest at build) | `nrfutil install device`, downloaded at build | Not reviewed. Proprietary vendor terms | None collected | Nordic Semiconductor, through nrfutil |
| Acroname BrainStem SDK | `brainstem==2.12.5` | `pip install` from PyPI. A vendor SDK with compiled libraries | Not reviewed. Vendor terms. `pip/INDEX.tsv` has what it declares | `pip/` | PyPI |

## Not in the image

These are mounted from the box host when the container starts. The image does
not contain them, so this file does not cover them: the SEGGER J-Link software,
the PicoScope SDK, and the oscilloscope daemon.

## When you change the Dockerfile

Add a row for a new download, and update the **Pin** cell when a version
changes. The test tells you which one it found. If a component has no notice in
the image, write that down: an honest "None collected" is what the review needs.
