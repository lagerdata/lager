# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Hardware security module (HSM) commands.

Commands that log in to the HSM read the password from --password-stdin,
then from the LAGER_HSM_PASSWORD environment variable, then from a hidden
prompt. There is no --password option, because an option value stays in the
shell history and shows in the process list. Lager never stores the password.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import sys

import click
from ...core.net_group import NetGroup
from ...core.net_helpers import (
    resolve_box,
    resolve_box_locked,
    display_nets,
    post_net_command,
    validate_net_exists,
)

HSM_ROLE = "hsm"
PASSWORD_ENV = "LAGER_HSM_PASSWORD"

# The box validates these too (box/lager/security/hsm/yubihsm2.py); the CLI
# keeps a copy so that --help lists the choices without a box.
# No RSA: the HSM takes up to minutes to generate an RSA key, longer than
# hardware_service lets one call run before it restarts. Python scripts call
# the driver directly and can generate RSA keys.
ALGORITHMS = ("ec-p256", "ec-p384", "ec-p521", "ec-k256", "ed25519")
MECHANISMS = ("ecdsa-sha256", "ecdsa-sha384", "ecdsa-sha512", "eddsa",
              "rsa-pkcs1-sha256", "rsa-pkcs1-sha384", "rsa-pkcs1-sha512",
              "rsa-pss-sha256", "rsa-pss-sha384", "rsa-pss-sha512")
OBJECT_TYPES = ("asymmetric-key", "authentication-key", "opaque", "wrap-key",
                "hmac-key", "template", "otp-aead-key")


def _login_options(f):
    f = click.option("--password-stdin", is_flag=True, default=False,
                     help=f"Read the password from the first line of stdin. "
                          f"Without it, {PASSWORD_ENV} is used, then a prompt")(f)
    f = click.option("--auth-key-id", type=click.IntRange(1, 0xFFFF), default=1,
                     show_default=True, help="Authentication key to log in with")(f)
    return f


def _read_password(password_stdin: bool, auth_key_id: int) -> str:
    if password_stdin:
        value = sys.stdin.readline().rstrip("\r\n")
        if not value:
            raise click.UsageError("--password-stdin is set, but stdin has no password.")
        return value
    value = os.environ.get(PASSWORD_ENV)
    if value:
        return value
    return click.prompt(f"Password for auth key {auth_key_id}", hide_input=True)


def _target(ctx, box):
    """Resolve the box and the net for a subcommand. Returns (box_ip, netname) or None."""
    if box is None:
        box = getattr(ctx.obj, "hsm_box", None)
    box_ip = resolve_box_locked(ctx, box, "hsm")
    netname = getattr(ctx.obj, "hsm_netname", None)
    if netname is None:
        display_nets(ctx, box_ip, None, HSM_ROLE, "HSM")
        return None
    netname = netname.strip()
    if validate_net_exists(ctx, box_ip, netname, HSM_ROLE) is None:
        return None
    return box_ip, netname


def _send(ctx, box_ip, netname, action, http_timeout=30.0, **params):
    return post_net_command(ctx, box_ip, netname, action, role=HSM_ROLE, quiet=True,
                            http_timeout=http_timeout, **params)


def _login(password_stdin, auth_key_id):
    return {"auth_key_id": auth_key_id,
            "password": _read_password(password_stdin, auth_key_id)}


def _confirm(yes, password_stdin, question):
    if yes:
        return
    if password_stdin:
        raise click.UsageError(
            "--password-stdin needs --yes, because the confirmation prompt also reads stdin.")
    click.confirm(question, abort=True)


def _domains(value: str) -> list[int]:
    try:
        domains = [int(part) for part in value.split(",") if part.strip()]
    except ValueError:
        raise click.BadParameter("Give domain numbers separated by commas, e.g. 1,2.",
                                 param_hint="--domains") from None
    if not domains or any(not 1 <= d <= 16 for d in domains):
        raise click.BadParameter("Each domain must be a number from 1 to 16.",
                                 param_hint="--domains")
    return domains


@click.group(
    name="hsm",
    cls=NetGroup,
    invoke_without_command=True,
    help="Check and provision a hardware security module (HSM) net",
)
@click.option("--box", required=False, help="Lager Box name or IP")
@click.argument("netname", required=False, metavar="[NET_NAME]")
@click.pass_context
def hsm(ctx, box, netname):
    """HSM group.  Usage: lager hsm [NET_NAME] [COMMAND] --box [BOX_NAME]"""
    if netname is not None:
        ctx.obj.hsm_netname = netname
    ctx.obj.hsm_box = box

    if ctx.invoked_subcommand is None:
        box_ip = resolve_box(ctx, box, read_only=True)
        display_nets(ctx, box_ip, None, HSM_ROLE, "HSM")


hsm.net_examples = [
    "lager hsm hsm1 info --box <BOX>",
    "lager hsm hsm1 objects --box <BOX>",
    "lager hsm hsm1 generate --algorithm ec-p256 --label test --box <BOX>",
    "lager hsm hsm1 sign --id 0x0005 --mechanism ecdsa-sha256 --data-file msg.bin --box <BOX>",
    "lager hsm --box <BOX>               (list HSM nets)",
]


class _ObjectId(click.ParamType):
    """An object ID in decimal or hex (0x0005)."""

    name = "ID"

    def convert(self, value, param, ctx):
        try:
            object_id = int(str(value), 0)
        except ValueError:
            self.fail(f"'{value}' is not a number. Use decimal or hex (0x0005).", param, ctx)
        if not 0 <= object_id <= 0xFFFF:
            self.fail(f"{value} is out of range 0-65535.", param, ctx)
        return object_id


OBJECT_ID = _ObjectId()


@hsm.command(name="info", help="Show firmware, serial and log usage (no login)")
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit a machine-readable JSON object instead of formatted text")
@click.pass_context
def info(ctx, box, as_json):
    target = _target(ctx, box)
    if target is None:
        return
    box_ip, netname = target
    value = _send(ctx, box_ip, netname, "info").get("value") or {}
    if as_json:
        click.echo(json.dumps({"netname": netname, **value}))
        return
    click.secho(f"HSM '{netname}':", fg="green")
    click.echo(f"  Firmware:    {value.get('firmware')}")
    click.echo(f"  Serial:      {value.get('serial')}")
    click.echo(f"  Part number: {value.get('part_number') or '-'}")
    click.echo(f"  Log:         {value.get('log_used')}/{value.get('log_size')} entries")
    click.echo(f"  Algorithms:  {len(value.get('algorithms') or [])} supported")


@hsm.command(name="blink", help="Blink the HSM LED to find the device")
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option("--seconds", type=click.IntRange(0, 255), default=5, show_default=True,
              help="How long to blink")
@_login_options
@click.pass_context
def blink(ctx, box, seconds, auth_key_id, password_stdin):
    target = _target(ctx, box)
    if target is None:
        return
    box_ip, netname = target
    _send(ctx, box_ip, netname, "blink", http_timeout=seconds + 30.0,
          seconds=seconds, **_login(password_stdin, auth_key_id))
    click.secho(f"Blinking '{netname}' for {seconds} s", fg="green")


@hsm.command(name="objects", help="List the objects stored on the HSM")
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option("--type", "object_type", type=click.Choice(OBJECT_TYPES), default=None,
              help="Show only objects of this type")
@click.option("--label", default=None, help="Show only objects with this label")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit a machine-readable JSON array instead of a table")
@_login_options
@click.pass_context
def objects(ctx, box, object_type, label, as_json, auth_key_id, password_stdin):
    target = _target(ctx, box)
    if target is None:
        return
    box_ip, netname = target
    params = {"type": object_type, "label": label}
    rows = _send(ctx, box_ip, netname, "objects",
                 **{k: v for k, v in params.items() if v is not None},
                 **_login(password_stdin, auth_key_id)).get("value") or []
    if as_json:
        click.echo(json.dumps(rows))
        return
    if not rows:
        click.echo("No objects.")
        return
    table = [("ID", "Type", "Algorithm", "Domains", "Label")]
    for row in rows:
        table.append((f"0x{int(row['id']):04X}", row.get("type") or "",
                      row.get("algorithm") or "", _domain_ranges(row.get("domains") or []),
                      row.get("label") or ""))
    widths = [max(len(cells[i]) for cells in table) for i in range(4)]
    for cells in table:
        click.echo("  ".join(cell.ljust(width) for cell, width in zip(cells, widths))
                   + "  " + cells[4])


def _domain_ranges(domains) -> str:
    """[1, 2, 3, 5] -> '1-3,5'."""
    parts = []
    for d in sorted(int(x) for x in domains):
        if parts and parts[-1][1] == d - 1:
            parts[-1][1] = d
        else:
            parts.append([d, d])
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in parts)


@hsm.command(name="generate", help="Generate an asymmetric key on the HSM")
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option("--algorithm", type=click.Choice(ALGORITHMS), required=True,
              help="Key algorithm")
@click.option("--id", "object_id", type=OBJECT_ID, default=0, show_default=True,
              help="Object ID for the key. 0 lets the HSM choose")
@click.option("--label", default="", help="Label to store with the key")
@click.option("--domains", default="1", show_default=True,
              help="Domains the key belongs to, separated by commas")
@click.option("--capability", "capabilities", multiple=True,
              help="Capability to give the key (repeatable). "
                   "Default: the signing capability for the algorithm")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit a machine-readable JSON object instead of the PEM")
@_login_options
@click.pass_context
def generate(ctx, box, algorithm, object_id, label, domains, capabilities, as_json,
             auth_key_id, password_stdin):
    domain_list = _domains(domains)
    target = _target(ctx, box)
    if target is None:
        return
    box_ip, netname = target
    value = _send(ctx, box_ip, netname, "generate", algorithm=algorithm, object_id=object_id,
                  label=label, domains=domain_list, capabilities=list(capabilities) or None,
                  **_login(password_stdin, auth_key_id)).get("value") or {}
    if as_json:
        click.echo(json.dumps(value))
        return
    click.secho(f"Generated {algorithm} key 0x{int(value['id']):04X}", fg="green", err=True)
    click.echo(value["public_key_pem"], nl=False)


@hsm.command(name="pubkey", help="Print the public key of an asymmetric key as PEM")
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option("--id", "object_id", type=OBJECT_ID, required=True, help="Object ID of the key")
@_login_options
@click.pass_context
def pubkey(ctx, box, object_id, auth_key_id, password_stdin):
    target = _target(ctx, box)
    if target is None:
        return
    box_ip, netname = target
    pem = _send(ctx, box_ip, netname, "pubkey", object_id=object_id,
                **_login(password_stdin, auth_key_id)).get("value")
    click.echo(pem, nl=False)


@hsm.command(name="sign", help="Sign data with an asymmetric key")
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option("--id", "object_id", type=OBJECT_ID, required=True, help="Object ID of the key")
@click.option("--mechanism", type=click.Choice(MECHANISMS), required=True,
              help="Signature mechanism")
@click.option("--data-file", type=click.Path(exists=True, dir_okay=False), default=None,
              help="File that holds the data to sign")
@click.option("--data-hex", default=None, help="Data to sign, as hex")
@click.option("--out", type=click.Path(dir_okay=False, writable=True), default=None,
              help="Write the raw signature to this file instead of printing hex")
@_login_options
@click.pass_context
def sign(ctx, box, object_id, mechanism, data_file, data_hex, out, auth_key_id, password_stdin):
    if (data_file is None) == (data_hex is None):
        raise click.UsageError("Give exactly one of --data-file and --data-hex.")
    if data_file is not None:
        with open(data_file, "rb") as fh:
            data = fh.read()
    else:
        try:
            data = bytes.fromhex(data_hex)
        except ValueError:
            raise click.BadParameter("Not valid hex.", param_hint="--data-hex") from None
    target = _target(ctx, box)
    if target is None:
        return
    box_ip, netname = target
    value = _send(ctx, box_ip, netname, "sign", object_id=object_id, mechanism=mechanism,
                  data=base64.b64encode(data).decode("ascii"),
                  **_login(password_stdin, auth_key_id)).get("value") or {}
    try:
        signature = base64.b64decode(value["signature"], validate=True)
    except (KeyError, binascii.Error):
        click.secho("Error: the box returned no valid signature.", fg="red", err=True)
        raise SystemExit(1)
    if out:
        with open(out, "wb") as fh:
            fh.write(signature)
        click.secho(f"Wrote {len(signature)}-byte signature to {out}", fg="green", err=True)
    else:
        click.echo(signature.hex())


@hsm.command(name="delete", help="Delete an object from the HSM")
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option("--id", "object_id", type=OBJECT_ID, required=True, help="Object ID to delete")
@click.option("--type", "object_type", type=click.Choice(OBJECT_TYPES),
              default="asymmetric-key", show_default=True, help="Type of the object")
@click.option("--yes", is_flag=True, default=False, help="Do not ask for confirmation")
@_login_options
@click.pass_context
def delete(ctx, box, object_id, object_type, yes, auth_key_id, password_stdin):
    target = _target(ctx, box)
    if target is None:
        return
    box_ip, netname = target
    _confirm(yes, password_stdin,
             f"Delete {object_type} 0x{object_id:04X} from '{netname}'? You cannot undo this.")
    _send(ctx, box_ip, netname, "delete", object_id=object_id, object_type=object_type,
          **_login(password_stdin, auth_key_id))
    click.secho(f"Deleted {object_type} 0x{object_id:04X}", fg="green")


@hsm.command(name="reset", help="Erase the HSM and restore the factory login")
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option("--yes", is_flag=True, default=False, help="Do not ask for confirmation")
@_login_options
@click.pass_context
def reset(ctx, box, yes, auth_key_id, password_stdin):
    target = _target(ctx, box)
    if target is None:
        return
    box_ip, netname = target
    _confirm(yes, password_stdin,
             f"Reset '{netname}'? This erases every key and object on the HSM "
             f"and restores the factory login. You cannot undo this.")
    _send(ctx, box_ip, netname, "reset", http_timeout=90.0,
          **_login(password_stdin, auth_key_id))
    click.secho(f"Reset '{netname}'. The HSM has the factory login again.", fg="green")
