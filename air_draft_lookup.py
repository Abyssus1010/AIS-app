"""
Exploratory script: find the "air draft" (height of a vessel above the
waterline, laden) for a given ship by web-searching for it rather than
querying a fixed list of AIS-aggregator sites.

Air draft is not published by the big consumer AIS sites (VesselFinder,
MarineTraffic, etc. - confirmed by hand for a couple of test vessels), but
it does show up in places like:
  - shipowner/operator "ship particulars" PDFs (e.g. sal.global fleet sheets)
  - documents mirrored on scribd.com
  - charterer/broker vessel spec sheets
  - port/canal authority pre-arrival or transit documents

So instead of a hardcoded source list, this script:
  1. Runs a handful of web searches for "<vessel name> ... air draft / ship
     particulars" (DuckDuckGo HTML endpoint - no API key needed).
  2. Fetches whatever comes back, PDF or HTML alike.
  3. Scans the extracted text for air-draft-related lines.

The vessel name is a CLI argument, not fixed in code, and there is no
guarantee a given vessel's data is publicly indexed at all - this is a
best-effort probe, and it reports "no match" honestly when it finds nothing.

Only a genuine air draft field counts as a match - plain draft/draught
(depth below the waterline) is NOT accepted as a fallback, even though it's
far more commonly published. The two measurements are only loosely
correlated (see AIR_DRAFT_KEYWORDS below for why), and for a height-clearance
use case, a wrong "close enough" answer is worse than an honest "unknown".

Many operator "ship particulars" PDFs are flattened/scanned images with no
selectable text at all (pypdf then extracts 0 characters), so PDFs that come
back empty are re-processed with OCR (PyMuPDF renders each page to an image,
pytesseract reads it).

Requires (conda env "ais"): requests, beautifulsoup4, pypdf, playwright,
pymupdf, pytesseract, and the Tesseract OCR engine + English trained data.
The Windows installer needs admin/UAC, so it's simpler to pull the engine
from conda-forge straight into the env (no elevation needed) and grab the
language file by hand:
    conda install -n ais -c conda-forge tesseract -y
    curl -L -o <env>\\Library\\share\\tessdata\\eng.traineddata ^
        https://github.com/tesseract-ocr/tessdata_fast/raw/main/eng.traineddata
    playwright install chromium   # first time only
"""

import os
import re
import shutil
import sys
from datetime import datetime
from io import BytesIO
from urllib.parse import urlparse

import pymupdf
import pytesseract
import requests
from bs4 import BeautifulSoup
from PIL import Image
from pypdf import PdfReader

_conda_library = os.path.join(os.path.dirname(sys.executable), "Library")
_conda_tesseract_exe = os.path.join(_conda_library, "bin", "tesseract.exe")
_conda_tessdata = os.path.join(_conda_library, "share", "tessdata")

if not shutil.which("tesseract") and os.path.exists(_conda_tesseract_exe):
    pytesseract.pytesseract.tesseract_cmd = _conda_tesseract_exe

# Conda-forge's tesseract build doesn't bake in a default tessdata path, so
# this needs setting regardless of whether tesseract.exe itself was found via
# PATH (e.g. after `conda activate ais` puts Library\bin on PATH) or via the
# explicit override above - shutil.which succeeding does NOT mean tessdata is
# findable too.
if "TESSDATA_PREFIX" not in os.environ:
    for candidate in (r"C:\Program Files\Tesseract-OCR\tessdata", _conda_tessdata):
        if os.path.isdir(candidate):
            os.environ["TESSDATA_PREFIX"] = candidate
            break

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)

# Plain draft/draught (depth below the waterline) is deliberately NOT treated
# as a fallback for air draft (height above the waterline) here. The two are
# only loosely correlated - a lightly laden container ship can sit high in
# the water (shallow draft) while still being very tall (high air draft) -
# so accepting draft as a stand-in risks exactly the dangerous case for a
# height-clearance check: a tall ship reading as "fine" because its draft
# looked shallow. Only genuine air draft / air draught (and equivalent
# phrasings: keel-to-mast, overhead/vertical clearance) count as a match.
AIR_DRAFT_KEYWORDS = [
    "air draft",
    "air draught",
    "airdraft",
    "airdraught",
    "height above waterline",
    "height above w/l",
    "keel to mast",
    "keel to funnel",
    "keel to truck",
    "overhead clearance",
    "vertical clearance",
]

VALUE_PATTERN = re.compile(r"(\d[\d,]*\.?\d*)\s*(m|meters|metres|ft|feet)\b", re.IGNORECASE)
STANDALONE_VALUE_PATTERN = re.compile(r"^[\d,]+\.?\d*\s*(m|meters|metres|ft|feet)$", re.IGNORECASE)

SEARCH_QUERIES = [
    "{name} air draft",
    "{name} air draught",
    "{name} ship pdf",
]

MAX_RESULTS_PER_QUERY = 5
MAX_PAGES_TO_FETCH = 12


def duckduckgo_search(query: str, max_results: int = MAX_RESULTS_PER_QUERY):
    resp = requests.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    results = []
    for a in soup.select("a.result__a"):
        href = a.get("href")
        title = a.get_text(strip=True)
        if href:
            results.append((title, href))
        if len(results) >= max_results:
            break
    return results


def fetch_pdf_text(content: bytes) -> str:
    reader = PdfReader(BytesIO(content))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if len(text.strip()) < 30:
        text = ocr_pdf_text(content)
    return text


def ocr_pdf_text(content: bytes, zoom: float = 3.0) -> str:
    doc = pymupdf.open(stream=content, filetype="pdf")
    matrix = pymupdf.Matrix(zoom, zoom)
    all_lines = []
    for page in doc:
        pixmap = page.get_pixmap(matrix=matrix)
        image = Image.open(BytesIO(pixmap.tobytes("png")))
        all_lines.extend(ocr_image_to_rows(image))
    doc.close()
    return "\n".join(all_lines)


def ocr_image_to_rows(image) -> list:
    # Ship particulars sheets are almost always "label ... value" laid out in
    # a table. Tesseract's plain image_to_string reads block-by-block (every
    # label column top-to-bottom, then every value column top-to-bottom),
    # which silently scrambles which value belongs to which label. Grouping
    # words by their actual y-position on the page and re-sorting each group
    # left-to-right reconstructs the original rows instead.
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)

    words = []
    for i in range(len(data["text"])):
        text = data["text"][i].strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1
        if text and conf > 0:
            words.append(
                {
                    "text": text,
                    "left": data["left"][i],
                    "top": data["top"][i],
                    "height": data["height"][i],
                }
            )

    if not words:
        return []

    words.sort(key=lambda w: w["top"] + w["height"] / 2)
    heights = sorted(w["height"] for w in words)
    tolerance = max(heights[len(heights) // 2] * 0.6, 5)

    rows = []
    current_row = []
    row_center = None
    for w in words:
        center = w["top"] + w["height"] / 2
        if row_center is None or abs(center - row_center) <= tolerance:
            current_row.append(w)
            row_center = sum(x["top"] + x["height"] / 2 for x in current_row) / len(current_row)
        else:
            rows.append(current_row)
            current_row = [w]
            row_center = center
    if current_row:
        rows.append(current_row)

    return [" ".join(w["text"] for w in sorted(row, key=lambda w: w["left"])) for row in rows]


def fetch_html_text_rendered(url: str) -> str:
    # Fallback for JS-rendered pages: fresh browser per call so one crashed/
    # blocked page doesn't take the whole run down with it.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(2000)
            return page.locator("body").inner_text()
        finally:
            browser.close()


def looks_like_js_shell(text: str) -> bool:
    return len(text) < 400 and "loading" in text.lower()


def find_value_near(lines, i: int, same_line_start: int):
    # Same-line case: "DRAFT (TROPICAL) 10.644M" (typical of OCR-reconstructed
    # table rows). Cross-line case: "Current draught" / "11.3 m" on separate
    # lines (typical of scraped web text where each DOM node is its own line).
    if same_line_start is not None:
        window = lines[i][same_line_start: same_line_start + 40]
        m = VALUE_PATTERN.search(window)
        if m:
            return f"{m.group(1)} {m.group(2)}"

    for j in range(i + 1, min(i + 3, len(lines))):
        candidate = lines[j].strip()
        if not candidate:
            continue
        if STANDALONE_VALUE_PATTERN.match(candidate):
            return candidate
        m = VALUE_PATTERN.search(candidate)
        if m and len(candidate) < 20:
            return f"{m.group(1)} {m.group(2)}"
        break  # first non-empty line didn't look like a bare value; give up
    return None


def find_matches(text: str):
    lines = [line.rstrip() for line in text.splitlines()]
    hits = []
    for i, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped:
            continue
        low = stripped.lower()

        end_idx = None
        for keyword in AIR_DRAFT_KEYWORDS:
            idx = low.find(keyword)
            if idx != -1:
                end_idx = idx + len(keyword)
                break

        if end_idx is None:
            continue

        value = find_value_near(lines, i, end_idx)
        context = " | ".join(c.strip() for c in lines[max(0, i - 1): i + 3] if c.strip())
        hits.append({"line": stripped, "value": value, "context": context})
    return hits


def safe_filename(url: str) -> str:
    parsed = urlparse(url)
    name = (parsed.netloc + parsed.path).strip("/").replace("/", "_")
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:120] or "page"


def main():
    vessel_name = " ".join(sys.argv[1:]) or "PAC ALNATH"

    log_lines = []

    def log(msg: str = ""):
        print(msg)
        log_lines.append(msg)

    log(f"Searching the web for air draft of: {vessel_name}\n")

    seen_urls = set()
    candidates = []
    for template in SEARCH_QUERIES:
        query = template.format(name=vessel_name)
        log(f"[search] {query}")
        try:
            results = duckduckgo_search(query)
        except Exception as exc:
            log(f"  search failed: {exc}")
            continue
        for title, url in results:
            if url not in seen_urls:
                seen_urls.add(url)
                candidates.append((title, url))
        log(f"  -> {len(results)} result(s)")

    log(f"\n{len(candidates)} unique page(s) to check (capping at {MAX_PAGES_TO_FETCH})\n")

    summary = []
    for title, url in candidates[:MAX_PAGES_TO_FETCH]:
        log(f"--- {title} ---")
        log(url)

        text = None
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            resp.raise_for_status()
            # The URL's .pdf suffix is not reliable: query strings (tracking
            # params etc.) can hide it, and some "download" links redirect to
            # an HTML page instead of serving the file. Sniff the actual
            # response instead - Content-Type header first, then the PDF
            # magic bytes as a fallback for servers that mislabel it.
            content_type = resp.headers.get("content-type", "").lower()
            is_pdf = "pdf" in content_type or resp.content[:5] == b"%PDF-"
            if is_pdf:
                text = fetch_pdf_text(resp.content)
            else:
                text = BeautifulSoup(resp.text, "html.parser").get_text("\n", strip=True)
                if looks_like_js_shell(text):
                    text = fetch_html_text_rendered(url)
        except Exception as exc:
            log(f"  FAILED: {exc}\n")
            summary.append((title, url, None))
            continue

        hits = find_matches(text)
        if hits:
            for hit in hits:
                value = hit["value"] or "(value not parsed - see context)"
                log(f"  AIR DRAFT: {value}")
                log(f"    {hit['context']}")
        else:
            log("  No air draft field found.")
        summary.append((title, url, hits))
        log()

    log("=== Summary ===")
    for title, url, hits in summary:
        if hits is None:
            status = "failed to fetch"
        elif not hits:
            status = "no air draft field found"
        else:
            value = hits[0]["value"] or "value not parsed"
            status = f"AIR DRAFT: {value}"
        log(f"{status:30} {url}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_path = f"air_draft_summary_{safe_filename(vessel_name)}_{timestamp}.txt"
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines))
    print(f"\nSummary written to {summary_path}")


if __name__ == "__main__":
    main()
