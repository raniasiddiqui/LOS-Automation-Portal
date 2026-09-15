"""
LOS Automation Portal.

A verification + data-entry runner for the Loan Origination System, driven from a
Streamlit UI so a non-technical operator can pick a menu and get pass/fail.

This package deliberately REUSES the crawler that already understands this app
(SPA-safe navigation, settle detection, routed-content detection, the
destructive-action denylist) rather than reimplementing browser control. See
../crawler.py.

Nothing here modifies the existing crawl/FSD/test-generation pipeline.
"""
import os
import sys

# The reusable modules (crawler, config) live one level up.
# Insert that directory so `import crawler` works whether this package is run
# via `python -m los_automation...` from the project root or launched by
# Streamlit from elsewhere.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

PROJECT_ROOT = _PROJECT_ROOT

__all__ = ["PROJECT_ROOT"]
