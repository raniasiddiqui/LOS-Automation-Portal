"""
Orchestrates the full understand-the-app pipeline:

    crawler.py  --(site_map.json)-->  parser.py  --(structured pages)-->  knowledge_store.py

The expensive step is crawling (real browser, real logins, real page
loads). By default this script skips it whenever there's already
something to work with:

  - If the knowledge base already has pages and you didn't pass --recrawl,
    nothing runs at all — you're already good to go for test-case generation.
  - If the knowledge base is empty but a previous site_map.json exists,
    it re-parses that cached HTML instead of re-crawling.
  - Only crawls for real if neither exists, or you explicitly pass --recrawl.

Usage:
    python pipeline.py                # use cache if possible
    python pipeline.py --reparse      # re-run parser on existing site_map.json, refresh DB
    python pipeline.py --recrawl      # force a fresh crawl + reparse + refresh DB
"""
import argparse
import json
import os

import config
import crawler
import parser as page_parser
import knowledge_graph as ks


def run_crawl() -> str:
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    data = crawler.crawl()
    with open(config.CRAWL_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"Crawled {len(data)} pages. Saved raw map to {config.CRAWL_MAP_FILE}")
    return config.CRAWL_MAP_FILE


def run_parse_and_store(site_map_path: str):
    parsed_pages = page_parser.parse_site_map(site_map_path)

    with open(site_map_path, "r", encoding="utf-8") as f:
        raw_pages = {p["url"]: p for p in json.load(f)}

    ks.init_db()
    new_or_changed = 0
    with ks.connect() as conn:
        for parsed in parsed_pages:
            raw = raw_pages[parsed["url"]]
            changed = ks.upsert_page(conn, parsed["url"], parsed["title"], raw["html"])
            if changed:
                new_or_changed += 1
            ks.store_parsed_page(conn, parsed)

    total_forms = sum(len(p["forms"]) for p in parsed_pages)
    print(
        f"Stored {len(parsed_pages)} pages into the knowledge base "
        f"({new_or_changed} new/changed, {total_forms} forms) — {config.DB_FILE}"
    )


def main():
    ap = argparse.ArgumentParser(description="Crawl (if needed), parse, and index the app.")
    ap.add_argument("--recrawl", action="store_true", help="Force a fresh crawl even if cached data exists.")
    ap.add_argument("--reparse", action="store_true", help="Re-parse the existing site_map.json into the DB.")
    args = ap.parse_args()

    ks.init_db()

    if args.recrawl:
        site_map_path = run_crawl()
        run_parse_and_store(site_map_path)
        return

    if args.reparse:
        if not os.path.exists(config.CRAWL_MAP_FILE):
            print(f"No cached crawl found at {config.CRAWL_MAP_FILE} — run with --recrawl first.")
            return
        run_parse_and_store(config.CRAWL_MAP_FILE)
        return

    if ks.has_pages():
        print(
            f"Knowledge base already has {len(ks.get_pages())} pages — skipping crawl and parse. "
            "Pass --recrawl to force a fresh crawl, or --reparse to re-run the parser on cached HTML."
        )
        return

    if os.path.exists(config.CRAWL_MAP_FILE):
        print(f"Knowledge base is empty but a cached crawl exists at {config.CRAWL_MAP_FILE} — parsing that.")
        run_parse_and_store(config.CRAWL_MAP_FILE)
        return

    print("No cached crawl and empty knowledge base — running a full crawl.")
    site_map_path = run_crawl()
    run_parse_and_store(site_map_path)


if __name__ == "__main__":
    main()