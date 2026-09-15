# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.box.boxes

    Box commands for managing local configurations
"""
import click
import json
import queue
import shutil
import sys
import threading
import time
from typing import NamedTuple
from texttable import Texttable
from ...address_utils import validate_ip_or_hostname, VALID_FORMATS_CHEATSHEET
from ...box_storage import add_box, delete_box, delete_all_boxes, list_boxes, load_boxes, save_boxes, get_lager_file_path, format_lock_user
from ...core.group_usage import LagerGroup
from ...sort_utils import natural_sort_key
from ...core.utils import looks_like_release_tag


def _annotate_version(box_version, box_ref):
    """Version cell for the boxes table, naming the ref when it is not a
    release tag.

    A box deployed from a branch reports the same version number as one on the
    release tag, because a branch not yet bumped past the last release declares
    the same `__version__` (#266). Across a fleet that is how a box gets left on
    a branch and someone else runs a test against it believing it is on the
    release.

    Release-tagged and ref-less boxes render exactly as before: the annotation
    only appears where it changes the reading. The SHA is deliberately omitted
    -- it would widen the column for every row; `lager hello` reports it.
    """
    if not box_ref:
        return box_version
    ref_name = str(box_ref).split('@', 1)[0]
    if looks_like_release_tag(ref_name):
        return box_version
    return f'{box_version} ({ref_name})'


# Per-box HTTP budgets. A box that is powered off, mid-update (its container
# is down), or behind a dropped route burns the whole of both before it says
# anything, which is what used to make the whole listing feel hung.
_LOCK_TIMEOUT = 3
_DEFAULT_STATUS_TIMEOUT = 5
# Grace beyond the point where every box should have answered, before we stop
# waiting on the stragglers and print. This covers the part of a stall the
# socket timeouts do not: `requests` has no timeout for name resolution, so a
# box named by hostname behind a sick resolver outlasts both budgets above.
_STRAGGLER_GRACE = 3.0
# Collect-loop poll interval. Matches the spinner period, so the wheel is
# reached often enough to turn smoothly; also what bounds how long Ctrl+C
# waits. Idle cost is one timed queue wait per tick.
_POLL_INTERVAL = 0.1

_PENDING = 'pending'
_CANCELLED = 'cancelled'
_ABANDONED = 'no response'

# Statuses meaning the box identified itself. Produced here, not by the
# gateway, so matching on the text is safe.
_OK_STATUSES = frozenset({'current', 'needs update', 'newer'})
# The denial labels a `lager login` actually fixes. Other Stout verdicts
# ('no access', 'auth server down', 'token rejected') need an access grant or
# an admin, so offering a sign-in for them would be wrong.
_SIGNIN_STATUSES = frozenset({'sign-in required', 'session rejected'})


class _Row(NamedTuple):
    """One rendered line of the boxes table."""
    name: str
    ip: str
    user: str
    version: str          # display form; may carry a "(branch)" annotation
    status: str
    locked_by: str = ''
    busy: str = ''
    # Bare version as the box reported it, kept apart from `version` because
    # only this form may reach the on-disk cache -- the display form can
    # carry an annotation.
    version_raw: str = ''
    # Set by the gateway check rather than inferred from `status`, so a new
    # label in `gateway_auth.denial_label` cannot silently get counted as an
    # unreachable box.
    auth_denied: bool = False


def _status_color(status):
    if status == 'current':
        return 'green'
    if status == 'needs update':
        return 'yellow'
    if status == 'newer':
        return 'cyan'
    if status == _PENDING:
        return 'bright_black'
    return 'red'


def _resolve_auth_headers(boxes):
    """Stout bearer headers per box IP, resolved before any fan-out.

    Deliberately serial, on the calling thread. `auth_headers_for_box` can
    spend the refresh cookie, and Stout rotates that cookie on every
    successful refresh -- so N threads each finding the same near-expiry
    token would refresh N times, and the losers would rotate the session out
    from under the winner. The user would be logged out by `lager boxes`.

    Resolving here means any refresh happens once, before a second thread
    exists to race it. A worker does re-read the store between its two calls,
    to pick up a mapping its own first request just discovered, but by then
    the token is fresh and that read costs no round trip.
    """
    from ...gateway_auth import auth_headers_for_box
    headers = {}
    for _, ip, _ in boxes:
        if ip != 'unknown' and ip not in headers:
            headers[ip] = auth_headers_for_box(ip)
    return headers


def _probe_box(name, ip, user, port, status_timeout, cli_version, auth_headers):
    """Probe one box's lock and version state. Runs on a worker thread.

    Never raises. The collect loop needs a row for every box it started, and
    an exception escaping here would strand that box on 'pending' until the
    deadline instead of naming what went wrong.
    """
    import requests
    from ...box_storage import check_gateway_status

    if ip == 'unknown':
        return _Row(name, ip, user, '-', 'no IP')

    locked_by = ''
    try:
        lock_resp = requests.get(
            f'http://{ip}:{port}/lock',
            timeout=_LOCK_TIMEOUT,
            headers={'Cache-Control': 'no-cache', **auth_headers},
        )
        # Non-raising gateway check: one gated box must not abort the whole
        # table. On first contact this records the box->auth-server mapping
        # and retries with the stored token, so the /status call below
        # authenticates normally (no second round trip).
        # The budget is passed through so that retry is held to the same
        # allowance as the call it replays, rather than the far longer
        # default. The collect loop abandons this box at its deadline, and a
        # retry outliving that deadline would have us label a box that was
        # still answering. stream mirrors this buffered call, as the retry
        # requires.
        lock_resp, _ = check_gateway_status(
            lock_resp, ip, timeout=_LOCK_TIMEOUT, stream=False)
        if lock_resp.status_code == 200:
            lock_data = lock_resp.json()
            if lock_data.get('locked'):
                locked_by = format_lock_user(lock_data.get('user', '?'))
    except Exception:
        # Lock state is decoration; a box that fails here may still report a
        # version below, and that is the more useful answer.
        pass

    # The /lock call may have just learned that this box is gated -- the
    # box->auth-server mapping only exists once a 401 has disclosed it. Asking
    # again here is what lets /status authenticate up front instead of
    # spending a discovery round trip of its own. Cheap and safe on a worker
    # thread: the pre-warm already did any refresh this needs, so this is a
    # store read, and `access_token_for` is single-flight if it is not.
    from ...gateway_auth import auth_headers_for_box
    status_headers = auth_headers_for_box(ip)

    try:
        # /status on :9000 reports the box version (from /etc/lager/version).
        # It predates the newer capability fields, so even older box images
        # answer it -- unlike a brand-new endpoint would.
        response = requests.get(
            f'http://{ip}:{port}/status',
            timeout=status_timeout,
            headers={'Cache-Control': 'no-cache', 'Pragma': 'no-cache', **status_headers},
        )
        response, gate_verdict = check_gateway_status(
            response, ip, timeout=status_timeout, stream=False)
        if gate_verdict:
            return _Row(name, ip, user, '-', gate_verdict, locked_by, auth_denied=True)

        if response.status_code == 404:
            return _Row(name, ip, user, '-', 'old box', locked_by)
        if response.status_code != 200:
            return _Row(name, ip, user, '-', f'HTTP {response.status_code}', locked_by)

        try:
            data = response.json()
        except ValueError:
            return _Row(name, ip, user, '-', 'invalid JSON', locked_by)

        box_version = data.get('version') or data.get('box_version')
        if box_version == 'unknown':
            box_version = None
        if not box_version:
            return _Row(name, ip, user, '-', 'bad response', locked_by)

        # A box deployed from a branch reports the same version number as one
        # on the release tag, so the version column alone cannot tell them
        # apart (#266). Name the ref here; `lager hello` has the resolved SHA
        # too, which is too wide for a fleet listing.
        shown = _annotate_version(box_version, data.get('ref'))
        order = compare_versions(box_version, cli_version)
        status = 'current' if order == 0 else ('needs update' if order < 0 else 'newer')
        return _Row(name, ip, user, shown, status, locked_by, version_raw=box_version)

    except requests.exceptions.Timeout:
        return _Row(name, ip, user, '-', 'timeout', locked_by)
    except requests.exceptions.ConnectionError:
        return _Row(name, ip, user, '-', 'unreachable', locked_by)
    except Exception:
        return _Row(name, ip, user, '-', 'error', locked_by)


def _fit_row(prefix, cells, cols):
    """A table line truncated to `cols` terminal columns, colour applied after.

    `cells` are `(text, colour)` pairs rendered after `prefix`; a colour of
    None leaves that run unstyled.

    Colour last, because escape sequences are zero-width on screen but do
    count toward len(): slicing an already-styled string cuts at the wrong
    place and can clip the reset, bleeding colour into the rest of the
    terminal. That is also why `room` counts only visible characters -- the
    styled text already in `parts` must not be measured. Truncating at all
    matters because a wrapped line occupies two terminal rows while the
    repaint below moves the cursor up by the number of lines it wrote -- the
    mismatch is what shreds an in-place display.
    """
    parts = [prefix[:cols]]
    room = cols - len(parts[0])
    for text, color in cells:
        if room <= 0:
            break
        chunk = text[:room]
        parts.append(click.style(chunk, fg=color) if color else chunk)
        room -= len(chunk)
    return ''.join(parts)


class _LiveTable:
    """The in-progress listing, repainted in place while boxes answer.

    Only the main thread touches this. Workers hand finished rows to a queue
    and the collect loop performs every write to stdout, so there is no lock
    here and no second thread that can interleave half a frame.
    """

    _CLEAR = '\033[2K\r'
    _UP = '\033[1A'
    # Same braille wheel the old single-line spinner used, so the command
    # still looks like itself while it waits.
    _SPINNER = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    _SPINNER_PERIOD = 0.1

    def __init__(self, boxes, countdown_to):
        self._boxes = boxes
        self._countdown_to = countdown_to
        self._rows = {}
        self._painted = 0
        self._last_frame = None
        self._started = time.monotonic()

        self._w_name = max([len('name')] + [len(b[0]) for b in boxes])
        self._w_ip = max([len('ip')] + [len(b[1]) for b in boxes])
        self._w_user = max([len('user')] + [len(b[2]) for b in boxes])
        # Grow only, so a row arriving late never narrows the block.
        self._w_version = len('version')
        # Wide enough for the spinner glyph plus a space in front of the text.
        self._w_status = max(len('status'), len(_PENDING) + 2)
        # `locked by` is shown from the start, even though nothing can be
        # known about a lock until a box answers -- a column that appeared
        # only once the first locked box replied would shift every row
        # underneath it mid-wait.
        self._w_locked = len('locked by')

        self._live = sys.stdout.isatty()
        # Repainting walks the cursor back up over the block we drew. If the
        # block is taller than the terminal, its top has already scrolled off
        # and those moves land on the wrong rows. Print once at the end
        # instead of corrupting the scrollback.
        if self._live:
            rows_needed = len(boxes) + 3   # header, rule, footer
            if rows_needed > shutil.get_terminal_size(fallback=(80, 24)).lines:
                self._live = False

    def record(self, row):
        self._rows[row.name] = row
        self._w_version = max(self._w_version, len(row.version))
        self._w_status = max(self._w_status, len(row.status))
        self._w_locked = max(self._w_locked, len(row.locked_by))

    def _spinner(self):
        """The current wheel glyph.

        Derived from elapsed time rather than a paint counter, so the wheel
        turns at a steady rate no matter how often `paint` is reached.
        """
        step = int((time.monotonic() - self._started) / self._SPINNER_PERIOD)
        return self._SPINNER[step % len(self._SPINNER)]

    def pending(self):
        return [b for b in self._boxes if b[0] not in self._rows]

    def rows(self):
        """Every row in display order. Call only once nothing is pending."""
        return [self._rows[name] for name, _, _ in self._boxes]

    def _footer(self):
        waiting = len(self.pending())
        if not waiting:
            return ''
        noun = 'box' if waiting == 1 else 'boxes'
        left = int(self._countdown_to - time.monotonic())
        if left > 0:
            return f'Waiting on {waiting} {noun} - {left}s left, Ctrl+C to stop waiting'
        return f'Waiting on {waiting} {noun} - past timeout, Ctrl+C to stop waiting'

    def paint(self):
        if not self._live:
            return
        footer = self._footer()
        waiting = bool(self.pending())
        # The wheel only turns while something is outstanding, so a settled
        # table stops repainting entirely.
        spin = self._spinner() if waiting else ''
        # Skip frames that would render identically: without this the loop
        # rewrites the same block ten times a second for no reason.
        frame = (len(self._rows), self._w_version, self._w_status,
                 self._w_locked, footer, spin)
        if frame == self._last_frame:
            return
        self._last_frame = frame

        cols = max(20, shutil.get_terminal_size(fallback=(80, 24)).columns - 1)
        header = (
            f"{'name':<{self._w_name}}   {'ip':<{self._w_ip}}   "
            f"{'user':<{self._w_user}}   {'version':<{self._w_version}}   "
            f"{'status':<{self._w_status}}   {'locked by':<{self._w_locked}}"
        )
        lines = [header[:cols], ('=' * len(header))[:cols]]
        for name, ip, user in self._boxes:
            row = self._rows.get(name)
            if row is None:
                # Nothing is known yet, so both unresolved cells carry the
                # wheel rather than a claim about the box.
                version, status, locked = '-', f'{spin} {_PENDING}', spin
                # Asked for `_PENDING`, not `status`: the wheel prefix makes
                # the cell text no longer a status `_status_color` knows.
                status_color = locked_color = _status_color(_PENDING)
            else:
                version, status, locked = row.version, row.status, row.locked_by
                status_color = _status_color(row.status)
                locked_color = 'magenta' if locked else None
            prefix = (
                f"{name:<{self._w_name}}   {ip:<{self._w_ip}}   "
                f"{user:<{self._w_user}}   {version:<{self._w_version}}   "
            )
            lines.append(_fit_row(prefix, [
                (status.ljust(self._w_status), status_color),
                ('   ', None),
                (locked.ljust(self._w_locked), locked_color),
            ], cols))
        if footer:
            lines.append(click.style(footer[:cols], fg='bright_black'))

        # One write for the whole frame: rewinding and redrawing as separate
        # writes lets a slow terminal show the cleared block.
        rewind = (self._UP + self._CLEAR) * self._painted
        sys.stdout.write(rewind + ''.join(f'{line}\n' for line in lines))
        sys.stdout.flush()
        self._painted = len(lines)

    def erase(self):
        """Take the live block back down, so the final table replaces it."""
        if not self._live or not self._painted:
            return
        sys.stdout.write((self._UP + self._CLEAR) * self._painted)
        sys.stdout.flush()
        self._painted = 0


def _list_boxes_live(port=9000, timeout=_DEFAULT_STATUS_TIMEOUT):
    """Query all boxes for their versions and display status table.

    Boxes are probed concurrently and the table is drawn immediately, so an
    unreachable box costs its own timeout rather than delaying every row
    behind it. On a TTY the rows update in place as answers arrive, under a
    countdown; anywhere else (a pipe, CI) the same final table is printed
    once, unchanged.

    Threads are plain daemon threads rather than a `ThreadPoolExecutor` on
    purpose. A pool's context manager exits via `shutdown(wait=True)` and
    `concurrent.futures` additionally joins its workers at interpreter exit,
    so Ctrl+C would not take effect until every in-flight socket had already
    timed out -- measured at the full 8s on a four-box stall, which is
    exactly the wait this command exists to let you escape. Daemon threads
    are abandonable, which is what makes both Ctrl+C and the straggler
    deadline below actually prompt.
    """
    from ... import __version__ as cli_version

    saved_boxes = list_boxes()

    if not saved_boxes:
        click.echo("No boxes found. Add boxes with: lager boxes add --name [NAME] --ip [IP_ADDRESS] --user [USERNAME]")
        return

    boxes = []
    for name, box_info in sorted(saved_boxes.items(), key=lambda x: natural_sort_key(x[0])):
        if isinstance(box_info, dict):
            boxes.append((name, box_info.get('ip', 'unknown'),
                          box_info.get('user') or 'lagerdata'))
        else:
            boxes.append((name, box_info, 'lagerdata'))

    auth_headers = _resolve_auth_headers(boxes)

    answers = queue.Queue()

    def probe(name, ip, user):
        answers.put(_probe_box(name, ip, user, port, timeout, cli_version,
                               auth_headers.get(ip, {})))

    # One thread per box, uncapped. A cap would mean a second wave of boxes
    # that only starts once the first wave's timeouts expire, which both
    # doubles the worst case and makes the countdown below a lie. These
    # threads spend their whole life blocked on one socket, and a `.lager`
    # holds a hand-managed fleet -- tens, not thousands.
    for name, ip, user in boxes:
        threading.Thread(target=probe, args=(name, ip, user), daemon=True).start()

    # The countdown targets when a healthy box must have answered; we keep
    # waiting a little past it for stalls the socket timeouts do not bound.
    countdown_to = time.monotonic() + _LOCK_TIMEOUT + timeout
    give_up_at = countdown_to + _STRAGGLER_GRACE

    table = _LiveTable(boxes, countdown_to)
    table.paint()

    interrupted = False
    try:
        while table.pending() and time.monotonic() < give_up_at:
            try:
                # Returns at once when an answer is ready, so a burst of
                # replies renders without waiting out the poll interval.
                table.record(answers.get(timeout=_POLL_INTERVAL))
            except queue.Empty:
                pass
            table.paint()
    except KeyboardInterrupt:
        interrupted = True
    finally:
        table.erase()

    stranded = _CANCELLED if interrupted else _ABANDONED
    for name, ip, user in table.pending():
        table.record(_Row(name, ip, user, '-', stranded))

    results = table.rows()

    # Cache reported versions from here rather than from the workers:
    # `update_box_version` read-modify-writes ~/.lager, so concurrent calls
    # would drop each other's edits.
    from ...box_storage import update_box_version
    for row in results:
        if row.version_raw:
            update_box_version(row.name, row.version_raw)

    if interrupted:
        click.secho('Stopped waiting. Boxes that did not answer are marked '
                    f'"{_CANCELLED}".', fg='yellow', err=True)

    needs_update_count = sum(1 for r in results if r.status == 'needs update')
    newer_count = sum(1 for r in results if r.status == 'newer')
    auth_failed_count = sum(1 for r in results if r.auth_denied)
    failed_count = sum(1 for r in results
                       if r.status not in _OK_STATUSES and not r.auth_denied)

    any_locked = any(r.locked_by for r in results)
    any_busy = any(r.busy for r in results)

    name_width = max(len('name'), max(len(r.name) for r in results))
    ip_width = max(len('ip'), max(len(r.ip) for r in results))
    user_width = max(len('user'), max(len(r.user) for r in results))
    version_width = max(len('version'), max(len(r.version) for r in results))
    status_width = max(len('status'), max(len(r.status) for r in results))

    header = f"{'name':<{name_width}}   {'ip':<{ip_width}}   {'user':<{user_width}}   {'version':<{version_width}}   {'status':<{status_width}}"
    total_width = name_width + ip_width + user_width + version_width + status_width + 12

    if any_locked:
        locked_width = max(len('locked by'), max(len(r.locked_by) for r in results))
        header += f"   {'locked by':<{locked_width}}"
        total_width += locked_width + 3

    if any_busy:
        busy_width = max(len('busy'), max(len(r.busy) for r in results))
        header += f"   {'busy':<{busy_width}}"
        total_width += busy_width + 3

    click.echo(header)
    click.echo("=" * total_width)

    for row in results:
        cells = (f"{row.name:<{name_width}}   {row.ip:<{ip_width}}   "
                 f"{row.user:<{user_width}}   {row.version:<{version_width}}   ")
        click.echo(cells, nl=False)
        click.secho(row.status, fg=_status_color(row.status), nl=False)

        # Pad status to fixed width
        click.echo(' ' * (status_width - len(row.status)), nl=False)

        if any_locked:
            if row.locked_by:
                click.echo(f"   ", nl=False)
                click.secho(f"{row.locked_by:<{locked_width}}", fg='magenta', nl=False)
            else:
                click.echo(f"   {'':<{locked_width}}", nl=False)

        if any_busy:
            if row.busy:
                click.echo(f"   ", nl=False)
                click.secho(row.busy, fg='yellow')
            else:
                click.echo(f"   {'':<{busy_width}}")
        elif any_locked:
            click.echo()
        else:
            click.echo()

    click.echo(f'\nYour CLI: {cli_version}')

    if needs_update_count > 0:
        box_word = 'box' if needs_update_count == 1 else 'boxes'
        click.secho(f'{needs_update_count} {box_word} need updating', fg='yellow')
    if newer_count > 0:
        box_word = 'box is' if newer_count == 1 else 'boxes are'
        click.secho(f'{newer_count} {box_word} newer than your CLI', fg='cyan')
    if failed_count > 0:
        box_word = 'box' if failed_count == 1 else 'boxes'
        click.secho(f'{failed_count} {box_word} did not report a version', fg='red')
    if auth_failed_count > 0:
        from ...gateway_auth import auth_server_for_box
        box_word = 'box needs' if auth_failed_count == 1 else 'boxes need'
        click.secho(f'{auth_failed_count} {box_word} sign-in or an access grant', fg='red')
        # The denial recorded each box's auth server, so we can say exactly
        # where to sign in. Usually one server covers the whole fleet.
        signin_urls = sorted({
            url for r in results if r.status in _SIGNIN_STATUSES
            for url in [auth_server_for_box(r.ip)] if url
        })
        for url in signin_urls:
            click.secho(f'  Sign in with: lager login {url}', fg='red')

    root_locked = [r.name for r in results if r.locked_by == 'root']
    if root_locked:
        box_word = 'box is' if len(root_locked) == 1 else 'boxes are'
        click.secho(f'\nWarning: {len(root_locked)} {box_word} locked as root (likely locked from inside a Docker container).', fg='yellow')
        click.secho('Use --user to specify your username next time: lager boxes lock --box [BOX_NAME] --user [USERNAME]', fg='yellow')


@click.group(cls=LagerGroup, invoke_without_command=True)
@click.pass_context
def boxes(ctx):
    """Manage box names and IP addresses"""
    if ctx.invoked_subcommand is None:
        # Default behavior: query all boxes for their versions
        _list_boxes_live()


@boxes.command()
@click.option('--name', required=True, help='Name to assign to the box')
@click.option('--ip', required=True, help='IP address or DNS hostname of the box')
@click.option('--user', required=True, help='Username for SSH connection to the box (e.g. the account you log in as)')
@click.option('--version', required=False, help='Box version/branch (e.g., staging, main)')
@click.option('--yes', is_flag=True, help='Confirm the action without prompting.')
def add(name, ip, user, version, yes):
    """Add a box configuration"""
    if not name or name.strip() == "":
        click.echo(click.style("Error: Box name cannot be empty", fg='red'), err=True)
        raise click.Abort()

    try:
        ip = validate_ip_or_hostname(ip)
    except ValueError as e:
        click.echo(click.style(f"Error: {e}", fg='red'), err=True)
        for line in VALID_FORMATS_CHEATSHEET:
            click.echo(line, err=True)
        raise click.Abort()

    existing_boxes = list_boxes()
    existing_name = None
    existing_ip = None

    if name in existing_boxes:
        existing_box = existing_boxes[name]
        if isinstance(existing_box, dict):
            existing_name = (name, existing_box.get('ip', 'unknown'))
        else:
            existing_name = (name, existing_box)

    for box_name, box_info in existing_boxes.items():
        box_ip = box_info.get('ip') if isinstance(box_info, dict) else box_info
        if box_ip == ip and box_name != name:
            existing_ip = (box_name, box_ip)
            break

    if existing_name or existing_ip:
        click.echo(click.style(f"\n[WARNING] Duplicate box detected!", fg='yellow', bold=True))
        click.echo()

        # Determine the specific conflict and appropriate prompt
        if existing_name and existing_ip:
            # Both name and IP are duplicates (unusual edge case)
            click.echo(f"  A box with the name '{existing_name[0]}' already exists:")
            click.echo(f"    Current: {existing_name[0]} → {existing_name[1]}")
            click.echo(f"    New:     {name} → {ip}")
            click.echo()
            if existing_ip[0] != name:
                click.echo(f"  A box with the IP '{existing_ip[1]}' also already exists:")
                click.echo(f"    Current: {existing_ip[0]} → {existing_ip[1]}")
                click.echo()
            confirm_prompt = "Add this box anyway?"
        elif existing_name:
            # Same name, check if IP is also the same
            if existing_name[1] == ip:
                click.echo(f"  A box with the name '{existing_name[0]}' and IP '{ip}' already exists.")
                click.echo()
                confirm_prompt = "Update existing box?"
            else:
                click.echo(f"  A box with the name '{existing_name[0]}' already exists:")
                click.echo(f"    Current: {existing_name[0]} → {existing_name[1]}")
                click.echo(f"    New:     {name} → {ip}")
                click.echo()
                confirm_prompt = "Overwrite box with new IP?"
        else:
            # Only IP is duplicate (different name)
            click.echo(f"  A box with the IP '{existing_ip[1]}' already exists:")
            click.echo(f"    Current: {existing_ip[0]} → {existing_ip[1]}")
            click.echo(f"    New:     {name} → {ip}")
            click.echo()
            click.echo(f"  Adding '{name}' will overwrite '{existing_ip[0]}' (duplicate IP not allowed).")
            click.echo()
            confirm_prompt = "Overwrite existing box?"

        if not yes and not click.confirm(confirm_prompt, default=False):
            click.echo("Cancelled. Box not added.")
            return

        # If confirmed (or --yes flag used) and there's a duplicate IP with a different name,
        # delete the old box to prevent having two boxes with the same IP
        if existing_ip and existing_ip[0] != name:
            delete_box(existing_ip[0])
    else:
        if not yes:
            click.echo(f"\nYou are about to add the following box:")
            click.echo(f"  Name: {name}")
            click.echo(f"  IP:   {ip}")
            if user:
                click.echo(f"  User: {user}")
            click.echo()

            if not click.confirm("Add this box?", default=False):
                click.echo("Cancelled. Box not added.")
                return

    add_box(name, ip, user, version)
    success_msg = f"Added box '{name}' with IP '{ip}'"
    if user:
        success_msg += f" (user: {user})"
    if version:
        success_msg += f" (version: {version})"
    click.echo(click.style(success_msg, fg='green'))


@boxes.command('add-all')
@click.option('--yes', is_flag=True, help='Confirm the action without prompting.')
def add_all(yes):
    """Add all lager boxes from Tailscale network"""
    import subprocess
    import re

    click.echo(click.style("\nScanning Tailscale network for lager boxes...", fg='blue', bold=True))

    try:
        result = subprocess.run(['tailscale', 'status'], capture_output=True, text=True, check=True)
        output = result.stdout
    except FileNotFoundError:
        click.echo(click.style("Error: tailscale command not found. Is Tailscale installed?", fg='red'), err=True)
        raise click.Abort()
    except subprocess.CalledProcessError as e:
        click.echo(click.style(f"Error running tailscale status: {e}", fg='red'), err=True)
        raise click.Abort()

    boxes_found = []

    for line in output.strip().split('\n'):
        # Skip empty lines
        if not line.strip():
            continue

        # Split line into fields
        fields = line.split()
        if len(fields) < 2:
            continue

        ip = fields[0]
        name = fields[1]

        # Validate IP format (basic check for IPv4)
        if not re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
            continue

        # Check name length (5-8 characters)
        if len(name) >= 5 and len(name) <= 8:
            uppercase_name = name.upper()
            boxes_found.append((uppercase_name, ip))

    if not boxes_found:
        click.echo("No lager boxes found (looking for devices with names 5-8 characters long)")
        return

    click.echo()
    click.echo(click.style(f"Found {len(boxes_found)} lager box(es):", fg='cyan'))
    click.echo()
    for name, ip in boxes_found:
        click.echo(f"  {name} → {ip}")
    click.echo()

    if not yes:
        if not click.confirm(f"Add all {len(boxes_found)} box(es)?", default=True):
            click.echo("Cancelled. No boxes added.")
            return

    added_count = 0
    skipped_count = 0

    click.echo()
    for name, ip in boxes_found:
        existing_boxes = list_boxes()
        if name in existing_boxes:
            existing_ip = existing_boxes[name].get('ip') if isinstance(existing_boxes[name], dict) else existing_boxes[name]
            if existing_ip == ip:
                click.echo(f"  {name}: ", nl=False)
                click.secho('already exists (skipped)', fg='yellow')
                skipped_count += 1
                continue

        # Add the box (without triggering prompts)
        add_box(name, ip, None, None)
        click.echo(f"  {name}: ", nl=False)
        click.secho('added', fg='green')
        added_count += 1

    click.echo()
    click.echo(click.style('Summary:', fg='blue', bold=True))
    click.echo(f"  Added:   {added_count}")
    click.echo(f"  Skipped: {skipped_count}")

    if added_count > 0:
        click.echo()
        click.secho(f'[OK] Successfully added {added_count} box(es)', fg='green')


@boxes.command('delete')
@click.option('--name', required=True, help='Name of the box to delete')
@click.option('--yes', is_flag=True, help='Confirm the action without prompting.')
@click.pass_context
def delete(ctx, name, yes):
    """Delete a box configuration"""
    existing_boxes = list_boxes()
    if name not in existing_boxes:
        click.echo(click.style(f"Error: Box '{name}' not found in .lager file", fg='red'), err=True)
        available = sorted(existing_boxes.keys(), key=natural_sort_key)
        if available:
            click.echo(f"Available boxes: {', '.join(available)}", err=True)
        else:
            click.echo("No boxes configured. Add one with: lager boxes add --name [NAME] --ip [IP_ADDRESS] --user [USERNAME]", err=True)
        ctx.exit(1)

    box_info = existing_boxes[name]
    if isinstance(box_info, dict):
        ip = box_info.get('ip', 'unknown')
    else:
        ip = box_info

    if not yes:
        click.echo(f"\nYou are about to delete the following box:")
        click.echo(f"  Name: {name}")
        click.echo(f"  IP:   {ip}")
        click.echo()

        if not click.confirm("Delete this box?", default=False):
            click.echo("Cancelled. Box not deleted.")
            return

    if delete_box(name):
        click.echo(click.style(f"Deleted box '{name}' from .lager file", fg='green'))
    else:
        click.echo(click.style(f"Error: Failed to delete box '{name}'", fg='red'), err=True)
        ctx.exit(1)


@boxes.command('edit')
@click.option('--name', required=True, help='Name of the box to edit')
@click.option('--ip', required=False, help='New IP address or DNS hostname for the box')
@click.option('--user', required=False, help='New username for SSH connection')
@click.option('--version', required=False, help='New box version/branch')
@click.option('--new-name', required=False, help='New name for the box')
@click.option('--yes', is_flag=True, help='Confirm the action without prompting.')
def edit(name, ip, user, version, new_name, yes):
    """Edit a box configuration"""
    if ip is None and new_name is None and user is None and version is None:
        click.echo(click.style("Error: You must specify at least one change (--ip, --user, --version, or --new-name)", fg='red'), err=True)
        raise click.Abort()

    existing_boxes = list_boxes()
    if name not in existing_boxes:
        click.echo(click.style(f"Box '{name}' not found in .lager file", fg='red'), err=True)
        return

    box_info = existing_boxes[name]
    if isinstance(box_info, dict):
        current_ip = box_info.get('ip')
        current_user = box_info.get('user')
        current_version = box_info.get('version')
    else:
        current_ip = box_info
        current_user = None
        current_version = None

    if ip is not None:
        try:
            ip = validate_ip_or_hostname(ip)
        except ValueError as e:
            click.echo(click.style(f"Error: {e}", fg='red'), err=True)
            for line in VALID_FORMATS_CHEATSHEET:
                click.echo(line, err=True)
            raise click.Abort()

    # Determine new values (keep old if not specified)
    updated_ip = ip if ip else current_ip
    updated_user = user if user is not None else current_user
    updated_version = version if version is not None else current_version
    updated_name = new_name if new_name else name

    if new_name is not None:
        if not new_name or new_name.strip() == "":
            click.echo(click.style("Error: Box name cannot be empty", fg='red'), err=True)
            raise click.Abort()

        # Check if new name conflicts with existing box (unless it's the same box)
        if new_name != name and new_name in existing_boxes:
            existing_new_box = existing_boxes[new_name]
            existing_new_ip = existing_new_box.get('ip') if isinstance(existing_new_box, dict) else existing_new_box
            click.echo(click.style(f"\n[WARNING] A box with the name '{new_name}' already exists!", fg='yellow', bold=True))
            click.echo(f"  Existing: {new_name} → {existing_new_ip}")
            click.echo(f"  This operation will overwrite it.")
            click.echo()

    if not yes:
        click.echo(f"\nYou are about to edit the following box:")
        current_display = f"  Current: {name} → {current_ip}"
        if current_user:
            current_display += f" (user: {current_user})"
        click.echo(current_display)

        changes = []
        if new_name:
            changes.append(f"name: {name} → {updated_name}")
        if ip:
            changes.append(f"IP: {current_ip} → {updated_ip}")
        if user is not None:
            if current_user:
                changes.append(f"user: {current_user} → {updated_user}")
            else:
                changes.append(f"user: (none) → {updated_user}")
        if version is not None:
            if current_version:
                changes.append(f"version: {current_version} → {updated_version}")
            else:
                changes.append(f"version: (none) → {updated_version}")

        for change in changes:
            click.echo(f"  Change:  {change}")
        click.echo()

        if not click.confirm("Apply these changes?", default=False):
            click.echo("Cancelled. Box not modified.")
            return

    # If renaming, delete old entry
    if new_name and new_name != name:
        delete_box(name)

    add_box(updated_name, updated_ip, updated_user, updated_version)

    changes_made = []
    if new_name and new_name != name:
        changes_made.append(f"renamed '{name}' to '{updated_name}'")
    if ip:
        changes_made.append(f"changed IP to '{updated_ip}'")
    if user is not None:
        changes_made.append(f"changed user to '{updated_user}'")
    if version is not None:
        changes_made.append(f"changed version to '{updated_version}'")

    success_msg = f"Updated box"
    if changes_made:
        success_msg += ": " + ", ".join(changes_made)
    click.echo(click.style(success_msg, fg='green'))


@boxes.command('delete-all')
@click.option('--yes', is_flag=True, help='Confirm the action without prompting.')
def delete_all(yes):
    """Delete all box configurations"""
    saved_boxes = list_boxes()
    box_count = len(saved_boxes)

    if box_count == 0:
        click.echo("No boxes found in .lager file. Nothing to delete.")
        return

    click.echo(click.style(f"\n[WARNING] You are about to delete ALL {box_count} box(es) from .lager file:", fg='yellow', bold=True))
    click.echo()
    for name, box_info in sorted(saved_boxes.items(), key=lambda x: natural_sort_key(x[0])):
        if isinstance(box_info, dict):
            ip = box_info.get('ip', 'unknown')
        else:
            ip = box_info
        click.echo(f"  - {name} ({ip})")
    click.echo()

    if not yes and not click.confirm("Are you sure you want to delete ALL boxes?", default=False):
        click.echo("Cancelled. No boxes were deleted.")
        return

    count = delete_all_boxes()
    click.echo(click.style(f"[OK] Deleted all {count} box(es) from .lager file", fg='green'))


@boxes.command('list')
@click.pass_context
def list_duts_cmd(ctx):
    """List boxes"""
    # Reuse the default behavior
    ctx.invoke(boxes)


@boxes.command('export')
@click.option('--output', '-o', type=click.Path(), help='Output file path')
def export(output):
    """Export box configuration"""
    # Load the entire .lager file to preserve all data
    lager_file = get_lager_file_path()

    if not lager_file.exists():
        click.echo(click.style("No .lager file found. Nothing to export.", fg='yellow'))
        return

    try:
        with open(lager_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError:
        click.echo(click.style("Error: .lager file is not valid JSON", fg='red'), err=True)
        raise click.Abort()

    json_output = json.dumps(data, indent=2)

    if output:
        try:
            with open(output, 'w', encoding='utf-8') as f:
                f.write(json_output)
            click.echo(click.style(f"Exported configuration to {output}", fg='green'))
        except IOError as e:
            click.echo(click.style(f"Error writing to file: {e}", fg='red'), err=True)
            raise click.Abort()
    else:
        click.echo(json_output)


@boxes.command('import')
@click.argument('file', type=click.Path(exists=True))
@click.option('--merge', is_flag=True, help='Merge with existing boxes instead of replacing')
@click.option('--yes', is_flag=True, help='Confirm the action without prompting.')
def import_boxes(file, merge, yes):
    """Import box configuration"""
    try:
        with open(file, 'r', encoding='utf-8') as f:
            import_data = json.load(f)
    except json.JSONDecodeError:
        click.echo(click.style(f"Error: '{file}' is not valid JSON", fg='red'), err=True)
        raise click.Abort()
    except IOError as e:
        click.echo(click.style(f"Error reading file: {e}", fg='red'), err=True)
        raise click.Abort()

    # Validate that the import data has boxes (support both old DUTS and new BOXES keys)
    import_boxes_data = import_data.get('BOXES') or import_data.get('boxes') or import_data.get('DUTS') or import_data.get('duts', {})
    if not import_boxes_data:
        click.echo(click.style("Error: Import file does not contain any boxes", fg='red'), err=True)
        raise click.Abort()

    current_boxes = load_boxes()

    if merge:
        # Merge mode: show what will be added/updated
        new_boxes = set(import_boxes_data.keys()) - set(current_boxes.keys())
        updated_boxes = set(import_boxes_data.keys()) & set(current_boxes.keys())

        click.echo(click.style(f"\n{'Merge' if merge else 'Import'} Configuration", fg='cyan', bold=True))
        click.echo(f"Source: {file}")
        click.echo()

        if new_boxes:
            click.echo(click.style(f"Will add {len(new_boxes)} new box(es):", fg='green'))
            for name in sorted(new_boxes, key=natural_sort_key):
                ip = import_boxes_data[name].get('ip') if isinstance(import_boxes_data[name], dict) else import_boxes_data[name]
                click.echo(f"  + {name} → {ip}")
            click.echo()

        if updated_boxes:
            click.echo(click.style(f"Will update {len(updated_boxes)} existing box(es):", fg='yellow'))
            for name in sorted(updated_boxes, key=natural_sort_key):
                current_ip = current_boxes[name].get('ip') if isinstance(current_boxes[name], dict) else current_boxes[name]
                new_ip = import_boxes_data[name].get('ip') if isinstance(import_boxes_data[name], dict) else import_boxes_data[name]
                if current_ip != new_ip:
                    click.echo(f"  ~ {name}: {current_ip} → {new_ip}")
                else:
                    click.echo(f"  = {name} → {new_ip} (no change)")
            click.echo()

        if current_boxes and not new_boxes and not updated_boxes:
            click.echo(click.style("No changes (all boxes already exist with same values)", fg='green'))
            click.echo()

        if current_boxes:
            kept_boxes = set(current_boxes.keys()) - set(import_boxes_data.keys())
            if kept_boxes:
                click.echo(f"Will keep {len(kept_boxes)} existing box(es) not in import file")
    else:
        # Replace mode: show before and after
        click.echo(click.style("\n[WARNING] REPLACE MODE", fg='yellow', bold=True))
        click.echo(f"Source: {file}")
        click.echo()
        click.echo(click.style("This will COMPLETELY REPLACE your current box configuration!", fg='yellow'))
        click.echo()

        if current_boxes:
            click.echo(click.style(f"Current boxes ({len(current_boxes)}) will be DELETED:", fg='red'))
            for name, box_info in sorted(current_boxes.items(), key=lambda x: natural_sort_key(x[0])):
                ip = box_info.get('ip') if isinstance(box_info, dict) else box_info
                click.echo(f"  - {name} → {ip}")
            click.echo()

        click.echo(click.style(f"New boxes ({len(import_boxes_data)}) will be ADDED:", fg='green'))
        for name, box_info in sorted(import_boxes_data.items(), key=lambda x: natural_sort_key(x[0])):
            ip = box_info.get('ip') if isinstance(box_info, dict) else box_info
            click.echo(f"  + {name} → {ip}")
        click.echo()

    if not yes:
        action = "merge these boxes" if merge else "replace your box configuration"
        if not click.confirm(f"Do you want to {action}?", default=False):
            click.echo("Cancelled. No changes made.")
            return

    if merge:
        # Merge: combine current and import boxes
        merged_boxes = current_boxes.copy()
        merged_boxes.update(import_boxes_data)
        save_boxes(merged_boxes)
        click.echo(click.style(f"[OK] Successfully merged {len(import_boxes_data)} box(es) from {file}", fg='green'))
    else:
        # Replace: use only import boxes
        save_boxes(import_boxes_data)
        click.echo(click.style(f"[OK] Successfully imported {len(import_boxes_data)} box(es) from {file}", fg='green'))


from .lock import lock, unlock
boxes.add_command(lock)
boxes.add_command(unlock)


def compare_versions(v1, v2):
    """
    Compare two version strings.
    Returns:
        -1 if v1 < v2 (v1 is older)
         0 if v1 == v2
         1 if v1 > v2 (v1 is newer)
    """
    def parse_version(v):
        # Handle versions like "0.3.7" or "v0.3.7"
        v = v.lstrip('v')
        parts = []
        for part in v.split('.'):
            try:
                parts.append(int(part))
            except ValueError:
                parts.append(0)
        return parts

    v1_parts = parse_version(v1)
    v2_parts = parse_version(v2)

    # Pad shorter version with zeros
    max_len = max(len(v1_parts), len(v2_parts))
    v1_parts.extend([0] * (max_len - len(v1_parts)))
    v2_parts.extend([0] * (max_len - len(v2_parts)))

    for p1, p2 in zip(v1_parts, v2_parts):
        if p1 < p2:
            return -1
        elif p1 > p2:
            return 1
    return 0

