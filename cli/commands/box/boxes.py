# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.box.boxes

    Box commands for managing local configurations
"""
import click
import ipaddress
import json
from texttable import Texttable
from ...box_storage import add_box, delete_box, delete_all_boxes, list_boxes, load_boxes, save_boxes, get_lager_file_path
from ...sort_utils import natural_sort_key


def _list_boxes_live(port=5000, timeout=5):
    """Query all boxes for their versions and display status table."""
    import requests
    import sys
    import threading
    import time
    from ...box_storage import update_box_version
    from ... import __version__ as cli_version

    saved_boxes = list_boxes()

    if not saved_boxes:
        click.echo("No boxes found. Add boxes with: lager boxes add --name <NAME> --ip <IP>")
        return

    box_word = 'box' if len(saved_boxes) == 1 else 'boxes'
    spinner_chars = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
    spinner_stop = threading.Event()

    def spin():
        i = 0
        while not spinner_stop.is_set():
            sys.stdout.write(f'\r{spinner_chars[i % len(spinner_chars)]} Loading {len(saved_boxes)} {box_word}...')
            sys.stdout.flush()
            i += 1
            time.sleep(0.1)

    spinner_thread = threading.Thread(target=spin, daemon=True)
    spinner_thread.start()

    results = []
    failed_count = 0
    needs_update_count = 0
    newer_count = 0

    for name, box_info in sorted(saved_boxes.items(), key=lambda x: natural_sort_key(x[0])):
        if isinstance(box_info, dict):
            ip = box_info.get('ip', 'unknown')
        else:
            ip = box_info

        if ip == 'unknown':
            results.append((name, ip, '-', 'no IP'))
            failed_count += 1
            continue

        try:
            url = f'http://{ip}:{port}/cli-version'
            headers = {'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}
            response = requests.get(url, timeout=timeout, headers=headers)

            if response.status_code == 200:
                try:
                    data = response.json()
                    box_version = data.get('box_version')

                    if box_version:
                        update_box_version(name, box_version)

                        version_cmp = compare_versions(box_version, cli_version)

                        if version_cmp == 0:
                            results.append((name, ip, box_version, 'current'))
                        elif version_cmp < 0:
                            results.append((name, ip, box_version, 'needs update'))
                            needs_update_count += 1
                        else:
                            results.append((name, ip, box_version, 'newer'))
                            newer_count += 1
                    else:
                        results.append((name, ip, '-', 'bad response'))
                        failed_count += 1
                except ValueError:
                    results.append((name, ip, '-', 'invalid JSON'))
                    failed_count += 1

            elif response.status_code == 404:
                results.append((name, ip, '-', 'old box'))
                failed_count += 1
            else:
                results.append((name, ip, '-', f'HTTP {response.status_code}'))
                failed_count += 1

        except requests.exceptions.Timeout:
            results.append((name, ip, '-', 'timeout'))
            failed_count += 1
        except requests.exceptions.ConnectionError:
            results.append((name, ip, '-', 'unreachable'))
            failed_count += 1
        except Exception:
            results.append((name, ip, '-', 'error'))
            failed_count += 1

    spinner_stop.set()
    spinner_thread.join(timeout=1)
    sys.stdout.write('\r' + ' ' * 40 + '\r')
    sys.stdout.flush()

    name_width = max(len('name'), max(len(r[0]) for r in results))
    ip_width = max(len('ip'), max(len(r[1]) for r in results))
    version_width = max(len('version'), max(len(r[2]) for r in results))
    status_width = max(len('status'), max(len(r[3]) for r in results))

    header = f"{'name':<{name_width}}   {'ip':<{ip_width}}   {'version':<{version_width}}   {'status':<{status_width}}"
    total_width = name_width + ip_width + version_width + status_width + 9
    click.echo(header)
    click.echo("=" * total_width)

    for name, ip, version, status in results:
        row = f"{name:<{name_width}}   {ip:<{ip_width}}   {version:<{version_width}}   "
        click.echo(row, nl=False)

        if status == 'current':
            click.secho(status, fg='green')
        elif status == 'needs update':
            click.secho(status, fg='yellow')
        elif status == 'newer':
            click.secho(status, fg='cyan')
        else:
            click.secho(status, fg='red')

    click.echo(f'\nYour CLI: {cli_version}')

    if needs_update_count > 0:
        box_word = 'box' if needs_update_count == 1 else 'boxes'
        click.secho(f'{needs_update_count} {box_word} need updating', fg='yellow')
    if newer_count > 0:
        box_word = 'box is' if newer_count == 1 else 'boxes are'
        click.secho(f'{newer_count} {box_word} newer than your CLI', fg='cyan')
    if failed_count > 0:
        box_word = 'box' if failed_count == 1 else 'boxes'
        click.secho(f'{failed_count} {box_word} could not be reached', fg='red')


@click.group(invoke_without_command=True)
@click.pass_context
def boxes(ctx):
    """Manage box names and IP addresses"""
    if ctx.invoked_subcommand is None:
        # Default behavior: query all boxes for their versions
        _list_boxes_live()


@boxes.command()
@click.option('--name', required=True, help='Name to assign to the box')
@click.option('--ip', required=True, help='IP address of the box')
@click.option('--user', required=False, help='Username for SSH connection (defaults to lagerdata)')
@click.option('--version', required=False, help='Box version/branch (e.g., staging, main)')
@click.option('--yes', is_flag=True, help='Confirm the action without prompting.')
def add(name, ip, user, version, yes):
    """Add a box configuration"""
    if not name or name.strip() == "":
        click.echo(click.style("Error: Box name cannot be empty", fg='red'), err=True)
        raise click.Abort()

    if not ip or ip.strip() == "":
        click.echo(click.style("Error: IP address cannot be empty", fg='red'), err=True)
        raise click.Abort()

    try:
        ipaddress.ip_address(ip)
    except ValueError:
        click.echo(click.style(f"Error: '{ip}' is not a valid IP address", fg='red'), err=True)
        click.echo("Valid formats:", err=True)
        click.echo("  IPv4: 192.168.1.100, 10.0.0.1", err=True)
        click.echo("  IPv6: 2001:db8::1, fe80::1", err=True)
        click.echo("  Tailscale: 100.x.x.x (get from 'tailscale status')", err=True)
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
            click.echo("No boxes configured. Add one with: lager boxes add --name <name> --ip <ip>", err=True)
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
@click.option('--ip', required=False, help='New IP address for the box')
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

    # Determine new values (keep old if not specified)
    updated_ip = ip if ip else current_ip
    updated_user = user if user is not None else current_user
    updated_version = version if version is not None else current_version
    updated_name = new_name if new_name else name

    if ip is not None:
        if not ip or ip.strip() == "":
            click.echo(click.style("Error: IP address cannot be empty", fg='red'), err=True)
            raise click.Abort()

        try:
            ipaddress.ip_address(ip)
        except ValueError:
            click.echo(click.style(f"Error: '{ip}' is not a valid IP address", fg='red'), err=True)
            click.echo("Valid formats:", err=True)
            click.echo("  IPv4: 192.168.1.100, 10.0.0.1", err=True)
            click.echo("  IPv6: 2001:db8::1, fe80::1", err=True)
            click.echo("  Tailscale: 100.x.x.x (get from 'tailscale status')", err=True)
            raise click.Abort()

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


@boxes.command('connect')
@click.option('--box', required=True, help='Name of the box to connect')
@click.option('--url', default='https://api.stoutdata.ai', help='Control plane URL')
@click.option('--api-key', required=True, help='API key for control plane authentication')
@click.option('--heartbeat-interval', default=30, type=int, help='Heartbeat interval in seconds (default: 30)')
@click.option('--yes', is_flag=True, help='Confirm the action without prompting.')
def connect(box, url, api_key, heartbeat_interval, yes):
    """Connect a box to a control plane for heartbeat reporting."""
    import subprocess
    import time
    import requests
    import shlex
    from ...box_storage import get_box_ip, get_box_user
    from ...core.ssh_utils import get_reusable_ssh_command

    # Validate URL
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        click.secho(f"Error: URL scheme must be http or https, got '{parsed.scheme}'", fg='red', err=True)
        raise click.Abort()
    if not parsed.hostname:
        click.secho("Error: URL must include a hostname", fg='red', err=True)
        raise click.Abort()

    # Warn if control plane is unreachable from this machine (may still work from the box)
    import requests as _req
    try:
        _req.get(f'{url.rstrip("/")}/healthz', timeout=5)
    except _req.exceptions.RequestException:
        click.secho(
            f"Warning: Could not reach {url} from this machine. "
            "It may still be reachable from the box.",
            fg='yellow',
        )

    ip = get_box_ip(box)
    if not ip:
        click.secho(f"Error: Box '{box}' not found in configuration", fg='red', err=True)
        raise click.Abort()

    user = get_box_user(box) or 'lagerdata'

    if not yes:
        click.echo(f"\nConnect box '{box}' ({ip}) to control plane:")
        click.echo(f"  URL:       {url}")
        click.echo(f"  API Key:   {api_key[:8]}...")
        click.echo(f"  Interval:  {heartbeat_interval}s")
        click.echo()
        if not click.confirm("Proceed?", default=False):
            click.echo("Cancelled.")
            return

    config = json.dumps({
        'url': url,
        'api_key': api_key,
        'heartbeat_interval_seconds': heartbeat_interval,
        'enabled': True,
    })

    # Write config into the lager Docker container (avoids needing sudo on host)
    click.echo(f"Writing control plane config to {box}...")
    write_cmd = f'echo {shlex.quote(config)} | docker exec -u root -i lager tee /etc/lager/control_plane.json > /dev/null'
    ssh_cmd = get_reusable_ssh_command(ip, user=user, command=write_cmd)
    result = subprocess.run(ssh_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        click.secho(f"Error writing config: {result.stderr.strip()}", fg='red', err=True)
        raise click.Abort()

    # Restart the box container
    click.echo(f"Restarting lager container on {box}...")
    ssh_cmd = get_reusable_ssh_command(ip, user=user, command='docker restart lager')
    result = subprocess.run(ssh_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        click.secho(f"Error restarting container: {result.stderr.strip()}", fg='red', err=True)
        raise click.Abort()

    # Verify the box is healthy
    click.echo("Waiting for box to come back up...")
    time.sleep(5)
    try:
        resp = requests.get(f'http://{ip}:5000/status', timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            click.secho(f"Box '{box}' connected successfully!", fg='green')
            click.echo(f"  Healthy: {data.get('healthy')}")
            click.echo(f"  Version: {data.get('version')}")
            nets = data.get('nets', [])
            if nets:
                click.echo(f"  Nets:    {len(nets)}")
        else:
            click.secho(f"Warning: Box returned HTTP {resp.status_code}", fg='yellow')
    except requests.exceptions.RequestException as e:
        click.secho(f"Warning: Could not verify box status: {e}", fg='yellow')
        click.echo("The config was written. The box may still be starting up.")


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


