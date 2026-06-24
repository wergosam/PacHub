#!/usr/bin/env python3
"""
PacHub — notifier.py
Headless entry point run by the systemd --user timer (pachub-update-check).
Checks for available updates and, if any, sends a desktop notification.
Imports only `backend` (no GTK), so it stays lightweight in the background.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backend


def main():
    backend.run_update_notification_check()
    return 0


if __name__ == "__main__":
    sys.exit(main())
