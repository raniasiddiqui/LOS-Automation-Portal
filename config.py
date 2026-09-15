"""
Central configuration. All values can be overridden via a .env file
or environment variables — never hardcode credentials in source.
"""
import os
from dotenv import load_dotenv


load_dotenv()


# --- Target application ---
BASE_URL = os.getenv("QA_BASE_URL","http://nationalbankinternal-dev.risknucleus.com:341/riskNucleus")
LOGIN_URL = os.getenv("QA_LOGIN_URL", f"{BASE_URL}/login")
USERNAME = os.getenv("QA_USERNAME", "")
PASSWORD = os.getenv("QA_PASSWORD", "")

# CSS selectors for the login form — adjust per app
LOGIN_SELECTORS = {
    "username_input": os.getenv("QA_LOGIN_USER_SELECTOR", "#username"),
    "password_input": os.getenv("QA_LOGIN_PASS_SELECTOR", "#password"),
    "submit_button": os.getenv("QA_LOGIN_SUBMIT_SELECTOR", "button[type=submit]"),
}

# --- Crawl behavior ---
MAX_PAGES = int(os.getenv("QA_MAX_PAGES", "25"))
CRAWL_DEPTH = int(os.getenv("QA_CRAWL_DEPTH", "30"))
SAME_ORIGIN_ONLY = True
# 60s is plenty for a single navigation. The old value (1,500,000 ms = 25 min)
# meant one hung page could stall the whole crawl for nearly half an hour.
NAV_TIMEOUT_MS = int(os.getenv("QA_NAV_TIMEOUT_MS", "60000"))
HEADLESS = os.getenv("QA_HEADLESS", "true").lower() != "false"

# Click safe "open/expand/view" controls to discover modals and business
# forms. Set to false for a passive, zero-click crawl.
ALLOW_INTERACTION = os.getenv("QA_ALLOW_INTERACTION", "true").lower() != "false"
MAX_ACTION_STATES_PER_PAGE = int(os.getenv("QA_MAX_ACTION_STATES", "15"))

# --- Recursive exploration ---
# After the flat action-state pass, descend into each screen: walk its tabs
# and open records from its grid, then repeat inside what those reveal. This
# is what reaches "All Obligors -> open a case -> Basic Information /
# Sector & Industry / Facilities / Collaterals". Strictly read-only: no field
# is ever typed into, and every click still clears DESTRUCTIVE_ACTION_PATTERNS.
RECURSIVE_EXPLORATION = os.getenv("QA_RECURSIVE", "true").lower() != "false"
# 3 = list screen -> open a record -> its tabs -> nested tabs within a tab.
MAX_RECURSION_DEPTH = int(os.getenv("QA_MAX_RECURSION_DEPTH", "3"))
# Records opened per grid. Two is usually enough: rows of the same grid render
# the same detail layout, so extra rows cost time without adding structure.
MAX_ROWS_PER_SCREEN = int(os.getenv("QA_MAX_ROWS_PER_SCREEN", "2"))
# Hard ceiling on states one screen's descent may collect, so a deeply nested
# module cannot turn a single crawl into an unbounded walk.
MAX_DEEP_STATES_PER_PAGE = int(os.getenv("QA_MAX_DEEP_STATES", "40"))

# Paths to skip (logout, external redirects, file downloads, and the vendor
# admin-template demo pages — layout-content-detached-*.html and /changelog
# are shipped with the Angular theme, are not part of the app, and only ever
# yielded the un-bootstrapped 89KB index.html).
EXCLUDE_PATTERNS = [
    "logout", "signout", ".pdf", ".zip", "mailto:",
    "layout-content-detached", "changelog", "/assets/",
]

# --- Output ---
OUTPUT_DIR = os.getenv("QA_OUTPUT_DIR", "./output")
CRAWL_MAP_FILE = f"{OUTPUT_DIR}/site_map.json"
TEST_CASES_FILE = f"{OUTPUT_DIR}/test_cases.xlsx"

# --- Functional Specification Document ---
# Path to the FSD (.docx, .pdf, .md, .txt). Ingested by fsd_ingest.py, then
# matched to crawled screens by fsd_grounding.py.
FSD_FILE = os.getenv("QA_FSD_FILE", "")
# Minimum match score for an FSD step to count as implemented by a screen.
# Lower it if too many steps land in the coverage-gap sheet; raise it if
# steps are being matched to the wrong screens.
GROUNDING_THRESHOLD = float(os.getenv("QA_GROUNDING_THRESHOLD", "0.25"))

# --- LLM ---
# "anthropic" or "groq". Joining an FSD to a crawled UI graph is reasoning-
# heavy; Claude handles it noticeably better than a 70B open model.
LLM_PROVIDER = os.getenv("QA_LLM_PROVIDER", "groq").lower()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

_DEFAULT_MODELS = {"anthropic": "claude-opus-5", "groq": "openai/gpt-oss-120b"}
# Model for FSD ingestion (fsd_ingest.py) and anything else unspecified.
LLM_MODEL = os.getenv("QA_LLM_MODEL", _DEFAULT_MODELS.get(LLM_PROVIDER, "openai/gpt-oss-120b"))

# Model for test-case GENERATION (testcase_generator.py), kept separate because
# the two stages have opposite demands. Ingestion is input-heavy and wants
# faithfulness to the FSD's exact wording; generation is output-heavy — one
# detailed case runs 250-400 tokens, so a screen with 25 cases needs ~10k
# output tokens in a single reply.
GEN_LLM_MODEL = os.getenv("QA_GEN_LLM_MODEL", LLM_MODEL)
# Requested output ceiling for generation.
#
# Groq bills prompt + max_tokens against a per-MINUTE allowance, and the
# free "on_demand" tier is small: 8,000 TPM for openai/gpt-oss-120b and
# 12,000 for llama-3.3-70b-versatile. So this cannot be set generously —
# asking for 32,000 output makes every request fail with 413 no matter how
# short the prompt. 5,000 leaves room for a ~2,000-token prompt inside the
# tightest limit while still fitting roughly 15-20 detailed cases per reply.
# On a paid tier or on Anthropic, raise this substantially.
GEN_MAX_TOKENS = int(os.getenv("QA_GEN_MAX_TOKENS", "5000"))

# Client-side tokens-per-minute pacing for generation, so calls queue instead
# of failing with 413. Set it to the actual TPM of the model in GEN_LLM_MODEL:
#   llama-3.3-70b-versatile  12000   (free on_demand tier)
#   openai/gpt-oss-120b       8000   (free on_demand tier)
# 0 disables pacing — correct on Anthropic or a paid tier.
TPM_BUDGET = int(os.getenv("QA_TPM_BUDGET", "8000"))

DB_FILE = os.path.join(OUTPUT_DIR, "knowledge.db")