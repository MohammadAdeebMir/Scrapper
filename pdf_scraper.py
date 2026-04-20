#!/usr/bin/env python3
"""
PDF Scraper & Bulk Downloader
------------------------------
Scrapes all PDF links from a webpage and downloads them in bulk.

Usage:
    python pdf_scraper.py <url> [options]

Examples:
    python pdf_scraper.py https://example.com/resources
    python pdf_scraper.py https://example.com/resources -o ./my_pdfs
    python pdf_scraper.py https://example.com/resources -o ./my_pdfs --workers 10 --delay 0.5
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


# ── Helpers ────────────────────────────────────────────────────────────────────

def get_pdf_links(url: str, session: requests.Session) -> list[dict]:
    """Scrape all PDF links from a webpage."""
    print(f"\n🔍 Scanning: {url}")
    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"❌ Failed to load page: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    base_url = url

    found = []
    seen = set()

    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()

        # Resolve relative URLs
        full_url = urljoin(base_url, href)

        # Only keep .pdf links (check URL or Content-Type hint)
        if full_url in seen:
            continue
        if full_url.lower().endswith(".pdf") or "pdf" in full_url.lower():
            seen.add(full_url)
            # Try to get a clean filename from URL
            path = unquote(urlparse(full_url).path)
            filename = os.path.basename(path) or f"document_{len(found)+1}.pdf"
            if not filename.lower().endswith(".pdf"):
                filename += ".pdf"

            # Use link text as title hint if available
            link_text = tag.get_text(strip=True)

            found.append({
                "url": full_url,
                "filename": filename,
                "title": link_text or filename,
            })

    print(f"✅ Found {len(found)} PDF link(s)\n")
    return found


def sanitize_filename(name: str) -> str:
    """Remove characters that are unsafe in filenames."""
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = name.strip(". ")
    return name[:200] or "document.pdf"


def download_pdf(pdf: dict, output_dir: Path, session: requests.Session, delay: float) -> str:
    """Download a single PDF. Returns status message."""
    url = pdf["url"]
    filename = sanitize_filename(pdf["filename"])
    dest = output_dir / filename

    # Avoid overwriting — append a counter if needed
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

        # Confirm it's actually a PDF
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
        description="Scrape and bulk-download all PDFs from a webpage."
    )
    parser.add_argument("url", help="URL of the webpage to scrape")
    parser.add_argument(
        "-o", "--output",
        default="./downloaded_pdfs",
        help="Output folder (default: ./downloaded_pdfs)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=5,
        help="Parallel download threads (default: 5)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.3,
        help="Seconds to wait between requests (default: 0.3)",
    )
    parser.add_argument(
        "--user-agent",
        default="Mozilla/5.0 (compatible; PDFScraper/1.0)",
        help="Custom User-Agent header",
    )
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Shared session with headers
    session = requests.Session()
    session.headers.update({
        "User-Agent": args.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
    })

    # Step 1 — scrape
    pdfs = get_pdf_links(args.url, session)
    if not pdfs:
        print("No PDFs found. Check the URL or try a different page.")
        return

    # Step 2 — preview
    print(f"{'#':<4} {'Filename':<50} {'URL'}")
    print("-" * 100)
    for i, p in enumerate(pdfs, 1):
        print(f"{i:<4} {p['filename'][:48]:<50} {p['url']}")

    print(f"\n📥 Downloading {len(pdfs)} PDF(s) → {output_dir.resolve()}")
    print(f"   Threads: {args.workers} | Delay: {args.delay}s\n")

    # Step 3 — bulk download
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(download_pdf, pdf, output_dir, session, args.delay): pdf
            for pdf in pdfs
        }
        for i, future in enumerate(as_completed(futures), 1):
            msg = future.result()
            print(f"[{i}/{len(pdfs)}] {msg}")
            results.append(msg)

    # Step 4 — summary
    success = sum(1 for r in results if r.startswith("✅"))
    skipped = sum(1 for r in results if r.startswith("⚠️"))
    failed  = sum(1 for r in results if r.startswith("❌"))

    print(f"\n{'─'*50}")
    print(f"📊 Summary: {success} downloaded | {skipped} skipped | {failed} failed")
    print(f"📁 Saved to: {output_dir.resolve()}")


if __name__ == "__main__":
    main()
