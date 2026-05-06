# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

class PassthroughFunctionMapper:
    def __init__(self, net, device):
        self.net = net
        self.device = device

    def __getattr__(self, attr):
        return getattr(self.device, attr)
