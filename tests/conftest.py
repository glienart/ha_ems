"""Make the add-on's standalone modules importable by bare name.

The decision/forecast/pricing modules under ha_ems/app have no intra-package
imports, so we can import them directly (optimizer, scheduler, epex, ...).
"""
import os
import sys

APP_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "ha_ems", "app")
)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
