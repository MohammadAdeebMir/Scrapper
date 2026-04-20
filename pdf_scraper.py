#!/usr/bin/env python3
"""
PDF Scraper & Bulk Downloader — with Remote Browser Login
----------------------------------------------------------
Supports Google OAuth / SSO login via Playwright.

HOW IT WORKS:
  1. Run this script in Termux (or any terminal)
  2. Script launches a visible Chrome window automatically
  3. You log in with Google manually in that browser window
  4. Press Enter in the terminal when done
  5. Script grabs your session cookies and downloads all PDFs

INSTALL (Termux):
  pkg install python chromium
  pip install playwright beautifulsoup4 requests
  playwright install chromium

INSTALL (PC / Linux):
  pip install playwright beautifulsoup4 requests
  playwright install chromium

USAGE:
  python pdf_scraper.py https://selfstudys.com/advance-pc/...
  python pdf_scraper.py <url> -o ./my_pdfs --workers 5
  python pdf_scraper.py <url> --no-login         # for public pages
"""

import os
import re
import time
import argparse
import requests
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup


# ── PDF link scraper ───────────────────────────────────────────────────────────

def get_pdf_links(url: str, session: requests.Session) -> list[dict]:
    """Scrape all PDF links from a webpage using authenticated session."""
    print(f"\n🔍 Scanning page: {url}")
    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"❌ Failed to load page: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    found = []
    seen = set()

    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        full_url = urljoin(url, href)

        if full_url in seen:
            continue
        if full_url.lower().endswith(".pdf") or "pdf" in full_url.lower():
            seen.add(full_url)
            path = unquote(urlparse(full_url).path)
            filename = os.path.basename(path) or f"document_{len(found)+1}.pdf"
            if not filename.lower().endswith(".pdf"):
                filename += ".pdf"
            found.append({
                "url": full_url,
                "filename": filename,
                "title": tag.get_text(strip=True) or filename,
            })

    print(f"✅ Found {len(found)} PDF link(s)")
    return found


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = name.strip(". ")
    return name[:200] or "document.pdf"


def download_pdf(pdf: dict, output_dir: Path, session: requests.Session, delay: float) -> str:
    url      = pdf["url"]
    filename = sanitize_filename(pdf["filename"])
    dest     = output_dir / filename

    counter = 1
    while dest.exists():
        stem = Path(filename).stem
        dest = output_dir / f"{stem}_{counter}.pdf"
        counter += 1

    try:
        if delay > 0:
            time.sleep(delay)
        resp = session.get(url, timeout=30, stream=True)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")
        if "html" in content_type and "pdf" not in content_type:
            return f"⚠️  Skipped (not a PDF): {url}"

        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        size_kb = dest.stat().st_size / 1024
        return f"✅ {dest.name}  ({size_kb:.1f} KB)"

    except requests.RequestException as e:
        return f"❌ Failed [{filename}]: {e}"


# ── Browser login via Playwright ───────────────────────────────────────────────

def browser_login_and_get_cookies(target_url: str) -> dict:
    """
    Opens a real visible Chrome window, waits for you to log in,
    then steals the session cookies for use in requests.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("\n❌ Playwright not installed.")
        print("   Run: pip install playwright && playwright install chromium\n")
        raise SystemExit(1)

    print("\n" + "═" * 60)
    print("  🌐  BROWSER LOGIN REQUIRED")
    print("═" * 60)
    print("  ➤  A Chrome window is opening now...")
    print("  ➤  Log in with Google in that window")
    print("  ➤  Once logged in, come back here")
    print("  ➤  Press Enter to let the script take over")
    print("═" * 60 + "\n")

    cookies_dict = {}

    with sync_playwright() as p:
        # Launch a VISIBLE (non-headless) browser
        browser = p.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--start-maximized",
            ]
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        print(f"  Opening → {target_url}\n")
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass  # Page may redirect to login — that's fine

        # Pause — let user log in manually
        input("  ✅  Press Enter after you have logged in successfully...\n")

        # Brief pause for session to settle
        time.sleep(2)

        # Grab all cookies from browser
        raw_cookies = context.cookies()
        for c in raw_cookies:
            cookies_dict[c["name"]] = c["value"]

        print(f"  🍪  Captured {len(cookies_dict)} cookie(s) from browser")
        browser.close()

    return cookies_dict


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Scrape and bulk-download PDFs — supports Google OAuth login."
    )
    parser.add_argument("url", help="URL of the webpage to scrape for PDFs")
    parser.add_argument(
        "-o", "--output",
        default="./downloaded_pdfs",
        help="Output folder (default: ./downloaded_pdfs)",
    )
    parser.add_argument(
        "--workers", type=int, default=5,
        help="Parallel download threads (default: 5)",
    )
    parser.add_argument(
        "--delay", type=float, default=0.3,
        help="Seconds between requests (default: 0.3)",
    )
    parser.add_argument(
        "--no-login", action="store_true",
        help="Skip browser login — use for public pages",
    )
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Browser login ──────────────────────────────────────────────────
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })

    if not args.no_login:
        cookies = browser_login_and_get_cookies(args.url)
        session.cookies.update(cookies)
        print("\n  ✅  Session handed over to downloader!\n")
    else:
        print("\n  ⚡  Skipping login (--no-login)\n")

    # ── Step 2: Scrape PDF links ───────────────────────────────────────────────
    pdfs = get_pdf_links(args.url, session)
    if not pdfs:
        print("\n⚠️  No PDFs found.")
        print("  Possible reasons:")
        print("  • Login didn't complete — try again")
        print("  • PDFs are loaded via JavaScript (try --scroll flag in a future version)")
        print("  • Page has no PDF links\n")
        return

    # ── Step 3: Preview ────────────────────────────────────────────────────────
    print(f"\n{'#':<4} {'Filename':<50} {'URL'}")
    print("─" * 110)
    for i, p in enumerate(pdfs, 1):
        print(f"  {i:<3} {p['filename'][:48]:<50} {p['url']}")

    print(f"\n📥 Starting download of {len(pdfs)} PDF(s) → {output_dir.resolve()}")
    print(f"   Threads : {args.workers}")
    print(f"   Delay   : {args.delay}s\n")

    # ── Step 4: Bulk download ──────────────────────────────────────────────────
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(download_pdf, pdf, output_dir, session, args.delay): pdf
            for pdf in pdfs
        }
        for i, future in enumerate(as_completed(futures), 1):
            msg = future.result()
            print(f"  [{i:>3}/{len(pdfs)}] {msg}")
            results.append(msg)

    # ── Step 5: Summary ────────────────────────────────────────────────────────
    success = sum(1 for r in results if r.startswith("✅"))
    skipped = sum(1 for r in results if r.startswith("⚠️"))
    failed  = sum(1 for r in results if r.startswith("❌"))

    print(f"\n{'═' * 55}")
    print(f"  📊  {success} downloaded  |  {skipped} skipped  |  {failed} failed")
    print(f"  📁  Saved to: {output_dir.resolve()}")
    print(f"{'═' * 55}\n")


if __name__ == "__main__":
    main()
