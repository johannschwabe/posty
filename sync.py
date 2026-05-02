#!/usr/bin/env python3
"""
ePost → paperless-ngx sync

Two modes:
  python sync.py login   — opens browser for manual login + 2FA, saves session
  python sync.py sync    — headless browser scrape + REST download (no login needed)
"""

import json
import os
import re
import shutil
import sys
import urllib.parse
from pathlib import Path

from dotenv import load_dotenv
import httpx
from playwright.sync_api import sync_playwright

load_dotenv(Path(__file__).parent / ".env")

EPOST_URL = "https://app.epost.ch"
PAPERLESS_URL = os.environ.get("PAPERLESS_URL", "http://localhost:8000")
PAPERLESS_TOKEN = os.environ.get("PAPERLESS_TOKEN", "")

_data_dir = Path(os.environ.get("DATA_DIR", Path(__file__).parent))
SESSION_FILE = _data_dir / "session.json"
STATE_FILE = _data_dir / "synced_letters.json"

def _launch_opts() -> dict:
    """Use system Chromium if found. Adds --no-sandbox when running inside Docker."""
    opts = {}
    for name in ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome"):
        if path := shutil.which(name):
            opts["executable_path"] = path
            break
    in_docker = Path("/.dockerenv").exists()
    if in_docker:
        opts["args"] = ["--no-sandbox", "--disable-dev-shm-usage"]
    if in_docker and "executable_path" not in opts:
        raise RuntimeError("No Chromium found in container. Install chromium via apt.")
    print(f"Chromium: {opts.get('executable_path', 'playwright-bundled')} args={opts.get('args', [])}")
    return opts

# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()

def save_state(synced: set):
    STATE_FILE.write_text(json.dumps(sorted(synced), indent=2))

# ── Login (run locally once) ───────────────────────────────────────────────────

def cmd_login():
    """Open a real browser, let the user log in with 2FA, save session."""
    print("Opening browser. Complete the SwissID login + 2FA, then wait.")
    print("The browser will close automatically once you reach the ePost dashboard.\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, **_launch_opts())
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(EPOST_URL)

        # Wait up to 5 minutes for user to complete login
        page.wait_for_url("**/app.epost.ch/**ch.klara.**", timeout=300_000)
        print("Login detected. Saving session...")
        ctx.storage_state(path=str(SESSION_FILE))
        browser.close()

    print(f"Session saved to {SESSION_FILE}")
    print("Copy this file to your server and run: python sync.py sync")

# ── Browser-based scraping (headless, reuses saved session) ───────────────────

def get_letters(page) -> list[dict]:
    """Extract all letter IDs and metadata from the letterbox DOM."""
    return page.evaluate("""() => {
        const pattern = /letter-([0-9a-f]{24})\\b/;
        const seen = new Set();
        const results = [];
        for (const card of document.querySelectorAll('[class*="letter-"][class*="ui-outputpanel"]')) {
            const m = card.className.match(pattern);
            if (!m || seen.has(m[1])) continue;
            seen.add(m[1]);
            const t = sel => { const el = card.querySelector(sel); return el ? el.textContent.trim() : ''; };
            results.push({
                id: m[1],
                title:  t('.letter-title, .letter-content__title'),
                date:   t('.letter-content__date, [class*="creation-date"]'),
                sender: t('[class*="sender"]'),
            });
        }
        return results;
    }""")

def navigate_to_letterbox(page):
    """Click 'Digitaler Briefkasten' from the dashboard."""
    page.evaluate("""() => {
        const link = Array.from(document.querySelectorAll('a'))
            .find(e => e.textContent.includes('Digitaler Briefkasten'));
        if (link) link.click();
        else throw new Error('Digitaler Briefkasten link not found');
    }""")
    page.wait_for_url("**/DigitalLetterboxOverview**", timeout=15_000)
    page.wait_for_timeout(1500)

# ── Download + upload ─────────────────────────────────────────────────────────

def download_letter(page, letter_id: str) -> tuple[bytes, str]:
    """Download via the REST API using the browser's session cookies."""
    url = f"{EPOST_URL}/luz/api/epost-storage/downloads/letters/{letter_id}"
    resp = page.request.get(url)
    if resp.status == 401:
        raise RuntimeError("Session expired — run: python sync.py login")
    if resp.status != 200:
        raise RuntimeError(f"Download failed: HTTP {resp.status}")

    disposition = resp.headers.get("content-disposition", "")
    filename = f"{letter_id}.pdf"
    if m := re.search(r"filename\*=UTF-8''(.+)", disposition):
        filename = urllib.parse.unquote(m.group(1))
    elif m := re.search(r'filename="?([^";\r\n]+)"?', disposition):
        filename = m.group(1).strip()

    return resp.body(), filename

def get_or_create_tag(client: httpx.Client, name: str) -> int:
    """Return the paperless-ngx tag ID, creating it if needed."""
    resp = client.get("/api/tags/", params={"name": name})
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if results:
        return results[0]["id"]
    resp = client.post("/api/tags/", json={"name": name})
    resp.raise_for_status()
    return resp.json()["id"]

def get_or_create_correspondent(client: httpx.Client, name: str) -> int:
    """Return the paperless-ngx correspondent ID, creating it if needed."""
    resp = client.get("/api/correspondents/", params={"name": name})
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if results:
        return results[0]["id"]
    resp = client.post("/api/correspondents/", json={"name": name})
    resp.raise_for_status()
    return resp.json()["id"]

def upload_to_paperless(pdf_bytes: bytes, filename: str, letter: dict):
    if not PAPERLESS_TOKEN:
        raise ValueError("Set PAPERLESS_TOKEN in .env")

    headers = {"Authorization": f"Token {PAPERLESS_TOKEN}"}
    with httpx.Client(base_url=PAPERLESS_URL, headers=headers, timeout=60) as client:
        correspondent_id = get_or_create_correspondent(client, "Swiss Post / ePost")
        tag_id = get_or_create_tag(client, "mail")

        data = {"correspondent": str(correspondent_id), "tags": str(tag_id)}
        if letter.get("title"):
            data["title"] = letter["title"]
        if letter.get("date"):
            parts = letter["date"].split(".")
            if len(parts) == 3:
                data["created"] = f"{parts[2]}-{parts[1]}-{parts[0]}"

        resp = client.post(
            "/api/documents/post_document/",
            files={"document": (filename, pdf_bytes, "application/pdf")},
            data=data,
        )
    if resp.status_code not in (200, 201, 202):
        raise RuntimeError(f"paperless upload failed: {resp.status_code} {resp.text[:200]}")
    print(f"    → paperless-ngx task: {resp.text.strip()}")

# ── Sync ──────────────────────────────────────────────────────────────────────

def run_sync() -> tuple[list[dict], list[str]]:
    """Core sync. Returns (new_letters, errors). Raises on fatal session errors."""
    if not SESSION_FILE.exists():
        raise FileNotFoundError("No session found. Run first: python sync.py login")

    synced = load_state()
    new_letters: list[dict] = []
    errors: list[str] = []

    opts = _launch_opts()
    print(f"Launch opts: {opts}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, **opts)
        ctx = browser.new_context(storage_state=str(SESSION_FILE))
        page = ctx.new_page()

        print("Loading dashboard...")
        page.goto(EPOST_URL)
        print(f"URL after goto: {page.url}")

        if "login" in page.url or "swissid" in page.url:
            print(f"Page title: {page.title()}")
            browser.close()
            raise RuntimeError("Session expired — run: python sync.py login")

        print("Navigating to letterbox...")
        navigate_to_letterbox(page)
        ctx.storage_state(path=str(SESSION_FILE))  # persist any token renewals

        letters = get_letters(page)
        print(f"Found {len(letters)} letter(s).")

        for letter in letters:
            lid = letter["id"]
            if lid in synced:
                print(f"  Skip {lid[:8]}… (already synced)")
                continue

            label = letter.get("title") or lid
            print(f"  Syncing: {label[:60]}")
            try:
                pdf_bytes, filename = download_letter(page, lid)
                print(f"    Downloaded: {filename} ({len(pdf_bytes):,} bytes)")

                if PAPERLESS_TOKEN:
                    upload_to_paperless(pdf_bytes, filename, letter)
                else:
                    out = _data_dir / "downloads" / filename
                    out.parent.mkdir(exist_ok=True)
                    out.write_bytes(pdf_bytes)
                    print(f"    Saved locally: {out}")

                synced.add(lid)
                save_state(synced)
                new_letters.append(letter)

            except Exception as e:
                print(f"    ERROR: {e}", file=sys.stderr)
                errors.append(f"{label[:60]}: {e}")

        browser.close()

    return new_letters, errors


def cmd_sync():
    new_letters, errors = run_sync()
    print(f"\nDone. {len(new_letters)} new letter(s) synced.")

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if cmd == "login":
        cmd_login()
    elif cmd == "sync":
        cmd_sync()
    else:
        print(__doc__)
        sys.exit(1)
