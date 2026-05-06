"""
Universal docs → Markdown dumper (no args, single script).

"""

import asyncio
import hashlib
import json
import re
import signal
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse, urljoin

from crawl4ai import (
    AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode,
    AsyncUrlSeeder, SeedingConfig, MemoryAdaptiveDispatcher
)
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
from crawl4ai.content_scraping_strategy import LXMLWebScrapingStrategy
from lxml import html as LH

def folder_name_from_url(url: str) -> str:
    """Generate a unique folder name based on the URL's domain and path."""
    p = urlparse(url)
    domain = p.netloc.lower()
    path = p.path.strip("/")
    
    # Clean domain name (remove www, ports)
    domain = domain.replace("www.", "").replace(":", "_")
    
    # Create folder name components
    parts = [domain]
    if path:
        # Take first few path segments for uniqueness
        path_parts = [seg for seg in path.split("/") if seg][:2]
        if path_parts:
            safe_path = "_".join(re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-") or "section" for s in path_parts)
            parts.append(safe_path)
    
    folder_name = "_".join(parts)
    # Ensure folder name is filesystem safe
    folder_name = re.sub(r"[^A-Za-z0-9._-]+", "-", folder_name).strip("-")
    return folder_name or "doc_dump"


TEST_MODE = "--test" in sys.argv
if TEST_MODE:
    sys.argv.remove("--test")

UNIQUE_ON = True
if "--unique" in sys.argv:
    UNIQUE_ON = True
    sys.argv.remove("--unique")
if "--no-unique" in sys.argv:
    UNIQUE_ON = False
    sys.argv.remove("--no-unique")

try:
    if len(sys.argv) > 1:
        START_URL = sys.argv[1].strip()
    else:
        START_URL = input("Enter the doc URL to crawl: ").strip()
except (EOFError, IndexError):
    START_URL = ""

if not START_URL:
    print("Usage: python doc_to_md.py [--test] <URL>", file=sys.stderr)
    print("Example: python doc_to_md.py --test https://docs.example.com/page.html", file=sys.stderr)
    sys.exit(2)

OUTPUT_DIR = folder_name_from_url(START_URL)
CONCURRENCY = 16
PAGE_TIMEOUT_MS = 30000
MIN_DUP_WORDS = 100

USE_COOKIE_SELECTORS = False
USE_TARGET_SELECTORS = False

BINARY_EXT = {
    ".zip", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".bmp", ".tiff", ".mp4", ".webm", ".mp3", ".wav", ".mov",
    ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".dmg", ".exe", ".msi"
}

COOKIE_SEL = [
    "#onetrust-banner-sdk", "#onetrust-consent-sdk", ".ot-sdk-container",
    ".osano-cm-window", ".osano-cm-widget", ".cc-window",
    ".cookie", ".cookies", "#cookie", "#cookies",
    ".consent", "#consent", ".gdpr", ".cmp-container", "#cmpbox",
    ".truste_banner", ".privacy-banner", ".cookie-consent", ".cookie-notice",
    ".footer-cookie", ".consent-banner", "#privacy-cookies",
    ".doc-feedback", ".feedback", "#feedback", ".rating", ".rating-widget"
]


TARGETS = [
    "main", "article", "#content", ".content", ".page-content"
]


def normalize_url(u: str) -> str:
    p = urlparse(u)
    p = p._replace(fragment="")
    netloc = p.netloc.lower().replace(":80", "").replace(":443", "")
    path = (p.path or "/").rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), netloc, path, p.params, p.query, ""))


def base_prefix(url: str):
    p = urlparse(url)
    path = p.path
    if not path.endswith("/"):
        path = path.rsplit("/", 1)[0] + "/"
    root = f"{p.scheme}://{p.netloc}"
    base = root + path
    return root, p.netloc.lower(), base


def in_scope(u: str, host: str, base: str) -> bool:
    nu = normalize_url(u)
    if not nu.startswith(("http://", "https://")):
        return False
    pp = urlparse(nu)
    if pp.netloc.lower() != host:
        return False
    return nu.startswith(base)


def has_binary_ext(u: str) -> bool:
    path = urlparse(u).path.lower()
    return any(path.endswith(ext) for ext in BINARY_EXT)


def slug_from_url(u: str) -> str:
    p = urlparse(u)
    path = p.path
    if path.endswith("/"):
        path += "index"
    if path.endswith(".html") or path.endswith(".htm"):
        path = path[: path.rfind(".")]
    segs = [s for s in path.split("/") if s]
    safe = [re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-") or "section" for s in segs]
    return ("-".join(safe) or "index") + ".md"


def extract_links_html(html_str: str, base_url: str, host: str, base_prefix: str) -> set[str]:
    out: set[str] = set()
    if not html_str:
        return out
    try:
        doc = LH.fromstring(html_str)
    except Exception:
        return out
    for el in doc.xpath("//a[@href]"):
        href = (el.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        absu = urljoin(base_url, href)
        n = normalize_url(absu)
        if in_scope(n, host, base_prefix) and not has_binary_ext(n):
            out.add(n)
    return out


def extract_canonical(html_str: str, base_url: str, host: str, base_prefix: str) -> str | None:
    if not html_str:
        return None
    try:
        doc = LH.fromstring(html_str)
    except Exception:
        return None
    for href in doc.xpath("//link[translate(@rel,'CANONICAL','canonical')='canonical' and @href]/@href"):
        n = normalize_url(urljoin(base_url, href.strip()))
        if in_scope(n, host, base_prefix):
            return n
    for href in doc.xpath("//meta[translate(@property,'OG:URL','og:url')='og:url' and @content]/@content"):
        n = normalize_url(urljoin(base_url, href.strip()))
        if in_scope(n, host, base_prefix):
            return n
    return None


class DocDumper:
    def __init__(self, start_url: str, out_dir: str, dedup: bool = True):
        self.start_url = normalize_url(start_url)
        self.root, self.host, self.base = base_prefix(self.start_url)
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.html_dir = self.out / "html_dump"
        self.html_dir.mkdir(exist_ok=True)
        self.progress_file = self.out / "progress.json"

        self.seen_urls: set[str] = set()
        self.saved_urls: set[str] = set()
        self.content_hashes: set[str] = set()
        self.url_to_file: dict[str, str] = {}
        self.crawl_queue: list[str] = []
        self.enqueued_urls: set[str] = set()

        self.dedup_enabled = dedup
        self._chunk_db: dict[str, int] = {}
        self._shutdown_requested = False
        
        self._load_progress()
        
        self._setup_signal_handler()

        self._save_progress()

        self.md_gen = DefaultMarkdownGenerator(
            content_source="cleaned_html",
            options=dict(ignore_images=True),
        )

        self.browser_cfg = BrowserConfig(
            headless=True,
            browser_type="chromium",
            java_script_enabled=True,
            text_mode=True,
            light_mode=True,
        )

        self.run_cfg = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            scraping_strategy=LXMLWebScrapingStrategy(),
            markdown_generator=self.md_gen,

            target_elements=TARGETS if USE_TARGET_SELECTORS else None,
            excluded_tags=["script", "style"],
            excluded_selector=",".join(COOKIE_SEL) if USE_COOKIE_SELECTORS else "",

            exclude_external_links=False,
            process_iframes=True,
            remove_overlay_elements=True,

            scan_full_page=True,

            check_robots_txt=False,
            page_timeout=PAGE_TIMEOUT_MS,
            verbose=False,
            stream=True,
            semaphore_count=CONCURRENCY,
        )

    def _load_progress(self):
        """Load progress from progress.json if it exists."""
        if not self.progress_file.exists():
            return
        
        try:
            with open(self.progress_file, 'r', encoding='utf-8') as f:
                progress = json.load(f)
            
            if not isinstance(progress, dict) or 'metadata' not in progress or 'state' not in progress:
                print(f"[RESUME][WARN] Invalid progress file structure, starting fresh")
                return
            
            metadata = progress.get('metadata', {})
            if metadata.get('start_url') != self.start_url:
                print(f"[RESUME][WARN] Progress file has different start_url, starting fresh")
                return
            
            if metadata.get('host') != self.host:
                print(f"[RESUME][WARN] Progress file has different host, starting fresh")
                return
            
            state = progress.get('state', {})
            
            seen_urls = state.get('seen_urls', [])
            if isinstance(seen_urls, list):
                self.seen_urls = set(seen_urls)
            else:
                print(f"[RESUME][WARN] Invalid seen_urls format, ignoring")
                
            saved_urls = state.get('saved_urls', [])
            if isinstance(saved_urls, list):
                self.saved_urls = set(saved_urls)
            else:
                print(f"[RESUME][WARN] Invalid saved_urls format, ignoring")
                
            content_hashes = state.get('content_hashes', [])
            if isinstance(content_hashes, list):
                self.content_hashes = set(content_hashes)
            else:
                print(f"[RESUME][WARN] Invalid content_hashes format, ignoring")
            
            url_to_file = state.get('url_to_file', {})
            if isinstance(url_to_file, dict):
                self.url_to_file = url_to_file
            else:
                print(f"[RESUME][WARN] Invalid url_to_file format, ignoring")
            
            chunk_db = state.get('chunk_db', {})
            if isinstance(chunk_db, dict):
                try:
                    self._chunk_db = {k: int(v) for k, v in chunk_db.items()}
                except (ValueError, TypeError):
                    print(f"[RESUME][WARN] Invalid chunk_db format, ignoring deduplication state")
                    self._chunk_db = {}
            else:
                print(f"[RESUME][WARN] Invalid chunk_db format, ignoring")
            
            queue = state.get('queue', [])
            if isinstance(queue, list):
                self.crawl_queue = queue
            else:
                print(f"[RESUME][WARN] Invalid queue format, ignoring")
                
            enqueued = state.get('enqueued', [])
            if isinstance(enqueued, list):
                self.enqueued_urls = set(enqueued)
            else:
                print(f"[RESUME][WARN] Invalid enqueued format, ignoring")
            
            print(f"[RESUME][OK] Loaded progress: {len(self.saved_urls)} files saved, {len(self.seen_urls)} URLs seen")
            if self.crawl_queue:
                print(f"[RESUME][OK] Queue contains {len(self.crawl_queue)} URLs to process")
            
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"[RESUME][ERROR] Progress file is corrupted: {e}")
            print("[RESUME] Starting fresh...")
        except (IOError, OSError) as e:
            print(f"[RESUME][ERROR] Cannot read progress file: {e}")
            print("[RESUME] Starting fresh...")
        except Exception as e:
            print(f"[RESUME][ERROR] Unexpected error loading progress: {e}")
            print("[RESUME] Starting fresh...")

    def _save_progress(self):
        """Save current progress to progress.json."""
        try:
            progress = {
                "metadata": {
                    "start_url": self.start_url,
                    "host": self.host,
                    "dedup_enabled": self.dedup_enabled
                },
                "state": {
                    "seen_urls": list(self.seen_urls),
                    "saved_urls": list(self.saved_urls),
                    "content_hashes": list(self.content_hashes),
                    "url_to_file": self.url_to_file,
                    "chunk_db": self._chunk_db,
                    "queue": self.crawl_queue,
                    "enqueued": list(self.enqueued_urls)
                }
            }
            
            with open(self.progress_file, 'w', encoding='utf-8') as f:
                json.dump(progress, f, indent=2, ensure_ascii=False)
                
        except Exception as e:
            print(f"[PROGRESS][ERROR] Failed to save progress: {e}")

    def _setup_signal_handler(self):
        """Set up signal handler for graceful shutdown."""
        def signal_handler(signum, frame):
            print(f"\n[SHUTDOWN] Received signal {signum}, saving progress...")
            self._shutdown_requested = True
            self._save_progress()
            print("[SHUTDOWN] Progress saved. Exiting...")
            sys.exit(130)
        
        signal.signal(signal.SIGINT, signal_handler)
        if hasattr(signal, 'SIGTERM'):
            signal.signal(signal.SIGTERM, signal_handler)

    async def seed_urls(self) -> list[str]:
        print(f"[SEED] host={self.host} base={self.base}")
        urls: list[str] = []
        try:
            async with AsyncUrlSeeder() as seeder:
                cfg = SeedingConfig(source="sitemap", extract_head=False, pattern="*")
                found = await seeder.urls(self.host, cfg)
                urls = [normalize_url(u.get("url", "")) for u in found
                        if u.get("status") == "valid" and u.get("url")]
        except Exception as e:
            print(f"[SEED][WARN] {e}")
        urls = [u for u in urls if in_scope(u, self.host, self.base) and not has_binary_ext(u)]
        if self.start_url not in urls:
            urls.insert(0, self.start_url)
        
        original_count = len(urls)
        if self.seen_urls:
            urls = [u for u in urls if u not in self.seen_urls]
            print(f"[SEED] Filtered out {original_count - len(urls)} already seen URLs")
        
        uniq, seen = [], set()
        for u in urls:
            if u not in seen:
                seen.add(u)
                uniq.append(u)
        print(f"[SEED] in-scope = {len(uniq)}")
        return uniq

    def canonicalize(self, url: str, html_str: str) -> str:
        can = extract_canonical(html_str, url, self.host, self.base)
        if can and in_scope(can, self.host, self.base) and not has_binary_ext(can):
            return can
        return url

    async def crawl(self):
        if self.crawl_queue:
            print(f"[RESUME] Continuing with {len(self.crawl_queue)} URLs in queue")
            queue = [url for url in self.crawl_queue if url not in self.seen_urls]
            enq = set(self.enqueued_urls)
            print(f"[RESUME] Filtered queue: {len(queue)} URLs remaining to process")
        else:
            if TEST_MODE:
                print("[TEST] Test mode: crawling single URL only")
                seeds = [self.start_url]
            else:
                seeds = await self.seed_urls()
                if not seeds:
                    seeds = [self.start_url]
            queue = list(seeds)
            enq = set(queue)

        dispatcher = MemoryAdaptiveDispatcher(
            memory_threshold_percent=80.0,
            check_interval=1.0,
            max_session_permit=CONCURRENCY
        )

        async with AsyncWebCrawler(config=self.browser_cfg) as crawler:
            batch_size = 200
            hard_cap = 300_000
            batch_count = 0

            while queue and len(self.seen_urls) < hard_cap and not self._shutdown_requested:
                batch = []
                while queue and len(batch) < batch_size:
                    u = queue.pop(0)
                    if not has_binary_ext(u):
                        batch.append(u)
                if not batch:
                    break

                print(f"[BATCH] Processing {len(batch)} URLs concurrently...")
                
                batch_seen_start = len(self.seen_urls)
                
                cfg = self.run_cfg.clone()
                results = await crawler.arun_many(
                    urls=batch,
                    config=cfg,
                    dispatcher=dispatcher
                )
                new_urls_found = False
                async for res in results:
                    raw = normalize_url(getattr(res, "url", ""))
                    self.seen_urls.add(raw)

                    if not getattr(res, "success", False):
                        continue

                    html_str = getattr(res, "html", None) or getattr(res, "cleaned_html", "")
                    cur = self.canonicalize(raw, html_str)

                    await self.save_if_unique(cur, res)

                    lib_set: set[str] = set()
                    links_field = getattr(res, "links", None) or {}
                    for item in links_field.get("internal", []):
                        href = item.get("href") if isinstance(item, dict) else None
                        if not href:
                            continue
                        n = normalize_url(urljoin(cur, href))
                        if in_scope(n, self.host, self.base) and not has_binary_ext(n):
                            lib_set.add(n)

                    dom_set = extract_links_html(html_str, cur, self.host, self.base)

                    if not TEST_MODE:
                        for v in (lib_set | dom_set):
                            if v not in enq:
                                enq.add(v)
                                queue.append(v)
                                new_urls_found = True

                batch_processed = len(self.seen_urls) - batch_seen_start
                unique_queue_size = len(set(queue))
                print(f"[BATCH] Completed. Processed: {batch_processed}, Queue size: {unique_queue_size}, Total seen: {len(self.seen_urls)}")
                
                self.crawl_queue = queue
                self.enqueued_urls = enq
                
                if new_urls_found or batch_processed > 0:
                    self._save_progress()

    async def save_if_unique(self, url: str, res):
        if not in_scope(url, self.host, self.base) or url in self.saved_urls:
            return

        md_obj = res.markdown
        md = getattr(md_obj, "fit_markdown", None) or getattr(md_obj, "raw_markdown", str(md_obj))
        text_norm = re.sub(r"\s+", " ", md).strip()
        if not text_norm:
            return

        h = hashlib.blake2s(text_norm.encode("utf-8")).hexdigest()
        if h in self.content_hashes:
            return
        self.content_hashes.add(h)

        title = None
        if getattr(res, "head_data", None):
            title = res.head_data.get("title")
        if not title:
            m = re.search(r"^#\s+(.+)$", md, flags=re.MULTILINE)
            if m:
                title = m.group(1).strip()

        front = {
            "source_url": url,
            "title": title,
            "html_length": len(getattr(res, "cleaned_html", "") or getattr(res, "html", "") or ""),
        }
        yaml = "---\n" + "\n".join(
            f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in front.items() if v is not None
        ) + "\n---\n\n"

        md_clean = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', md)

        if self.dedup_enabled:
            md_clean = self._strip_repeating_chunks(md_clean)

        fname = slug_from_url(url)
        target = self.out / fname
        if target.exists():
            fname = fname[:-3] + "-dup.md"
            target = self.out / fname
        target.write_text(yaml + md_clean, encoding="utf-8")
        
        html_content = getattr(res, "cleaned_html", "") or getattr(res, "html", "")
        if html_content:
            html_fname = fname[:-3] + ".html"
            html_target = self.html_dir / html_fname
            html_target.write_text(html_content, encoding="utf-8")
        
        self.url_to_file[url] = fname
        self.saved_urls.add(url)
        print(f"[SAVE][OK] {url} -> {fname}")

    def _strip_repeating_chunks(self, text: str) -> str:
        """
        Remove any paragraph‑level chunk (≥MIN_DUP_WORDS) that has already appeared
        in earlier pages.  Uses a fast blake2s hash of a normalised paragraph.
        """
        out, changed = [], False
        for para in text.split("\n\n"):
            w = para.split()
            if len(w) >= MIN_DUP_WORDS:
                norm = re.sub(r"\s+", " ", para).strip().lower()
                h = hashlib.blake2s(norm.encode("utf-8")).hexdigest()
                if h in self._chunk_db:
                    changed = True
                    self._chunk_db[h] += 1
                    out.append("> **[possible repeating chunk]**")
                    continue
                self._chunk_db[h] = 1
            out.append(para)
        return "\n\n".join(out) if changed else text

    async def run(self):
        print(f"[INIT] host={urlparse(self.start_url).netloc.lower()} base={self.base}")
        try:
            await self.crawl()
        finally:
            self._save_progress()
            
        manifest = {
            "start_url": self.start_url,
            "host": urlparse(self.start_url).netloc.lower(),
            "base_prefix": self.base,
            "pages": len(self.url_to_file),
            "files": self.url_to_file,
        }
        (Path(OUTPUT_DIR) / "_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), "utf-8"
        )
        print(f"\nDone. Saved {len(self.url_to_file)} unique pages under {OUTPUT_DIR}")
        
        if self.progress_file.exists():
            try:
                self.progress_file.unlink()
                print("[CLEANUP] Removed progress.json after successful completion")
            except Exception as e:
                print(f"[CLEANUP][WARN] Could not remove progress.json: {e}")


if __name__ == "__main__":
    try:
        dumper = DocDumper(START_URL, OUTPUT_DIR, dedup=UNIQUE_ON)
        asyncio.run(dumper.run())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
