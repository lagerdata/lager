#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# colors.sh - Color definitions for Lager test output
#
# Usage:
#   source "${SCRIPT_DIR}/../framework/colors.sh"
#   echo -e "${GREEN}Success!${NC}"
#   echo -e "${RED}Error!${NC}"
#
# Available colors:
#   GREEN  - Success/pass indicators
#   RED    - Error/fail indicators
#   YELLOW - Warnings
#   BLUE   - Information/headers
#   BOLD   - Bold text
#   NC     - No Color (reset)

# ANSI color codes
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'  # No Color - resets formatting

# Export for subshells
export GREEN RED YELLOW BLUE BOLD NC
