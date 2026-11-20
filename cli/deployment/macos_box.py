# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Install a Lager Box on the Mac this CLI is running on.

A Box on macOS is two pieces, and this module provisions both:

* the **host agent**, a per-user launchd job running ``python -m lager.host``
  from a private Python environment. It owns every instrument;
* the **Box container**, the same image a Linux Box runs, started with no
  privileges and no devices. It reaches hardware only through the agent.

Nothing here is used when the target is a Linux Box: `lager install --box` /
`--ip` still runs ``setup_and_deploy_box.sh`` over SSH, unchanged.

Every step is safe to run again. Commands go through a ``run`` callable so
the steps can be exercised without a Mac, Docker or launchd.
"""
from __future__ import annotations

import hashlib
import io
import os
import platform
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence

try:
    from ..errors import LagerError
except ImportError:  # run on a remote Mac by macos_remote.py, without the CLI
    from _lager_errors import LagerError  # type: ignore

LABEL = "com.lagerdata.lager-host"
CONTAINER_NAME = "lager"
ETC_DIR = "/etc/lager"
# The name the CLI saved this Mac's Box under. Kept here, with the nets,
# so a reinstall after an uninstall (which forgets the saved Box) brings
# back the same name.
BOX_NAME_FILE = os.path.join(ETC_DIR, "box-name")
LOCAL_ADDRESS = "127.0.0.1"
IMAGE_REPO = "ghcr.io/lagerdata/lager-box"
# The only platform the Box image is published for today. It runs under the
# container runtime's x86 emulation on Apple silicon.
IMAGE_PLATFORM = "linux/amd64"
SOURCE_REPO = "https://github.com/lagerdata/lager"
LABJACK_REPO = "https://github.com/labjack/exodriver"
# LabJack's LJM installer for Apple silicon, which T-series devices (T4, T7,
# T8) need. LJM is closed source, so it cannot be built like the Exodriver;
# its signed libraries and data files are unpacked from the official package
# instead, with no admin rights and nothing written under /usr/local.
LJM_URL = ("https://files.labjack.com/installers/LJM/macOS/ARM64/release/"
           "LabJack-LJM_2025-05-07.zip")
LJM_SHA256 = "16439a170f450969b19bcb070823beb8e0bc566e3c68bfef7de92561d8db0768"
LJM_PKG = "LabJack-LJM_2025-05-07.pkg"
LJM_LIBRARY = "ljm_lib.pkg/Payload/libLabJackM-1.23.4.dylib"
# LJM is linked against /usr/local/lib/libusb-1.0.0.dylib. When that path is
# missing, dyld looks the name up on DYLD_FALLBACK_LIBRARY_PATH, which the
# agent's job points at the library directory first.
LJM_LIBUSB = "libusb_lib.pkg/Payload/libusb-1.0.0.dylib"
LJM_SHARE = "ljm_share.pkg/Payload/LabJack/LJM"
# The agent hands this to LJM before any device is opened; LJM otherwise
# reads it from /usr/local/share/LabJack/LJM.
LJM_CONSTANTS_ENV = "LAGER_LJM_CONSTANTS_FILE"
START_SCRIPT = "/app/lager/lager/docker/start-services-hostlink.sh"

# macOS's AirPlay receiver holds 5000, so the script service is published on
# 8301 -- the alias a Linux Box also publishes -- and the port is recorded
# with the saved Box so `lager python` uses it.
PYTHON_SERVICE_PORT = 8301
# (host port, container port). Everything else a Box publishes is reached
# through these or lives on the host already.
PUBLISHED_PORTS = ((9000, 9000), (PYTHON_SERVICE_PORT, 5000), (8100, 8100),
                   (8765, 8765), (8085, 8085))

# What the agent's Python needs to run the Box's hardware code natively.
# Versions are the ones the port was verified with on Apple silicon.
AGENT_REQUIREMENTS = (
    "requests>=2.31,<3",
    "numpy>=1.26",
    "Flask>=3.0,<4",
    "Werkzeug>=3.0,<4",
    "simplejson>=3.18",
    "PyYAML>=6.0",
    "pyserial==3.5",
    "pyusb>=1.2.1",
    "PyVISA>=1.14",
    "pyvisa-py>=0.7",
    "pexpect>=4.8",
    "pygdbmi==0.11.0.0",
    "LabJackPython==2.3.0",
    "brainstem",
    "simple-websocket>=1.0",
    "packaging",
)
# Drivers for instruments a bench may not have. Each is installed on its own
# and a failure is reported, not fatal: one vendor package without a build
# for this Mac must not cost the user every other instrument.
OPTIONAL_AGENT_REQUIREMENTS = (
    ("LabJack T-series", "labjack-ljm==1.23.0"),
    ("FTDI adapters", "pyftdi"),
    ("Total Phase Aardvark", "aardvark_py"),
    ("Joulescope", "joulescope"),
    ("Nordic PPK2", "ppk2-api"),
    ("Phidget", "Phidget22"),
    ("Yoctopuce", "yoctopuce"),
    ("YubiHSM", "yubihsm[usb]"),
    ("YKUSH hub", "hidapi"),
    # The same commit the Linux Box image installs.
    ("YKUSH hub", "git+https://github.com/Yepkit/pykush@d83ee856b4d0ea961b63b531fe4f4c533f6962f6"),
    ("MCC USB-202", "uldaq"),
)
MIN_PYTHON = (3, 10)
# The oscilloscope daemon does not build with an older Rust.
MIN_RUST = (1, 89)
PICO_SDK_HEADERS = Path("/Library/Frameworks/PicoSDK.framework/Headers")
# PicoTech's apt repository, which a Linux Box installs its drivers from. Its
# packages carry the C headers the daemon's build needs; they are the same on
# every architecture. PicoTech's macOS SDK has them too but is behind a form.
PICO_APT_URL = "https://labs.picotech.com/picoscope7/debian"
PICO_APT_INDEX = PICO_APT_URL + "/dists/picoscope/main/binary-amd64/Packages"
PICO_HEADER_PACKAGES = ("libps2000", "libps2000a", "libps3000a", "libps4000a",
                        "libps5000a", "libps6000a", "libpsospa")
PICO_HEADER_PREFIX = "opt/picoscope/include/"
# The drivers the daemon loads come from the PicoScope 7 application.
PICOSCOPE_APP = "/Applications/PicoScope 7 T&M.app"
PICOSCOPE_CASK = "picoscope"
JLINK_EXE = "/Applications/SEGGER/JLink/JLinkExe"
# Homebrew's cask runs SEGGER's own installer package, which needs sudo.
JLINK_CASK = "segger-jlink"
ORBSTACK_CASK = "orbstack"
# A Linux Box has Debian's gdb-multiarch; the agent uses this instead, for
# RTT auto-detection and target memory access over GDB.
ARM_GDB = "arm-none-eabi-gdb"
LIBUSB_FORMULA = "libusb"
LIBUSB_DYLIB = "lib/libusb-1.0.dylib"
# What the agent cannot reach without libusb: pyusb, which pyvisa-py uses for
# USBTMC, and pyftdi. Named this way in the "Set up" and "Not set up" lines.
USB_INSTRUMENTS = "USB instruments (USBTMC supplies, loads and meters; FTDI adapters)"
RUST_FORMULA = "rust"
# Started, in this order, when Docker is installed but not running.
RUNTIME_APPS = (("OrbStack", "/Applications/OrbStack.app"),
                ("Docker", "/Applications/Docker.app"))
HOMEBREW_PREFIXES = ("/opt/homebrew", "/usr/local")
DOCKER_SOCKETS = ("~/.orbstack/run/docker.sock", "~/.docker/run/docker.sock")
DOCKER_PATHS = ("/usr/local/bin/docker", "/opt/homebrew/bin/docker",
                "~/.orbstack/bin/docker",
                "/Applications/Docker.app/Contents/Resources/bin/docker")

Run = Callable[..., subprocess.CompletedProcess]


def _run(argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(list(argv), check=False, **kwargs)


@dataclass
class Layout:
    """Where the pieces live for one user."""

    home: Path = field(default_factory=Path.home)

    @property
    def app_dir(self) -> Path:
        return self.home / "Library" / "Application Support" / "Lager"

    @property
    def venv(self) -> Path:
        return self.app_dir / "host" / "venv"

    @property
    def python(self) -> Path:
        return self.venv / "bin" / "python"

    @property
    def lib_dir(self) -> Path:
        return self.app_dir / "host" / "lib"

    @property
    def ljm_dir(self) -> Path:
        """LJM's data files (constants, startup configs, log)."""
        return self.app_dir / "host" / "share" / "LabJack" / "LJM"

    @property
    def build_dir(self) -> Path:
        return self.app_dir / "build"

    @property
    def source_dir(self) -> Path:
        return self.app_dir / "src"

    @property
    def scope_daemon(self) -> Path:
        return self.app_dir / "host" / "bin" / "oscilloscope-daemon"

    @property
    def log_dir(self) -> Path:
        return self.home / "Library" / "Logs" / "Lager"

    @property
    def plist(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


# --------------------------------------------------------------- detection

def check_platform(system: Optional[str] = None, machine: Optional[str] = None) -> None:
    system = system or platform.system()
    machine = machine or platform.machine()
    if system != "Darwin":
        raise LagerError("This installer is for macOS.",
                         cause=f"This machine reports {system}.")
    if machine != "arm64":
        raise LagerError(
            "A Lager Box on macOS needs an Apple silicon Mac.",
            cause=f"This Mac reports {machine}.",
            fixes=["Install on an Apple silicon Mac or a Linux machine: "
                   "lager install --ip <address>"])


class DockerMissing(LagerError):
    """No Docker runtime is installed, as opposed to one that is not running."""


def find_docker(run: Run = _run, environ=None) -> tuple:
    """``(docker, env)`` for a running Docker Desktop or OrbStack daemon.

    A login shell usually has ``docker`` on PATH with a working context. A
    launchd job or an SSH session often has neither, so the usual install
    locations and sockets are tried too.
    """
    environ = dict(os.environ if environ is None else environ)
    binaries = [shutil.which("docker", path=environ.get("PATH"))]
    binaries += [os.path.expanduser(p) for p in DOCKER_PATHS]
    binaries = [b for b in dict.fromkeys(binaries) if b and os.path.exists(b)]
    if not binaries:
        raise DockerMissing(
            "Docker is not installed on this Mac.",
            cause="A Lager Box runs in a container.",
            fixes=["Install OrbStack (https://orbstack.dev) or Docker Desktop "
                   "(https://www.docker.com/products/docker-desktop), start it, "
                   "then run `lager install` again."])
    docker = binaries[0]
    envs = [environ]
    for sock in DOCKER_SOCKETS:
        path = os.path.expanduser(sock)
        if os.path.exists(path):
            envs.append(dict(environ, DOCKER_HOST=f"unix://{path}"))
    for env in envs:
        if run([docker, "info"], env=env, timeout=30).returncode == 0:
            return docker, env
    raise LagerError(
        "Docker is installed but not running.",
        fixes=["Start OrbStack or Docker Desktop, wait until it reports "
               "running, then run `lager install` again."])


def ensure_docker(run: Run = _run, echo=print, confirm=None,
                  wait_s: float = 180.0) -> tuple:
    """:func:`find_docker`, starting the runtime when it is installed but
    stopped, and offering to install OrbStack when none is installed.

    ``confirm`` asks the user a yes/no question; without it nothing is
    installed and a missing runtime is an error.
    """
    try:
        return find_docker(run)
    except DockerMissing:
        # An app never opened may not have put its `docker` in place yet.
        if not any(os.path.isdir(path) for _, path in RUNTIME_APPS):
            brew = homebrew_prefix_with("bin/brew")
            if confirm is None or brew is None or not confirm(
                    f"Docker is not installed. Install OrbStack with Homebrew "
                    f"(brew install --cask {ORBSTACK_CASK})?"):
                raise
            echo(f"Installing OrbStack (Homebrew cask {ORBSTACK_CASK})...")
            result = run([os.path.join(brew, "bin", "brew"), "install", "--cask",
                          ORBSTACK_CASK], capture_output=False, timeout=1800)
            if result.returncode != 0 or not os.path.isdir(RUNTIME_APPS[0][1]):
                raise LagerError("Homebrew did not install OrbStack.",
                                 fixes=[f"brew install --cask {ORBSTACK_CASK}",
                                        "Then run `lager install` again."])
    except LagerError:
        pass  # installed but not running: started below
    app = next((name for name, path in RUNTIME_APPS if os.path.isdir(path)), None)
    if app is None:
        return find_docker(run)  # raises the "not running" error
    echo(f"Starting {app}; its first start can take a minute...")
    run(["open", "-a", app], timeout=60)
    deadline = time.monotonic() + wait_s
    while True:
        try:
            return find_docker(run)
        except LagerError:
            if time.monotonic() >= deadline:
                raise
        time.sleep(3.0)


def running_from_checkout() -> Optional[Path]:
    """The source checkout this CLI runs from, if it carries the Box code."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / "box" / "lager" / "host" / "__main__.py").exists() else None


def source_version(root: Path) -> str:
    """``__version__`` declared by the checkout at ``root``, or ''."""
    try:
        text = (root / "cli" / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def image_for(version: str) -> str:
    return f"{IMAGE_REPO}:v{version.lstrip('v')}"


# ------------------------------------------------------------------- steps

def ensure_source(layout: Layout, version: str, run: Run = _run,
                  source: Optional[Path] = None) -> Path:
    """The Box source both pieces run: the checkout, or a clone of ``version``.

    The agent and the container are given the same tree, so the two sides of
    the link can never be from different releases. ``source`` is a tree
    already on this Mac (one a remote install copied over) and is used as is.
    """
    if source is not None:
        if not (source / "box" / "lager" / "host" / "__main__.py").exists():
            raise LagerError(f"There is no Lager Box source at {source}.")
        return source
    checkout = running_from_checkout()
    if checkout is not None:
        return checkout
    target = layout.source_dir / version
    if (target / "box" / "lager" / "host" / "__main__.py").exists():
        return target
    if not shutil.which("git"):
        raise LagerError(
            "git is not installed on this Mac.",
            fixes=["Run `xcode-select --install`, then `lager install` again."])
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(target, ignore_errors=True)
    result = run(["git", "clone", "--depth", "1", "--branch", version,
                  SOURCE_REPO, str(target)], timeout=600)
    if result.returncode != 0:
        raise LagerError(
            f"The CLI did not download Lager {version}.",
            cause=_last_line(result.stderr),
            fixes=["Check the network connection and that the version exists: "
                   f"{SOURCE_REPO}/releases"])
    return target


def ensure_config_dir(run: Run = _run, user: Optional[str] = None) -> bool:
    """Create ``/etc/lager`` owned by the user. True if ``sudo`` was needed."""
    if os.path.isdir(ETC_DIR) and os.access(ETC_DIR, os.W_OK):
        os.chmod(ETC_DIR, 0o700)
        return False
    user = user or os.environ.get("USER") or str(os.getuid())
    # Inherit the terminal: sudo asks for the password itself.
    result = run(["sudo", "sh", "-c",
                  f"mkdir -p {ETC_DIR} && chown {user} {ETC_DIR} && chmod 700 {ETC_DIR}"],
                 capture_output=False)
    if result.returncode != 0 or not os.access(ETC_DIR, os.W_OK):
        raise LagerError(
            f"The CLI did not create {ETC_DIR}.",
            cause="It is the Box's configuration directory and needs "
                  "administrator rights to create, once.",
            fixes=[f"sudo mkdir -p {ETC_DIR} && sudo chown {user} {ETC_DIR} "
                   f"&& chmod 700 {ETC_DIR}", "Then run `lager install` again."])
    return True


def base_python() -> str:
    """A Python new enough for the Box code, to build the agent's venv from."""
    if sys.version_info >= MIN_PYTHON:
        return sys.executable
    for prefix in HOMEBREW_PREFIXES:
        for name in ("python3.13", "python3.12", "python3.11", "python3.10"):
            candidate = os.path.join(prefix, "bin", name)
            if os.path.exists(candidate):
                return candidate
    wanted = ".".join(str(n) for n in MIN_PYTHON)
    raise LagerError(
        f"The host agent needs Python {wanted} or newer.",
        cause=f"This CLI runs on Python {platform.python_version()}.",
        fixes=["brew install python@3.12, or install Python 3.12 from "
               "https://www.python.org/downloads/macos/",
               "Then run `lager install` again."])


def ensure_agent_env(layout: Layout, run: Run = _run,
                     python: Optional[str] = None) -> List[str]:
    """Create the agent's private Python environment and install into it.

    Returns a line for each optional driver package that did not install.
    """
    if not layout.python.exists():
        layout.venv.parent.mkdir(parents=True, exist_ok=True)
        result = run([python or base_python(), "-m", "venv", str(layout.venv)], timeout=300)
        if result.returncode != 0:
            raise LagerError("The CLI did not create the host agent's Python environment.",
                             cause=_last_line(result.stderr))
    result = run([str(layout.python), "-m", "pip", "install", "--quiet",
                  "--disable-pip-version-check", *AGENT_REQUIREMENTS], timeout=1800)
    if result.returncode != 0:
        raise LagerError(
            "The CLI did not install the host agent's Python packages.",
            cause=_last_line(result.stderr),
            fixes=["Check the network connection, then run `lager install` again."])
    skipped = []
    for what, requirement in OPTIONAL_AGENT_REQUIREMENTS:
        result = run([str(layout.python), "-m", "pip", "install", "--quiet",
                      "--disable-pip-version-check", requirement], timeout=900)
        if result.returncode != 0:
            skipped.append(f"{what} support: {requirement} did not install "
                           f"({_last_line(result.stderr) or 'pip failed'})")
    return skipped


def homebrew_prefix_with(relative: str) -> Optional[str]:
    for prefix in HOMEBREW_PREFIXES:
        if os.path.exists(os.path.join(prefix, relative)):
            return prefix
    return None


def library_path(layout: Layout) -> str:
    """Where the agent's process looks for native libraries (libusb, the
    LabJack library), in the order it looks."""
    return ":".join([str(layout.lib_dir)]
                    + [f"{p}/lib" for p in HOMEBREW_PREFIXES] + ["/usr/lib"])


def ensure_libusb(run: Run = _run, echo=print) -> Optional[str]:
    """Install libusb through Homebrew when it is missing. A reason, or None.

    Without it the agent reaches no USBTMC or FTDI instrument, and nothing
    fails until a script first touches one, so a missing libusb must be
    named by the install.
    """
    if homebrew_prefix_with(LIBUSB_DYLIB) is not None:
        return None
    brew = homebrew_prefix_with("bin/brew")
    if brew is None:
        return ("libusb is not installed and Homebrew is not available to install it "
                "(https://brew.sh, then `brew install libusb`)")
    echo("Installing libusb (Homebrew)...")
    result = run([os.path.join(brew, "bin", "brew"), "install", LIBUSB_FORMULA], timeout=1800)
    if result.returncode != 0 or homebrew_prefix_with(LIBUSB_DYLIB) is None:
        return (f"`brew install {LIBUSB_FORMULA}` did not install it "
                f"({_last_line(result.stderr) or 'brew failed'})")
    return None


_LIBUSB_PROBE = ("import usb.backend.libusb1 as b, sys; "
                 "sys.exit(0 if b.get_backend() is not None else 3)")


def agent_loads_libusb(layout: Layout, run: Run = _run) -> Optional[str]:
    """Check the agent's own Python finds libusb on the agent's library path.
    A reason, or None."""
    result = run(["/usr/bin/env", f"DYLD_FALLBACK_LIBRARY_PATH={library_path(layout)}",
                  str(layout.python), "-c", _LIBUSB_PROBE], timeout=60)
    if result.returncode == 0:
        return None
    return ("the host agent cannot load libusb"
            + (f" ({_last_line(result.stderr)})" if result.returncode != 3 else "")
            + "; check `brew info libusb`, then run `lager install` again")


def build_labjack_library(layout: Layout, run: Run = _run,
                          echo=lambda _line: None) -> Optional[str]:
    """Build LabJack's USB library from LabJack's source. A reason, or None.

    Optional: without it everything but LabJack U3/U6/UE9 devices works.
    """
    if (layout.lib_dir / "liblabjackusb.dylib").exists():
        return None
    prefix = homebrew_prefix_with("include/libusb-1.0/libusb.h")
    if prefix is None:
        return "libusb is not installed (brew install libusb)"
    echo("Building the LabJack U3/U6 library...")
    if not (shutil.which("git") and shutil.which("make") and shutil.which("cc")):
        return "the command line developer tools are not installed (xcode-select --install)"
    source = layout.build_dir / "exodriver"
    shutil.rmtree(source, ignore_errors=True)
    source.parent.mkdir(parents=True, exist_ok=True)
    if run(["git", "clone", "--depth", "1", LABJACK_REPO, str(source)],
           timeout=300).returncode != 0:
        return f"could not download {LABJACK_REPO}"
    library_dir = source / "liblabjackusb"
    result = run(["make", f"CFLAGS=-I{prefix}/include",
                  f"LIBFLAGS=-L{prefix}/lib -lusb-1.0 -lc"],
                 cwd=str(library_dir), timeout=300)
    built = sorted(library_dir.glob("liblabjackusb*.dylib"))
    if result.returncode != 0 or not built:
        return f"the build failed ({_last_line(result.stderr) or 'no library produced'})"
    layout.lib_dir.mkdir(parents=True, exist_ok=True)
    for path in built:
        shutil.copy2(path, layout.lib_dir / path.name)
    if not (layout.lib_dir / "liblabjackusb.dylib").exists():
        shutil.copy2(built[-1], layout.lib_dir / "liblabjackusb.dylib")
    return None


def install_ljm_library(layout: Layout, run: Run = _run,
                        echo=lambda _line: None) -> Optional[str]:
    """Unpack LabJack's LJM library from LabJack's installer. A reason, or None.

    Optional: without it everything but LabJack T-series devices works. The
    package is checked against a pinned SHA-256 and only expanded, never
    installed: the libraries go in the agent's library directory and the data
    files under ``layout.ljm_dir``, so ``lager uninstall`` removes them too.
    """
    library = layout.lib_dir / "libLabJackM.dylib"
    constants = layout.ljm_dir / "ljm_constants.json"
    if library.exists() and constants.exists():
        return None
    echo("Installing the LabJack T-series library (LJM)...")
    work = layout.build_dir / "ljm"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    try:
        archive = work / "ljm.zip"
        # curl, not urllib: it trusts the system keychain, so it works behind
        # a TLS-inspecting corporate proxy.
        if run(["curl", "-fsSL", "--retry", "2", "-o", str(archive), LJM_URL],
               timeout=900).returncode != 0 or not archive.exists():
            return f"could not download {LJM_URL}"
        digest = hashlib.sha256()
        with open(archive, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        if digest.hexdigest() != LJM_SHA256:
            return f"the download from {LJM_URL} does not match its expected checksum"
        if run(["unzip", "-q", "-o", str(archive), LJM_PKG, "-d", str(work)],
               timeout=300).returncode != 0:
            return f"could not unpack {LJM_PKG}"
        expanded = work / "expanded"
        if run(["pkgutil", "--expand-full", str(work / LJM_PKG), str(expanded)],
               timeout=600).returncode != 0:
            return f"could not expand {LJM_PKG}"
        pieces = [expanded / LJM_LIBRARY, expanded / LJM_LIBUSB,
                  expanded / LJM_SHARE / "ljm_constants.json"]
        missing = [p.name for p in pieces if not p.exists()]
        if missing:
            return f"LabJack's package is missing {', '.join(missing)}"
        layout.lib_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(expanded / LJM_LIBRARY, library)
        shutil.copy2(expanded / LJM_LIBUSB, layout.lib_dir / "libusb-1.0.0.dylib")
        shutil.rmtree(layout.ljm_dir, ignore_errors=True)
        shutil.copytree(expanded / LJM_SHARE, layout.ljm_dir)
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def ensure_arm_gdb(run: Run = _run, echo=print) -> Optional[str]:
    """Install an ARM GDB through Homebrew. A reason, or None.

    Optional: without it J-Link and OpenOCD work, but RTT auto-detection does
    not.
    """
    if homebrew_prefix_with(f"bin/{ARM_GDB}") is not None:
        return None
    brew = homebrew_prefix_with("bin/brew")
    if brew is None:
        return f"{ARM_GDB} is not installed and Homebrew is not available to install it"
    echo(f"Installing {ARM_GDB} (Homebrew)...")
    result = run([os.path.join(brew, "bin", "brew"), "install", ARM_GDB], timeout=1800)
    if result.returncode != 0 or homebrew_prefix_with(f"bin/{ARM_GDB}") is None:
        return (f"`brew install {ARM_GDB}` did not install it "
                f"({_last_line(result.stderr) or 'brew failed'})")
    return None


def ensure_jlink(run: Run = _run, echo=print) -> Optional[str]:
    """Install SEGGER's J-Link software through Homebrew. A reason, or None.

    Optional: without it everything but J-Link debugging works.
    """
    if os.path.exists(JLINK_EXE):
        return None
    brew = homebrew_prefix_with("bin/brew")
    if brew is None:
        return ("SEGGER's J-Link software is not installed and Homebrew is not "
                "available to install it (https://www.segger.com/downloads/jlink)")
    echo("Installing SEGGER's J-Link software (Homebrew cask "
         f"{JLINK_CASK}, under SEGGER's terms of use)...")
    # Inherit the terminal: SEGGER's installer asks sudo for the password.
    result = run([os.path.join(brew, "bin", "brew"), "install", "--cask", JLINK_CASK],
                 capture_output=False, timeout=1800)
    if result.returncode != 0 or not os.path.exists(JLINK_EXE):
        return (f"`brew install --cask {JLINK_CASK}` did not install it; run that, "
                "then `lager install` again")
    return None


def sdk_include_tree(layout: Layout, sdk_headers: Path = PICO_SDK_HEADERS) -> Optional[Path]:
    """PicoTech's macOS SDK headers laid out the way the daemon's build expects.

    The macOS SDK keeps the headers every family shares in ``shared/``; the
    build looks for them beside each family's own. This makes a tree of
    links with both together. None when the SDK is not installed.
    """
    shared = sdk_headers / "shared"
    families = [d for d in sorted(sdk_headers.glob("libps*")) if d.is_dir()]
    if not shared.is_dir() or not families:
        return None
    tree = layout.build_dir / "picoscope-include"
    shutil.rmtree(tree, ignore_errors=True)
    for family in families:
        target = tree / family.name
        target.mkdir(parents=True)
        for header in list(family.glob("*.h")) + list(shared.glob("*.h")):
            link = target / header.name
            if not link.exists():
                link.symlink_to(header)
    return tree


def _apt_packages(index: str) -> dict:
    """``{package: (filename, sha256)}`` from an apt Packages index; the last
    entry for a package wins, as the index lists versions oldest first."""
    found = {}
    for stanza in index.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in stanza.splitlines()
                      if ": " in line and not line.startswith(" "))
        if {"Package", "Filename", "SHA256"} <= fields.keys():
            found[fields["Package"]] = (fields["Filename"], fields["SHA256"])
    return found


def _deb_data(deb: bytes) -> tarfile.TarFile:
    """The data archive inside a .deb (an ar archive)."""
    if not deb.startswith(b"!<arch>\n"):
        raise ValueError("not a .deb")
    offset = 8
    while offset + 60 <= len(deb):
        name = deb[offset:offset + 16].decode().strip().rstrip("/")
        size = int(deb[offset + 48:offset + 58].decode().strip())
        body = deb[offset + 60:offset + 60 + size]
        if name.startswith("data.tar"):
            return tarfile.open(fileobj=io.BytesIO(body), mode="r:*")
        offset += 60 + size + (size % 2)
    raise ValueError("no data archive in the .deb")


def _fetch(url: str) -> bytes:
    # PicoTech's server refuses Python's default User-Agent with 403.
    request = urllib.request.Request(url, headers={"User-Agent": "lager-install"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def apt_include_tree(layout: Layout, fetch=_fetch) -> Optional[str]:
    """Download PicoTech's headers from its apt repository. A reason, or None.

    Each package is checked against the SHA-256 in PicoTech's index, and only
    its headers are kept, under the build directory: nothing is installed.
    """
    tree = layout.build_dir / "picoscope-include"
    try:
        listed = _apt_packages(fetch(PICO_APT_INDEX).decode("utf-8", "replace"))
        missing = [p for p in PICO_HEADER_PACKAGES if p not in listed]
        if missing:
            return f"PicoTech's repository does not list {', '.join(missing)}"
        staging = layout.build_dir / "picoscope-include.new"
        shutil.rmtree(staging, ignore_errors=True)
        for package in PICO_HEADER_PACKAGES:
            filename, sha256 = listed[package]
            deb = fetch(f"{PICO_APT_URL}/{filename}")
            if hashlib.sha256(deb).hexdigest() != sha256.lower():
                return f"{package} from PicoTech's repository did not match its checksum"
            with _deb_data(deb) as data:
                for member in data.getmembers():
                    name = member.name.lstrip("./")
                    relative = name[len(PICO_HEADER_PREFIX):]
                    if (not member.isfile() or not name.startswith(PICO_HEADER_PREFIX)
                            or not relative.endswith(".h")
                            or ".." in Path(relative).parts):
                        continue
                    target = staging / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data.extractfile(member).read())
    except (OSError, ValueError, tarfile.TarError) as exc:
        if tree.is_dir():
            return None  # offline: the headers from the last install still build
        return f"PicoTech's headers could not be downloaded ({exc})"
    shutil.rmtree(tree, ignore_errors=True)
    staging.rename(tree)
    return None


def pico_include_tree(layout: Layout, fetch=_fetch) -> tuple:
    """``(tree, None)`` with PicoTech's headers, or ``(None, reason)``.

    The macOS SDK's when it is installed, otherwise PicoTech's apt packages'.
    """
    tree = sdk_include_tree(layout)
    if tree is not None:
        return tree, None
    reason = apt_include_tree(layout, fetch)
    return (None, reason) if reason else (layout.build_dir / "picoscope-include", None)


def ensure_picoscope_app(run: Run = _run, echo=print) -> Optional[str]:
    """Install the PicoScope 7 application through Homebrew. A reason, or None.

    Its arm64 drivers are what the daemon loads. Optional: without it
    everything but PicoScope oscilloscopes works.
    """
    if os.path.isdir(PICOSCOPE_APP):
        return None
    brew = homebrew_prefix_with("bin/brew")
    if brew is None:
        return ("the PicoScope 7 application is not installed and Homebrew is not "
                "available to install it (https://www.picotech.com/downloads)")
    echo(f"Installing PicoScope 7 for its drivers (Homebrew cask {PICOSCOPE_CASK}, "
         "under PicoTech's terms of use)...")
    # Inherit the terminal: PicoTech's installer asks sudo for the password.
    result = run([os.path.join(brew, "bin", "brew"), "install", "--cask", PICOSCOPE_CASK],
                 capture_output=False, timeout=1800)
    if result.returncode != 0 or not os.path.isdir(PICOSCOPE_APP):
        return (f"`brew install --cask {PICOSCOPE_CASK}` did not install it; run that, "
                "then `lager install` again")
    return None


def _version(text: str) -> tuple:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(part or 0) for part in match.groups()) if match else ()


def rust_toolchain(cargo: str, run: Run = _run) -> Optional[list]:
    """Extra ``cargo`` arguments selecting a new enough Rust, or None.

    The default toolchain when it is new enough. Otherwise the newest
    numbered toolchain rustup already has: nothing is installed or updated.
    """
    if _version(run([cargo, "--version"], timeout=60).stdout) >= MIN_RUST:
        return []
    rustup = shutil.which("rustup") or os.path.join(os.path.dirname(cargo), "rustup")
    if not os.path.exists(rustup):
        return None
    listed = run([rustup, "toolchain", "list"], timeout=60).stdout or ""
    names = [line.split()[0] for line in listed.splitlines()
             if line[:1].isdigit() and _version(line) >= MIN_RUST]
    return [f"+{max(names, key=_version)}"] if names else None


def find_cargo() -> str:
    """The ``cargo`` to build with: on PATH, rustup's, or Homebrew's. The path
    returned may not exist."""
    return shutil.which("cargo") or next(
        (c for c in [os.path.expanduser("~/.cargo/bin/cargo")]
         + [f"{p}/bin/cargo" for p in HOMEBREW_PREFIXES] if os.path.exists(c)),
        os.path.expanduser("~/.cargo/bin/cargo"))


def ensure_rust(run: Run = _run, echo=print) -> Optional[str]:
    """Install Rust through Homebrew when none is installed. A reason, or None.

    Optional: Rust only builds the oscilloscope daemon. A Rust that is
    already installed is left alone, even when too old: it may be rustup's,
    which the user manages.
    """
    if os.path.exists(find_cargo()):
        return None
    brew = homebrew_prefix_with("bin/brew")
    if brew is None:
        return "Rust is not installed and Homebrew is not available to install it"
    echo("Installing Rust (Homebrew)...")
    result = run([os.path.join(brew, "bin", "brew"), "install", RUST_FORMULA], timeout=1800)
    if result.returncode != 0 or not os.path.exists(find_cargo()):
        return (f"`brew install {RUST_FORMULA}` did not install it "
                f"({_last_line(result.stderr) or 'brew failed'})")
    return None


def build_scope_daemon(layout: Layout, source: Path, run: Run = _run,
                       echo=lambda _line: None) -> Optional[str]:
    """Build the oscilloscope daemon natively. A reason, or None.

    Optional: without it everything but oscilloscope nets works. PicoTech's
    licence keeps its headers and drivers out of Lager, so they come from
    PicoTech: the headers from its apt repository (or its macOS SDK), the
    drivers from its PicoScope 7 application.
    """
    crate = source / "box" / "oscilloscope-daemon"
    cargo = find_cargo()
    if not os.path.exists(cargo):
        return "Rust is not installed (https://rustup.rs)"
    if not crate.exists():
        return "this version of Lager has no oscilloscope daemon source"
    include, reason = pico_include_tree(layout)
    if include is None:
        return reason
    toolchain = rust_toolchain(cargo, run)
    if toolchain is None:
        wanted = ".".join(str(n) for n in MIN_RUST)
        return f"Rust {wanted} or newer is needed (rustup update stable)"
    target = layout.build_dir / "scope-target"
    echo("Building the oscilloscope daemon (a few minutes the first time)...")
    result = run([cargo, *toolchain, "build", "--release", "--target-dir", str(target)],
                 cwd=str(crate), timeout=3600,
                 env=dict(os.environ, LAGER_PICOSCOPE_INCLUDE=str(include)))
    built = [p for p in (target / "release" / "daemon",
                         target / "release" / "oscilloscope-daemon") if p.exists()]
    if result.returncode != 0 or not built:
        return f"the build failed ({_last_line(result.stderr) or 'no binary produced'})"
    layout.scope_daemon.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built[0], layout.scope_daemon)
    return None


def launch_agent_plist(layout: Layout, source: Path, environ=None) -> bytes:
    """The launchd job that keeps the agent running while the user is logged in."""
    library = library_path(layout)
    path = ":".join([f"{p}/bin" for p in HOMEBREW_PREFIXES]
                    + [os.path.expanduser("~/.cargo/bin"),
                       "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    env = {"PYTHONPATH": str(source / "box"), "PATH": path, "PYTHONUNBUFFERED": "1"}
    if layout.scope_daemon.exists():
        env["LAGER_HOST_SCOPE_DAEMON"] = str(layout.scope_daemon)
    constants = layout.ljm_dir / "ljm_constants.json"
    if constants.exists():
        env[LJM_CONSTANTS_ENV] = str(constants)
    log =str(layout.log_dir / "host-agent.log")
    return plistlib.dumps({
        "Label": LABEL,
        # macOS drops DYLD_* variables whenever it starts a system binary, so
        # the library path is set by `env` as the last step before Python.
        "ProgramArguments": ["/usr/bin/env",
                             f"DYLD_FALLBACK_LIBRARY_PATH={library}",
                             str(layout.python), "-m", "lager.host", "run"],
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "StandardOutPath": log,
        "StandardErrorPath": log,
    })


def _domain() -> str:
    return f"gui/{os.getuid()}"


def stop_agent(layout: Layout, run: Run = _run) -> None:
    run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], timeout=60)


def start_agent(layout: Layout, source: Path, run: Run = _run,
                wait_s: float = 30.0) -> None:
    """(Re)install the launchd job and wait for the agent's endpoint record."""
    layout.log_dir.mkdir(parents=True, exist_ok=True)
    layout.plist.parent.mkdir(parents=True, exist_ok=True)
    stop_agent(layout, run)
    endpoint = os.path.join(ETC_DIR, "host_agent.json")
    layout.plist.write_bytes(launch_agent_plist(layout, source))
    result = None
    # bootout returns before launchd has finished removing the old job.
    for _ in range(10):
        result = run(["launchctl", "bootstrap", _domain(), str(layout.plist)], timeout=60)
        if result.returncode == 0:
            break
        time.sleep(1.0)
    if result is None or result.returncode != 0:
        raise LagerError(
            "The CLI did not start the host agent.",
            cause=_last_line(result.stderr) if result else None,
            fixes=["The agent runs in a logged-in desktop session: log in at "
                   "the Mac's screen, then run `lager install` again."])
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if os.path.exists(endpoint) and _agent_listening(endpoint):
            return
        time.sleep(0.5)
    raise LagerError(
        "The host agent did not start.",
        fixes=[f"Read its log: tail -50 '{layout.log_dir}/host-agent.log'"])


def _agent_listening(endpoint_file: str) -> bool:
    import json
    try:
        with open(endpoint_file, encoding="utf-8") as fh:
            record = json.load(fh)
        with socket.create_connection((record.get("bind", LOCAL_ADDRESS),
                                       int(record["port"])), timeout=1.0):
            return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def docker_run_argv(docker: str, image: str, source: Path, etc_real: str,
                    bind: str = LOCAL_ADDRESS) -> List[str]:
    """The container: no privileges, no devices, config and code mounted."""
    argv = [docker, "run", "-d", "--name", CONTAINER_NAME, "--restart", "always",
            "--platform", IMAGE_PLATFORM,
            "-v", f"{source / 'box' / 'lager'}:/app/lager/lager:ro",
            "-v", f"{etc_real}:/etc/lager"]
    for host_port, container_port in PUBLISHED_PORTS:
        argv += ["-p", f"{bind}:{host_port}:{container_port}"]
    return argv + [image, START_SCRIPT]


def anonymous_env(docker: str, env: dict, run: Run = _run) -> dict:
    """``env`` pointed at the same daemon, with an empty Docker configuration.

    An empty configuration has no credential store and no contexts, so the
    daemon the current context names is carried over as ``DOCKER_HOST``.
    """
    import tempfile
    clean = dict(env, DOCKER_CONFIG=tempfile.mkdtemp(prefix="lager-docker-"))
    if not clean.get("DOCKER_HOST"):
        host = (run([docker, "context", "inspect", "--format",
                     "{{.Endpoints.docker.Host}}"], env=env, timeout=30).stdout or "").strip()
        if host:
            clean["DOCKER_HOST"] = host
    return clean


def start_container(docker: str, env: dict, image: str, source: Path,
                    run: Run = _run, echo=lambda _line: None,
                    bind: str = LOCAL_ADDRESS) -> None:
    # /etc is a symlink on macOS and OrbStack does not follow it for a bind
    # mount (the container silently gets an empty directory).
    etc_real = os.path.realpath(ETC_DIR)
    nets = os.path.join(ETC_DIR, "saved_nets.json")
    if not os.path.exists(nets):
        with open(nets, "w", encoding="utf-8") as fh:
            fh.write("[]")
    if not image_present(docker, env, image, run):
        echo(f"Downloading the Box image {image} (several GB; this takes a while)...")
        _check_pull(pull_image(docker, env, image, run), image)
    run([docker, "rm", "-f", CONTAINER_NAME], env=env, timeout=120)
    result = run(docker_run_argv(docker, image, source, etc_real, bind),
                 env=env, timeout=300)
    if result.returncode != 0:
        cause = _last_line(result.stderr)
        fixes = []
        if "port is already allocated" in cause or "address already in use" in cause:
            fixes = ["Another program holds one of the Box's ports "
                     f"({', '.join(str(h) for h, _ in PUBLISHED_PORTS)}). "
                     "Stop it, then run `lager install` again."]
        raise LagerError("The CLI did not start the Box container.",
                         cause=cause, fixes=fixes)


def image_present(docker: str, env: dict, image: str, run: Run = _run) -> bool:
    return run([docker, "image", "inspect", image], env=env, timeout=60).returncode == 0


def pull_image(docker: str, env: dict, image: str,
               run: Run = _run) -> subprocess.CompletedProcess:
    """Download the Box image. Docker's own output is captured: without a
    terminal it is a line per layer event, hundreds of them."""
    pull = [docker, "pull", "--platform", IMAGE_PLATFORM, image]
    result = run(pull, env=env, timeout=3600)
    if result.returncode != 0:
        # Over SSH the login keychain is locked, and a Docker set up to keep
        # credentials there fails every pull, even of a public image. Pull
        # once more with no credential store at all.
        result = run(pull, env=anonymous_env(docker, env, run), timeout=3600)
    return result


def _check_pull(result: subprocess.CompletedProcess, image: str) -> None:
    if result.returncode != 0:
        raise LagerError(
            f"The CLI did not download the Box image {image}.",
            cause=_last_line(result.stderr),
            fixes=["Check the network connection, then run `lager install` again.",
                   "For a development version, pass --version with a release "
                   "tag that has an image."])


class BackgroundPull:
    """Downloads the Box image while the agent is built: the download is the
    longest step and needs only the network."""

    def __init__(self, docker: str, env: dict, image: str, run: Run = _run,
                 echo=print):
        self.image = image
        self.result = None
        self.thread = None
        if image_present(docker, env, image, run):
            return
        echo(f"Downloading the Box image {image} in the background "
             "(several GB)...")
        self.started = time.monotonic()

        def pull():
            self.result = pull_image(docker, env, image, run)
        self.thread = threading.Thread(target=pull, name="lager-image-pull", daemon=True)
        self.thread.start()

    def wait(self, echo=print, every_s: float = 30.0) -> None:
        """Return once the image is there, saying so every ``every_s`` seconds
        while it is not."""
        if self.thread is None:
            return
        self.thread.join(timeout=0)
        if self.thread.is_alive():
            echo("Waiting for the Box image to finish downloading...")
        while self.thread.is_alive():
            self.thread.join(timeout=every_s)
            if self.thread.is_alive():
                minutes = (time.monotonic() - self.started) / 60
                echo(f"  still downloading ({minutes:.0f} min so far)...")
        _check_pull(self.result, self.image)


def homebrew_installs(skip: Sequence[str] = ()) -> List[str]:
    """What an install would add with Homebrew on this Mac, as it would be
    named to the user. Nothing when Homebrew is not installed (those pieces
    are skipped then, with the reason)."""
    if homebrew_prefix_with("bin/brew") is None:
        return []
    wanted = []
    if homebrew_prefix_with(LIBUSB_DYLIB) is None:
        wanted.append("libusb")
    if not any(os.path.isdir(path) for _, path in RUNTIME_APPS) and shutil.which("docker") is None:
        wanted.append("OrbStack (Docker)")
    if "jlink" not in skip and not os.path.exists(JLINK_EXE):
        wanted.append("SEGGER J-Link")
    if "picoscope" not in skip:
        if not os.path.isdir(PICOSCOPE_APP):
            wanted.append("PicoScope 7")
        if not os.path.exists(find_cargo()):
            wanted.append("Rust")
    if homebrew_prefix_with(f"bin/{ARM_GDB}") is None:
        wanted.append("an ARM GDB")
    return wanted


# The Homebrew installers that ask for the Mac's password themselves.
ASKS_FOR_PASSWORD = ("SEGGER J-Link", "PicoScope 7")


_LINK_PROBE = ("import urllib.request;"
               "print(urllib.request.urlopen("
               "'http://localhost:8080/_hostlink/status', timeout=10).read().decode())")


def wait_for_link(docker: str, env: dict, run: Run = _run, wait_s: float = 120.0) -> str:
    """Wait until the container has shaken hands with the agent."""
    deadline = time.monotonic() + wait_s
    last = ""
    while time.monotonic() < deadline:
        result = run([docker, "exec", CONTAINER_NAME, "python3", "-c", _LINK_PROBE],
                     env=env, timeout=30)
        last = (result.stdout or result.stderr or "").strip()
        if result.returncode == 0 and '"connected": true' in last.replace("'", '"'):
            return last
        time.sleep(2.0)
    raise LagerError(
        "The Box container did not connect to the host agent.",
        cause=_last_line(last),
        fixes=[f"docker logs {CONTAINER_NAME}",
               "tail -50 ~/Library/Logs/Lager/host-agent.log"])


def remembered_box_name() -> Optional[str]:
    """The name this Mac's Box was last saved under, if one was recorded."""
    try:
        with open(BOX_NAME_FILE, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def remember_box_name(name: str) -> None:
    try:
        with open(BOX_NAME_FILE, "w", encoding="utf-8") as fh:
            fh.write(name + "\n")
    except OSError:
        pass


def write_state(box_version: str, cli_version: str, ref: str) -> None:
    """What `lager hello` and `lager boxes` report, as a Linux install writes."""
    for name, content in (("version", f"{box_version}|{cli_version}"), ("ref", ref)):
        with open(os.path.join(ETC_DIR, name), "w", encoding="utf-8") as fh:
            fh.write(content + "\n")


def _last_line(text) -> str:
    lines = [line for line in (text or "").strip().splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


# ------------------------------------------------------------- entry points

@dataclass
class Result:
    source: Path
    image: str
    box_version: str
    skipped: List[str] = field(default_factory=list)
    ready: List[str] = field(default_factory=list)


def install(version: str, cli_version: str, *, echo=print, run: Run = _run,
            layout: Optional[Layout] = None, bind: str = LOCAL_ADDRESS,
            source: Optional[Path] = None, confirm=None,
            skip: Sequence[str] = ()) -> Result:
    """Provision the agent and the container on this Mac.

    ``bind`` is the address the Box's ports are published on: this Mac only
    by default, every interface for a Box that other machines must reach.
    ``source`` is a Box source tree already on this Mac, used instead of the
    checkout or a download. ``confirm`` asks the user a yes/no question
    before something is installed on their behalf (see :func:`ensure_docker`).
    ``skip`` leaves out optional pieces: "jlink", "picoscope".
    """
    layout = layout or Layout()
    from_tree = source is not None or running_from_checkout() is not None
    check_platform()
    echo("Checking Docker...")
    docker, env = ensure_docker(run, echo, confirm)

    echo("Preparing the Box configuration directory...")
    ensure_config_dir(run)

    source = ensure_source(layout, version, run, source)
    box_version = source_version(source) or version.lstrip("v")
    image = image_for(box_version if from_tree else version)
    download = BackgroundPull(docker, env, image, run, echo)

    echo("Installing the host agent's Python packages...")
    skipped = ensure_agent_env(layout, run)
    ready = []

    def optional(feature, step):
        reason = step()
        (skipped.append(f"{feature}: {reason}") if reason else ready.append(feature))

    # Asked first: pyusb also finds a libusb Homebrew did not install.
    optional(USB_INSTRUMENTS, lambda: agent_loads_libusb(layout, run) and (
        ensure_libusb(run, echo) or agent_loads_libusb(layout, run)))
    optional("LabJack U3/U6 support", lambda: build_labjack_library(layout, run, echo))
    optional("LabJack T4/T7/T8 support", lambda: install_ljm_library(layout, run, echo))
    if "picoscope" in skip:
        echo("Leaving out PicoScope support (--skip-picoscope).")
    else:
        optional("Oscilloscope support", lambda: ensure_rust(run, echo)
                 or build_scope_daemon(layout, source, run, echo))
        optional("PicoScope support", lambda: ensure_picoscope_app(run, echo))
    if "jlink" in skip:
        echo("Leaving out J-Link debugging (--skip-jlink).")
    else:
        optional("J-Link debugging", lambda: ensure_jlink(run, echo))
    optional("RTT auto-detection", lambda: ensure_arm_gdb(run, echo))
    echo("Starting the host agent...")
    start_agent(layout, source, run)

    download.wait(echo)
    echo("Starting the Box container...")
    start_container(docker, env, image, source, run, echo, bind)
    wait_for_link(docker, env, run)
    ref = f"checkout:{source}" if from_tree else version
    write_state(box_version, cli_version, ref)
    return Result(source=source, image=image, box_version=box_version,
                  skipped=skipped, ready=ready)


def installed_pieces(layout: Optional[Layout] = None,
                     keep_images: bool = False) -> List[str]:
    """What an uninstall would remove, for a dry run."""
    layout = layout or Layout()
    pieces = [f"the Box container '{CONTAINER_NAME}'",
              f"the host agent's launchd job ({layout.plist})"]
    if not keep_images:
        pieces.append(f"the Box images ({IMAGE_REPO})")
    if layout.app_dir.exists():
        pieces.append(f"the host agent's files ({layout.app_dir})")
    return pieces


def backup_config(layout: Optional[Layout] = None) -> Optional[Path]:
    """Copy ``/etc/lager`` aside before it is emptied. The copy, or None."""
    layout = layout or Layout()
    if not os.path.isdir(ETC_DIR) or not os.listdir(ETC_DIR):
        return None
    target = layout.home / f"lager-config-backup-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copytree(ETC_DIR, target)
    os.chmod(target, 0o700)
    return target


def uninstall(*, purge_config: bool = False, echo=print, run: Run = _run,
              layout: Optional[Layout] = None, keep_images: bool = False) -> None:
    """Remove the container, its images and the agent. Configuration is kept
    by default. Images are removed as a Linux uninstall removes them, unless
    ``keep_images``.

    ``/etc/lager`` itself stays even when purged: removing it would need
    administrator rights, and an empty directory is harmless.
    """
    layout = layout or Layout()
    docker = env = None
    try:
        docker, env = find_docker(run)
    except LagerError:
        echo("Docker is not running; the Box container was not removed.")
    if docker:
        run([docker, "rm", "-f", CONTAINER_NAME], env=env, timeout=120)
        if not keep_images:
            listed = run([docker, "images", "--format", "{{.Repository}}:{{.Tag}}",
                          IMAGE_REPO], env=env, timeout=60).stdout or ""
            images = [line for line in listed.split() if not line.endswith(":<none>")]
            if images:
                echo("Removing the Box images...")
                run([docker, "rmi", "-f", *images], env=env, timeout=300)
    stop_agent(layout, run)
    if layout.plist.exists():
        layout.plist.unlink()
    shutil.rmtree(layout.app_dir, ignore_errors=True)
    if purge_config and os.path.isdir(ETC_DIR):
        backup = backup_config(layout)
        if backup:
            echo(f"Configuration backed up to {backup}")
        for entry in Path(ETC_DIR).glob("*"):
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
