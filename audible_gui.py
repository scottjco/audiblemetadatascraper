import sys
import os
import shutil
import re
import random
import json
import time
import io
import subprocess
import threading
import traceback
import configparser
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SETTINGS_PATH = os.path.join(BASE_DIR, "settings.ini")


def load_settings():
    config = configparser.ConfigParser()
    if os.path.exists(SETTINGS_PATH):
        try:
            config.read(SETTINGS_PATH)
        except (configparser.Error, OSError):
            pass
    if "Settings" not in config:
        config["Settings"] = {}
    return config


def save_settings(config):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            config.write(f)
    except OSError:
        pass

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

INITIAL_RETRY_DELAY = 0.1
RETRY_DELAY_INCREMENT = 0.05
MAX_RETRY_DELAY = 1.0
MAX_RETRY_SECONDS = 60

STOP_EVENT = threading.Event()


class StoppedByUser(Exception):
    pass


def _sanitize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _with_retries(fn, label="request", initial_delay=None, increment=None, max_delay=None, max_retry_seconds=None, max_attempts=None):
    initial_delay = INITIAL_RETRY_DELAY if initial_delay is None else initial_delay
    increment = RETRY_DELAY_INCREMENT if increment is None else increment
    max_delay = MAX_RETRY_DELAY if max_delay is None else max_delay
    max_retry_seconds = MAX_RETRY_SECONDS if max_retry_seconds is None else max_retry_seconds

    start = time.monotonic()
    attempt = 0
    while True:
        if STOP_EVENT.is_set():
            raise StoppedByUser()
        try:
            return fn()
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                raise
            if STOP_EVENT.is_set():
                raise StoppedByUser()
            if max_attempts is not None and attempt + 1 >= max_attempts:
                raise
            if time.monotonic() - start >= max_retry_seconds:
                raise
            delay = min(initial_delay + increment * attempt, max_delay)
            delay += random.uniform(0, delay * 0.25)  # jitter, avoids synced retry storms
            print(f"{label} failed ({exc}), retrying in {int(delay * 1000)}ms... (attempt {attempt + 2})")
            if STOP_EVENT.wait(delay):
                raise StoppedByUser()
            attempt += 1
        except Exception as exc:
            if STOP_EVENT.is_set():
                raise StoppedByUser()
            if max_attempts is not None and attempt + 1 >= max_attempts:
                raise
            if time.monotonic() - start >= max_retry_seconds:
                raise
            delay = min(initial_delay + increment * attempt, max_delay)
            delay += random.uniform(0, delay * 0.25)
            print(f"{label} failed ({exc}), retrying in {int(delay * 1000)}ms... (attempt {attempt + 2})")
            if STOP_EVENT.wait(delay):
                raise StoppedByUser()
            attempt += 1


# Page fetch (product page GET) is less sensitive than the internal
# licenserequest API, but still gets rate-limited if hit too fast across
# a batch of books. Slower than the old default, faster than chapters.
PAGE_INITIAL_RETRY_DELAY = 1.5
PAGE_RETRY_DELAY_INCREMENT = 2.0
PAGE_MAX_RETRY_DELAY = 15.0
PAGE_MAX_RETRY_SECONDS = 120

# The chapters/licenserequest endpoint is an internal API, not meant for
# this kind of traffic, and reacts to bursty retries by rate-limiting
# harder. Back off much slower and longer than the normal page/image
# retry policy: start at 3s, add 4s per attempt, cap at 30s, give it up
# to 3 minutes total before giving up on that title's chapters.
CHAPTER_INITIAL_RETRY_DELAY = 3.0
CHAPTER_RETRY_DELAY_INCREMENT = 4.0
CHAPTER_MAX_RETRY_DELAY = 30.0
CHAPTER_MAX_RETRY_SECONDS = 180


IMAGE_MODIFIER_RE = re.compile(r"(\._[A-Z]{2}\d+_)+(?=\.\w+$)")


def fetch_audible_data(url: str) -> dict:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    data = {
        "title": "",
        "authors": "",
        "narrators": "",
        "length": "",
        "release_date": "",
        "language": "",
        "series": "",
        "genres": "",
        "summary": "",
        "cover_url": "",
        "_raw_description_html": "",
    }

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict):
                continue
            type_val = item.get("@type", "")
            type_str = " ".join(type_val) if isinstance(type_val, list) else str(type_val)
            if "BreadcrumbList" in type_str:
                for li in item.get("itemListElement", []):
                    name = (li.get("item") or {}).get("name")
                    if name and name.strip().lower() != "home":
                        data.setdefault("_genre_list", []).append(name.strip())
                continue
            if any(
                t in type_str
                for t in ("Organization", "WebSite", "SiteNavigationElement")
            ):
                continue
            if not data["title"] and item.get("name"):
                data["title"] = item["name"].strip()
            if not data["_raw_description_html"] and item.get("description"):
                data["_raw_description_html"] = item["description"]
            author = item.get("author")
            if author and not data["authors"]:
                data["authors"] = _names_from_ld(author)
            reader = item.get("readBy") or item.get("narrator")
            if reader and not data["narrators"]:
                data["narrators"] = _names_from_ld(reader)
            if not data["release_date"] and item.get("datePublished"):
                data["release_date"] = item["datePublished"].strip()
            if not data["language"] and item.get("inLanguage"):
                data["language"] = item["inLanguage"].strip()
            duration = item.get("duration")
            if duration and not data["length"]:
                data["length"] = _iso_duration_to_readable(duration)
            image = item.get("image")
            if image and not data["cover_url"]:
                data["cover_url"] = image if isinstance(image, str) else ""

    genre_list = data.pop("_genre_list", [])
    for chip in soup.find_all("adbl-chip"):
        text = chip.get_text(strip=True)
        if text:
            genre_list.append(text)
    m = re.search(r'"categories"\s*:\s*(\[[^\]]*\])', resp.text)
    if m:
        try:
            for cat in json.loads(m.group(1)):
                if isinstance(cat, dict) and cat.get("name"):
                    genre_list.append(cat["name"].strip())
        except (json.JSONDecodeError, TypeError):
            pass
    data["genres"] = ", ".join(_dedupe(genre_list))

    if not data["title"]:
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            data["title"] = og_title["content"].strip()
    if not data["title"]:
        h1 = soup.find("h1")
        if h1:
            data["title"] = h1.get_text(strip=True)

    if not data["authors"]:
        author_links = soup.select('a[href*="/author/"]') or soup.select(".authorLabel a")
        names = (_clean_contributor_name(a.get_text(strip=True)) for a in author_links)
        data["authors"] = ", ".join(_dedupe(names))

    if not data["narrators"]:
        narrator_links = soup.select('a[href*="searchNarrator="]') or soup.select(".narratorLabel a")
        names = (_clean_contributor_name(a.get_text(strip=True)) for a in narrator_links)
        data["narrators"] = ", ".join(_dedupe(names))

    if not data["series"]:
        series_el = soup.select_one('a[href*="/series/"], li.seriesLabel a')
        if series_el:
            raw_series = series_el.get_text(strip=True)
            # Audible's own text is usually "Series Name, Book N" - drop the
            # comma so it reads "Series Name Book N" instead.
            data["series"] = re.sub(r",\s*(Book\s+\d+)$", r" \1", raw_series)

            # Fallback: some product pages' series link has no book number at
            # all. Check three places in order, since where the number shows
            # up varies by title: (1) a short dedicated heading right under
            # the title like "Volume 3", (2) the "Vol." baked directly into
            # the main title itself (e.g. "..., Vol. 3") or the og:title meta
            # - checked against the raw page elements, not just our already-
            # parsed data["title"], since that sometimes comes from JSON-LD
            # data that omits the volume suffix even when the visible title
            # includes it, and (3) the URL slug as a last resort.
            if not re.search(r"Book\s+\d+$", data["series"]):
                vol_num = None
                for tag in soup.find_all(["h2", "h3", "span"]):
                    m = re.fullmatch(r"Vol(?:ume)?\.?\s*(\d+)", tag.get_text(strip=True), re.I)
                    if m:
                        vol_num = m.group(1)
                        break
                if not vol_num:
                    title_candidates = [data["title"] or ""]
                    h1 = soup.find("h1")
                    if h1:
                        title_candidates.append(h1.get_text(strip=True))
                    og_title = soup.find("meta", property="og:title")
                    if og_title and og_title.get("content"):
                        title_candidates.append(og_title["content"])
                    for candidate in title_candidates:
                        vol_match = re.search(r"\bVol(?:ume)?\.?\s*(\d+)\b", candidate, re.I)
                        if vol_match:
                            vol_num = vol_match.group(1)
                            break
                if not vol_num:
                    vol_match = re.search(r"[-_]Vol(?:ume)?[-_](\d+)[-_]", url, re.I)
                    if vol_match:
                        vol_num = vol_match.group(1)
                if vol_num:
                    data["series"] = f"{data['series']} Book {vol_num}"

    if not data["cover_url"]:
        og_image = soup.find("meta", property="og:image")
        if og_image and og_image.get("content"):
            data["cover_url"] = og_image["content"].strip()

    if not data["cover_url"]:
        img = soup.select_one('img[src*="media-amazon.com/images/I/"]')
        if img and img.get("src"):
            data["cover_url"] = img["src"].strip()

    page_text = soup.get_text("\n", strip=True)
    if not data["length"]:
        m = re.search(r"Length:\s*([^\n]+)", page_text)
        if m:
            data["length"] = m.group(1).strip()
    if not data["release_date"]:
        m = re.search(r"Release date:\s*([^\n]+)", page_text)
        if m:
            data["release_date"] = m.group(1).strip()
    if not data["language"] or (len(data["language"]) <= 5 and " " not in data["language"]):
        m = re.search(r"Language:\s*([^\n]+)", page_text)
        if m:
            data["language"] = m.group(1).strip()

    summary_el = soup.select_one(
        'adbl-text-block[slot="summary"], [id*="publisher-summary"], .productPublisherSummary'
    )
    if summary_el:
        formatted = html_summary_to_text(summary_el)
        if formatted:
            data["summary"] = formatted
    if not data["summary"] and data.get("_raw_description_html"):
        desc_soup = BeautifulSoup(data["_raw_description_html"], "lxml")
        formatted = html_summary_to_text(desc_soup)
        if formatted:
            data["summary"] = formatted
    if not data["summary"]:
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            data["summary"] = meta_desc["content"].strip()

    data["release_date"] = _normalize_release_date(data["release_date"])
    if data["language"]:
        data["language"] = data["language"][:1].upper() + data["language"][1:]

    subtitle_el = soup.select_one('h2[slot="subtitle"], .subtitle, .bc-heading-subtitle')
    if subtitle_el:
        subtitle = subtitle_el.get_text(strip=True)
        if subtitle and data["title"] and subtitle.lower() not in data["title"].lower():
            data["title"] = f"{data['title']} - {subtitle}"

    return data


def _is_bold_tag(tag) -> bool:
    if tag.name in ("b", "strong"):
        return True
    classes = " ".join(tag.get("class", []) or []).lower()
    style = (tag.get("style") or "").lower()
    return "bold" in classes or "bold" in style or any(w in style for w in ("700", "800", "900"))


def _render_inline(node) -> str:
    if isinstance(node, str):
        return str(node)
    if node.name == "br":
        return "\n"
    inner = "".join(_render_inline(c) for c in node.children)
    if _is_bold_tag(node):
        return f"<strong>{inner}</strong>"
    return inner


def html_summary_to_text(element) -> str:
    blocks = []
    for child in element.children:
        if isinstance(child, str):
            text = str(child).strip()
        else:
            text = _render_inline(child).strip()
        if text:
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r" *\n *", "\n", text)
            text = re.sub(r"\n{2,}", "\n\n", text)
            text = text.strip()
        if text and not re.match(r"^\u00a9\s*\d{4}", text):
            blocks.append(text)
    return "\n\n".join(blocks)


def _clean_contributor_name(name: str) -> str:
    parts = [p.strip() for p in name.split(" - ") if p.strip()]
    cleaned = []
    for p in parts:
        if cleaned and cleaned[-1].lower() == p.lower():
            continue
        cleaned.append(p)
    return " - ".join(cleaned)


def _names_from_ld(value) -> str:
    if isinstance(value, str):
        return _clean_contributor_name(value.strip())
    if isinstance(value, dict):
        return _clean_contributor_name(value.get("name", "").strip())
    if isinstance(value, list):
        names = []
        for v in value:
            if isinstance(v, dict) and v.get("name"):
                names.append(_clean_contributor_name(v["name"].strip()))
            elif isinstance(v, str):
                names.append(_clean_contributor_name(v.strip()))
        return ", ".join(_dedupe(names))
    return ""


def _iso_duration_to_readable(duration: str) -> str:
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", duration)
    if not m:
        return duration
    hours, minutes = m.groups()
    parts = []
    if hours:
        parts.append(f"{hours} hrs")
    if minutes:
        parts.append(f"{minutes} mins")
    return " and ".join(parts) if parts else duration


def _dedupe(seq):
    seen = set()
    out = []
    for item in seq:
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _normalize_release_date(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return raw

    formats = [
        "%Y-%m-%d",
        "%d-%m-%y",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d/%m/%y",
        "%m/%d/%Y",
        "%B %d, %Y",
        "%d %B %Y",
        "%b %d, %Y",
        "%d %b %Y",
        "%Y/%m/%d",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%m-%d-%Y")
        except ValueError:
            continue
    return raw


def _extract_asin(url: str) -> str:
    m = re.search(r"/([A-Z0-9]{10})(?:[/?]|$)", url)
    return m.group(1) if m else "cover"


def _slugify(title: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", title or "")
    slug = re.sub(r"_+", "_", slug).strip("_").lower()
    return slug


AUDIBLE_CATALOG_RESPONSE_GROUPS = [
    "categories",
    "category_ladders",
    "claim_code_url",
    "contributors",
    "media",
    "price",
    "product_attrs",
    "product_desc",
    "product_extended_attrs",
    "product_plan_details",
    "product_plans",
    "provided_review",
    "rating",
    "relationships",
    "review_attrs",
    "reviews",
    "sample",
    "series",
    "sku",
    "ws4v",
]


def _audible_api_host(url: str) -> str:
    netloc = urlsplit(url).netloc
    parts = netloc.split(".")
    if "audible" in parts:
        idx = parts.index("audible")
        tld = ".".join(parts[idx:])
        return f"https://api.{tld}"
    return "https://api.audible.com"


def fetch_hd_cover_url(page_url: str, asin: str):
    api_host = _audible_api_host(page_url)
    api_url = f"{api_host}/1.0/catalog/products/{asin}"
    params = {
        "response_groups": ",".join(AUDIBLE_CATALOG_RESPONSE_GROUPS),
        "image_sizes": "500,1024",
    }
    resp = requests.get(api_url, params=params, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    images = payload.get("product", {}).get("product_images") or {}

    large = images.get("1024")
    if large:
        return large.replace("._SL1024_", "")

    small = images.get("500")
    if small:
        return small.replace("._SL500_", "")

    return None


def build_cover_urls(cover_url: str, page_url: str = "", asin: str = "") -> dict:
    result = {"size_500": "", "hd": ""}
    if not cover_url:
        return result

    base, ext = _split_image_url(cover_url)
    if base is None:
        result["size_500"] = cover_url
    else:
        result["size_500"] = f"{base}._SL500_.{ext}"
        result["hd"] = f"{base}._SL2000_.{ext}"

    if page_url and asin:
        try:
            api_hd = fetch_hd_cover_url(page_url, asin)
            if api_hd:
                result["hd"] = api_hd
        except Exception:
            pass

    return result


def _split_image_url(url: str):
    m = re.match(r"^(https?://[^?]+/images/I/[^./]+)((?:\._[A-Z]{2}\d+_)*)\.(\w+)(?:\?.*)?$", url)
    if not m:
        return None, None
    base, _modifiers, ext = m.groups()
    return base, ext


def _download_once(url: str, dest_path: str) -> None:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    if resp.status_code != 200:
        raise requests.exceptions.RequestException(f"HTTP {resp.status_code}")
    content_type = resp.headers.get("Content-Type", "")
    if not content_type.startswith("image"):
        raise ValueError(f"Unexpected content type: {content_type}")
    with open(dest_path, "wb") as f:
        f.write(resp.content)


def download_image(url: str, dest_path: str) -> bool:
    try:
        _with_retries(lambda: _download_once(url, dest_path), label=f"Download of {dest_path}")
        return True
    except StoppedByUser:
        raise
    except Exception:
        return False


AUDIBLE_BITRATE_KBPS = 126


def _parse_length_to_seconds(length_str: str):
    if not length_str:
        return None
    hours = 0
    minutes = 0
    m = re.search(r"(\d+)\s*hr", length_str, re.I)
    if m:
        hours = int(m.group(1))
    m = re.search(r"(\d+)\s*min", length_str, re.I)
    if m:
        minutes = int(m.group(1))
    if hours == 0 and minutes == 0:
        return None
    return hours * 3600 + minutes * 60


def estimate_filesize_mb(length_str: str):
    seconds = _parse_length_to_seconds(length_str)
    if seconds is None:
        return None
    bytes_total = (AUDIBLE_BITRATE_KBPS * 1000 * seconds) / 8
    return bytes_total / (1024 * 1024)


def build_report_text(data: dict, include_filesize: bool = True) -> str:
    lines = [
        data["summary"],
        "<h3>"+data["title"] + "</h3>",
        f"By: {data['authors']}",
        f"Narrated By: {data['narrators']}",
        f"Series: {data['series'] or 'Stand-alone Title'}",
        f"Genres: {data['genres']}",
        f"Length: {data['length']}",
        f"Release date: {data['release_date']}",
        f"Language: {data['language']}",
    ]
    if include_filesize:
        mb = estimate_filesize_mb(data["length"])
        filesize_str = f"{mb:.1f} MB" if mb is not None else ""
        lines.append(f"Filesize: {filesize_str}")
    return "\n".join(lines)


TEXT_DIR = "text"
COVERS_DIR = "covers"
MASTER_FILENAME = "Master.txt"


def save_report(text: str, slug: str) -> None:
    os.makedirs(TEXT_DIR, exist_ok=True)
    path = os.path.join(TEXT_DIR, f"{slug}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print()
    print(f"Saved report to {path}")


def append_to_master(text: str) -> None:
    os.makedirs(TEXT_DIR, exist_ok=True)
    path = os.path.join(TEXT_DIR, MASTER_FILENAME)
    file_has_content = os.path.exists(path) and os.path.getsize(path) > 0
    with open(path, "a", encoding="utf-8") as f:
        if file_has_content:
            f.write("\n\n" + ("-" * 60) + "\n\n")
        f.write(text + "\n")
    print()
    print(f"Appended report to {path}")


def handle_covers(data: dict, slug: str, page_url: str, asin: str, fetch_hd_covers: bool = True) -> None:
    print()
    urls = build_cover_urls(data["cover_url"], page_url, asin)

    if not urls["size_500"]:
        print("Cover: not found on page.")
        return

    os.makedirs(COVERS_DIR, exist_ok=True)

    path_500 = os.path.join(COVERS_DIR, f"{slug}_500.jpg")
    if download_image(urls["size_500"], path_500):
        print(f"Saved 500x500 cover to {path_500}")
    else:
        print("Could not download the 500x500 cover.")

    if fetch_hd_covers and urls["hd"]:
        path_hd = os.path.join(COVERS_DIR, f"{slug}_cover_hd.jpg")
        if download_image(urls["hd"], path_hd):
            print(f"Saved HD cover to {path_hd}")


# --- Chapter fetching -------------------------------------------------
# Works on ANY Audible marketplace: we hit the licenserequest endpoint on
# whatever domain the product URL itself uses (www.audible.co.uk,
# www.audible.com, www.audible.de, etc.) rather than hardcoding one TLD.
# This grants a "Preview" license for the sample and includes the full
# chapter list (titles + start offsets) even with no login/cookies at all.

CHAPTER_DEVICE_TYPE_ID = "A3SSF9KOQX7TIJ"  # WebPlayerApplication device type


def _random_session_id(length: int = 16) -> str:
    import string

    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def fetch_chapters(page_url: str, asin: str):
    """Fetch the chapter list for a title's preview/sample.

    Returns a list of chapter dicts (with 'title', 'start_offset_sec', etc.)
    or None if this title has no sample / no chapter data available.
    Raises on network/HTTP errors so the caller can decide how to log it.
    """
    netloc = urlsplit(page_url).netloc  # e.g. www.audible.co.uk, www.audible.com
    base = f"https://{netloc}"
    api_url = f"{base}/audible-api/1.0/content/{asin}/licenserequest"

    payload = {
        "supported_media_features": {
            "drm_types": ["Dash", "Mpeg"],
            "codecs": ["mp4a.40.2", "mp4a.40.42"],
            "chapter_titles_type": "Tree",
            "previews": True,
            "catalog_samples": True,
        },
        "response_groups": "chapter_info, content_reference, last_position_heard, certificate, pdf_url",
        "chapter_titles_type": "Tree",
        "asin": asin,
        "consumption_type": "Streaming",
        "tenant_id": "Audible",
        "use_adaptive_bit_rate": True,
        "session_id": _random_session_id(),
        "spatial": True,
        "supported_features": ["BypassOpeningCredits"],
    }

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "client-id": "WebPlayerApplication",
        "x-aud-device-type": CHAPTER_DEVICE_TYPE_ID,
        "x-device-type-id": CHAPTER_DEVICE_TYPE_ID,
        "Referer": (
            f"{base}/webplayer?asin={asin}&contentDeliveryType=Unknown&isSample=true"
            f"&ref_=a_pd_jpp_cloudplayer_{asin}&overrideLph=false&initialCPLaunch=true"
        ),
        "User-Agent": HEADERS["User-Agent"],
        "Accept-Language": HEADERS["Accept-Language"],
    }

    resp = requests.post(api_url, headers=headers, data=json.dumps(payload), timeout=20)
    resp.raise_for_status()
    body = resp.json()
    chapter_info = (
        body.get("content_license", {}).get("content_metadata", {}).get("chapter_info")
    )
    if not chapter_info:
        return None
    return chapter_info.get("chapters") or None


def format_chapters_text(chapters, title: str = "") -> str:
    lines = []
    if title:
        lines.append(title)
        lines.append("")
    for ch in chapters:
        secs = ch.get("start_offset_sec", 0)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        timestamp = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        lines.append(f"[{timestamp}] {ch.get('title', '')}")
    return "\n".join(lines)


def save_chapters_file(text: str, slug: str) -> None:
    os.makedirs(TEXT_DIR, exist_ok=True)
    path = os.path.join(TEXT_DIR, f"{slug}_chapters.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(f"Saved chapters to {path}")


def handle_chapters(url: str, asin: str, slug: str, title: str) -> None:
    """Best-effort chapter fetch: retries transient failures the same way
    the page fetch does, but on 404 or final failure, just log a note and
    move on rather than stopping the batch."""
    try:
        chapters = _with_retries(
            lambda: fetch_chapters(url, asin),
            label="Fetching chapters",
            initial_delay=CHAPTER_INITIAL_RETRY_DELAY,
            increment=CHAPTER_RETRY_DELAY_INCREMENT,
            max_delay=CHAPTER_MAX_RETRY_DELAY,
            max_retry_seconds=CHAPTER_MAX_RETRY_SECONDS,
            max_attempts=3,
        )
    except StoppedByUser:
        raise
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            print("No chapters available for this title (no sample found).")
        else:
            print(f"Could not fetch chapters (skipping): {exc}")
        return
    except Exception as exc:
        print(f"Could not fetch chapters (skipping): {exc}")
        return

    if not chapters:
        print("No chapters available for this title (no sample, or none listed).")
        return

    chapters_text = format_chapters_text(chapters, title)
    save_chapters_file(chapters_text, slug)


def _is_already_downloaded(asin: str, one_file_mode: bool) -> bool:
    """Check whether this ASIN has already been fetched, without touching
    the network - reads Master.txt in one-file mode, or checks for an
    existing per-book file otherwise."""
    if one_file_mode:
        master_path = os.path.join(BASE_DIR, TEXT_DIR, MASTER_FILENAME)
        if not os.path.exists(master_path):
            return False
        try:
            with open(master_path, "r", encoding="utf-8") as f:
                return f"ASIN: {asin}" in f.read()
        except OSError:
            return False
    else:
        text_dir = os.path.join(BASE_DIR, TEXT_DIR)
        if not os.path.isdir(text_dir):
            return False
        return any(name.endswith(f"_{asin}.txt") or name == f"{asin}.txt" for name in os.listdir(text_dir))


def run_fetch_logic(
    url,
    download_covers_state,
    additional_text_state,
    filesize_estimates_state,
    one_file_mode_state,
    fetch_chapters_state,
    fetch_hd_covers_state,
):
    url = _sanitize_url(url)

    asin = _extract_asin(url)
    if _is_already_downloaded(asin, one_file_mode_state["value"]):
        print(f"Already downloaded (ASIN {asin}), skipping.")
        return

    try:
        data = _with_retries(
            lambda: fetch_audible_data(url),
            label="Fetching page",
            initial_delay=PAGE_INITIAL_RETRY_DELAY,
            increment=PAGE_RETRY_DELAY_INCREMENT,
            max_delay=PAGE_MAX_RETRY_DELAY,
            max_retry_seconds=PAGE_MAX_RETRY_SECONDS,
        )
    except StoppedByUser:
        raise
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            print("This appears to be an invalid URL, please try again.")
        else:
            print(f"Error fetching page: {exc}")
        return
    except Exception as exc:
        print(f"Error fetching page: {exc}")
        return

    title_slug = _slugify(data.get("title", ""))
    file_base = f"{title_slug}_{asin}" if title_slug else asin

    report_text = build_report_text(data, include_filesize=filesize_estimates_state["value"])
    extra = additional_text_state["value"].strip()
    if extra:
        report_text = f"{report_text}\n{extra}"
    report_text = f"{report_text}\nASIN: {asin}"
    print(report_text)

    if one_file_mode_state["value"]:
        append_to_master(report_text)
    else:
        save_report(report_text, file_base)

    if download_covers_state["value"]:
        handle_covers(data, file_base, url, asin, fetch_hd_covers=fetch_hd_covers_state["value"])
    else:
        print()
        print("Skipping cover download (unchecked).")

    if fetch_chapters_state["value"]:
        print()
        handle_chapters(url, asin, file_base, data.get("title", ""))


def main_cli():
    if len(sys.argv) != 2:
        print("Usage: python audible_extractor.py <audible product page URL>")
        sys.exit(1)

    url = _sanitize_url(sys.argv[1])

    asin = _extract_asin(url)
    if _is_already_downloaded(asin, one_file_mode=False):
        print(f"Already downloaded (ASIN {asin}), skipping.")
        return

    try:
        data = _with_retries(
            lambda: fetch_audible_data(url),
            label="Fetching page",
            initial_delay=PAGE_INITIAL_RETRY_DELAY,
            increment=PAGE_RETRY_DELAY_INCREMENT,
            max_delay=PAGE_MAX_RETRY_DELAY,
            max_retry_seconds=PAGE_MAX_RETRY_SECONDS,
        )
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            print("This appears to be an invalid URL, please try again.")
        else:
            print(f"Error fetching page: {exc}")
        sys.exit(1)
    except Exception as exc:
        print(f"Error fetching page: {exc}")
        sys.exit(1)

    asin = _extract_asin(url)
    title_slug = _slugify(data.get("title", ""))
    file_base = f"{title_slug}_{asin}" if title_slug else asin

    report_text = build_report_text(data)
    report_text = f"{report_text}\nASIN: {asin}"
    print(report_text)
    save_report(report_text, file_base)
    handle_covers(data, file_base, url, asin, fetch_hd_covers=True)

    print()
    handle_chapters(url, asin, file_base, data.get("title", ""))


class TextRedirector(io.TextIOBase):
    def __init__(self, widget):
        self.widget = widget

    def write(self, s):
        self.widget.after(0, self._append, s)
        return len(s)

    def _append(self, s):
        self.widget.configure(state="normal")
        self.widget.insert("end", s)
        self.widget.see("end")
        self.widget.configure(state="disabled")

    def flush(self):
        pass


def _select_all(widget):
    widget.tag_add("sel", "1.0", "end-1c")
    widget.mark_set("insert", "1.0")
    widget.see("insert")
    return "break"


def add_context_menu(widget, cut=True, paste=True, respect_disabled=True):
    import tkinter as tk

    menu = tk.Menu(widget, tearoff=0)
    if cut:
        menu.add_command(label="Cut", command=lambda: widget.event_generate("<<Cut>>"))
    menu.add_command(label="Copy", command=lambda: widget.event_generate("<<Copy>>"))
    if paste:
        menu.add_command(label="Paste", command=lambda: widget.event_generate("<<Paste>>"))
    menu.add_separator()
    menu.add_command(label="Select All", command=lambda: _select_all(widget))

    def show_menu(event):
        if respect_disabled and str(widget.cget("state")) == "disabled":
            return
        menu.tk_popup(event.x_root, event.y_root)

    widget.bind("<Button-3>", show_menu)
    return menu


def set_text_box_enabled(text_widget, enabled):
    if enabled:
        text_widget.configure(state="normal", bg="white", fg="black")
    else:
        text_widget.configure(state="disabled", bg="#e0e0e0", fg="#888888")


def worker(
    urls,
    output_text,
    fetch_btn,
    url_text,
    status_var,
    clear_btn,
    download_covers_state,
    additional_text_state,
    additional_text,
    filesize_estimates_state,
    one_file_mode_state,
    fetch_chapters_state,
    fetch_hd_covers_state,
):
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    redirector = TextRedirector(output_text)
    sys.stdout = redirector
    sys.stderr = redirector
    try:
        total = len(urls)
        for i, url in enumerate(urls, start=1):
            if STOP_EVENT.is_set():
                print("Stopped.")
                break
            output_text.after(0, lambda i=i, total=total: status_var.set(f"Processing {i}/{total}"))
            print(f"=== [{i}/{total}] {url} ===")
            start_time = time.monotonic()
            try:
                run_fetch_logic(
                    url,
                    download_covers_state,
                    additional_text_state,
                    filesize_estimates_state,
                    one_file_mode_state,
                    fetch_chapters_state,
                    fetch_hd_covers_state,
                )
            except StoppedByUser:
                print("Stopped.")
                break
            elapsed = time.monotonic() - start_time
            print()

            # Pace requests across the whole batch, not just around chapters -
            # helps avoid tripping Audible's rate limiting on the page fetch
            # itself when processing many books back-to-back. Range is wide
            # and random on purpose so the gap between requests doesn't look
            # like a fixed-interval bot.
            if i < total and not STOP_EVENT.is_set():
                if elapsed > 8:
                    # The last fetch needed retries (503s etc.) - Audible is
                    # already grumpy with us, so back off a lot harder before
                    # trying again instead of resuming at normal speed.
                    delay = random.uniform(6.0, 10.0)
                    print(f"That one needed retries - cooling off for {delay:.1f}s before continuing...")
                else:
                    delay = random.uniform(4.0, 9.0)
                if STOP_EVENT.wait(delay):
                    print("Stopped.")
                    break
    except Exception:
        print("Unexpected error:")
        print(traceback.format_exc())
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        STOP_EVENT.clear()
        output_text.after(0, lambda: fetch_btn.config(text="Fetch All", state="normal"))
        output_text.after(0, lambda: clear_btn.config(state="normal"))
        output_text.after(0, lambda: set_text_box_enabled(url_text, True))
        output_text.after(0, lambda: set_text_box_enabled(additional_text, True))
        output_text.after(0, lambda: status_var.set("Waiting..."))


def clear_url_box(url_text):
    url_text.delete("1.0", "end")


def open_folder(folder_name):
    path = os.path.join(BASE_DIR, folder_name)
    from tkinter import messagebox

    try:
        os.makedirs(path, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        else:
            subprocess.run(["xdg-open", path], check=False)
    except Exception as exc:
        messagebox.showerror("Error", f"Could not open folder:\n{path}\n\n{exc}")


def open_master_file():
    text_dir = os.path.join(BASE_DIR, TEXT_DIR)
    path = os.path.join(text_dir, MASTER_FILENAME)
    from tkinter import messagebox

    try:
        os.makedirs(text_dir, exist_ok=True)
        if not os.path.exists(path):
            open(path, "a", encoding="utf-8").close()
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        else:
            subprocess.run(["xdg-open", path], check=False)
    except Exception as exc:
        messagebox.showerror("Error", f"Could not open file:\n{path}\n\n{exc}")


def on_button_click(
    url_text,
    output_text,
    fetch_btn,
    status_var,
    clear_btn,
    download_covers_state,
    additional_text_state,
    additional_text,
    filesize_estimates_state,
    one_file_mode_state,
    fetch_chapters_state,
    fetch_hd_covers_state,
):
    from tkinter import messagebox

    try:
        if fetch_btn["text"] == "Fetch All":
            raw = url_text.get("1.0", "end")
            urls = [line.strip() for line in raw.splitlines() if line.strip()]
            if not urls:
                messagebox.showwarning("No URLs", "Paste at least one Audible URL first.")
                return
            STOP_EVENT.clear()
            fetch_btn.config(text="Stop")
            clear_btn.config(state="disabled")
            set_text_box_enabled(url_text, False)
            set_text_box_enabled(additional_text, False)
            output_text.configure(state="normal")
            output_text.delete("1.0", "end")
            output_text.configure(state="disabled")
            threading.Thread(
                target=worker,
                args=(
                    urls,
                    output_text,
                    fetch_btn,
                    url_text,
                    status_var,
                    clear_btn,
                    download_covers_state,
                    additional_text_state,
                    additional_text,
                    filesize_estimates_state,
                    one_file_mode_state,
                    fetch_chapters_state,
                    fetch_hd_covers_state,
                ),
                daemon=True,
            ).start()
        else:
            STOP_EVENT.set()
            fetch_btn.config(state="disabled")
    except Exception as exc:
        messagebox.showerror("Error", f"{exc}\n\n{traceback.format_exc()}")


def main_gui():
    import tkinter as tk
    from tkinter import scrolledtext

    os.chdir(BASE_DIR)

    root = tk.Tk()
    root.title("Audible Metadata Extractor")
    root.geometry("760x620")

    top_frame = tk.Frame(root)
    top_frame.pack(fill="x", padx=10, pady=10)

    tk.Label(top_frame, text="Audible URLs (one per line):").pack(anchor="w")

    url_box_frame = tk.Frame(top_frame)
    url_box_frame.pack(fill="x", pady=(4, 6))

    url_text = tk.Text(url_box_frame, height=8, wrap="none")
    url_scrollbar = tk.Scrollbar(url_box_frame, command=url_text.yview)
    url_text.configure(yscrollcommand=url_scrollbar.set)
    url_text.pack(side="left", fill="both", expand=True)
    url_scrollbar.pack(side="right", fill="y")
    add_context_menu(url_text, cut=True, paste=True)

    button_row = tk.Frame(top_frame)
    button_row.pack(anchor="e")

    settings = load_settings()
    saved_one_file_mode = settings["Settings"].getboolean("one_file_mode", fallback=False)
    saved_filesize_estimates = settings["Settings"].getboolean("filesize_estimates", fallback=True)
    saved_download_covers = settings["Settings"].getboolean("download_covers", fallback=True)
    saved_fetch_chapters = settings["Settings"].getboolean("fetch_chapters", fallback=True)
    saved_fetch_hd_covers = settings["Settings"].getboolean("fetch_hd_covers", fallback=True)

    one_file_mode_state = {"value": saved_one_file_mode}
    one_file_mode_var = tk.BooleanVar(value=saved_one_file_mode)

    def on_one_file_mode_toggle():
        one_file_mode_state["value"] = one_file_mode_var.get()
        settings["Settings"]["one_file_mode"] = str(one_file_mode_var.get())
        save_settings(settings)

    one_file_mode_check = tk.Checkbutton(
        button_row,
        text="One File Mode",
        variable=one_file_mode_var,
        command=on_one_file_mode_toggle,
    )
    one_file_mode_check.grid(row=0, column=0, sticky="w", padx=(0, 6), pady=(0, 4))

    filesize_estimates_state = {"value": saved_filesize_estimates}
    filesize_estimates_var = tk.BooleanVar(value=saved_filesize_estimates)

    def on_filesize_estimates_toggle():
        filesize_estimates_state["value"] = filesize_estimates_var.get()
        settings["Settings"]["filesize_estimates"] = str(filesize_estimates_var.get())
        save_settings(settings)

    filesize_estimates_check = tk.Checkbutton(
        button_row,
        text="Filesize Estimates",
        variable=filesize_estimates_var,
        command=on_filesize_estimates_toggle,
    )
    filesize_estimates_check.grid(row=0, column=1, sticky="w", padx=(0, 6), pady=(0, 4))

    download_covers_state = {"value": saved_download_covers}
    download_covers_var = tk.BooleanVar(value=saved_download_covers)

    def on_download_covers_toggle():
        download_covers_state["value"] = download_covers_var.get()
        settings["Settings"]["download_covers"] = str(download_covers_var.get())
        save_settings(settings)

    download_covers_check = tk.Checkbutton(
        button_row,
        text="Fetch Covers",
        variable=download_covers_var,
        command=on_download_covers_toggle,
    )
    download_covers_check.grid(row=0, column=2, sticky="w", padx=(0, 6), pady=(0, 4))

    fetch_chapters_state = {"value": saved_fetch_chapters}
    fetch_chapters_var = tk.BooleanVar(value=saved_fetch_chapters)

    def on_fetch_chapters_toggle():
        fetch_chapters_state["value"] = fetch_chapters_var.get()
        settings["Settings"]["fetch_chapters"] = str(fetch_chapters_var.get())
        save_settings(settings)

    fetch_chapters_check = tk.Checkbutton(
        button_row,
        text="Fetch Chapters",
        variable=fetch_chapters_var,
        command=on_fetch_chapters_toggle,
    )
    fetch_chapters_check.grid(row=0, column=3, sticky="w", padx=(0, 6), pady=(0, 4))

    clear_btn = tk.Button(button_row, text="Clear")
    clear_btn.grid(row=0, column=4, sticky="ew", padx=(0, 6), pady=(0, 4))

    fetch_btn = tk.Button(button_row, text="Fetch All")
    fetch_btn.grid(row=0, column=5, sticky="ew", pady=(0, 4))

    fetch_hd_covers_state = {"value": saved_fetch_hd_covers}
    fetch_hd_covers_var = tk.BooleanVar(value=saved_fetch_hd_covers)

    def on_fetch_hd_covers_toggle():
        fetch_hd_covers_state["value"] = fetch_hd_covers_var.get()
        settings["Settings"]["fetch_hd_covers"] = str(fetch_hd_covers_var.get())
        save_settings(settings)

    fetch_hd_covers_check = tk.Checkbutton(
        button_row,
        text="Fetch HD Covers",
        variable=fetch_hd_covers_var,
        command=on_fetch_hd_covers_toggle,
    )
    # Same column as "Fetch Chapters" above it, so it lines up underneath.
    fetch_hd_covers_check.grid(row=1, column=3, sticky="w", padx=(0, 6))

    def on_clear_folders_click():
        from tkinter import messagebox

        confirmed = messagebox.askyesno(
            "Clear Folders",
            "Are you sure? This will permanently delete everything in the "
            "'text' and 'covers' folders.",
        )
        if not confirmed:
            return

        deleted_any = False
        for folder in (TEXT_DIR, COVERS_DIR):
            folder_path = os.path.join(BASE_DIR, folder)
            if not os.path.isdir(folder_path):
                continue
            for name in os.listdir(folder_path):
                item_path = os.path.join(folder_path, name)
                try:
                    if os.path.isfile(item_path) or os.path.islink(item_path):
                        os.remove(item_path)
                    elif os.path.isdir(item_path):
                        shutil.rmtree(item_path)
                    deleted_any = True
                except Exception as exc:
                    print(f"Could not delete {item_path}: {exc}")

        messagebox.showinfo(
            "Clear Folders",
            "Folders cleared." if deleted_any else "Nothing to clear — folders were already empty.",
        )

    clear_folders_btn = tk.Button(button_row, text="Clear Folders", command=on_clear_folders_click)
    # Spans the same two columns as "Clear" + "Fetch All" above it, so its
    # width matches from where "Clear" starts to where "Fetch All" ends.
    clear_folders_btn.grid(row=1, column=4, columnspan=2, sticky="ew")

    output_text = scrolledtext.ScrolledText(root, state="disabled", wrap="word")
    add_context_menu(output_text, cut=False, paste=False, respect_disabled=False)

    status_var = tk.StringVar(value="Waiting...")
    status_label = tk.Label(root, textvariable=status_var, anchor="w", relief="sunken", padx=6)
    status_label.pack(side="bottom", fill="x")

    folder_btn_frame = tk.Frame(root)
    folder_btn_frame.pack(side="bottom", fill="x", padx=10, pady=(0, 4))
    tk.Button(
        folder_btn_frame, text="Open Covers Folder", command=lambda: open_folder("covers")
    ).pack(side="left")
    tk.Button(
        folder_btn_frame, text="Open Text Folder", command=lambda: open_folder("text")
    ).pack(side="left", padx=(6, 0))
    tk.Button(
        folder_btn_frame, text="Open Master.txt (One File Mode)", command=open_master_file
    ).pack(side="left", padx=(6, 0))

    additional_frame = tk.Frame(root)
    additional_frame.pack(side="bottom", fill="x", padx=10, pady=(0, 4))
    tk.Label(additional_frame, text="Additional text (appended to every report):").pack(anchor="w")

    additional_box_frame = tk.Frame(additional_frame)
    additional_box_frame.pack(fill="x")
    additional_text = tk.Text(additional_box_frame, height=4, wrap="word")
    additional_scrollbar = tk.Scrollbar(additional_box_frame, command=additional_text.yview)
    additional_text.configure(yscrollcommand=additional_scrollbar.set)
    additional_text.pack(side="left", fill="both", expand=True)
    additional_scrollbar.pack(side="right", fill="y")
    add_context_menu(additional_text, cut=True, paste=True)

    additional_text_path = os.path.join(BASE_DIR, "additional.txt")
    additional_text_state = {"value": ""}
    autosave_job = {"id": None}

    try:
        with open(additional_text_path, "r", encoding="utf-8") as f:
            existing_additional = f.read()
    except (FileNotFoundError, OSError):
        existing_additional = ""

    if existing_additional:
        additional_text.insert("1.0", existing_additional)
    additional_text_state["value"] = existing_additional

    def save_additional_text():
        autosave_job["id"] = None
        content = additional_text.get("1.0", "end-1c")
        try:
            with open(additional_text_path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError:
            pass

    def on_additional_text_modified(event):
        additional_text.edit_modified(False)
        additional_text_state["value"] = additional_text.get("1.0", "end-1c")
        if autosave_job["id"] is not None:
            root.after_cancel(autosave_job["id"])
        autosave_job["id"] = root.after(5000, save_additional_text)

    additional_text.bind("<<Modified>>", on_additional_text_modified)

    output_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    clear_btn.config(command=lambda: clear_url_box(url_text))
    fetch_btn.config(
        command=lambda: on_button_click(
            url_text,
            output_text,
            fetch_btn,
            status_var,
            clear_btn,
            download_covers_state,
            additional_text_state,
            additional_text,
            filesize_estimates_state,
            one_file_mode_state,
            fetch_chapters_state,
            fetch_hd_covers_state,
        )
    )
    url_text.focus()

    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main_cli()
    else:
        main_gui()
