#!/usr/bin/env python3
"""
PDF Scraper & Bulk Downloader — Termux/Android Edition
-------------------------------------------------------
Uses system Chromium (pkg install chromium) via Chrome DevTools Protocol.
No pyppeteer, no playwright needed. Works on Android Termux.

INSTALL:
  pkg install chromium
  pip install requests beautifulsoup4 websocket-client

USAGE:
  python pdf_scraper.py https://selfstudys.com/...
  python pdf_scraper.py https://selfstudys.com/... -o ~/storage/downloads/pdfs
  python pdf_scraper.py https://selfstudys.com/... --no-login
"""

import os
import re
import sys
import json
import time
import argparse
import requests
import subprocess
import threading
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup

try:
    import websocket
except ImportError:
    print("❌ Missing dependency: pip install websocket-client")
    sys.exit(1)


# ── Chrome DevTools Protocol helper ───────────────────────────────────────────

class CDPSession:
    """Minimal Chrome DevTools Protocol client over WebSocket."""

    def __init__(self, ws_url: str):
        self._id = 0
        self._results = {}
        self._lock = threading.Event()
        self.ws = websocket.WebSocketApp(
            ws_url,
            on_message=self._on_message,
            on_error=self._on_error,
        )
        t = threading.Thread(target=self.ws.run_forever, daemon=True)
        t.start()
        time.sleep(1)  # wait for connection

    def _on_message(self, ws, msg):
        data = json.loads(msg)
        if "id" in data:
            self._results[data["id"]] = data
            self._lock.set()

    def _on_error(self, ws, err):
        print(f"WebSocket error: {err}")

    def send(self, method: str, params: dict = None) -> dict:
        self._id += 1
        msg_id = self._id
        payload = {"id": msg_id, "method": method, "params": params or {}}
        self._lock.clear()
        self.ws.send(json.dumps(payload))
        self._lock.wait(timeout=15)
        return self._results.get(msg_id, {})

    def close(self):
        self.ws.close()


# ── Browser login via system Chromium ─────────────────────────────────────────

def find_chromium() -> str:
    """Find the correct chromium binary name on this system."""
    candidates = ["chromium", "chromium-browser", "google-chrome", "chrome"]
    for name in candidates:
        result = subprocess.run(["which", name], capture_output=True, text=True)
        if result.returncode == 0:
            print(f"  ✅ Found browser: {name}")
            return name
    print("❌ Chromium not found! Install it with: pkg install chromium")
    sys.exit(1)


def browser_login_and_get_cookies(target_url: str) -> dict:
    """
    Launch system Chromium with remote debugging,
    wait for user to log in, then steal cookies.
    """
    debug_port = 9222
    chromium_bin = find_chromium()

    chromium_cmd = [
        chromium_bin,
        f"--remote-debugging-port={debug_port}",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        target_url,
    ]

    print("\n" + "═" * 60)
    print("  🌐  BROWSER LOGIN REQUIRED")
    print("═" * 60)
    print("  ➤  Chromium is launching now...")
    print("  ➤  Log in with Google in the browser")
    print("  ➤  Come back here when done")
    print("  ➤  Press Enter to let the script take over")
    print("═" * 60 + "\n")

    # Launch Chromium in background
    proc = subprocess.Popen(
        chromium_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for Chromium to start and open debug port
    print("  ⏳ Waiting for Chromium to start...")
    for _ in range(20):
        time.sleep(1)
        try:
            r = requests.get(f"http://localhost:{debug_port}/json", timeout=2)
            if r.ok:
                break
        except Exception:
            pass
    else:
        print("❌ Chromium didn't start in time. Try running manually:")
        print(f"   chromium-browser --remote-debugging-port={debug_port}")
        proc.kill()
        sys.exit(1)

    print("  ✅ Chromium started!\n")

    # Wait for user to log in
    input("  ✅  Press Enter after you have logged in...\n")
    time.sleep(2)

    # Get list of open tabs
    tabs = requests.get(f"http://localhost:{debug_port}/json").json()
    if not tabs:
        print("❌ No browser tabs found.")
        proc.kill()
        sys.exit(1)

    # Connect to the first/active tab via WebSocket
    ws_url = tabs[0]["webSocketDebuggerUrl"]
    session = CDPSession(ws_url)

    # Get all cookies via CDP
    result = session.send("Network.getAllCookies")
    raw_cookies = result.get("result", {}).get("cookies", [])
    cookies_dict = {c["name"]: c["value"] for c in raw_cookies}

    print(f"  🍪  Captured {len(cookies_dict)} cookie(s) from browser\n")

    session.close()
    proc.terminate()

    return cookies_dict


# ── PDF scraper ────────────────────────────────────────────────────────────────

def get_pdf_links(url: str, session: requests.Session) -> list[dict]:
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


# ── Downloader ─────────────────────────────────────────────────────────────────

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


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Scrape & bulk-download PDFs — works on Termux/Android."
    )
    parser.add_argument("url",            help="Webpage URL to scrape for PDFs")
    parser.add_argument("-o", "--output", default="./downloaded_pdfs",
                        help="Output folder (default: ./downloaded_pdfs)")
    parser.add_argument("--workers",      type=int,   default=5,
                        help="Parallel threads (default: 5)")
    parser.add_argument("--delay",        type=float, default=0.3,
                        help="Delay between requests in seconds (default: 0.3)")
    parser.add_argument("--no-login",     action="store_true",
                        help="Skip browser login — use for public pages")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Login ──────────────────────────────────────────────────────────
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept":          "text/html,application/xhtml+xml,application/pdf,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })

    if not args.no_login:
        cookies = browser_login_and_get_cookies(args.url)
        session.cookies.update(cookies)
        print("  ✅  Session handed over to downloader!\n")
    else:
        print("\n  ⚡  Skipping login (--no-login)\n")

    # ── Step 2: Scrape ─────────────────────────────────────────────────────────
    pdfs = get_pdf_links(args.url, session)
    if not pdfs:
        print("\n⚠️  No PDFs found. Possible reasons:")
        print("  • Login didn't complete — try again")
        print("  • PDFs load dynamically via JavaScript")
        print("  • Page has no PDF links\n")
        return

    # ── Step 3: Preview ────────────────────────────────────────────────────────
    print(f"\n{'#':<5} {'Filename':<50} URL")
    print("─" * 110)
    for i, p in enumerate(pdfs, 1):
        print(f"  {i:<3} {p['filename'][:48]:<50} {p['url']}")

    print(f"\n📥 Downloading {len(pdfs)} PDF(s) → {output_dir.resolve()}")
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
