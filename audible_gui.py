import sys
import os
import re
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


def _with_retries(fn, label="request"):
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
            if time.monotonic() - start >= MAX_RETRY_SECONDS:
                raise
            delay = min(INITIAL_RETRY_DELAY + RETRY_DELAY_INCREMENT * attempt, MAX_RETRY_DELAY)
            print(f"{label} failed ({exc}), retrying in {int(delay * 1000)}ms... (attempt {attempt + 2})")
            if STOP_EVENT.wait(delay):
                raise StoppedByUser()
            attempt += 1
        except Exception as exc:
            if STOP_EVENT.is_set():
                raise StoppedByUser()
            if time.monotonic() - start >= MAX_RETRY_SECONDS:
                raise
            delay = min(INITIAL_RETRY_DELAY + RETRY_DELAY_INCREMENT * attempt, MAX_RETRY_DELAY)
            print(f"{label} failed ({exc}), retrying in {int(delay * 1000)}ms... (attempt {attempt + 2})")
            if STOP_EVENT.wait(delay):
                raise StoppedByUser()
            attempt += 1


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
            data["series"] = series_el.get_text(strip=True)

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
        data["summary"] or "(not found)",
        "",
        data["title"] or "(not found)",
        f"By: {data['authors']}",
        f"Narrated By: {data['narrators']}",
        f"Series: {data['series']}",
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


def save_report(text: str, slug: str) -> None:
    os.makedirs(TEXT_DIR, exist_ok=True)
    path = os.path.join(TEXT_DIR, f"{slug}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print()
    print(f"Saved report to {path}")


def handle_covers(data: dict, slug: str, page_url: str, asin: str) -> None:
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

    if urls["hd"]:
        path_hd = os.path.join(COVERS_DIR, f"{slug}_cover_hd.jpg")
        if download_image(urls["hd"], path_hd):
            print(f"Saved HD cover to {path_hd}")


def run_fetch_logic(url, download_covers_state, additional_text_state, filesize_estimates_state):
    url = _sanitize_url(url)
    try:
        data = _with_retries(lambda: fetch_audible_data(url), label="Fetching page")
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

    asin = _extract_asin(url)
    title_slug = _slugify(data.get("title", ""))
    file_base = f"{title_slug}_{asin}" if title_slug else asin

    report_text = build_report_text(data, include_filesize=filesize_estimates_state["value"])
    extra = additional_text_state["value"].strip()
    if extra:
        report_text = f"{report_text}\n{extra}"
    print(report_text)
    save_report(report_text, file_base)

    if download_covers_state["value"]:
        handle_covers(data, file_base, url, asin)
    else:
        print()
        print("Skipping cover download (unchecked).")


def main_cli():
    if len(sys.argv) != 2:
        print("Usage: python audible_extractor.py <audible product page URL>")
        sys.exit(1)

    url = _sanitize_url(sys.argv[1])
    try:
        data = _with_retries(lambda: fetch_audible_data(url), label="Fetching page")
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
    print(report_text)
    save_report(report_text, file_base)
    handle_covers(data, file_base, url, asin)


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
            try:
                run_fetch_logic(url, download_covers_state, additional_text_state, filesize_estimates_state)
            except StoppedByUser:
                print("Stopped.")
                break
            print()
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
    saved_filesize_estimates = settings["Settings"].getboolean("filesize_estimates", fallback=True)
    saved_download_covers = settings["Settings"].getboolean("download_covers", fallback=True)

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
    filesize_estimates_check.pack(side="left", padx=(0, 6))

    download_covers_state = {"value": saved_download_covers}
    download_covers_var = tk.BooleanVar(value=saved_download_covers)

    def on_download_covers_toggle():
        download_covers_state["value"] = download_covers_var.get()
        settings["Settings"]["download_covers"] = str(download_covers_var.get())
        save_settings(settings)

    download_covers_check = tk.Checkbutton(
        button_row,
        text="Download covers",
        variable=download_covers_var,
        command=on_download_covers_toggle,
    )
    download_covers_check.pack(side="left", padx=(0, 6))

    clear_btn = tk.Button(button_row, text="Clear")
    clear_btn.pack(side="left", padx=(0, 6))

    fetch_btn = tk.Button(button_row, text="Fetch All")
    fetch_btn.pack(side="left")

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
        )
    )
    url_text.focus()

    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main_cli()
    else:
        main_gui()
