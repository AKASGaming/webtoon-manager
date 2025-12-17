import subprocess
import threading
import queue
import sqlite3
import os
import shutil
import requests
import json
import logging
import re
import html
from bs4 import BeautifulSoup  # pyright: ignore[reportMissingModuleSource]
from flask import Flask, render_template, request, jsonify, Response, send_from_directory  # pyright: ignore[reportMissingImports]
from datetime import datetime, timedelta, timezone
import hashlib
from urllib.parse import urlparse
import time

# Setup basic logging
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

# --- CONFIGURATION ---
DB_PATH = '/app/db/webtoons.db'
DOWNLOAD_DIR = '/app/downloads'
CACHE_DIR = '/app/cache/thumbnails'
LOG_QUEUE = queue.Queue()
PROCESS_LOCK = threading.Lock()
DB_WRITE_LOCK = threading.RLock()
SCRAPE_LOCK = threading.Lock()
# Auto-check interval in seconds (default: 5 minutes = 300 seconds)
# This will be updated from settings, but we need a default
AUTO_CHECK_INTERVAL = 300  # 5 minutes

# Default path template: [Author]/[Series]/[Episode #] - [Episode Name].ext
PATH_TEMPLATE_DEFAULT = "author_series_flat"

def resolve_base_folder_template(path_template: str) -> str:
    """
    Convert a full path template into a base folder mode for ensure_series_folder.
    """
    if not path_template:
        return "author_series"
    if path_template.startswith("series_only"):
        return "series_only"
    return "author_series"

def sanitize_filename(name: str) -> str:
	"""
	Make a filesystem-safe name while keeping it readable.
	Also avoids trailing dots/spaces that confuse Windows / SMB
	and cause 8.3-style names like KWPGB2~7.
	"""
	if not name:
		return ""

	allowed = " ._-()"
	# Replace disallowed characters with underscore
	cleaned = "".join(c if c.isalnum() or c in allowed else "_" for c in name)
	# Collapse multiple spaces
	cleaned = re.sub(r"\s+", " ", cleaned).strip()
	# Strip trailing dots and spaces (Windows / Samba friendly)
	cleaned = cleaned.rstrip(". ")

	if not cleaned:
		return "untitled"

	return cleaned

def ensure_series_folder(subscription_id, conn, folder_template: str = "author_series") -> str:
    """
    Build and ensure the base download folder for a subscription:
    Default: [Author]/[Series Name]
    """
    c = conn.cursor()
    row = c.execute(
        "SELECT title, artist FROM subscriptions WHERE id = ?",
        (subscription_id,)
    ).fetchone()

    title = row["title"] if row and row["title"] else f"Series_{subscription_id}"
    artist = row["artist"] if row and row["artist"] else "Unknown Artist"

    base = DOWNLOAD_DIR
    if folder_template == "author_series":
        base = os.path.join(base, sanitize_filename(artist))
    elif folder_template == "series_only":
        base = DOWNLOAD_DIR

    series_dir = os.path.join(base, sanitize_filename(title))
    os.makedirs(series_dir, exist_ok=True)
    return series_dir

PROCESSES = {}
EPISODE_PROGRESS = {}
# Track subscriptions that are currently triggering download_all to prevent duplicates
DOWNLOAD_ALL_IN_PROGRESS = set()
DOWNLOAD_ALL_LOCK = threading.Lock()

LAST_AUTO_CHECK = None
NEXT_AUTO_CHECK = None


# Ensure cache directory exists
if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR)

# --- DATABASE SETUP ---
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn

def dict_factory(cursor, row):
    """
    Row factory that returns dicts instead of sqlite.Row objects.
    Used by the auto-download worker.
    """
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}

def init_db():
    """Initialize database schema and run migrations. Returns (success, error_message)"""
    try:
        conn = get_db()
        c = conn.cursor()

        try:
            c.execute("PRAGMA journal_mode=WAL;")
            c.execute("PRAGMA busy_timeout = 30000;")  # 30 seconds
        except Exception as e:
            logging.warning(f"Could not set SQLite pragmas: {e}")

        # Subscriptions (Series)
        c.execute('''CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            url TEXT UNIQUE,
            artist TEXT,
            thumbnail TEXT,
            last_updated DATETIME
        )''')

        # Episodes
        c.execute('''
            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER,
                title TEXT,
                url TEXT,
                ep_num INTEGER,
                thumbnail TEXT,
                cached_thumbnail TEXT,
                downloaded BOOLEAN DEFAULT 0,
                file_path TEXT,
                published_date DATETIME,
                FOREIGN KEY(subscription_id) REFERENCES subscriptions(id)
            )
        ''')

        # Download Jobs
        c.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comic_title TEXT,
            status TEXT, -- 'running', 'completed', 'failed'
            log TEXT,
            created_at DATETIME
        )''')
        
        # Migrate jobs table to add started_at and finished_at if they don't exist
        c.execute("PRAGMA table_info(jobs)")
        cols = [row[1] for row in c.fetchall()]
        if "started_at" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN started_at DATETIME")
        if "finished_at" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN finished_at DATETIME")
        if "is_auto" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN is_auto BOOLEAN DEFAULT 0")

        # Global settings
        c.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        
        # Per-series settings
        c.execute('''
            CREATE TABLE IF NOT EXISTS series_settings (
                subscription_id INTEGER PRIMARY KEY,
                auto_download_latest BOOLEAN DEFAULT 0,
                schedule_day INTEGER,
                schedule_time TEXT,
                format_override TEXT,
                path_template_override TEXT,
                FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
            )
        ''')
        
        # Migrate existing series_settings table to add new columns if they don't exist
        c.execute("PRAGMA table_info(series_settings)")
        cols = [row[1] for row in c.fetchall()]
        if "format_override" not in cols:
            c.execute("ALTER TABLE series_settings ADD COLUMN format_override TEXT")
        if "path_template_override" not in cols:
            c.execute("ALTER TABLE series_settings ADD COLUMN path_template_override TEXT")
        if "download_all_after_cache" not in cols:
            c.execute("ALTER TABLE series_settings ADD COLUMN download_all_after_cache BOOLEAN DEFAULT 0")
        
        c.execute("""
            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL,
                ep_num INTEGER NOT NULL,
                title TEXT,
                thumbnail TEXT,
                cached_thumbnail TEXT,
                url TEXT,
                downloaded INTEGER DEFAULT 0,
                file_path TEXT,
                UNIQUE(subscription_id, ep_num)
            )
        """)
        # Ensure episodes.download_error exists (for caution indicator on failed downloads)
        c.execute("PRAGMA table_info(episodes)")
        cols = [row[1] for row in c.fetchall()]
        if "download_error" not in cols:
            c.execute("ALTER TABLE episodes ADD COLUMN download_error INTEGER DEFAULT 0")
        
        # Processing status table to track currently processing series
        c.execute('''
            CREATE TABLE IF NOT EXISTS processing_status (
                subscription_id INTEGER PRIMARY KEY,
                status TEXT, -- 'caching', 'processing', 'idle'
                started_at DATETIME,
                FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
            )
    ''')
        
        conn.commit()
        conn.close()
        return True, None
    except sqlite3.OperationalError as e:
        error_msg = f"Database structure error: {str(e)}. The database may need to be migrated."
        logging.error(error_msg)
        return False, error_msg
    except Exception as e:
        error_msg = f"Database initialization error: {str(e)}"
        logging.error(error_msg)
        return False, error_msg

# Initialize DB on startup
DB_INIT_ERROR = None
if not os.path.exists('/app/db'):
    os.makedirs('/app/db')
success, error = init_db()
if not success:
    DB_INIT_ERROR = error

def get_setting(key, default=None):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,)
        ).fetchone()
        if not row or row["value"] is None:
            return default
        return row["value"]
    finally:
        conn.close()

DEFAULT_MAX_CONCURRENT_JOBS = 4
MAX_CONCURRENT_JOBS = int(get_setting("max_concurrent_jobs", DEFAULT_MAX_CONCURRENT_JOBS))

def send_discord_webhook(event_type, title, description, color=0x5865F2, fields=None, thumbnail=None):
    """
    Send a Discord webhook notification with an embed.
    
    Args:
        event_type: Type of event (new_episode, download_start, download_complete, download_failed)
        title: Embed title
        description: Embed description
        color: Embed color (hex integer, default is Discord blurple)
        fields: List of dicts with 'name' and 'value' keys for embed fields
        thumbnail: URL for embed thumbnail
    """
    webhook_url = get_setting("discord_webhook_url", "")
    if not webhook_url or not webhook_url.strip():
        return
    
    # Check if this event type should be notified
    notify_map = {
        "new_episode": get_setting("discord_notify_new_episode", "1") == "1",
        "download_start": get_setting("discord_notify_download_start", "0") == "1",
        "download_complete": get_setting("discord_notify_download_complete", "1") == "1",
        "download_failed": get_setting("discord_notify_download_failed", "1") == "1",
        "series_added": get_setting("discord_notify_series_added", "1") == "1",
    }
    
    if not notify_map.get(event_type, False):
        return
    
    # Ensure fields don't exceed Discord's limits
    validated_fields = None
    if fields:
        # Discord field limits: name max 256, value max 1024
        validated_fields = []
        for field in fields[:25]:  # Discord allows max 25 fields
            field_name = str(field.get("name", ""))[:256].strip()
            field_value = str(field.get("value", ""))[:1024]
            # Only add fields with non-empty name and value
            if field_name and field_value:
                validated_fields.append({
                    "name": field_name,
                    "value": field_value,
                    "inline": bool(field.get("inline", False))
                })
        if not validated_fields:
            validated_fields = None
    
    # Build embed - Discord requires at least title or description
    embed = {
        "color": int(color),
    }
    
    # Add title if provided (max 256 chars)
    if title and str(title).strip():
        embed["title"] = str(title)[:256]
    
    # Add description if provided (max 4096 chars)
    if description and str(description).strip():
        embed["description"] = str(description)[:4096]
    
    # Add timestamp in ISO 8601 format with Z suffix (Discord requirement)
    # Format: 2022-09-27T18:00:00.000Z
    now_utc = datetime.now(timezone.utc)
    timestamp_str = now_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    embed["timestamp"] = timestamp_str
    
    # Add fields if any
    if validated_fields:
        embed["fields"] = validated_fields
    
    # Add thumbnail if valid URL
    if thumbnail and str(thumbnail).strip():
        thumb_url = str(thumbnail).strip()
        # Basic URL validation
        if thumb_url.startswith(("http://", "https://")):
            embed["thumbnail"] = {"url": thumb_url}
    
    # Discord requires at least title or description in embed
    if "title" not in embed and "description" not in embed:
        logging.warning(f"Cannot send Discord webhook ({event_type}): embed must have title or description")
        return
    
    payload = {
        "embeds": [embed]
    }
    
    try:
        response = requests.post(webhook_url.strip(), json=payload, timeout=5)
        response.raise_for_status()
        logging.debug(f"Discord webhook sent successfully for event: {event_type}")
    except requests.exceptions.HTTPError as e:
        # Log response body for debugging 400 errors
        error_detail = ""
        try:
            if hasattr(e.response, 'text'):
                error_detail = f" - Response: {e.response.text[:200]}"
        except:
            pass
        logging.warning(f"Failed to send Discord webhook ({event_type}): {e}{error_detail}")
    except requests.exceptions.RequestException as e:
        logging.warning(f"Failed to send Discord webhook ({event_type}): {e}")
    except Exception as e:
        logging.warning(f"Unexpected error sending Discord webhook ({event_type}): {e}")

def set_setting(key, value):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value) if value is not None else None)
        )
        conn.commit()
    finally:
        conn.close()


# --- IMAGE CACHING/PROXYING ---
def cache_image(image_url):
    """
    Downloads an image from a URL, saves it locally, and returns the local path.
    Uses the URL hash as the filename to prevent duplicates.
    Checks for existing cached files before downloading.
    """
    if not image_url:
        return None

    url_hash = hashlib.md5(image_url.encode('utf-8')).hexdigest()
    
    # Check if already cached with any extension (check all extensions first)
    for ext in ['.jpg', '.png', '.gif']:
        potential_path = os.path.join(CACHE_DIR, f"{url_hash}{ext}")
        if os.path.exists(potential_path):
            logging.debug(f"Using cached image: {potential_path}")
            return f"{url_hash}{ext}"
    
    # Not cached, proceed to download
    filename = f"{url_hash}.jpg"  # Default filename (will be updated based on content type)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0 Safari/537.36"
        ),
        "Referer": "https://www.webtoons.com/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    requests.packages.urllib3.disable_warnings(
        requests.packages.urllib3.exceptions.InsecureRequestWarning
    )

    try:
        r = requests.get(image_url, headers=headers, verify=False, timeout=10)
        r.raise_for_status()

        content_type = r.headers.get('Content-Type', 'image/jpeg')
        if 'image/png' in content_type:
            filename = f"{url_hash}.png"
        elif 'image/gif' in content_type:
            filename = f"{url_hash}.gif"
        else:
            filename = f"{url_hash}.jpg"

        local_path = os.path.join(CACHE_DIR, filename)
        with open(local_path, 'wb') as f:
            f.write(r.content)

        logging.info(f"Cached image: {image_url} -> {local_path}")
        return filename

    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to cache image {image_url}: {e}")
        return None

@app.route('/api/proxy_image/<filename>')
def proxy_image(filename):
    """Serves the cached image file."""
    try:
        return send_from_directory(CACHE_DIR, filename)
    except Exception as e:
        logging.error(f"Error serving cached image {filename}: {e}")
        return jsonify({"status": "error", "message": "File not found"}), 404

# --- SCRAPER HELPER ---
def scrape_webtoon_meta(url):
    """Scrapes series title, artist, and thumbnail from a Webtoon series list page.

    Returns a dict: {"title": ..., "artist": ..., "thumbnail": ...}
    or None on failure. This version is tuned against the sample HTML pages you
    sent (Castle Swimmer, Avatar, Batman: WFA, KAI, K I D D O S, Summer Boo, Jackson's Diary)
    and is intentionally conservative so we only return None on a true failure.
    """
    if not url or not url.startswith("http"):
        logging.error("Invalid URL passed to scraper.")
        return None

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    requests.packages.urllib3.disable_warnings(
        requests.packages.urllib3.exceptions.InsecureRequestWarning
    )

    # --- Fetch page ---
    try:
        resp = requests.get(url, headers=headers, timeout=15, verify=False)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        logging.error(f"Request Error during series scrape: {e}")
        return None

    try:
        soup = BeautifulSoup(resp.content, "html.parser")

        # ----------------------
        # Title
        # ----------------------
        title = None

        # 1) Prefer Open Graph title (stable across all your examples)
        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            title = og_title["content"].strip()

        # 2) Fall back to the main H1 on the page
        if not title:
            h1 = soup.find("h1")
            if h1:
                # Use a separator so nested spans don't get crushed together
                title = h1.get_text(" ", strip=True)

        # 3) Fall back to the <title> element
        if not title and soup.title and soup.title.string:
            raw_title = soup.title.string.strip()
            title = raw_title.split("|")[0].strip() if "|" in raw_title else raw_title

        if not title:
            logging.error(f"Could not extract series title from page: {url}")
            return None

        # Normalize whitespace and fix missing spaces after colons
        title = re.sub(r"\s+", " ", title).strip()
        # Ensure "Batman:Wayne" becomes "Batman: Wayne"
        title = re.sub(r":(?=\S)", ": ", title)
        title = re.sub(r"\s{2,}", " ", title)
        
        # Decode any HTML entities in the title (e.g., &amp;, &acute;, etc.)
        title = html.unescape(title)


        # ----------------------
        # Artist
        # ----------------------
        artist = None

        # 1) Reliable source: meta property="com-linewebtoon:webtoon:author"
        meta_author = soup.find(
            "meta", attrs={"property": "com-linewebtoon:webtoon:author"}
        )
        if meta_author and meta_author.get("content"):
            artist = meta_author["content"].strip()

        # 2) Fallback: detail_header author_area (covers older layouts)
        if not artist:
            author_area = soup.select_one(".detail_header .author_area")
            if author_area:
                # Prefer explicit links inside the author_area
                link_texts = [
                    a.get_text(" ", strip=True)
                    for a in author_area.find_all("a")
                    if a.get_text(strip=True)
                ]
                if link_texts:
                    # If multiple anchors, join with " / "
                    artist = " / ".join(link_texts)
                else:
                    text = author_area.get_text(" ", strip=True)
                    text = text.replace("author info", "").strip(" -|·")
                    if text:
                        artist = text

        # 3) Last-ditch: any <a class="author"> on the page
        if not artist:
            a_tag = soup.find("a", class_="author")
            if a_tag:
                artist = a_tag.get_text(" ", strip=True)

        if not artist:
            artist = "Unknown"

        # Decode HTML entities in the artist (e.g., Nico&acute;sinmyfeelings)
        artist = html.unescape(artist)
        artist = re.sub(r"\s+", " ", artist).strip()


        # ----------------------
        # Thumbnail
        # ----------------------
        thumbnail = None

        # 1) Prefer Open Graph image (clean series icon / cover)
        og_image = soup.find("meta", attrs={"property": "og:image"})
        if og_image and og_image.get("content"):
            thumbnail = og_image["content"].strip()

        # 2) Fallback: main header thumbnail (if og:image missing)
        if not thumbnail:
            img_tag = soup.select_one(".detail_header .thmb img")
            if img_tag and img_tag.get("src"):
                thumbnail = img_tag["src"].strip()

        if not thumbnail:
            logging.error(f"Could not extract thumbnail image from page: {url}")
            return None

        # Cache the series thumbnail and build proxied URL
        cached_series_thumb = cache_image(thumbnail)
        display_thumbnail_url = (
            f"/api/proxy_image/{cached_series_thumb}" if cached_series_thumb else thumbnail
        )

        return {
            "title": title,
            "artist": artist,
            "thumbnail": display_thumbnail_url,
        }

    except Exception as e:
        logging.exception(f"General Error during series scrape: {e}")
        return None



def safe_insert_episode(c, conn, subscription_id, title, url, ep_num, thumbnail, cached_thumbnail, published_date):
    """Insert episode only if that URL for the subscription does not already exist.
    This avoids requiring a DB migration for a UNIQUE constraint while preventing duplicates."""
    try:
        exists = c.execute(
            "SELECT id FROM episodes WHERE subscription_id = ? AND url = ?",
            (subscription_id, url)
        ).fetchone()
        if exists:
            return False
        c.execute(
            "INSERT INTO episodes (subscription_id, title, url, ep_num, thumbnail, cached_thumbnail, published_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (subscription_id, title, url, ep_num, thumbnail, cached_thumbnail, published_date)
        )
        conn.commit()
        return True
    except Exception:
        logging.exception("safe_insert_episode error")
        return False

def scrape_episodes(sub_id, series_url, limit=None, first_page_only=False, force_rescan=False):
    """
    Scrape episodes for a series and cache their thumbnails.

    - Walks pages until it either:
      * hits an empty page, or
      * sees a duplicate episode URL (looped past last page), or
      * reaches an optional `limit`, or
      * `first_page_only` is True (stops after first page).
    - Caches each episode thumbnail locally.
    - Updates the subscription's `last_updated`.
    - Updates both:
        - subscription-level progress (for processing cards)
        - URL-level progress (for the add-subscription modal)
    """

    # Ensure only ONE series is being scraped at a time (global)
    with SCRAPE_LOCK:
        logging.info(f"Scraping episodes for sub_id={sub_id}")

        conn = None
        highest_ep_num = None   # Track highest episode number for progress
        current_ep_num = None   # Track current episode being processed

        try:
            conn = get_db()
            c = conn.cursor()
            last_updated_set = False

            headers = {"User-Agent": "Mozilla/5.0"}

            page = 1
            scraped = 0
            seen_urls = set()

            # Mark this subscription as processing so any device can show a processing card
            update_processing_status(
                sub_id,
                status="caching",
                title="Processing series...",
                subtitle="Starting episode scrape",
                progress=0.0,
            )

            while True:
                logging.info(f"[SCRAPER] Scraping page {page} for sub_id={sub_id}")

                paged_url = (
                    f"{series_url}&page={page}"
                    if "?" in series_url
                    else f"{series_url}?page={page}"
                )

                # Retry logic for network issues
                max_retries = 3
                r = None
                for retry in range(max_retries):
                    try:
                        r = requests.get(paged_url, headers=headers, timeout=30)
                        if r.status_code == 200:
                            break
                        elif retry < max_retries - 1:
                            logging.warning(f"[SCRAPER] Page {page} returned {r.status_code}, retrying ({retry + 1}/{max_retries})...")
                            time.sleep(2)  # Wait before retry
                    except Exception as e:
                        if retry < max_retries - 1:
                            logging.warning(f"[SCRAPER] Error fetching page {page}: {e}, retrying ({retry + 1}/{max_retries})...")
                            time.sleep(2)
                        else:
                            logging.error(f"[SCRAPER] Failed to fetch page {page} after {max_retries} attempts: {e}")
                            raise

                if not r or r.status_code != 200:
                    logging.warning(
                        f"[SCRAPER] Page {page} returned {r.status_code if r else 'None'}, stopping."
                    )
                    break

                soup = BeautifulSoup(r.text, "html.parser")
                episode_items = soup.select("li[id^='episode_']")

                if not episode_items:
                    # For initial scrape (not first_page_only), check if we've seen episodes before
                    # If this is page 1 and we have no episodes, something is wrong
                    # If this is a later page, it might be the end
                    if first_page_only or page == 1:
                        logging.warning(
                            f"[SCRAPER] No episodes found on page {page} (first_page_only={first_page_only}), stopping."
                        )
                        break
                    else:
                        # For later pages, check if we've scraped any episodes yet
                        # If we have, this might be the end. If not, it's an error.
                        if scraped == 0:
                            logging.warning(
                                f"[SCRAPER] No episodes found on page {page} and no episodes scraped yet, stopping."
                            )
                            break
                        else:
                            logging.info(
                                f"[SCRAPER] No episodes found on page {page}, but {scraped} episodes already scraped. "
                                f"Assuming end of series."
                            )
                            break

                new_items_on_page = 0
                duplicate_detected = False

                for item in episode_items:
                    link_tag = item.select_one("a")
                    title_tag = item.select_one("span.subj")
                    thumb_tag = item.select_one("img")

                    if not link_tag or not title_tag:
                        continue

                    link = link_tag.get("href")
                    if not link:
                        continue

                    # Stop if we loop past the last page and see duplicates
                    # During force_rescan, we still want to stop on duplicates (means we've looped)
                    if link in seen_urls:
                        logging.info(
                            f"[SCRAPER] Duplicate episode detected. "
                            f"Stopping at page {page}."
                        )
                        duplicate_detected = True
                        break

                    seen_urls.add(link)

                    # Get text but exclude elements with "tx_up" and "ico_bgm" classes (usually update indicators)
                    # Remove these elements from the title tag before extracting text
                    title_tag_copy = BeautifulSoup(str(title_tag), "html.parser")
                    for elem in title_tag_copy.select(".tx_up, .ico_bgm"):
                        elem.decompose()
                    subj = title_tag_copy.get_text(strip=True)

                    ep_match = re.search(r"episode_no=(\d+)", link)
                    if not ep_match:
                        continue
                    ep_num = int(ep_match.group(1))

                    # Track highest episode number (first episode scraped is
                    # usually the latest/highest)
                    if highest_ep_num is None or ep_num > highest_ep_num:
                        highest_ep_num = ep_num
                    current_ep_num = ep_num

                    original_thumb_url = thumb_tag["src"] if thumb_tag else None

                    # Date from a > span.date (as Webtoon displays it)
                    published_date = None
                    date_span = item.select_one("a > span.date") or item.select_one(
                        "span.date"
                    )
                    if date_span:
                        raw_date = date_span.get_text(strip=True)
                        if raw_date:
                            published_date = raw_date

                    # --- progress calculation ---
                    progress = 0.0
                    if highest_ep_num and highest_ep_num > 0 and current_ep_num:
                        # Episode 100 (highest): (100-100+1)/100 = 1% (just started)
                        # Episode 50: (100-50+1)/100 = 51% (halfway)
                        # Episode 1:  (100-1+1)/100   = 100% (done)
                        progress = max(
                            0.0,
                            min(
                                1.0,
                                (highest_ep_num - current_ep_num + 1)
                                / highest_ep_num,
                            ),
                        )

                    # Subscription-level progress (processing cards)
                    update_processing_status(
                        sub_id,
                        status="caching",
                        title="Processing series...",
                        subtitle=f"Caching – Episode {ep_num}, Page {page}",
                        progress=progress,
                        current_episode=current_ep_num,
                        total_episodes=highest_ep_num,
                    )

                    # Episode-level progress keyed by series URL (add modal)
                    update_processing_status(
                        series_url,
                        status="caching",
                        page=page,
                        episode=ep_num,
                        title=subj,
                        progress=progress,
                        current_episode=current_ep_num,
                        total_episodes=highest_ep_num,
                    )

                    cached_thumb_url = None
                    if original_thumb_url:
                        logging.info(
                            f"[CACHE] EP {ep_num} \u2192 caching thumbnail (page {page})"
                        )
                        cached_thumb_url = cache_image(original_thumb_url)

                    # --- insert into episodes including published_date ---
                    inserted = safe_insert_episode(
                        c,
                        conn,
                        sub_id,
                        subj,
                        link,
                        ep_num,
                        original_thumb_url,
                        cached_thumb_url,
                        published_date,
                    )
                    if inserted:
                        new_items_on_page += 1
                        scraped += 1

                        # Update subscription.last_updated once (if present)
                        if published_date and not last_updated_set:
                            try:
                                c.execute(
                                    "UPDATE subscriptions "
                                    "SET last_updated = ? WHERE id = ?",
                                    (published_date, sub_id),
                                )
                                conn.commit()
                                last_updated_set = True
                            except Exception:
                                logging.exception(
                                    "Failed to update subscriptions.last_updated"
                                )

                    if limit and scraped >= limit:
                        break

                # ----- end for item -----

                if limit and scraped >= limit:
                    logging.info(
                        f"[SCRAPER] Reached limit {limit}. "
                        f"Stopping scrape for sub_id={sub_id}."
                    )
                    break

                if duplicate_detected:
                    break

                # During a force_rescan, continue checking pages even if no new episodes found
                # (to find any missing episodes that might be on later pages)
                if new_items_on_page == 0 and not force_rescan:
                    logging.info(
                        f"[SCRAPER] No NEW episodes on page {page}. "
                        f"Stopping for sub_id={sub_id}."
                    )
                    break
                elif new_items_on_page == 0 and force_rescan:
                    logging.info(
                        f"[SCRAPER] No NEW episodes on page {page}, but continuing (force_rescan mode) for sub_id={sub_id}."
                    )

                # If first_page_only is True, stop after processing the first page
                if first_page_only:
                    logging.info(f"[SCRAPER] First page only mode: stopping after page {page}")
                    break

                page += 1

                # update progress to reflect next page we're about to scrape
                update_processing_status(
                    sub_id,
                    status="caching",
                    title="Processing series...",
                    subtitle=f"Scanning page {page}",
                )
                update_processing_status(
                    series_url,
                    status="caching",
                    page=page,
                )

            # Final safety sync of last_updated from the episodes table
            try:
                c.execute(
                    """
                    UPDATE subscriptions
                    SET last_updated = COALESCE(
                        (
                            SELECT published_date
                            FROM episodes
                            WHERE subscription_id = ?
                              AND published_date IS NOT NULL
                            ORDER BY ep_num DESC
                            LIMIT 1
                        ),
                        datetime('now')
                    )
                    WHERE id = ?
                    """,
                    (sub_id, sub_id),
                )
                conn.commit()
            except Exception:
                logging.exception("Failed to sync subscriptions.last_updated")
            
            # Check if we should download all episodes after caching (before closing connection)
            # Use a transaction to atomically check and clear the flag to prevent duplicate triggers
            download_all_flag = False
            if sub_id:
                try:
                    # Use a transaction with row-level locking to prevent race conditions
                    c.execute("BEGIN IMMEDIATE")
                    settings_row = c.execute(
                        "SELECT download_all_after_cache FROM series_settings WHERE subscription_id = ?",
                        (sub_id,)
                    ).fetchone()
                    
                    # Check if row exists and flag is set (SQLite stores booleans as integers: 0 or 1)
                    if settings_row is not None:
                        flag_value = settings_row["download_all_after_cache"]
                        # Explicitly check for 1 or True (SQLite stores as integer)
                        if flag_value == 1 or flag_value is True:
                            download_all_flag = True
                            # Clear the flag immediately to prevent re-triggering
                            c.execute(
                                "UPDATE series_settings SET download_all_after_cache = 0 WHERE subscription_id = ?",
                                (sub_id,)
                            )
                            conn.commit()
                            logging.info(f"[SCRAPER] download_all_after_cache flag set for sub_id={sub_id}, will trigger download of all episodes")
                        else:
                            conn.commit()
                            logging.debug(f"[SCRAPER] download_all_after_cache flag is {flag_value} (not set) for sub_id={sub_id}")
                    else:
                        conn.commit()
                        logging.debug(f"[SCRAPER] No series_settings row found for sub_id={sub_id}, skipping download_all")
                except (KeyError, TypeError, IndexError) as e:
                    # Row doesn't exist or column doesn't exist - that's fine, just skip
                    logging.debug(f"[SCRAPER] Exception checking download_all_after_cache for sub_id={sub_id}: {e}")
                    if conn:
                        try:
                            conn.rollback()
                        except:
                            pass
                except Exception as e:
                    logging.exception(f"[SCRAPER] Error checking download_all_after_cache for sub_id={sub_id}: {e}")
                    if conn:
                        try:
                            conn.rollback()
                        except:
                            pass

        except Exception as e:
            logging.exception(f"Error in scrape_episodes for sub_id={sub_id}: {e}")
            raise
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    logging.exception(
                        "Error closing database connection in scrape_episodes"
                    )

            logging.info(f"[SCRAPER] Finished. Total episodes scraped: {scraped}")
            
            # Clear progress for URL-level (used by add modal)
            update_processing_status(series_url, status="idle")
            
            # Trigger download of all episodes if flag was set (after connection is closed)
            # Use a thread-safe check to prevent duplicate triggers
            if download_all_flag and sub_id:
                logging.info(f"[SCRAPER] download_all_flag is True for sub_id={sub_id}, starting trigger_download_all thread")
                # Update status to show we're preparing the download (don't set to idle yet)
                update_processing_status(
                    sub_id,
                    status="processing",
                    title="Preparing download...",
                    subtitle="Initializing download of all episodes"
                )
                
                # Double-check the flag is still cleared to prevent race conditions
                def trigger_download_all():
                    global DOWNLOAD_ALL_IN_PROGRESS
                    
                    logging.info(f"[SCRAPER] trigger_download_all function started for sub_id={sub_id}")
                    
                    # Use a lock to prevent concurrent triggers for the same subscription
                    with DOWNLOAD_ALL_LOCK:
                        if sub_id in DOWNLOAD_ALL_IN_PROGRESS:
                            logging.warning(f"[SCRAPER] download_all already in progress for sub_id={sub_id}, skipping duplicate trigger")
                            return
                        DOWNLOAD_ALL_IN_PROGRESS.add(sub_id)
                        logging.info(f"[SCRAPER] Added sub_id={sub_id} to DOWNLOAD_ALL_IN_PROGRESS")
                    
                    try:
                        # Small delay to ensure database transaction is fully committed
                        time.sleep(0.5)
                        
                        # Get a new connection for the download
                        conn_dl = get_db()
                        try:
                            c_dl = conn_dl.cursor()
                            
                            # Verify the flag is still cleared (double-check)
                            flag_check = c_dl.execute(
                                "SELECT download_all_after_cache FROM series_settings WHERE subscription_id = ?",
                                (sub_id,)
                            ).fetchone()
                            
                            if flag_check and flag_check["download_all_after_cache"]:
                                # Flag was somehow set again - clear it and skip to prevent duplicate
                                logging.warning(f"[SCRAPER] download_all_after_cache flag was re-set for sub_id={sub_id}, clearing and skipping to prevent duplicate")
                                c_dl.execute(
                                    "UPDATE series_settings SET download_all_after_cache = 0 WHERE subscription_id = ?",
                                    (sub_id,)
                                )
                                conn_dl.commit()
                                return
                            
                            sub_info = c_dl.execute(
                                "SELECT title, url FROM subscriptions WHERE id = ?",
                                (sub_id,)
                            ).fetchone()
                            
                            if sub_info:
                                # Get the max episode number from the database
                                max_ep_row = c_dl.execute(
                                    "SELECT MAX(ep_num) AS max_ep FROM episodes WHERE subscription_id = ?",
                                    (sub_id,)
                                ).fetchone()
                                
                                max_ep = None
                                if max_ep_row and max_ep_row["max_ep"] is not None:
                                    max_ep = int(max_ep_row["max_ep"])
                                
                                # Create download request payload
                                download_payload = {
                                    "url": sub_info["url"],
                                    "subscription_id": sub_id,
                                    "format": get_setting("format", "images"),
                                    "path_template": get_setting("path_template", PATH_TEMPLATE_DEFAULT)
                                }
                                
                                # Add start and end episode numbers if we have episodes
                                if max_ep is not None and max_ep > 0:
                                    download_payload["start"] = 1
                                    download_payload["end"] = max_ep
                                    logging.info(f"[SCRAPER] Triggering download of all episodes (1-{max_ep}) for sub_id={sub_id}")
                                else:
                                    logging.warning(f"[SCRAPER] No episodes found for sub_id={sub_id}, skipping download_all")
                                    # Remove from in-progress set before returning
                                    with DOWNLOAD_ALL_LOCK:
                                        DOWNLOAD_ALL_IN_PROGRESS.discard(sub_id)
                                    return
                                
                                # Remove from DOWNLOAD_ALL_IN_PROGRESS before calling start_download()
                                # This allows start_download() to proceed, and it will track the job in PROCESSES instead
                                with DOWNLOAD_ALL_LOCK:
                                    DOWNLOAD_ALL_IN_PROGRESS.discard(sub_id)
                                    logging.info(f"[SCRAPER] Removed sub_id={sub_id} from DOWNLOAD_ALL_IN_PROGRESS before calling start_download()")
                                
                                # Create a download request for all episodes
                                logging.info(f"[SCRAPER] Calling start_download() with payload: {download_payload}")
                                try:
                                    with app.test_request_context(json=download_payload):
                                        result = start_download()
                                        logging.info(f"[SCRAPER] start_download() returned: {result}")
                                    # Download started successfully - the job will update PROCESSES with job_id
                                    # The frontend will see this when it polls
                                except Exception as dl_err:
                                    logging.exception(f"[SCRAPER] Error calling start_download() for sub_id={sub_id}: {dl_err}")
                                    # On error, clear processing status
                                    update_processing_status(sub_id, status="idle")
                                    raise
                                logging.info(f"[SCRAPER] Successfully triggered download of all episodes for sub_id={sub_id}")
                        finally:
                            conn_dl.close()
                    except Exception as e:
                        logging.exception(f"[SCRAPER] Error triggering download_all for sub_id={sub_id}: {e}")
                        # Remove from in-progress set on error
                        with DOWNLOAD_ALL_LOCK:
                            DOWNLOAD_ALL_IN_PROGRESS.discard(sub_id)
                        # Clear processing status on error
                        update_processing_status(sub_id, status="idle")
                    # Note: We don't need a finally block here because we already removed it before calling start_download()
                
                # Run in background thread with a small delay to ensure transaction is committed
                threading.Thread(target=trigger_download_all, daemon=True).start()
            else:
                # No download_all flag - clear progress normally
                update_processing_status(sub_id, status="idle")

# --- DOWNLOAD WORKER ---
def run_download_process(job_id, cmd, subscription_id, format_choice, separate, downloaded_latest, start_ch, end_ch, path_template_override=None):
    global PROCESSES, EPISODE_PROGRESS

    conn = get_db()
    conn.row_factory = dict_factory
    c = conn.cursor()

    # Fetch the job row
    c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    job = c.fetchone()
    if not job:
        logging.error(f"Job {job_id} not found in DB.")
        return

    # Normalize cmd (it may be stored as JSON string or list)
    if isinstance(cmd, str):
        try:
            cmd = json.loads(cmd)
        except Exception:
            # Fall back: treat as a single shell string
            cmd = [cmd]

    # Mark job as running (use UTC time for consistency)
    from datetime import timezone
    c.execute(
        "UPDATE jobs SET status = ?, started_at = ? WHERE id = ?",
        ("running", datetime.now(timezone.utc).isoformat(), job_id),
    )
    conn.commit()

    # Track that this subscription has a running job
    if subscription_id is not None:
        PROCESSES[subscription_id] = {"job_id": job_id}
        # Update processing status to show download is running
        update_processing_status(
            subscription_id,
            status="processing",
            title="Downloading episodes...",
            subtitle=f"Job #{job_id} in progress"
        )

    logging.info(f"Starting command: {' '.join(cmd)}")
    LOG_QUEUE.put(f"[Job {job_id}] Starting: {' '.join(cmd)}")
    
    # Send Discord webhook for download start
    if subscription_id:
        c.execute("SELECT title, artist, thumbnail FROM subscriptions WHERE id = ?", (subscription_id,))
        sub_row = c.fetchone()
        if sub_row:
            series_title = sub_row["title"] or "Unknown Series"
            series_artist = sub_row["artist"] or "Unknown Artist"
            thumbnail_url = sub_row.get("thumbnail")
            
            # Get episode info if available
            episode_info = ""
            if start_ch is not None and end_ch is not None:
                if start_ch == end_ch:
                    ep_row = c.execute(
                        "SELECT title FROM episodes WHERE subscription_id = ? AND ep_num = ?",
                        (subscription_id, start_ch)
                    ).fetchone()
                    if ep_row and ep_row.get("title"):
                        episode_info = f"Episode {start_ch}: {ep_row['title']}"
                    else:
                        episode_info = f"Episode {start_ch}"
                else:
                    episode_info = f"Episodes {start_ch}-{end_ch}"
            
            send_discord_webhook(
                event_type="download_start",
                title="📥 Download Started",
                description=f"**{series_title}**\nby {series_artist}",
                color=0x3498db,  # Blue
                fields=[
                    {"name": "Episode(s)", "value": episode_info or "All episodes", "inline": True},
                    {"name": "Job ID", "value": str(job_id), "inline": True},
                    {"name": "Status", "value": "Starting download...", "inline": False}
                ],
                thumbnail=thumbnail_url
            )

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )
    
    # Store the process in PROCESSES for cancellation
    with PROCESS_LOCK:
        if subscription_id is not None:
            PROCESSES[subscription_id] = {"job_id": job_id, "process": proc, "subscription_id": subscription_id}
        # Also store by job_id for easier lookup
        PROCESSES[job_id] = {"job_id": job_id, "process": proc, "subscription_id": subscription_id}

    # Stream output in real-time
    stdout_lines = []
    try:
        for line in iter(proc.stdout.readline, ''):
            if line:
                line = line.rstrip()
                if line:
                    stdout_lines.append(line)
                    # Send to log queue immediately
                    LOG_QUEUE.put(f"[Job {job_id}] {line}")
        proc.wait()
    except Exception as e:
        logging.error(f"Error reading subprocess output: {e}")
        LOG_QUEUE.put(f"[Job {job_id}] Error reading output: {e}")
        try:
            proc.wait()
        except:
            pass
    
    retcode = proc.returncode
    stdout = '\n'.join(stdout_lines)
    
    logging.info(f"Job {job_id} finished with code {retcode}")
    logging.info(f"Job {job_id} stdout:\n{stdout}")
    
    if retcode == 0:
        LOG_QUEUE.put(f"[Job {job_id}] ✓ Completed successfully")
    else:
        LOG_QUEUE.put(f"[Job {job_id}] ✗ Failed with exit code {retcode}")

    # Check for download errors in stdout (even if exit code is 0)
    has_download_error = "Download error" in stdout or "cannot identify image file" in stdout or "Failed to download" in stdout
    
    # Check if we got "Download complete!" message
    has_complete_message = "Download complete!" in stdout
    
    # Determine expected vs actual episodes downloaded
    expected_episodes = None
    if start_ch is not None and end_ch is not None:
        expected_episodes = end_ch - start_ch + 1
    elif downloaded_latest:
        expected_episodes = 1
    
    # Determine success: exit code 0, has complete message, no download errors
    success = (retcode == 0 and has_complete_message and not has_download_error)
    
    errors_encountered = False
    status = "completed" if success else "failed"
    
    # Initialize actual_episodes_downloaded for summary message
    actual_episodes_downloaded = 0

    # --- Mark episodes as downloaded / error flags -------------------------
    if subscription_id is not None:
        # Determine which episodes this job was meant to download
        # (when downloaded_latest is True, this is usually just the latest ep)
        query = """
            SELECT id, ep_num
            FROM episodes
            WHERE subscription_id = ?
        """
        params = [subscription_id]

        if downloaded_latest:
            # Get the latest episode (highest ep_num) for this subscription
            query = """
                SELECT id, ep_num
                FROM episodes
                WHERE subscription_id = ?
                ORDER BY ep_num DESC
                LIMIT 1
            """
            params = [subscription_id]
        else:
            if start_ch is not None:
                query += " AND ep_num >= ?"
                params.append(start_ch)
            if end_ch is not None:
                query += " AND ep_num <= ?"
                params.append(end_ch)

        c.execute(query, params)
        eps = c.fetchall()

        if eps:
            if success:
                # Mark all targeted episodes as downloaded and clear any error flag
                ep_ids = [e["id"] for e in eps]
                c.execute(
                    f"""
                    UPDATE episodes
                    SET downloaded = 1,
                        download_error = 0
                    WHERE id IN ({','.join('?' for _ in ep_ids)})
                    """,
                    ep_ids,
                )
            else:
                # Job failed → mark targeted episodes with error flag
                ep_ids = [e["id"] for e in eps]
                c.execute(
                    f"""
                    UPDATE episodes
                    SET download_error = 1
                    WHERE id IN ({','.join('?' for _ in ep_ids)})
                    """,
                    ep_ids,
                )
                errors_encountered = True
        conn.commit()
        
        # Count actual episodes downloaded AFTER marking them in database
        # (files are moved during post-processing, so temp directory count is unreliable)
        actual_episodes_downloaded = 0
        if expected_episodes and expected_episodes > 1:
            try:
                # Count episodes that were actually marked as downloaded for this subscription
                # in the expected range
                count_query = """
                    SELECT COUNT(*) as cnt FROM episodes
                    WHERE subscription_id = ? AND downloaded = 1
                """
                count_params = [subscription_id]
                
                if start_ch is not None:
                    count_query += " AND ep_num >= ?"
                    count_params.append(start_ch)
                if end_ch is not None:
                    count_query += " AND ep_num <= ?"
                    count_params.append(end_ch)
                
                count_row = c.execute(count_query, count_params).fetchone()
                if count_row:
                    actual_episodes_downloaded = count_row.get("cnt", 0) or 0
                
                # If we expected multiple episodes but got fewer than expected, check if it's a failure
                # Only mark as failure if we have download errors OR got significantly fewer (less than 50%)
                if has_download_error and actual_episodes_downloaded < expected_episodes:
                    # Has errors and partial download = failure
                    success = False
                    status = "failed"
                elif not has_download_error and actual_episodes_downloaded < expected_episodes:
                    # No errors but partial download - might be legitimate (some episodes might not exist)
                    # Only mark as failure if we got significantly fewer (less than 50% of expected)
                    if actual_episodes_downloaded < (expected_episodes * 0.5):
                        success = False
                        has_download_error = True
                        status = "failed"
            except Exception as e:
                logging.exception(f"Error counting downloaded episodes: {e}")
                # If counting fails, don't change success status based on count

    # --- POST-PROCESSING: move files from tmp/job_<id> into final path ------
    try:
        # Derive the temp directory for this job purely from the job id
        job_tmp_dir = os.path.join(DOWNLOAD_DIR, "tmp", f"job_{job_id}")

        if not os.path.isdir(job_tmp_dir):
            logging.warning(f"[POST] Temp dir not found for job {job_id}: {job_tmp_dir}")
            if success:
                # We expected files but didn't find them → warning
                errors_encountered = True
                status = "warning"
        else:
            # Figure out effective format + path template
            # 1) Global settings
            c.execute("SELECT key, value FROM settings")
            global_settings_raw = {row["key"]: row["value"] for row in c.fetchall()}

            global_format = global_settings_raw.get("format", "pdf")
            global_template = global_settings_raw.get("path_template", PATH_TEMPLATE_DEFAULT)

            # 2) Per-series overrides
            sub_row = None
            if subscription_id is not None:
                c.execute("SELECT * FROM subscriptions WHERE id = ?", (subscription_id,))
                sub_row = c.fetchone()

            series_title = (sub_row.get("title") if sub_row else job.get("comic_title")) or "Series"
            series_author = (sub_row.get("artist") if sub_row else job.get("artist")) or "Unknown"

            # Folder/filename template override for this call always wins,
            # then per-series override, then global template.
            template_key = (
                path_template_override
                or (sub_row.get("path_template_override") if sub_row else None)
                or global_template
                or PATH_TEMPLATE_DEFAULT
            )
            
            # Map template keys to actual template strings
            def resolve_template_string(template_key):
                template_map = {
                    "author_series_flat": "[Author]/[Series]/[Episode #] - [Episode Name]",
                    "author_series_folder": "[Author]/[Series]/[Episode #] - [Episode Name]",
                    "series_only_flat": "[Series]/[Episode #]",
                }
                return template_map.get(template_key, "[Author]/[Series]/[Episode #] - [Episode Name]")
            
            template = resolve_template_string(template_key)

            # Format override: per-series or global
            format_override = None
            if sub_row and sub_row.get("format_override"):
                format_override = sub_row["format_override"]

            effective_format = format_override or global_format or "pdf"

            # Download target range (for single-ep jobs we know start_ch == end_ch)
            ep_range = None
            ep_titles_map = {}  # Map ep_num to title for single-episode downloads
            if start_ch is not None and end_ch is not None:
                if start_ch == end_ch:
                    ep_range = start_ch
                    # Get episode title for single-episode downloads
                    if subscription_id:
                        ep_row = c.execute(
                            "SELECT title FROM episodes WHERE subscription_id = ? AND ep_num = ?",
                            (subscription_id, start_ch)
                        ).fetchone()
                        if ep_row:
                            # Use title if available, otherwise use empty string (not None)
                            ep_titles_map[start_ch] = ep_row.get("title") or ""
                else:
                    # Multi-episode download - use range format
                    ep_range = f"{start_ch}-{end_ch}"
                    # For multi-episode downloads, we'll need to extract episode numbers from filenames
                    # Build a map of all episode titles in the range
                    if subscription_id:
                        ep_rows = c.execute(
                            "SELECT ep_num, title FROM episodes WHERE subscription_id = ? AND ep_num >= ? AND ep_num <= ? ORDER BY ep_num",
                            (subscription_id, start_ch, end_ch)
                        ).fetchall()
                        for ep_row in ep_rows:
                            ep_titles_map[ep_row["ep_num"]] = ep_row.get("title") or ""

            # Helper to build final destination path components
            def build_final_path(base_root, author, series, ep_no_or_range, ep_title, fmt):
                safe_author = sanitize_filename(author) or "Unknown"
                safe_series = sanitize_filename(series) or "Series"
                safe_ep_title = sanitize_filename(ep_title) if ep_title else ""
                
                # Get episode naming settings
                ep_format = get_setting("episode_number_format", "number")
                ep_padding = int(get_setting("episode_number_padding", "0") or "0")
                ep_separator = get_setting("episode_separator", " - ")
                ep_include_title = get_setting("episode_include_title", "1") == "1"
                
                # Format episode number based on settings
                def format_episode_number(ep_no):
                    if ep_no is None:
                        return ""
                    
                    try:
                        ep_int = int(ep_no)
                    except (ValueError, TypeError):
                        return str(ep_no)
                    
                    # Apply padding if specified
                    if ep_padding > 0:
                        ep_str = str(ep_int).zfill(ep_padding)
                    else:
                        ep_str = str(ep_int)
                    
                    # Apply format
                    if ep_format == "number":
                        return ep_str
                    elif ep_format == "episode_number":
                        return f"Episode {ep_str}"
                    elif ep_format == "ep_number":
                        return f"Ep {ep_str}"
                    elif ep_format == "e_number":
                        return f"E{ep_str}"
                    elif ep_format == "number_padded":
                        # Already padded above, but ensure at least 3 digits
                        return str(ep_int).zfill(max(3, ep_padding))
                    elif ep_format == "episode_number_padded":
                        return f"Episode {str(ep_int).zfill(max(3, ep_padding))}"
                    elif ep_format == "ep_number_padded":
                        return f"Ep {str(ep_int).zfill(max(3, ep_padding))}"
                    elif ep_format == "e_number_padded":
                        return f"E{str(ep_int).zfill(max(3, ep_padding))}"
                    else:
                        return ep_str
                
                ep_part = format_episode_number(ep_no_or_range) if ep_no_or_range is not None else ""

                # We support:
                #   [Author]/[Series]/[Episode #] - [Episode Name].ext
                #   [Author]/[Series]/[Series] - [Episode #].ext
                #   [Series]/[Series] - [Episode #].ext
                #   [Series]/[Episode #].ext
                #   (and variants without Episode Name)
                segments = []
                filename = None

                if template == "[Author]/[Series]/[Episode #] - [Episode Name]":
                    segments = [safe_author, safe_series]
                    if ep_part:  # We have an episode number
                        if safe_ep_title and ep_include_title:
                            # Use episode number and title with custom separator
                            filename = f"{ep_part}{ep_separator}{safe_ep_title}"
                        else:
                            # No title or title disabled, just use episode number
                            filename = ep_part
                    else:
                        # No episode number, fallback to placeholder
                        filename = "{{name}}"
                elif template == "[Author]/[Series]/[Series] - [Episode #]":
                    segments = [safe_author, safe_series]
                    filename = f"{safe_series} - {ep_part}" if ep_part else safe_series
                elif template == "[Series]/[Series] - [Episode #]":
                    segments = [safe_series]
                    filename = f"{safe_series} - {ep_part}" if ep_part else safe_series
                elif template == "[Series]/[Episode #]":
                    segments = [safe_series]
                    filename = ep_part if ep_part else "{{name}}"
                else:
                    # Fallback to sane default - use episode naming settings
                    segments = [safe_author, safe_series]
                    if ep_part:
                        if safe_ep_title and ep_include_title:
                            filename = f"{ep_part}{ep_separator}{safe_ep_title}"
                        else:
                            filename = ep_part
                    else:
                        # No episode number available - use placeholder
                        filename = "{{name}}"

                # Decide extension by format
                if fmt == "images":
                    ext = ""  # raw image folder, filenames already correct
                elif fmt in ("zip", "cbz"):
                    ext = f".{fmt}"
                else:
                    # default to pdf if not images/zip/cbz
                    ext = ".pdf"

                # Build path; {name} will be replaced by base filename if present
                dest_dir = os.path.join(base_root, *segments)
                if filename:
                    final_name = filename
                else:
                    final_name = "{{name}}"

                return dest_dir, final_name, ext

            # Now walk files in the temp dir and move them
            files = os.listdir(job_tmp_dir)
            if not files:
                logging.warning(f"[POST] No files in temp dir for job {job_id}: {job_tmp_dir}")
                if success:
                    errors_encountered = True
                    status = "warning"
            else:
                # Sort files for consistent processing order (especially important for multi-episode downloads)
                files = sorted([f for f in files if os.path.isfile(os.path.join(job_tmp_dir, f))])
                
                # For multi-episode downloads, match files sequentially to episodes from the database
                # All episode numbers and titles come from the database (already scraped during caching)
                current_ep_index = 0
                episode_list = []
                if start_ch is not None and end_ch is not None and start_ch != end_ch:
                    # Build ordered list of actual episode numbers from database (not just the range)
                    # This ensures we only match to episodes that actually exist
                    if subscription_id:
                        ep_rows = c.execute(
                            "SELECT ep_num FROM episodes WHERE subscription_id = ? AND ep_num >= ? AND ep_num <= ? ORDER BY ep_num ASC",
                            (subscription_id, start_ch, end_ch)
                        ).fetchall()
                        episode_list = [row["ep_num"] for row in ep_rows]
                    else:
                        # Fallback to range if no subscription_id
                        episode_list = list(range(start_ch, end_ch + 1))
                    
                    # Log mismatch between files and episodes for debugging
                    if len(files) != len(episode_list):
                        logging.warning(f"[POST] Mismatch: {len(files)} files but {len(episode_list)} episodes in range {start_ch}-{end_ch} for job {job_id} (subscription_id={subscription_id})")
                        logging.info(f"[POST] Episode list: {episode_list[:10]}{'...' if len(episode_list) > 10 else ''}")
                    else:
                        logging.info(f"[POST] Matched {len(files)} files to {len(episode_list)} episodes for job {job_id}")
                
                for fname in files:
                    src_path = os.path.join(job_tmp_dir, fname)
                    if not os.path.isfile(src_path):
                        continue

                    base_name, ext = os.path.splitext(fname)

                    # Always use database for episode numbers and titles
                    # No filename extraction - database is the source of truth
                    ep_for_name = None
                    ep_title = None
                    
                    if start_ch is not None and end_ch is not None:
                        if start_ch == end_ch:
                            # Single episode download - use episode number and title from database
                            ep_for_name = start_ch
                            if start_ch in ep_titles_map:
                                ep_title = ep_titles_map[start_ch]
                                if ep_title == "":
                                    ep_title = None
                        else:
                            # Multi-episode download - match files sequentially to episodes from database
                            if episode_list and current_ep_index < len(episode_list):
                                ep_for_name = episode_list[current_ep_index]
                                if ep_for_name in ep_titles_map:
                                    ep_title = ep_titles_map[ep_for_name]
                                    if ep_title == "":
                                        ep_title = None
                                current_ep_index += 1
                                logging.info(f"[POST] Matched file '{fname}' to episode {ep_for_name} from database (title: {ep_title or 'N/A'})")
                            else:
                                # All episodes in range have been matched, use range format as fallback
                                ep_for_name = ep_range
                                logging.warning(f"[POST] All episodes in range already matched, using range format for '{fname}'")
                    elif ep_range is not None:
                        # Fallback to range if available
                        ep_for_name = ep_range

                    dest_dir, filename_template, ext_override = build_final_path(
                        DOWNLOAD_DIR, series_author, series_title, ep_for_name, ep_title, effective_format
                    )

                    # For images format, always preserve the original file extension
                    # For other formats, use the format-specific extension
                    if effective_format == "images":
                        # Don't change extensions for image files - preserve original
                        final_ext = ext or ""
                    elif ext_override:
                        # Use format-specific extension (pdf, zip, cbz)
                        final_ext = ext_override
                    else:
                        # Fallback to original extension
                        final_ext = ext or ""

                    # For single-episode downloads, we should use episode number and title from DB
                    # NOT the base_name from webtoon-downloader's filename
                    if "{{name}}" in filename_template:
                        # This should only happen for multi-episode or fallback cases
                        # For single-episode downloads, filename_template should already be correct
                        final_stem = filename_template.replace("{{name}}", base_name)
                    else:
                        # Filename template is already correct (e.g., "43 - Special Announcement!")
                        final_stem = filename_template

                    final_stem = sanitize_filename(final_stem)
                    dest_path = os.path.join(dest_dir, final_stem + final_ext)

                    os.makedirs(dest_dir, exist_ok=True)
                    try:
                        shutil.move(src_path, dest_path)
                        logging.info(f"[POST] Moved '{src_path}' → '{dest_path}'")
                    except Exception as move_err:
                        logging.error(f"[POST] Error moving '{src_path}' → '{dest_path}': {move_err}")
                        errors_encountered = True
                        status = "warning"

                # Remove the temp folder if everything moved successfully, or if it's empty and the job failed
                should_cleanup = False
                if not errors_encountered:
                    should_cleanup = True
                elif not success:
                    # Check if temp folder is empty (failed job with no files)
                    try:
                        remaining_files = [f for f in os.listdir(job_tmp_dir) if os.path.isfile(os.path.join(job_tmp_dir, f))]
                        if len(remaining_files) == 0:
                            should_cleanup = True
                            logging.info(f"[POST] Temp dir is empty for failed job {job_id}, cleaning up")
                    except Exception:
                        pass
                
                if should_cleanup:
                    try:
                        shutil.rmtree(job_tmp_dir, ignore_errors=True)
                        logging.info(f"[POST] Cleaned temp dir for job {job_id}: {job_tmp_dir}")
                    except Exception as rm_err:
                        logging.error(f"[POST] Error removing temp dir for job {job_id}: {rm_err}")
                        # not a hard failure

    except Exception as e:
        logging.error(f"[POST] Unexpected post-processing error for job {job_id}: {e}")
        errors_encountered = True
        if success:
            status = "warning"

    # --- Update job final status -------------------------------------------
    # Use UTC time for consistency
    from datetime import timezone
    c.execute(
        """
        UPDATE jobs
        SET status = ?,
            finished_at = ?
        WHERE id = ?
        """,
        (status, datetime.now(timezone.utc).isoformat(), job_id),
    )
    conn.commit()
    
    # Send Discord webhook for download completion/failure
    if subscription_id:
        c.execute("SELECT title, artist, thumbnail FROM subscriptions WHERE id = ?", (subscription_id,))
        sub_row = c.fetchone()
        if sub_row:
            series_title = sub_row["title"] or "Unknown Series"
            series_artist = sub_row["artist"] or "Unknown Artist"
            thumbnail_url = sub_row.get("thumbnail")
            
            # Get episode info if available
            episode_info = ""
            if start_ch is not None and end_ch is not None:
                if start_ch == end_ch:
                    ep_row = c.execute(
                        "SELECT title FROM episodes WHERE subscription_id = ? AND ep_num = ?",
                        (subscription_id, start_ch)
                    ).fetchone()
                    if ep_row and ep_row.get("title"):
                        episode_info = f"Episode {start_ch}: {ep_row['title']}"
                    else:
                        episode_info = f"Episode {start_ch}"
                else:
                    episode_info = f"Episodes {start_ch}-{end_ch}"
            
            if success:
                send_discord_webhook(
                    event_type="download_complete",
                    title="✅ Download Complete",
                    description=f"**{series_title}**\nby {series_artist}",
                    color=0x10b981,  # Green
                    fields=[
                        {"name": "Episode(s)", "value": episode_info or "All episodes", "inline": True},
                        {"name": "Job ID", "value": str(job_id), "inline": True},
                        {"name": "Status", "value": "Successfully downloaded", "inline": False}
                    ],
                    thumbnail=thumbnail_url
                )
            else:
                error_msg = stdout[-500:] if stdout else "Unknown error"
                if len(error_msg) > 1024:
                    error_msg = error_msg[:1021] + "..."
                send_discord_webhook(
                    event_type="download_failed",
                    title="❌ Download Failed",
                    description=f"**{series_title}**\nby {series_artist}",
                    color=0xef4444,  # Red
                    fields=[
                        {"name": "Episode(s)", "value": episode_info or "All episodes", "inline": True},
                        {"name": "Job ID", "value": str(job_id), "inline": True},
                        {"name": "Error", "value": error_msg, "inline": False}
                    ],
                    thumbnail=thumbnail_url
                )

    # Clear PROCESSES entry for this subscription and job, if any
    with PROCESS_LOCK:
        if subscription_id is not None and subscription_id in PROCESSES:
            del PROCESSES[subscription_id]
        if job_id in PROCESSES:
            del PROCESSES[job_id]

    # Build summary message for logs (after we know actual_episodes_downloaded)
    summary_parts = []
    if has_download_error or not success:
        summary_parts.append("✗ Download had errors")
    if expected_episodes and 'actual_episodes_downloaded' in locals() and actual_episodes_downloaded > 0:
        summary_parts.append(f"Downloaded {actual_episodes_downloaded} of {expected_episodes} expected episodes")
        if actual_episodes_downloaded < expected_episodes:
            summary_parts.append("(partial download)")
        else:
            summary_parts.append("(all episodes downloaded and post-processed)")
    elif expected_episodes and success and not has_download_error:
        # If we don't have a count but succeeded, assume all were downloaded
        summary_parts.append(f"Downloaded {expected_episodes} of {expected_episodes} expected episodes (all episodes downloaded and post-processed)")
    
    if summary_parts:
        summary_msg = " | ".join(summary_parts)
        LOG_QUEUE.put(f"[Job {job_id}] {summary_msg}")
        # Also append to stdout for database storage
        stdout += f"\n{summary_msg}"
    
    logging.info(f"Job {job_id} post-processing finished with status '{status}'")
    
    # Update job final status and save logs to database
    from datetime import timezone
    try:
        c.execute(
            """
            UPDATE jobs
            SET status = ?,
                finished_at = ?,
                log = ?
            WHERE id = ?
            """,
            (status, datetime.now(timezone.utc).isoformat(), stdout, job_id),
        )
        conn.commit()
    except Exception as e:
        logging.error(f"Error updating job status/logs in database: {e}")
    
    # Close database connection
    try:
        conn.close()
    except Exception as e:
        logging.error(f"Error closing database connection in run_download_process: {e}")

# make sure these globals exist once near the top
# (These are already defined at the top of the file)

def update_processing_status(key, status=None, page=None, episode=None, title=None, subtitle=None, progress=None, current_episode=None, total_episodes=None):
    """
    key: either subscription_id (int) or series_url (str).
    - If int -> update PROCESSES (shown on subscription card)
    - If str -> update EPISODE_PROGRESS keyed by series URL (polled by add modal)
    Accepts keyword args for backward compatibility.
    """
    try:
        if isinstance(key, int):
            # subscription-card style update
            sub_id = key
            conn = get_db()
            try:
                c = conn.cursor()
                if status == 'idle':
                    with PROCESS_LOCK:
                        PROCESSES.pop(key, None)
                    # Remove from database
                    c.execute("DELETE FROM processing_status WHERE subscription_id = ?", (sub_id,))
                    conn.commit()
                    return
                
                # Update in-memory PROCESSES
                with PROCESS_LOCK:
                    proc_data = {
                        "status": status or "caching",
                        "title": title if title is not None else PROCESSES.get(key, {}).get("title"),
                        "subtitle": subtitle if subtitle is not None else PROCESSES.get(key, {}).get("subtitle")
                    }
                    if progress is not None:
                        proc_data["progress"] = progress
                    if current_episode is not None:
                        proc_data["current_episode"] = current_episode
                    if total_episodes is not None:
                        proc_data["total_episodes"] = total_episodes
                    PROCESSES[key] = proc_data
                
                # Update database
                from datetime import timezone
                c.execute("""
                    INSERT INTO processing_status (subscription_id, status, started_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(subscription_id) DO UPDATE SET
                        status = excluded.status,
                        started_at = CASE 
                            WHEN excluded.status != 'idle' AND status = 'idle' THEN excluded.started_at
                            ELSE started_at
                        END
                """, (sub_id, status or "caching", datetime.now(timezone.utc).isoformat()))
                conn.commit()
            except Exception as e:
                logging.exception(f"Error updating processing_status table: {e}")
                if conn:
                    try:
                        conn.rollback()
                    except:
                        pass
            finally:
                try:
                    conn.close()
                except:
                    pass
            return

        # key is a series URL (string)
        if status == "idle":
            EPISODE_PROGRESS.pop(key, None)
            return

        entry = EPISODE_PROGRESS.get(key, {})
        entry.update({
            "status": status or entry.get("status", "caching"),
            "page": page if page is not None else entry.get("page"),
            "episode": episode if episode is not None else entry.get("episode"),
            "title": title if title is not None else entry.get("title"),
            "subtitle": subtitle if subtitle is not None else entry.get("subtitle"),
        })
        # Add progress data if provided
        if progress is not None:
            entry["progress"] = progress
        if current_episode is not None:
            entry["current_episode"] = current_episode
        if total_episodes is not None:
            entry["total_episodes"] = total_episodes
        EPISODE_PROGRESS[key] = entry
    except Exception:
        # never let progress-tracking break scraping/processing
        logging.exception("update_processing_status error")
        return

def auto_download_worker():
    global LAST_AUTO_CHECK, NEXT_AUTO_CHECK, AUTO_CHECK_INTERVAL
    
    # Load initial interval from settings
    interval_minutes = int(get_setting("auto_check_interval", 5))
    interval_seconds = interval_minutes * 60  # Convert to seconds
    AUTO_CHECK_INTERVAL = interval_seconds
    
    logging.info("Auto-download worker started; checking every %s seconds (%s minutes).", interval_seconds, interval_minutes)

    while True:
        try:
            # Use local time for schedule checks
            now = datetime.now()
            # But send UTC timestamps to frontend for proper timezone conversion
            now_utc = datetime.now(timezone.utc)
            LAST_AUTO_CHECK = now_utc.isoformat()
            NEXT_AUTO_CHECK = (now_utc + timedelta(seconds=AUTO_CHECK_INTERVAL)).isoformat()

            conn = get_db()
            conn.row_factory = dict_factory
            c = conn.cursor()

            c.execute("""
                SELECT
                    s.id AS subscription_id,
                    s.url AS series_url,
                    s.title AS series_title,
                    ss.auto_download_latest AS auto_download_latest,
                    ss.schedule_day AS schedule_day,
                    ss.schedule_time AS schedule_time
                FROM subscriptions s
                LEFT JOIN series_settings ss
                    ON ss.subscription_id = s.id
            """)
            rows = c.fetchall()

            logging.info("[AUTO] Cycle at %s – found %d subscriptions", now.isoformat(), len(rows))
            LOG_QUEUE.put(f"[AUTO] Auto-check cycle started - checking {len(rows)} subscriptions")

            for row in rows:
                sub_id = row["subscription_id"]
                series_url = row["series_url"]
                series_title = row.get("series_title") or "Unknown Series"
                auto_latest = row["auto_download_latest"]
                schedule_day = row["schedule_day"]
                schedule_time = row["schedule_time"]

                # 1) auto-download must be enabled (explicitly check for 1/True, not just truthy)
                # Handle None from LEFT JOIN - if series_settings row doesn't exist, auto_latest will be None
                if auto_latest is None or auto_latest == 0 or auto_latest is False:
                    continue

                # 2) Day check (0–6, Mon–Sun)
                if schedule_day is not None:
                    try:
                        sched_day_int = int(schedule_day)
                    except (TypeError, ValueError):
                        sched_day_int = None

                    if sched_day_int is not None and now.weekday() != sched_day_int:
                        continue

                # 3) Time check HH:MM (strict match)
                if schedule_time:
                    try:
                        hour_str, minute_str = schedule_time.split(":")
                        hour = int(hour_str)
                        minute = int(minute_str)
                        if now.hour != hour or now.minute != minute:
                            continue
                    except Exception:
                        # bad time format -> don't block entirely
                        pass

                # Check if subscription is currently being scraped/cached
                # 1. Check if subscription is in PROCESSES with caching status
                with PROCESS_LOCK:
                    if sub_id in PROCESSES:
                        proc_info = PROCESSES[sub_id]
                        # Check if it's a processing status dict (not a job tracking dict)
                        if isinstance(proc_info, dict) and "status" in proc_info:
                            status = proc_info.get("status", "")
                            if status in ("caching", "processing"):
                                LOG_QUEUE.put(f"[AUTO] Skipping sub_id={sub_id} - still being cached/scraped")
                                logging.info("[AUTO] Skipping auto-download for sub_id=%s - still being cached/scraped", sub_id)
                                continue
                        # If it has a job_id, it's a download job
                        elif isinstance(proc_info, dict) and "job_id" in proc_info:
                            LOG_QUEUE.put(f"[AUTO] Skipping sub_id={sub_id} - download already in progress (job_id={proc_info.get('job_id')})")
                            logging.info("[AUTO] Skipping auto-download for sub_id=%s - download already in progress (job_id=%s)", 
                                        sub_id, proc_info.get("job_id"))
                            continue
                
                # 2. Check if subscription is in EPISODE_PROGRESS (being scraped)
                if series_url in EPISODE_PROGRESS:
                    progress_info = EPISODE_PROGRESS[series_url]
                    if progress_info.get("status") in ("caching", "processing"):
                        LOG_QUEUE.put(f"[AUTO] Skipping sub_id={sub_id} - still being scraped")
                        logging.info("[AUTO] Skipping auto-download for sub_id=%s - still being scraped", sub_id)
                        continue
                
                # 3. Check if there are any running jobs for this subscription
                # Match by comic_title (which should match the subscription title)
                series_title_for_job = c.execute(
                    "SELECT title FROM subscriptions WHERE id = ?",
                    (sub_id,)
                ).fetchone()
                
                if series_title_for_job:
                    running_job = c.execute("""
                        SELECT id FROM jobs 
                        WHERE status = 'running' 
                        AND comic_title = ?
                        LIMIT 1
                    """, (series_title_for_job["title"],)).fetchone()
                    if running_job:
                        LOG_QUEUE.put(f"[AUTO] Skipping sub_id={sub_id} - job {running_job['id']} already running")
                        logging.info("[AUTO] Skipping auto-download for sub_id=%s - job %s already running", sub_id, running_job["id"])
                        continue
                
                # Check database for processing status (most reliable)
                processing_check = c.execute("""
                    SELECT status FROM processing_status 
                    WHERE subscription_id = ? AND status IN ('caching', 'processing')
                """, (sub_id,)).fetchone()
                if processing_check:
                    LOG_QUEUE.put(f"[AUTO] Skipping sub_id={sub_id} - currently processing (database check)")
                    logging.info("[AUTO] Skipping sub_id=%s - currently processing (database check)", sub_id)
                    continue
                
                # Final check right before scraping to prevent race conditions
                with PROCESS_LOCK:
                    if sub_id in PROCESSES:
                        proc_info = PROCESSES[sub_id]
                        if isinstance(proc_info, dict):
                            if "status" in proc_info and proc_info.get("status") in ("caching", "processing"):
                                LOG_QUEUE.put(f"[AUTO] Skipping sub_id={sub_id} - currently processing")
                                logging.info("[AUTO] Skipping sub_id=%s - currently processing", sub_id)
                                continue
                            if "job_id" in proc_info:
                                LOG_QUEUE.put(f"[AUTO] Skipping sub_id={sub_id} - download job in progress")
                                logging.info("[AUTO] Skipping sub_id=%s - download job in progress", sub_id)
                                continue
                
                if series_url in EPISODE_PROGRESS:
                    progress_info = EPISODE_PROGRESS[series_url]
                    if progress_info.get("status") in ("caching", "processing"):
                        LOG_QUEUE.put(f"[AUTO] Skipping sub_id={sub_id} - currently being scraped")
                        logging.info("[AUTO] Skipping sub_id=%s - currently being scraped", sub_id)
                        continue
                
                LOG_QUEUE.put(f"[AUTO] Checking {series_title}")
                logging.info("[AUTO] Scraping episodes for sub_id=%s (%s)", sub_id, series_url)
                # Only scrape first page for auto-checks since new episodes are at the top
                scrape_episodes(sub_id, series_url, first_page_only=True)

                # Get series info for webhook
                series_info = c.execute(
                    "SELECT title, artist, thumbnail FROM subscriptions WHERE id = ?",
                    (sub_id,)
                ).fetchone()
                series_title = series_info["title"] if series_info else "Unknown Series"
                series_artist = series_info["artist"] if series_info else "Unknown Artist"
                thumbnail_url = series_info.get("thumbnail") if series_info else None

                # Get the latest episode (highest ep_num)
                latest = c.execute("""
                    SELECT id, ep_num, downloaded, title
                    FROM episodes
                    WHERE subscription_id = ?
                    ORDER BY ep_num DESC
                    LIMIT 1
                """, (sub_id,)).fetchone()
                
                # Check if subscription has any episodes yet (might still be initializing)
                if not latest:
                    LOG_QUEUE.put(f"[AUTO] Skipping sub_id={sub_id} - no episodes found yet (still initializing)")
                    logging.info("[AUTO] Skipping auto-download for sub_id=%s - no episodes found yet (still initializing)", sub_id)
                    continue
                
                # Check if this is a new episode (compare with last known)
                last_known_row = c.execute("""
                    SELECT MAX(ep_num) AS max_ep FROM episodes 
                    WHERE subscription_id = ? AND downloaded = 1
                """, (sub_id,)).fetchone()
                last_known = (last_known_row.get("max_ep") if last_known_row and last_known_row.get("max_ep") is not None else 0) or 0

                if latest and latest["ep_num"] > last_known:
                    # New episode detected - send webhook
                    episode_title = latest.get("title") or f"Episode {latest['ep_num']}"
                    send_discord_webhook(
                        event_type="new_episode",
                        title="🆕 New Episode Found",
                        description=f"**{series_title}**\nby {series_artist}",
                        color=0x9b59b6,  # Purple
                        fields=[
                            {"name": "Episode", "value": f"#{latest['ep_num']}: {episode_title}", "inline": False},
                            {"name": "Status", "value": "Auto-downloading..." if auto_latest else "Available for download", "inline": False}
                        ],
                        thumbnail=thumbnail_url
                    )

                if latest and not latest["downloaded"]:
                    # Check database for processing status first (most reliable)
                    processing_check = c.execute("""
                        SELECT status FROM processing_status 
                        WHERE subscription_id = ? AND status IN ('caching', 'processing')
                    """, (sub_id,)).fetchone()
                    if processing_check:
                        LOG_QUEUE.put(f"[AUTO] Skipping sub_id={sub_id} - currently processing (database check)")
                        logging.info("[AUTO] Skipping auto-download for sub_id=%s - currently processing (database check)", sub_id)
                        continue
                    
                    # Final check right before creating download job to prevent race conditions
                    # 1. Check if subscription is in PROCESSES (has active job or is processing)
                    with PROCESS_LOCK:
                        if sub_id in PROCESSES:
                            proc_info = PROCESSES[sub_id]
                            if isinstance(proc_info, dict):
                                if "status" in proc_info and proc_info.get("status") in ("caching", "processing"):
                                    LOG_QUEUE.put(f"[AUTO] Skipping sub_id={sub_id} - currently processing")
                                    logging.info("[AUTO] Skipping auto-download for sub_id=%s - currently processing", sub_id)
                                    continue
                                if "job_id" in proc_info:
                                    LOG_QUEUE.put(f"[AUTO] Skipping sub_id={sub_id} - download already in progress (job_id=%s)", proc_info.get("job_id"))
                                    logging.info("[AUTO] Skipping auto-download for sub_id=%s - download already in progress (job_id=%s)", 
                                                sub_id, proc_info.get("job_id"))
                                    continue
                    
                    # 2. Check if "download all" is in progress
                    with DOWNLOAD_ALL_LOCK:
                        if sub_id in DOWNLOAD_ALL_IN_PROGRESS:
                            logging.info("[AUTO] Skipping auto-download for sub_id=%s - download_all already in progress", sub_id)
                            continue
                    
                    # 3. Check if there are any running jobs for this subscription (by subscription_id in PROCESSES or by comic_title)
                    # This is more comprehensive than just checking PROCESSES
                    series_title_for_job = c.execute(
                        "SELECT title FROM subscriptions WHERE id = ?",
                        (sub_id,)
                    ).fetchone()
                    
                    if series_title_for_job:
                        # Check for any running job with this comic_title
                        running_job_check = c.execute("""
                            SELECT id, status FROM jobs 
                            WHERE status = 'running' 
                            AND comic_title = ?
                            LIMIT 1
                        """, (series_title_for_job["title"],)).fetchone()
                        if running_job_check:
                            LOG_QUEUE.put(f"[AUTO] Skipping sub_id={sub_id} - job {running_job_check['id']} already running")
                            logging.info("[AUTO] Skipping auto-download for sub_id=%s - job %s already running", sub_id, running_job_check["id"])
                            continue
                    
                    # 4. Check if there are multiple undownloaded episodes and a recent job might be downloading them
                    # This helps prevent auto-download from triggering while "download all" is running
                    undownloaded_count = c.execute("""
                        SELECT COUNT(*) as cnt FROM episodes 
                        WHERE subscription_id = ? AND downloaded = 0
                    """, (sub_id,)).fetchone()
                    
                    if undownloaded_count and undownloaded_count.get("cnt", 0) > 1:
                        # Check if there's a very recent running job (within last 5 minutes) for this series
                        # This might be a "download all" that just started
                        if series_title_for_job:
                            recent_job = c.execute("""
                                SELECT id FROM jobs 
                                WHERE status = 'running' 
                                AND comic_title = ?
                                AND started_at > datetime('now', '-5 minutes')
                                ORDER BY id DESC
                                LIMIT 1
                            """, (series_title_for_job["title"],)).fetchone()
                            
                            if recent_job:
                                LOG_QUEUE.put(f"[AUTO] Skipping sub_id={sub_id} - recent job {recent_job['id']} running, may be downloading multiple episodes")
                                logging.info("[AUTO] Skipping auto-download for sub_id=%s - recent job running (job_id=%s), may be downloading multiple episodes", 
                                            sub_id, recent_job["id"])
                                continue
                    
                    logging.info("[AUTO] Auto-downloading latest episode id=%s (ep_num=%s) for sub_id=%s", 
                                latest["id"], latest["ep_num"], sub_id)

                    # Get series settings for format and path_template
                    series_settings = c.execute("""
                        SELECT format_override, path_template_override
                        FROM series_settings
                        WHERE subscription_id = ?
                    """, (sub_id,)).fetchone()
                    
                    # Get global settings
                    global_format = get_setting("format", "images")
                    global_path_template = get_setting("path_template", PATH_TEMPLATE_DEFAULT)
                    
                    format_choice = (series_settings["format_override"] if series_settings and series_settings["format_override"] 
                                    else global_format) if series_settings else global_format
                    path_template = (series_settings["path_template_override"] if series_settings and series_settings["path_template_override"]
                                    else global_path_template) if series_settings else global_path_template

                    data = {
                        "url": series_url,
                        "subscription_id": sub_id,
                        "latest": True,
                        "format": format_choice,
                        "path_template": path_template,
                        "_auto": True  # Mark this as an AUTO job
                    }

                    # Re-use your existing /api/download logic
                    try:
                        with app.test_request_context(json=data):
                            result = start_download()
                            if result and hasattr(result, 'status_code') and result.status_code == 200:
                                LOG_QUEUE.put(f"[AUTO] Successfully started download for sub_id={sub_id}")
                            else:
                                LOG_QUEUE.put(f"[AUTO] Failed to start download for sub_id={sub_id}")
                    except Exception as e:
                        LOG_QUEUE.put(f"[AUTO] Error starting download for sub_id={sub_id}: {e}")
                        logging.exception(f"[AUTO] Error starting download for sub_id={sub_id}: {e}")

            conn.close()

        except Exception as e:
            logging.exception("Error in auto-download worker: %s", e)

        # Re-check interval from settings in case it was updated
        interval_minutes = int(get_setting("auto_check_interval", 5))
        AUTO_CHECK_INTERVAL = interval_minutes * 60  # Convert to seconds
        
        time.sleep(AUTO_CHECK_INTERVAL)

@app.route('/')
def index():
    return render_template('index.html', db_error=DB_INIT_ERROR)

@app.route('/api/db/check', methods=['GET'])
def check_database():
    """Check if database is healthy and up to date"""
    try:
        conn = get_db()
        c = conn.cursor()
        
        # Try to access all required tables and columns
        required_checks = [
            ("jobs", ["id", "comic_title", "status", "log", "created_at", "started_at", "finished_at", "is_auto"]),
            ("episodes", ["id", "subscription_id", "ep_num", "downloaded", "download_error"]),
            ("subscriptions", ["id", "title", "url", "artist", "thumbnail"]),
            ("series_settings", ["subscription_id", "auto_download_latest", "format_override", "path_template_override", "download_all_after_cache"]),
            ("settings", ["key", "value"]),
            ("processing_status", ["subscription_id", "status", "started_at"]),
        ]
        
        missing_items = []
        for table, columns in required_checks:
            try:
                c.execute(f"SELECT COUNT(*) FROM {table}")
                c.fetchone()
                # Check columns
                c.execute(f"PRAGMA table_info({table})")
                existing_cols = [row[1] for row in c.fetchall()]
                for col in columns:
                    if col not in existing_cols:
                        missing_items.append(f"{table}.{col}")
            except sqlite3.OperationalError as e:
                return jsonify({
                    "status": "error",
                    "message": f"Database table '{table}' error: {str(e)}",
                    "needs_migration": True
                }), 500
        
        conn.close()
        
        if missing_items:
            return jsonify({
                "status": "error",
                "message": f"Database is missing required columns: {', '.join(missing_items)}",
                "needs_migration": True,
                "missing_items": missing_items
            }), 200
        
        return jsonify({"status": "ok", "needs_migration": False})
    except Exception as e:
        logging.exception("Error checking database health")
        return jsonify({
            "status": "error",
            "message": f"Database check failed: {str(e)}",
            "needs_migration": True
        }), 500

@app.route('/api/db/migrate', methods=['POST'])
def migrate_database():
    """Attempt to migrate/update the database structure"""
    try:
        success, error = init_db()
        if success:
            return jsonify({"status": "success", "message": "Database migrated successfully"})
        else:
            return jsonify({"status": "error", "message": error}), 500
    except Exception as e:
        logging.exception("Error migrating database")
        return jsonify({"status": "error", "message": f"Migration failed: {str(e)}"}), 500

# 1. SUBSCRIPTIONS
@app.route('/api/subscriptions', methods=['GET', 'POST'])
def subscriptions():
    conn = get_db()
    try:
        if request.method == 'POST':
            try:
                url = request.json.get('url')
                if not url:
                    logging.error("Subscription POST: Missing 'url' in request JSON.")
                    return jsonify({"status": "error", "message": "Missing URL in request"}), 400

                auto_download_latest = request.json.get('auto_download_latest', False)
                download_all_after_cache = request.json.get('download_all_after_cache', False)

                # ✅ EARLY DUPLICATE CHECK – no scraping/insert if already in DB
                cur = conn.cursor()
                existing = cur.execute(
                    "SELECT id FROM subscriptions WHERE url = ?",
                    (url,)
                ).fetchone()
                if existing:
                    return jsonify({"status": "error", "message": "Already subscribed"}), 409

                meta = scrape_webtoon_meta(url)
                if meta:
                    try:
                        with DB_WRITE_LOCK:
                            cur = conn.cursor()
                            # Insert with last_updated initially NULL so we can
                            # control when it gets set (after first successful scrape)
                            cur.execute(
                                "INSERT INTO subscriptions (title, url, artist, thumbnail, last_updated) "
                                "VALUES (?, ?, ?, ?, ?)",
                                (meta['title'], url, meta['artist'], meta['thumbnail'], None)
                            )
                            sub_id = cur.lastrowid
                            
                            # If auto-download or download-all is enabled, create/update series_settings entry
                            if auto_download_latest or download_all_after_cache:
                                download_all_value = 1 if download_all_after_cache else 0
                                auto_dl_value = 1 if auto_download_latest else 0
                                logging.info(f"[SUBSCRIPTION] Saving series_settings for sub_id={sub_id}: auto_download_latest={auto_dl_value}, download_all_after_cache={download_all_value}")
                                cur.execute("""
                                    INSERT INTO series_settings (subscription_id, auto_download_latest, download_all_after_cache)
                                    VALUES (?, ?, ?)
                                    ON CONFLICT(subscription_id) DO UPDATE SET
                                        auto_download_latest = excluded.auto_download_latest,
                                        download_all_after_cache = excluded.download_all_after_cache
                                """, (sub_id, auto_dl_value, download_all_value))
                                
                                # Verify it was saved correctly
                                verify_row = cur.execute(
                                    "SELECT auto_download_latest, download_all_after_cache FROM series_settings WHERE subscription_id = ?",
                                    (sub_id,)
                                ).fetchone()
                                if verify_row:
                                    logging.info(f"[SUBSCRIPTION] Verified series_settings for sub_id={sub_id}: auto_download_latest={verify_row['auto_download_latest']}, download_all_after_cache={verify_row['download_all_after_cache']}")
                            
                            conn.commit()

                            # Send Discord webhook for series addition
                            send_discord_webhook(
                                event_type="series_added",
                                title="📚 Series Added",
                                description=f"**{meta['title']}**\nby {meta.get('artist', 'Unknown Artist')}",
                                color=0xf59e0b,  # Amber/Orange
                                fields=[
                                    {"name": "Auto-Download", "value": "Enabled" if auto_download_latest else "Disabled", "inline": True},
                                    {"name": "Status", "value": "Scraping episodes...", "inline": True}
                                ],
                                thumbnail=meta.get('thumbnail')
                            )

                            # Mark as processing immediately so frontend shows processing card
                            update_processing_status(
                                sub_id,
                                status="caching",
                                title="Processing series...",
                                subtitle="Starting episode scrape"
                            )

                            # Initial episode fetch in background thread so it persists even if user refreshes
                            def scrape_in_background():
                                try:
                                    scrape_episodes(sub_id, url)
                                except Exception as e:
                                    logging.exception(f"Error in background scrape for sub_id={sub_id}: {e}")
                                    # Clear processing status on error
                                    update_processing_status(sub_id, status="idle")
                            
                            thread = threading.Thread(target=scrape_in_background, daemon=True)
                            thread.start()

                            new_sub = conn.execute(
                                "SELECT * FROM subscriptions WHERE id = ?",
                                (sub_id,)
                            ).fetchone()
                            return jsonify({"status": "success", "data": dict(new_sub)})
                    except sqlite3.IntegrityError:
                        return jsonify({"status": "error", "message": "Already subscribed"}), 409
                    except Exception as e:
                        logging.exception(f"Database/Episode Scrape Error: {e}")
                        return jsonify({"status": "error", "message": "Server error during DB save"}), 500

                # meta was falsy or we otherwise didn't return earlier
                return jsonify({
                    "status": "error",
                    "message": "Could not fetch metadata. Please verify the URL and check server logs for details."
                }), 400

            except Exception as e:
                logging.exception(f"Request Parsing Error in subscriptions POST: {e}")
                return jsonify({"status": "error", "message": "Invalid request format"}), 400

        # ===== GET /api/subscriptions =====
        rows = conn.execute("""
            SELECT
                s.*,
                ss.auto_download_latest AS auto_download_latest,
                ss.schedule_day        AS schedule_day,
                ss.schedule_time       AS schedule_time
            FROM subscriptions s
            LEFT JOIN series_settings ss
                ON ss.subscription_id = s.id
            ORDER BY s.title
        """).fetchall()

        out = []
        for row in rows:
            d = dict(row)

            # attach whether the subscription is currently being processed (server-side)
            with PROCESS_LOCK:
                proc = PROCESSES.get(d.get("id"))
            if proc:
                d["is_processing"] = True
                d["processing_title"] = proc.get("title")
                d["processing_subtitle"] = proc.get("subtitle")
                d["processing_progress"] = proc.get("progress")
                d["processing_current_episode"] = proc.get("current_episode")
                d["processing_total_episodes"] = proc.get("total_episodes")
            else:
                d["is_processing"] = False

            out.append(d)

        return jsonify(out)

    except Exception as e:
        logging.exception(f"Error in subscriptions endpoint: {e}")
        return jsonify({"status": "error", "message": "Internal server error"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass

@app.route('/api/subscription_progress')
def subscription_progress():
    """
    Return current scraping progress for a series, keyed by its URL.
    Used by the frontend while a new series is being added.
    """
    series_url = request.args.get('url')
    if not series_url:
        return jsonify({})

    prog = EPISODE_PROGRESS.get(series_url)
    if not prog:
        return jsonify({})

    return jsonify(prog)

@app.route('/api/subscription_by_url')
def subscription_by_url():
    """
    Return a single subscription by URL, including processing status.
    Used for updating individual cards without refreshing the entire list.
    """
    series_url = request.args.get('url')
    if not series_url:
        return jsonify({"status": "error", "message": "Missing URL"}), 400

    conn = get_db()
    try:
        row = conn.execute("""
            SELECT
                s.*,
                ss.auto_download_latest AS auto_download_latest,
                ss.schedule_day        AS schedule_day,
                ss.schedule_time       AS schedule_time
            FROM subscriptions s
            LEFT JOIN series_settings ss
                ON ss.subscription_id = s.id
            WHERE s.url = ?
        """, (series_url,)).fetchone()

        if not row:
            return jsonify({"status": "error", "message": "Subscription not found"}), 404

        d = dict(row)

        # attach whether the subscription is currently being processed (server-side)
        with PROCESS_LOCK:
            proc = PROCESSES.get(d.get("id"))
        if proc:
            d["is_processing"] = True
            d["processing_title"] = proc.get("title")
            d["processing_subtitle"] = proc.get("subtitle")
            d["processing_progress"] = proc.get("progress")
            d["processing_current_episode"] = proc.get("current_episode")
            d["processing_total_episodes"] = proc.get("total_episodes")
        else:
            d["is_processing"] = False

        return jsonify(d)
    except Exception as e:
        logging.exception(f"Error in subscription_by_url endpoint: {e}")
        return jsonify({"status": "error", "message": "Internal server error"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass

@app.route('/api/auto_status')
def auto_status():
    """
    Simple status for the auto-download worker: last and next check times.
    """
    def fmt(ts):
        if not ts:
            return None
        if isinstance(ts, str):
            return ts
        return ts.isoformat()

    return jsonify({
        "last": fmt(LAST_AUTO_CHECK),
        "next": fmt(NEXT_AUTO_CHECK),
        "interval_seconds": AUTO_CHECK_INTERVAL,
    })

@app.route('/api/subscription/<int:sub_id>', methods=['DELETE'])
def delete_subscription(sub_id):
    """
    Delete a subscription and all its episodes, including any downloaded files
    and cache paths referenced by episodes.file_path.
    """
    conn = get_db()
    c = conn.cursor()

    try:
        # Get all file paths for this subscription's episodes
        c.execute(
            "SELECT DISTINCT file_path FROM episodes WHERE subscription_id = ? AND file_path IS NOT NULL",
            (sub_id,)
        )
        rows = c.fetchall()
        file_paths = [row['file_path'] for row in rows]

        # Remove files/directories on disk
        for path in file_paths:
            if not path:
                continue
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                elif os.path.isfile(path):
                    os.remove(path)
            except Exception as e:
                logging.error(f"Error deleting path '{path}' for subscription {sub_id}: {e}")

        # Delete episodes and subscription records
        c.execute("DELETE FROM episodes WHERE subscription_id = ?", (sub_id,))
        c.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
        conn.commit()

        return jsonify({"status": "success"}), 200

    except Exception as e:
        logging.exception(f"Error deleting subscription {sub_id}: {e}")
        conn.rollback()
        return jsonify({"status": "error", "message": "Failed to delete subscription"}), 500

    finally:
        conn.close()

# 2. EPISODES
@app.route('/api/subscription/<int:sub_id>/episodes')
def get_episodes(sub_id):
    conn = get_db()
    c = conn.cursor()

    try:
        # Pagination params
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 18))
        if page < 1:
            page = 1
        if limit <= 0:
            limit = 18

        per_page = limit
        offset = (page - 1) * per_page

        # Total episodes for this series
        total_row = conn.execute(
            "SELECT COUNT(*) AS c FROM episodes WHERE subscription_id = ?",
            (sub_id,)
        ).fetchone()
        total = total_row["c"] if total_row else 0

        # Latest ep number (for the green badge)
        latest_row = conn.execute(
            "SELECT MAX(ep_num) AS latest_num FROM episodes WHERE subscription_id = ?",
            (sub_id,)
        ).fetchone()
        latest_ep_num = (
            latest_row["latest_num"]
            if latest_row and latest_row["latest_num"] is not None
            else 0
        )

        # Small-limit mode (used by the download modal: limit=5)
        if limit < 18:
            rows = conn.execute(
                "SELECT * FROM episodes WHERE subscription_id = ? "
                "ORDER BY ep_num DESC LIMIT ?",
                (sub_id, limit),
            ).fetchall()

            episodes_list = []
            for row in rows:
                ep = dict(row)
                thumb = ep.get("cached_thumbnail")
                if thumb:
                    ep["display_thumbnail"] = f"/api/proxy_image/{thumb}"
                else:
                    ep["display_thumbnail"] = ep.get("thumbnail") or ""
                ep["is_latest"] = ep["ep_num"] == latest_ep_num
                episodes_list.append(ep)

            pages = (total // limit) + 1 if total > limit else 1

            return jsonify(
                {
                    "episodes": episodes_list,
                    "total": total,
                    "page": 1,
                    "pages": pages,
                }
            )

        # Normal paginated mode (episodes view)
        rows = conn.execute(
            "SELECT * FROM episodes WHERE subscription_id = ? "
            "ORDER BY ep_num DESC LIMIT ? OFFSET ?",
            (sub_id, per_page, offset),
        ).fetchall()

        episodes_list = []
        for row in rows:
            ep = dict(row)
            thumb = ep.get("cached_thumbnail")
            if thumb:
                ep["display_thumbnail"] = f"/api/proxy_image/{thumb}"
            else:
                ep["display_thumbnail"] = ep.get("thumbnail") or ""
            ep["is_latest"] = ep["ep_num"] == latest_ep_num
            episodes_list.append(ep)

        import math

        pages = math.ceil(total / per_page) if total > 0 else 1

        return jsonify(
            {
                "episodes": episodes_list,
                "total": total,
                "page": page,
                "pages": pages,
            }
        )

    except Exception as e:
        logging.exception(f"Error in get_episodes: {e}")
        return jsonify({"status": "error", "message": "Failed to fetch episodes"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass

@app.route('/api/subscription/<int:sub_id>/rescan', methods=['POST'])
def rescan_episodes(sub_id):
    """Manually trigger a full re-scrape of episodes for a series"""
    try:
        conn = get_db()
        c = conn.cursor()
        
        # Get the subscription URL
        row = c.execute(
            "SELECT url FROM subscriptions WHERE id = ?",
            (sub_id,)
        ).fetchone()
        
        if not row:
            return jsonify({"status": "error", "message": "Subscription not found"}), 404
        
        series_url = row[0]
        
        # Check if already processing
        with PROCESS_LOCK:
            if sub_id in PROCESSES:
                proc_info = PROCESSES[sub_id]
                if isinstance(proc_info, dict):
                    if "status" in proc_info and proc_info.get("status") in ("caching", "processing"):
                        return jsonify({"status": "error", "message": "Series is currently being processed"}), 409
        
        if series_url in EPISODE_PROGRESS:
            progress_info = EPISODE_PROGRESS[series_url]
            if progress_info.get("status") in ("caching", "processing"):
                return jsonify({"status": "error", "message": "Series is currently being scraped"}), 409
        
        # Trigger re-scrape in background thread (full scrape, not first_page_only, with force_rescan)
        def scrape_in_background():
            try:
                logging.info(f"[RESCAN] Starting full re-scrape for sub_id={sub_id}")
                scrape_episodes(sub_id, series_url, first_page_only=False, force_rescan=True)
                logging.info(f"[RESCAN] Completed full re-scrape for sub_id={sub_id}")
            except Exception as e:
                logging.exception(f"Error in background re-scrape for sub_id={sub_id}: {e}")
                update_processing_status(sub_id, status="idle")
        
        thread = threading.Thread(target=scrape_in_background, daemon=True)
        thread.start()
        
        return jsonify({"status": "success", "message": "Re-scan started"})
        
    except Exception as e:
        logging.exception(f"Error in rescan_episodes: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass

@app.route('/api/episodes/delete', methods=['POST'])
def delete_episodes():
    ids = request.json.get('ids', [])
    if not ids:
        return jsonify({"status": "success"})

    conn = get_db()
    try:
        placeholders = ",".join(["?"] * len(ids))
        conn.execute(
            f"""
            UPDATE episodes
            SET downloaded = 0,
                download_error = 0,
                file_path = NULL
            WHERE id IN ({placeholders})
            """,
            ids,
        )
        conn.commit()
        return jsonify({"status": "success"})
    except Exception as e:
        logging.exception(f"Error in delete_episodes: {e}")
        return jsonify({"status": "error", "message": "Failed to delete episodes"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass

# 3. DOWNLOAD
@app.route('/api/download', methods=['POST'])
def start_download():
    try:
        data = request.json or {}
        url = data.get('url')
        if not url:
            return jsonify({"status": "error", "message": "Missing URL"}), 400

        start_ch = data.get('start')
        end_ch = data.get('end')
        latest = data.get('latest', False)
        subscription_id = data.get('subscription_id')
        
        # Check if a download is already in progress for this subscription
        if subscription_id is not None:
            # 1. Check if subscription is in PROCESSES (has active job)
            with PROCESS_LOCK:
                if subscription_id in PROCESSES:
                    proc_info = PROCESSES[subscription_id]
                    # Only reject if it has a job_id (actual download job), not just a status update
                    if isinstance(proc_info, dict) and proc_info.get("job_id"):
                        existing_job_id = proc_info.get("job_id")
                        logging.warning(f"[DOWNLOAD] Skipping duplicate download for sub_id={subscription_id} - download already in progress (job_id={existing_job_id})")
                        return jsonify({"status": "error", "message": "Download already in progress for this series"}), 409
            
            # 2. Check if "download all" is in progress
            with DOWNLOAD_ALL_LOCK:
                if subscription_id in DOWNLOAD_ALL_IN_PROGRESS:
                    logging.warning(f"[DOWNLOAD] Skipping duplicate download for sub_id={subscription_id} - download_all already in progress")
                    return jsonify({"status": "error", "message": "Download all already in progress for this series"}), 409

        conn = get_db()
        cur = conn.cursor()
        comic_title = "Download Task"
        
        # If latest is True but start_ch/end_ch are not set, determine the latest episode number
        # This is needed for proper post-processing naming
        if latest and subscription_id and (start_ch is None or end_ch is None):
            latest_ep = cur.execute("""
                SELECT ep_num FROM episodes 
                WHERE subscription_id = ? 
                ORDER BY ep_num DESC 
                LIMIT 1
            """, (subscription_id,)).fetchone()
            # sqlite3.Row objects use dictionary-style access, not .get()
            if latest_ep and latest_ep["ep_num"] is not None:
                latest_ep_num = latest_ep["ep_num"]
                start_ch = latest_ep_num
                end_ch = latest_ep_num
                logging.info(f"[DOWNLOAD] Determined latest episode number: {latest_ep_num} for subscription {subscription_id}")
        
        # Get format and path_template - check request first, then per-series settings, then global
        format_choice = data.get("format")
        path_template = data.get("path_template")
        
        if subscription_id:
            row = cur.execute("SELECT title FROM subscriptions WHERE id = ?", (subscription_id,)).fetchone()
            if row:
                comic_title = row["title"]
            
            # If format/path_template not provided, check per-series settings
            if not format_choice or not path_template:
                series_settings = cur.execute(
                    "SELECT format_override, path_template_override FROM series_settings WHERE subscription_id = ?",
                    (subscription_id,)
                ).fetchone()
                
                if series_settings:
                    if not format_choice and series_settings["format_override"]:
                        format_choice = series_settings["format_override"]
                    if not path_template and series_settings["path_template_override"]:
                        path_template = series_settings["path_template_override"]
        
        # Fall back to global settings if still not set
        if not format_choice:
            format_choice = get_setting("format", "images")
        if not path_template:
            path_template = get_setting("path_template", PATH_TEMPLATE_DEFAULT)
        
        # Ensure we have defaults
        format_choice = format_choice or "images"
        path_template = path_template or PATH_TEMPLATE_DEFAULT

        # Check if this is an AUTO job
        # AUTO jobs are created by auto_download_worker and always have latest=True
        # We can also check if there's a thread-local flag, but for now use the pattern
        is_auto = 0
        # Check if latest is True and subscription_id is set (typical AUTO pattern)
        # Also check if the request has an auto flag (we'll set this in auto_download_worker)
        if data.get("latest") and subscription_id and data.get("_auto", False):
            is_auto = 1
        # Fallback: if latest=True and subscription_id, assume it's AUTO (manual downloads usually specify start/end)
        elif data.get("latest") and subscription_id and not data.get("start") and not data.get("end"):
            is_auto = 1
        
        # Use UTC time for consistency
        from datetime import timezone
        cur.execute(
            "INSERT INTO jobs (comic_title, status, log, created_at, is_auto) VALUES (?, ?, ?, ?, ?)",
            (comic_title, "running", "Starting...", datetime.now(timezone.utc).isoformat(), is_auto)
        )
        job_id = cur.lastrowid
        conn.commit()
        conn.close()

        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        tmp_root = os.path.join(DOWNLOAD_DIR, "tmp")
        os.makedirs(tmp_root, exist_ok=True)
        job_out_dir = os.path.join(tmp_root, f"job_{job_id}")
        os.makedirs(job_out_dir, exist_ok=True)

        cmd = ["webtoon-downloader", url, "--out", job_out_dir]
        # Don't use --start/--end with --latest (webtoon-downloader doesn't allow it)
        if latest:
            cmd.append("--latest")
        else:
            # Only add --start/--end if not using --latest
            if start_ch is not None:
                cmd.extend(["--start", str(start_ch)])
            if end_ch is not None:
                cmd.extend(["--end", str(end_ch)])
        if format_choice and format_choice != "images":
            cmd.extend(["--save-as", format_choice])
        if data.get("separate"):
            cmd.append("--separate")
        
        # Add concurrency settings from global settings
        concurrent_chapters = get_setting("concurrent_chapters", 6)
        concurrent_pages = get_setting("concurrent_pages", 120)
        if concurrent_chapters:
            cmd.extend(["--concurrent-chapters", str(concurrent_chapters)])
        if concurrent_pages:
            cmd.extend(["--concurrent-pages", str(concurrent_pages)])
        
        # Add debug flag if enabled
        if get_setting("debug_mode", "0") == "1":
            cmd.append("--debug")
        
        separate = data.get("separate", False)

        t = threading.Thread(
            target=run_download_process,
            args=(job_id, cmd, subscription_id, format_choice, separate, latest, start_ch, end_ch, path_template)
        )
        t.daemon = True
        t.start()
        return jsonify({"status": "success", "job_id": job_id})
    except Exception as e:
        logging.exception("Error in /api/download")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    if request.method == 'GET':
        conn = get_db()
        cur = conn.cursor()

        rows = cur.execute("SELECT key, value FROM settings").fetchall()
        conn.close()

        data = {row["key"]: row["value"] for row in rows}

        return jsonify({
            "format": data.get("format", "images"),
            "path_template": data.get("path_template", PATH_TEMPLATE_DEFAULT),
            "concurrent_chapters": int(data.get("concurrent_chapters", 6)),
            "concurrent_pages": int(data.get("concurrent_pages", 120)),
            "debug_mode": data.get("debug_mode", "0"),
            "auto_check_interval": int(data.get("auto_check_interval", 5)),
            "discord_webhook_url": data.get("discord_webhook_url", ""),
            "discord_notify_new_episode": data.get("discord_notify_new_episode", "1") == "1",
            "discord_notify_download_start": data.get("discord_notify_download_start", "0") == "1",
            "discord_notify_download_complete": data.get("discord_notify_download_complete", "1") == "1",
            "discord_notify_download_failed": data.get("discord_notify_download_failed", "1") == "1",
            "discord_notify_series_added": data.get("discord_notify_series_added", "1") == "1",
            "episode_number_format": data.get("episode_number_format", "number"),
            "episode_number_padding": int(data.get("episode_number_padding", 0)),
            "episode_separator": data.get("episode_separator", " - "),
            "episode_include_title": data.get("episode_include_title", True) != False,
        })

    else:  # POST
        payload = request.json or {}

        set_setting("format", payload.get("format", "images"))
        set_setting("path_template", payload.get("path_template", PATH_TEMPLATE_DEFAULT))
        set_setting("concurrent_chapters", payload.get("concurrent_chapters", 6))
        set_setting("concurrent_pages", payload.get("concurrent_pages", 120))
        set_setting("debug_mode", "1" if payload.get("debug_mode", False) else "0")
        
        # Update auto-check interval
        auto_check_interval = payload.get("auto_check_interval", 5)
        auto_check_interval = max(1, min(1440, int(auto_check_interval)))
        set_setting("auto_check_interval", str(auto_check_interval))
        global AUTO_CHECK_INTERVAL
        AUTO_CHECK_INTERVAL = auto_check_interval * 60
        
        # Discord webhook settings
        set_setting("discord_webhook_url", payload.get("discord_webhook_url", ""))
        set_setting("discord_notify_new_episode", "1" if payload.get("discord_notify_new_episode", True) else "0")
        set_setting("discord_notify_download_start", "1" if payload.get("discord_notify_download_start", False) else "0")
        set_setting("discord_notify_download_complete", "1" if payload.get("discord_notify_download_complete", True) else "0")
        set_setting("discord_notify_download_failed", "1" if payload.get("discord_notify_download_failed", True) else "0")
        set_setting("discord_notify_series_added", "1" if payload.get("discord_notify_series_added", True) else "0")
        
        # Episode naming settings
        set_setting("episode_number_format", payload.get("episode_number_format", "number"))
        set_setting("episode_number_padding", str(payload.get("episode_number_padding", 0)))
        set_setting("episode_separator", payload.get("episode_separator", " - "))
        set_setting("episode_include_title", "1" if payload.get("episode_include_title", True) else "0")

        return jsonify({"status": "ok"})

@app.route('/api/series_settings/<int:sub_id>', methods=['GET', 'POST'])
def api_series_settings(sub_id):
    conn = get_db()
    cur = conn.cursor()

    try:
        if request.method == 'GET':
            row = cur.execute("""
                SELECT auto_download_latest, schedule_day, schedule_time, format_override, path_template_override
                FROM series_settings
                WHERE subscription_id = ?
            """, (sub_id,)).fetchone()

            if not row:
                return jsonify({
                    "autoDownloadLatest": False,
                    "scheduleDay": None,
                    "scheduleTime": "",
                    "formatOverride": "",
                    "pathTemplateOverride": ""
                })

            return jsonify({
                "autoDownloadLatest": bool(row["auto_download_latest"]),
                "scheduleDay": row["schedule_day"],
                "scheduleTime": row["schedule_time"] or "",
                "formatOverride": row["format_override"] or "",
                "pathTemplateOverride": row["path_template_override"] or ""
            })

        # POST = save
        payload = request.json or {}

        cur.execute("""
            INSERT INTO series_settings (subscription_id, auto_download_latest, schedule_day, schedule_time, format_override, path_template_override)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(subscription_id) DO UPDATE SET
                auto_download_latest = excluded.auto_download_latest,
                schedule_day = excluded.schedule_day,
                schedule_time = excluded.schedule_time,
                format_override = excluded.format_override,
                path_template_override = excluded.path_template_override
        """, (
            sub_id,
            1 if payload.get("autoDownloadLatest") else 0,
            payload.get("scheduleDay"),
            payload.get("scheduleTime") or "",
            payload.get("formatOverride") or "",
            payload.get("pathTemplateOverride") or ""
        ))

        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        logging.exception(f"Error in api_series_settings: {e}")
        conn.rollback()
        return jsonify({"status": "error", "message": "Failed to save settings"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass

# 4. JOBS / STREAM
@app.route('/api/stream')
def stream_logs():
    def event_stream():
        while True:
            try:
                msg = LOG_QUEUE.get(timeout=10)
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield ": heartbeat\n\n"
    return Response(event_stream(), mimetype="text/event-stream")

@app.route('/api/jobs', methods=['GET'])
def list_jobs():
    conn = get_db()
    try:
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT id, comic_title, status, created_at, started_at, log, COALESCE(is_auto, 0) as is_auto FROM jobs ORDER BY id DESC LIMIT 100"
        ).fetchall()
        # Convert is_auto to boolean for frontend
        jobs = []
        for row in rows:
            job = dict(row)
            job['is_auto'] = bool(job.get('is_auto', 0))
            jobs.append(job)
        return jsonify(jobs)
    finally:
        try:
            conn.close()
        except Exception:
            pass

@app.route('/api/jobs/logs', methods=['GET'])
def get_job_logs():
    """Return recent logs from all jobs for the logs viewer"""
    try:
        # Get logs from the last 100 jobs
        conn = get_db()
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT log FROM jobs WHERE log IS NOT NULL AND log != '' ORDER BY id DESC LIMIT 100"
        ).fetchall()
        
        # Extract log lines and reverse to show oldest first
        all_logs = []
        for row in rows:
            log_text = row[0] if row[0] else ""
            if log_text:
                # Split by newlines and add job markers
                lines = log_text.split('\n')
                for line in lines:
                    if line.strip():
                        all_logs.append(line.strip())
        
        # Also check for AUTO logs in the queue (recent ones)
        # We'll get the last 500 lines from recent jobs
        all_logs = all_logs[-500:] if len(all_logs) > 500 else all_logs
        
        return jsonify({"logs": all_logs})
    except Exception as e:
        logging.exception(f"Error in get_job_logs: {e}")
        return jsonify({"logs": []})
    finally:
        try:
            conn.close()
        except Exception:
            pass

@app.route('/api/jobs/<int:job_id>/logs', methods=['GET'])
def get_single_job_logs(job_id):
    """Return logs for a specific job"""
    conn = get_db()
    try:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT log, comic_title, status FROM jobs WHERE id = ?",
            (job_id,)
        ).fetchone()
        
        if not row:
            return jsonify({"status": "error", "message": "Job not found"}), 404
        
        log_text = row[0] if row[0] else "No logs available for this job."
        comic_title = row[1] or "Unknown"
        status = row[2] or "unknown"
        
        # Split logs into lines
        log_lines = log_text.split('\n') if log_text else []
        
        return jsonify({
            "job_id": job_id,
            "comic_title": comic_title,
            "status": status,
            "logs": log_lines
        })
    except Exception as e:
        logging.exception(f"Error in get_single_job_logs: {e}")
        return jsonify({"status": "error", "message": "Failed to fetch logs"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass

@app.route('/api/jobs/<int:job_id>/cancel', methods=['POST'])
def cancel_job(job_id):
    global PROCESSES
    with PROCESS_LOCK:
        proc_info = PROCESSES.get(job_id)
        if not proc_info or "process" not in proc_info:
            # Try to find by searching through PROCESSES
            proc_info = None
            for key, value in PROCESSES.items():
                if isinstance(value, dict) and value.get("job_id") == job_id and "process" in value:
                    proc_info = value
                    break
            
            if not proc_info or "process" not in proc_info:
                return jsonify({"status": "error", "message": "Job is not running"}), 404
        
        proc = proc_info["process"]
        try:
            # Terminate the process
            proc.terminate()
            # Give it a moment to terminate gracefully
            time.sleep(1)
            # Check if it's still running
            if proc.poll() is None:
                # Force kill if it didn't terminate
                proc.kill()
                proc.wait()
            else:
                # Process already terminated
                proc.wait()
            
            # Update job status in database
            conn = get_db()
            try:
                conn.execute(
                    "UPDATE jobs SET status = 'failed', log = ? WHERE id = ?",
                    (f"Job cancelled by user", job_id)
                )
                conn.commit()
            except Exception as e:
                logging.error(f"Error updating job status: {e}")
            finally:
                conn.close()
            
            # Clean up process tracking
            subscription_id = proc_info.get("subscription_id")
            if subscription_id and subscription_id in PROCESSES:
                del PROCESSES[subscription_id]
            if job_id in PROCESSES:
                del PROCESSES[job_id]
            
            LOG_QUEUE.put(f"[Job {job_id}] ✗ Cancelled by user")
            logging.info(f"Job {job_id} cancelled successfully")
        except Exception as e:
            logging.error(f"Error cancelling job {job_id}: {e}")
            return jsonify({"status": "error", "message": f"Failed to terminate process: {str(e)}"}), 500
    return jsonify({"status": "success"})

@app.route('/api/reset', methods=['POST'])
def reset_everything():
    conn = get_db()
    c = conn.cursor()

    try:
        # Clear all database tables including settings
        c.execute("DELETE FROM jobs")
        c.execute("DELETE FROM episodes")
        c.execute("DELETE FROM subscriptions")
        c.execute("DELETE FROM series_settings")
        c.execute("DELETE FROM settings")  # Also clear global settings

        conn.commit()
    except Exception as e:
        logging.exception(f"Error clearing database in reset: {e}")
        conn.rollback()
        return jsonify({"status": "error", "message": "Failed to reset database"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Clear download folders
    try:
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    except Exception as e:
        logging.error(f"Error clearing download directory: {e}")

    # Clear cache directory (thumbnails)
    try:
        if os.path.exists(CACHE_DIR):
            for filename in os.listdir(CACHE_DIR):
                file_path = os.path.join(CACHE_DIR, filename)
                try:
                    if os.path.isfile(file_path):
                        os.remove(file_path)
                    elif os.path.isdir(file_path):
                        shutil.rmtree(file_path, ignore_errors=True)
                except Exception as e:
                    logging.warning(f"Error removing cache file {file_path}: {e}")
        logging.info("Cache directory cleared")
    except Exception as e:
        logging.error(f"Error clearing cache directory: {e}")

    return jsonify({"status": "ok"})

# threading.Thread(target=auto_download_worker, daemon=True).start()
# if __name__ == '__main__':
    # app.run(host='0.0.0.0', port=8128, debug=True)

if __name__ == '__main__':
    # When debug=True, Werkzeug's reloader runs this file twice.
    # Only start the auto-download worker in the actual serving process.
    if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        threading.Thread(target=auto_download_worker, daemon=True).start()

    app.run(host='0.0.0.0', port=8128, debug=True)
