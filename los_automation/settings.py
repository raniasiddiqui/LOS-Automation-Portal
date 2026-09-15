"""
Settings for the automation portal, kept separate from the crawler's config.py so
the two concerns cannot bleed into each other.

The write allowlist is the single most important value in this file. It is read
from server-side environment only and is NEVER settable from the Streamlit UI.
"""
import os
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

import config as crawler_config  # noqa: E402  (path set up by package __init__)


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------

VERIFY = "VERIFY"      # read-only; the crawler's destructive denylist applies
TRANSACT = "TRANSACT"  # writes allowed, authored flows only, dev host only


# --------------------------------------------------------------------------
# Write allowlist — fail closed
# --------------------------------------------------------------------------

# Hosts (with port) that authored flows may create/save records on. Anything not
# listed here is refused BEFORE a browser is launched. An empty value blocks
# every write, which is the safe direction for a mistake to fall in.
_raw_hosts = os.getenv("LOS_ALLOWED_WRITE_HOSTS",
                       "nationalbankinternal-dev.risknucleus.com:341")
ALLOWED_WRITE_HOSTS = {h.strip().lower() for h in _raw_hosts.split(",") if h.strip()}


def host_of(url: str) -> str:
    return (urlparse(url).netloc or "").lower()


def write_allowed(url: str) -> tuple[bool, str]:
    """
    Returns (allowed, reason). Reason is operator-readable and is shown verbatim
    in the UI, so it must explain what to do rather than just refusing.
    """
    host = host_of(url)
    if not host:
        return False, f"Could not read a hostname from {url!r}."
    if not ALLOWED_WRITE_HOSTS:
        return False, ("No write hosts are configured, so all data entry is blocked. "
                       "Set LOS_ALLOWED_WRITE_HOSTS on the server to enable it.")
    if host not in ALLOWED_WRITE_HOSTS:
        return False, (f"{host} is not approved for data entry. Approved: "
                       f"{', '.join(sorted(ALLOWED_WRITE_HOSTS))}. "
                       f"Read-only verification is still available.")
    return True, f"{host} is approved for data entry."


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARTIFACTS_DIR = os.getenv("LOS_ARTIFACTS_DIR", os.path.join(BASE_DIR, "artifacts"))
RESULTS_DB = os.getenv("LOS_RESULTS_DB", os.path.join(BASE_DIR, "results.db"))


# --------------------------------------------------------------------------
# Run behaviour
# --------------------------------------------------------------------------

BASE_URL = crawler_config.BASE_URL
HEADLESS = os.getenv("LOS_HEADLESS", "true").lower() != "false"
# One extra attempt before a check is called FAIL, to absorb ordinary SPA flake.
RETRIES = int(os.getenv("LOS_RETRIES", "1"))
MAX_CONCURRENT_RUNS = int(os.getenv("LOS_MAX_CONCURRENT_RUNS", "2"))
STEP_TIMEOUT_MS = int(os.getenv("LOS_STEP_TIMEOUT_MS", "20000"))

# The record every run verifies. Pinning it matters: the crawler opens whichever
# row happens to be first, so results were not comparable between runs. Fixing
# the id means the same data is checked every time.
CASE_ID = os.getenv("LOS_CASE_ID", "52224-2026")
# How many grid pages to page through looking for that record. Paging is used
# instead of the grid's search box so the tool never types into the application.
MAX_GRID_PAGES = int(os.getenv("LOS_MAX_GRID_PAGES", "12"))
