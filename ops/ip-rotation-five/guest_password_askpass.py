#!/usr/bin/env python3
"""OpenSSH-only password bridge. Never accepts a password in argv or a file."""
import os
import sys

value = os.environ.get("SPN_GUEST_SETUP_PASSWORD")
if value is None or any(char in value for char in ("\x00", "\r", "\n")):
    raise SystemExit(1)
sys.stdout.write(value + "\n")
