# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import logging

log = logging.getLogger('lager')
log.setLevel(logging.INFO)  # Changed from DEBUG to INFO to suppress debug messages

ch = logging.StreamHandler()
ch.setLevel(logging.INFO)  # Changed from DEBUG to INFO to suppress debug messages

formatter = logging.Formatter('%(asctime)s %(levelname)s %(filename)s:%(lineno)d %(message)s')
ch.setFormatter(formatter)
log.addHandler(ch)

def trace(msg, *args, **kwargs):
    if logging.getLogger().isEnabledFor(logging.DEBUG - 5):
        logging.log(logging.DEBUG - 5, msg)

logging.addLevelName(logging.DEBUG - 5, "TRACE")
logging.trace = trace
logging.Logger.trace = trace
