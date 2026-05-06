# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import sys

def raise_for_status(resp):
    """Raises :class:`HTTPError`, if one occurred.
       Copied from requests and modified. """
    from requests.exceptions import HTTPError

    http_error_msg = ''
    if isinstance(resp.reason, bytes):
        # We attempt to decode utf-8 first because some servers
        # choose to localize their reason strings. If the string
        # isn't utf-8, we fall back to iso-8859-1 for all other
        # encodings. (See PR #3538)
        try:
            reason = resp.reason.decode('utf-8')
        except UnicodeDecodeError:
            reason = resp.reason.decode('iso-8859-1')
    else:
        reason = resp.reason

    if 400 <= resp.status_code < 500:
        http_error_msg = u'%s Client Error: %s for url: %s' % (resp.status_code, reason, resp.url)

    elif 500 <= resp.status_code < 600:
        http_error_msg = u'%s Server Error: %s for url: %s' % (resp.status_code, reason, resp.url)

    try:
        # If there's a lager error hiding in there, pull it out
        lager_error = resp.json()
        http_error_msg += f": {lager_error['error']['code']}: {lager_error['error']['description']}"
    except (ValueError, KeyError):
        pass

    if http_error_msg:
        raise HTTPError(http_error_msg, response=resp)

def echo_response(resp, file=sys.stderr.buffer):
    for byte in resp.iter_content(chunk_size=1):
        file.write(byte)

