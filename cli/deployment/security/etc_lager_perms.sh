#!/bin/sh
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
#
# Create /etc/lager and give it the ownership a Lager Box needs.
#
# Installed by `lager install` to /usr/local/lib/lager/etc_lager_perms.sh,
# root-owned, and run through sudo. The box's sudoers file grants exactly this
# path with NO arguments, so there is nothing for a caller to steer.
#
# Why a fixed script and not a grant on the commands it runs: the work needs a
# tree walk that skips one directory, and the only ways to say that in sudoers
# are to grant `find` (whose -exec runs anything as root) or a recursive chown
# (which cannot skip). Neither is ever granted. This script is the whole job,
# so the grant can name it and stop.
#
# It is installed only inside the password-authenticated sudo session of
# `lager install`. There is deliberately no NOPASSWD grant that installs it,
# so its content cannot be replaced without the sudo password.
#
# What it does:
#
#   /etc/lager is shared by two writers, and both need it:
#     - the container, which runs as www-data (uid 33)             -> owner
#     - start_box.sh, which runs on the host as the box login user,
#       and whose box_config renderers create files here           -> group
#   Owner-only 33:33 755 silently broke every renderer, because creating a
#   file needs write permission on the DIRECTORY. setgid (2775) keeps files
#   created here in the login user's group.
#
#   authorized_keys.d is SKIPPED. It holds the .pub files that authorize SSH,
#   and its ownership is managed separately (root-owned on a locked-down box,
#   so nothing but the key manager can add a key). Sweeping it into uid 33
#   would let code running in the container authorize its own SSH key.
#   -prune leaves whatever owner that directory already has.
#
#   chown -h, so a symbolic link under /etc/lager has the LINK re-owned and
#   never the file it points at. uid 33 can create links here.
#
# The group is the invoking user's, taken from SUDO_GID. sudo sets that itself;
# a caller cannot choose it without a SETENV grant, and none is given.
#
# Exit codes: 64 arguments were passed, 77 not root, 78 no usable group.

set -eu

# One assignment per line, and nothing else on the line: the unit test points
# these two at a scratch directory by rewriting exactly these lines.
PATH=/usr/sbin:/usr/bin:/sbin:/bin
ETC_LAGER=/etc/lager
export PATH

CONTAINER_UID=33

if [ "$#" -ne 0 ]; then
    echo "etc_lager_perms.sh takes no arguments" >&2
    exit 64
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "etc_lager_perms.sh must run as root. Run it with sudo." >&2
    exit 77
fi

gid="${SUDO_GID:-}"
if [ -z "$gid" ] && [ -n "${SUDO_USER:-}" ]; then
    # Some sudo builds set SUDO_USER and not SUDO_GID.
    gid="$(id -g "$SUDO_USER" 2>/dev/null || true)"
fi

case "$gid" in
    ''|*[!0-9]*)
        echo "etc_lager_perms.sh cannot tell which group to give /etc/lager to: run it with sudo, as the box login user." >&2
        exit 78
        ;;
esac

# Group 0 is right only when root itself is the login user. From anyone else
# it means the environment is not what sudo would have set.
if [ "$gid" -eq 0 ] && [ "${SUDO_UID:-}" != "0" ]; then
    echo "etc_lager_perms.sh refuses group 0 for a login user that is not root." >&2
    exit 78
fi

mkdir -p "$ETC_LAGER"
find "$ETC_LAGER" -path "$ETC_LAGER/authorized_keys.d" -prune -o -exec chown -h "$CONTAINER_UID:$gid" {} +
chmod 2775 "$ETC_LAGER"
