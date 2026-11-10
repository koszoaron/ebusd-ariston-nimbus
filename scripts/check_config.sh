#!/bin/sh
# Validate config/ with ebusd's own config checker. Never connects to a device.
# EBUSD: path to the ebusd binary (default: ebusd on PATH).
set -e
cd "$(dirname "$0")/.."
"${EBUSD:-ebusd}" --configpath=./config -f --enablehex --loglevel=debug --checkconfig
