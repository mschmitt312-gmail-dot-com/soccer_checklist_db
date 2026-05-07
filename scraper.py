import os
import time
import requests
import re
import json
import unicodedata
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urljoin

# --------------------------------------------------
# CONFIG  (edit these to control the run)
# --------------------------------------------------
PARENT_URL       = "https://cartophilic-info-exch.blogspot.com/"   # discovers all index pages
BASE_FOLDER      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "soccer_checklists")
WAIT_SECONDS     = 5       # polite delay between requests (use 5+ for full runs)
CHECKLIST_LIMIT  = 1000        # how many sets to INCLUDE per run (skipped sets don't count)
SKIP_IMAGES      = False    # True = skip image downloads (faster for testing text)
FORCE_REPROCESS  = False    # False = skip folders that already exist (production mode)
START_OFFSET     = 0        # skip the first N links in the combined deduplicated link list
YEAR_CUTOFF      = 1959     # only include sets from this year or earlier

# Cache file -- one URL per line, every URL the scraper has ever processed.
# Loaded at startup so hollowed/merged child folders and other edge cases
# are skipped instantly on subsequent runs without any network requests.
URL_CACHE_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scraper_url_cache.txt")


# --------------------------------------------------
# TEXT HELPERS
# --------------------------------------------------

def clean(text):
    if not text:
        return ""
    return text.replace('\xa0', ' ').replace('\u200b', '').strip()


def safe_folder_name(name):
    name = clean(name)
    # Decompose accented/special chars to ASCII equivalents (e.g. C-accent -> C)
    # then drop anything that still isn't ASCII (e.g. guillemets, Cyrillic, etc.)
    name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    name = re.sub(r'[\\/*?:"<>|]', '', name)
    name = re.sub(r'\s+', '_', name)
    return name[:120]


def url_slug(url):
    m = re.search(r'/(\d{4}/\d{2}/[^/]+)\.html', url)
    return m.group(1).replace('/', '_') if m else "unknown"


# --------------------------------------------------
# YEAR PARSING
# --------------------------------------------------

def parse_start_year(text):
    """
    Extract the first (start) 4-digit year from any string.
        "1987"      -> 1987
        "1987-88"   -> 1987
        "1986/87"   -> 1986
        "2019-20"   -> 2019
    Returns None if no plausible year is found.
    """
    if not text:
        return None
    m = re.search(r'\b(1[89]\d{2}|20\d{2})\b', text)
    return int(m.group(1)) if m else None


def year_from_og_title(og_title):
    """
    FIX 3: Try to extract year from the og:title as a fallback.
    Titles typically end with "(1986-87)" or "(1985)" etc.
    We look for the LAST 4-digit year in the title since that's usually
    the most explicit one (e.g. "Some Set (1985-86)" -> 1985).
    """
    if not og_title:
        return None
    matches = re.findall(r'\b(1[89]\d{2}|20\d{2})\b', og_title)
    return int(matches[0]) if matches else None


def resolve_year(season_raw, og_title):
    """
    Return the best year we can determine, checking page body first,
    then falling back to the og:title.
    """
    year = parse_start_year(season_raw)
    if year is not None:
        return year
    return year_from_og_title(og_title)


def is_within_cutoff(year, cutoff=YEAR_CUTOFF):
    """
    Returns True  -> include this set
    Returns False -> skip, too recent
    Returns None  -> year still unknown after all fallbacks
    """
    if year is None:
        return None
    return year <= cutoff


# --------------------------------------------------
# SKIPPED SET LOG
# --------------------------------------------------

SKIPPED_LOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "skipped_sets.txt"
)

def _load_logged_urls():
    """FIX 1: Return the set of URLs already in the skipped log."""
    if not os.path.exists(SKIPPED_LOG_PATH):
        return set()
    seen = set()
    with open(SKIPPED_LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            # Lines are: "title | season | reason | url"
            parts = line.strip().split(" | ")
            if len(parts) >= 4:
                seen.add(parts[-1])
    return seen

_LOGGED_URLS = None   # loaded once per run in main()

def log_skipped(og_title, season_raw, url, reason="too recent"):
    """Append one line to the log — but never duplicate a URL."""
    global _LOGGED_URLS
    if url in _LOGGED_URLS:
        return
    line = "{} | {} | {} | {}\n".format(
        og_title or "(unknown title)",
        season_raw or "(unknown year)",
        reason,
        url
    )
    with open(SKIPPED_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line)
    _LOGGED_URLS.add(url)


def init_skipped_log():
    """Write header if file doesn't exist yet; load existing URLs."""
    global _LOGGED_URLS
    if not os.path.exists(SKIPPED_LOG_PATH):
        with open(SKIPPED_LOG_PATH, "w", encoding="utf-8") as f:
            f.write("Set Name | Season | Reason | Source URL\n")
            f.write("-" * 80 + "\n")
    _LOGGED_URLS = _load_logged_urls()


# --------------------------------------------------
# URL CACHE  (persistent fast-skip for all processed URLs)
# --------------------------------------------------

def load_url_cache():
    """
    Load every URL the scraper has ever processed from scraper_url_cache.txt.
    Returns a set of URL strings.
    """
    if not os.path.exists(URL_CACHE_PATH):
        return set()
    with open(URL_CACHE_PATH, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def append_url_cache(url):
    """Append a single URL to the cache file (one per line)."""
    with open(URL_CACHE_PATH, "a", encoding="utf-8") as f:
        f.write(url + "\n")


# --------------------------------------------------
# FAST-SKIP INDEX
# --------------------------------------------------

def build_downloaded_urls():
    """
    Scan all existing export.json files and return a set of source_url
    values for sets that have already been downloaded.

    This lets the main loop skip already-processed URLs with a simple set
    lookup instead of fetching the page just to discover the folder exists.
    Only reads the first 2 KB of each file (source_url is always at the top).
    """
    seen = set()
    if not os.path.isdir(BASE_FOLDER):
        return seen
    pat = re.compile(r'"source_url"\s*:\s*"([^"]+)"')
    for folder in os.listdir(BASE_FOLDER):
        jpath = os.path.join(BASE_FOLDER, folder, "export.json")
        if not os.path.exists(jpath):
            continue
        try:
            with open(jpath, encoding="utf-8", errors="replace") as f:
                head = f.read(2000)
            m = pat.search(head)
            if m:
                seen.add(m.group(1))
        except Exception:
            pass
    return seen


# --------------------------------------------------
# LINK COLLECTION
# --------------------------------------------------

def get_index_urls(parent_url):
    """
    Fetch the parent/home page and return all index page URLs.
    Index pages are identified by having 'index' in their URL path.
    We deliberately exclude individual set pages (which contain '/20' but
    not 'index') so we don't accidentally scrape the home page's recent posts.
    """
    print("Fetching parent page to discover index URLs: " + parent_url)
    r = requests.get(parent_url, timeout=30)
    soup = BeautifulSoup(r.text, 'html.parser')
    index_urls = []
    seen = set()
    for a in soup.find_all('a', href=True):
        href = a['href']
        label = a.get_text(strip=True)
        if ('cartophilic-info-exch' in href
                and 'index' in href.lower()
                and href not in seen):
            index_urls.append((label, href))
            seen.add(href)
    print("Found {} index pages:".format(len(index_urls)))
    for label, url in index_urls:
        print("  [{}]  {}".format(label, url))
    return index_urls


def get_checklist_links(index_url):
    """
    Fetch a single index page and return all individual set URLs listed on it.
    A set URL is any cartophilic link that contains '/20' (the year path segment
    common to all blog post URLs) but does NOT contain 'index' in the path.
    """
    print("  Scanning index: " + index_url)
    r = requests.get(index_url, timeout=30)
    soup = BeautifulSoup(r.text, 'html.parser')
    links = []
    seen = set()
    for a in soup.find_all('a', href=True):
        href = a['href']
        if (href.startswith('http')
                and "cartophilic-info-exch" in href
                and "/20" in href
                and "index" not in href.lower()
                and href not in seen):
            links.append(href)
            seen.add(href)
    print("    -> {} set links found".format(len(links)))
    return links


def collect_all_links(parent_url):
    """
    Discover all index pages from the parent URL, then gather every set link
    from each index page.  Deduplicates across indexes so each set URL appears
    only once in the final list even if it is referenced by multiple indexes.
    """
    index_pages = get_index_urls(parent_url)
    all_links = []
    seen = set()
    for label, idx_url in index_pages:
        for link in get_checklist_links(idx_url):
            if link not in seen:
                all_links.append(link)
                seen.add(link)
    print("\nTotal unique set links across all indexes: {}".format(len(all_links)))
    return all_links


# --------------------------------------------------
# METADATA EXTRACTION
# --------------------------------------------------

def _is_leaf_span(span):
    """
    FIX 2: Return True only if this span has no child <span> or <b> tags.
    Parent containers produce multi-line text blobs that shift all the
    field mappings — we want leaf-level elements only.
    """
    return not span.find(['span', 'b'])


def extract_header_meta(block, og_title=None):
    """
    Post header order:
        0. Season       e.g. "2019-20" or "1992"
        1. Set name     e.g. "1.FC Kaiserslautern Autogrammkarten"
        2. Team name    e.g. "1.FC Kaiserslautern"
        3. Country      e.g. "Germany"
        4. Card count   e.g. "25? cards" or "52 stickers"

    Three passes from strictest to most permissive.
    FIX 2: We now only consider LEAF spans (no nested children) so that
    parent containers with multi-line blobs don't corrupt the mapping.
    """
    meta = {
        "season_raw": None,
        "set_name": None,
        "publisher": None,
        "country": None,
        "total_cards_raw": None,
        "total_cards": None,
        "count_is_approximate": False,
    }

    numbered_pat = re.compile(r'^\d+\.')

    def iter_header(spans, bold_only):
        texts = []
        for span in spans:
            # FIX 2: skip non-leaf spans (containers with nested tags)
            if not _is_leaf_span(span):
                continue
            t = clean(span.get_text())
            if not t:
                continue
            if numbered_pat.match(t):
                break
            is_bold = (span.name == 'b') or bool(span.parent and span.parent.name == 'b')
            if bold_only and not is_bold:
                continue
            if t not in texts:
                texts.append(t)
                yield t
            if len(texts) >= 5:
                break

    # Pass 1: direct children, bold leaf spans only
    header_texts = list(iter_header(
        block.find_all(['span', 'b'], recursive=False), bold_only=True))

    # Pass 2: all leaf spans, bold only
    if len(header_texts) < 3:
        header_texts = list(iter_header(block.find_all(['span', 'b']), bold_only=True))

    # Pass 3: all leaf spans, any style (older post formats)
    if len(header_texts) < 3:
        header_texts = list(iter_header(block.find_all(['span', 'b']), bold_only=False))

    for i, txt in enumerate(header_texts[:5]):
        if i == 0:
            meta["season_raw"] = txt
        elif i == 1:
            meta["set_name"] = txt
        elif i == 2:
            meta["publisher"] = txt
        elif i == 3:
            meta["country"] = txt
        elif i == 4:
            meta["total_cards_raw"] = txt
            m = re.search(r'(\d+)', txt)
            if m:
                meta["total_cards"] = int(m.group(1))
            meta["count_is_approximate"] = '?' in txt

    # Lightweight field-shift correction:
    # If country looks like a card count (e.g. "192 photos", "120 cards") and
    # total_cards_raw does not contain a number, the author omitted the country
    # field and everything shifted left by one position. Move the value across.
    CARD_COUNT_PAT = re.compile(
        r'^\d+\??\s*(cards?|stickers?|photos?|prints?|postcards?|'
        r'cigarette cards?|trade cards?|album pages?|badges?|wrappers?|'
        r'known|in set|confirmed)',
        re.IGNORECASE
    )
    country_val = meta.get("country") or ""
    cards_val   = meta.get("total_cards_raw") or ""
    if CARD_COUNT_PAT.match(country_val) and not CARD_COUNT_PAT.match(cards_val):
        # country slot actually holds the card count -- shift it over
        meta["total_cards_raw"] = country_val
        m = re.search(r'(\d+)', country_val)
        if m:
            meta["total_cards"] = int(m.group(1))
        meta["count_is_approximate"] = '?' in country_val
        meta["country"] = None

    return meta


# --------------------------------------------------
# DESCRIPTION EXTRACTION
# --------------------------------------------------

def extract_description(block):
    """
    Capture any free-text notes the blog author wrote between the header
    metadata block and the start of the numbered checklist.

    The header block is always exactly 5 fields (season, set name, publisher,
    country, card count), all formatted in bold. After those 5 fields, any
    remaining non-checklist text -- bold or plain -- is description content.
    Some authors bold their description paragraphs, so we cannot use bold as
    a reliable signal once the header fields are exhausted.

    Returns a single stripped string joining all description paragraphs,
    or None if nothing is found.
    """
    if not block:
        return None

    numbered_pat = re.compile(r'^\d+\.\s')
    inner = block.find('div') or block

    description_parts = []
    header_fields_seen = 0
    past_header = False

    for child in inner.children:
        if not hasattr(child, 'name') or not child.name:
            continue

        text = clean(child.get_text())
        if not text:
            continue

        # Numbered checklist line -- stop
        if numbered_pat.match(text):
            break

        has_bold = bool(child.find('b'))

        if not past_header:
            if has_bold and header_fields_seen < 5:
                # Still consuming header fields
                header_fields_seen += 1
                if header_fields_seen >= 5:
                    past_header = True
            elif header_fields_seen > 0:
                # Non-bold text after at least one header field -- past header
                past_header = True
                description_parts.append(text)
        else:
            description_parts.append(text)

    if not description_parts:
        return None

    return ' '.join(description_parts)


# --------------------------------------------------
# CHECKLIST EXTRACTION
# --------------------------------------------------

def extract_checklist(block):
    """
    Returns list of: { "card_number": int, "player_name": str, "confirmed": bool }

    Requires a literal period after the card number so metadata lines like
    "100 cards" or "25 stickers" are not mistakenly captured as cards.
    """
    pattern = re.compile(r'^(\d+)\.\s*(.*)')
    not_confirmed_pat = re.compile(r'\s*-+\s*not confirmed\s*$', re.IGNORECASE)

    seen = set()
    cards = []

    for span in block.find_all('span'):
        txt = clean(span.get_text())
        m = pattern.match(txt)
        if not m:
            continue

        card_num = int(m.group(1))
        card_text = clean(m.group(2))

        confirmed = True
        if not_confirmed_pat.search(card_text):
            confirmed = False
            card_text = not_confirmed_pat.sub('', card_text).strip()

        card_text = re.sub(r'\s*-+\s*$', '', card_text).strip()

        key = str(card_num) + "_" + card_text
        if key not in seen:
            cards.append({
                "card_number": card_num,
                "player_name": card_text,
                "confirmed": confirmed
            })
            seen.add(key)

    cards.sort(key=lambda x: x["card_number"])
    return cards


# --------------------------------------------------
# IMAGE HANDLING
# --------------------------------------------------

def download_images(block, folder, base_url):
    if SKIP_IMAGES:
        return []
    imgs = block.find_all('img') if block else []
    saved = []
    for img in imgs:
        img_url = img.get('src') or img.get('data-src')
        if not img_url:
            continue
        if not img_url.startswith('http'):
            img_url = urljoin(base_url, img_url)
        try:
            img_data = requests.get(img_url, timeout=15).content
            filename = img_url.split('/')[-1].split('?')[0].replace('%20', '_')
            if not filename.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                filename += '.jpg'
            with open(os.path.join(folder, filename), 'wb') as fh:
                fh.write(img_data)
            saved.append(filename)
            print("    - image: " + filename)
        except Exception as e:
            print("    [ERR] image " + str(img_url) + ": " + str(e))
    return saved


# --------------------------------------------------
# SAVE FUNCTIONS
# --------------------------------------------------

def save_json(folder, url, og_title, meta, cards, images, description=None):
    data = {
        "source_url":           url,
        "og_title":             og_title,
        "set_name":             meta.get("set_name") or og_title,
        "publisher":            meta.get("publisher"),
        "country":              meta.get("country"),
        "season_raw":           meta.get("season_raw"),
        "total_cards_raw":      meta.get("total_cards_raw"),
        "total_cards":          meta.get("total_cards"),
        "count_is_approximate": meta.get("count_is_approximate", False),
        "cards_found":          len(cards),
        "description":          description,
        "checklist":            cards,
        "images":               images,
        "scraped_at":           datetime.utcnow().isoformat() + "Z"
    }
    with open(os.path.join(folder, "export.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def save_summary_txt(folder, url, data):
    lines = [
        "Source:     " + url,
        "Set Name:   " + str(data.get('set_name')),
        "Publisher:  " + str(data.get('publisher')),
        "Country:    " + str(data.get('country')),
        "Season:     " + str(data.get('season_raw')),
        "Cards:      " + str(data.get('total_cards_raw')) + " (parsed: " + str(data.get('total_cards')) + ")",
        "In List:    " + str(data.get('cards_found')),
        "Scraped:    " + str(data.get('scraped_at')),
    ]
    if data.get('description'):
        lines += ["", "-- DESCRIPTION --", data.get('description')]
    lines += [
        "",
        "-- CHECKLIST --",
    ]
    for card in data.get("checklist", []):
        status = "" if card["confirmed"] else "  [not confirmed]"
        lines.append("  {:>3}. {}{}".format(card['card_number'], card['player_name'], status))
    with open(os.path.join(folder, "summary.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main():
    os.makedirs(BASE_FOLDER, exist_ok=True)
    init_skipped_log()   # loads existing logged URLs for dedup (FIX 1)

    # Build fast-skip index from three sources:
    #   1. scraper_url_cache.txt  -- every URL ever processed (fastest, most complete)
    #   2. export.json source_url fields  -- downloaded sets not yet in cache
    #   3. skipped_sets.txt       -- sets skipped as too-recent or unknown-year
    if not FORCE_REPROCESS:
        cached_urls     = load_url_cache()
        downloaded_urls = build_downloaded_urls()
        fast_skip_urls  = cached_urls | downloaded_urls | _LOGGED_URLS
        print("Fast-skip index: {} cached + {} from JSON + {} from skip log = {} unique".format(
            len(cached_urls), len(downloaded_urls), len(_LOGGED_URLS), len(fast_skip_urls)))
    else:
        fast_skip_urls = set()
        print("FORCE_REPROCESS=True -- fast-skip index disabled")

    links = collect_all_links(PARENT_URL)
    print("\nProcessing up to {} sets (year <= {}), offset {}.".format(
        CHECKLIST_LIMIT, YEAR_CUTOFF, START_OFFSET))
    print("FORCE_REPROCESS={} | SKIP_IMAGES={}".format(FORCE_REPROCESS, SKIP_IMAGES))
    print("")

    exported_count      = 0
    skipped_recent      = 0
    skipped_exists      = 0
    skipped_unknown_yr  = 0

    for url in links[START_OFFSET:]:
        if exported_count >= CHECKLIST_LIMIT:
            break

        # Fast skip -- no network request needed for already-processed URLs
        if url in fast_skip_urls:
            skipped_exists += 1
            continue

        # Fetch
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
        except Exception as e:
            print("[ERR] Fetch failed " + url + ": " + str(e))
            time.sleep(WAIT_SECONDS)
            continue

        soup = BeautifulSoup(r.text, 'html.parser')

        og_title_tag = soup.find("meta", property="og:title")
        og_title = clean(og_title_tag["content"]) \
            if og_title_tag and "content" in og_title_tag.attrs else None

        # Extract header metadata (needed for year check)
        block = soup.find('div', class_='post-body entry-content')
        meta = extract_header_meta(block, og_title) if block else {}
        season_raw = meta.get("season_raw")

        # FIX 3: resolve year from page body first, then og:title fallback
        year = resolve_year(season_raw, og_title)
        decision = is_within_cutoff(year)

        if decision is False:
            label = og_title or url_slug(url)
            print("  [SKIP recent {}] {}".format(year, label))
            log_skipped(og_title, season_raw or str(year), url,
                        reason="too recent (>{})".format(YEAR_CUTOFF))
            fast_skip_urls.add(url)
            append_url_cache(url)
            skipped_recent += 1
            continue

        if decision is None:
            label = og_title or url_slug(url)
            print("  [SKIP unknown year] {}".format(label))
            log_skipped(og_title, season_raw, url, reason="year unknown")
            fast_skip_urls.add(url)
            append_url_cache(url)
            skipped_unknown_yr += 1
            continue

        # Year is within cutoff -- proceed
        folder_name = safe_folder_name(og_title) if og_title else url_slug(url)
        folder = os.path.join(BASE_FOLDER, folder_name)

        if os.path.exists(folder) and not FORCE_REPROCESS:
            # Folder exists but wasn't in the index (e.g. hollowed child folder)
            print("  [SKIP exists] " + folder_name)
            fast_skip_urls.add(url)
            append_url_cache(url)
            skipped_exists += 1
            continue

        if not block:
            print("  [ERR] No post body: " + url)
            continue

        os.makedirs(folder, exist_ok=True)

        cards       = extract_checklist(block)
        images      = download_images(block, folder, url)
        description = extract_description(block)
        data        = save_json(folder, url, og_title, meta, cards, images, description)
        save_summary_txt(folder, url, data)

        print("[OK] [{}/{}] {}".format(exported_count + 1, CHECKLIST_LIMIT, og_title))
        print("     Season: {} | Country: {} | Cards: {} ({})".format(
            meta.get('season_raw'), meta.get('country'),
            meta.get('total_cards'), meta.get('total_cards_raw')))
        print("     Checklist rows: {} | Images: {} | Description: {}".format(
            len(cards), len(images), "yes" if description else "none"))

        fast_skip_urls.add(url)
        append_url_cache(url)
        exported_count += 1
        time.sleep(WAIT_SECONDS)

    print("")
    print("=" * 50)
    print("Run complete.")
    print("  Exported (included):      {}".format(exported_count))
    print("  Skipped (too recent):     {}".format(skipped_recent))
    print("  Skipped (unknown year):   {}".format(skipped_unknown_yr))
    print("  Skipped (already exists): {}".format(skipped_exists))
    print("  Output:      " + BASE_FOLDER)
    print("  Skipped log: " + SKIPPED_LOG_PATH)
    print("  URL cache:   " + URL_CACHE_PATH)


if __name__ == "__main__":
    main()
