"""Open-Graph link previews for message bodies.

Messages.app draws a card under a URL (title, site, image). We don't get
Apple's `LPLinkMetadata` for MAP history or for balloons we failed to parse,
so the UI fetches the same metadata clientside: pull the page, read
`og:*` / `<title>` / favicon-ish site name, cache the result, and optionally
download a preview image.

Pure stdlib on purpose — no `requests`. Safe enough for chat: only http(s),
timeouts, size caps, and a private-IP block so a crafted URL can't probe the
LAN. Failures are cached briefly so a bad host doesn't get hammered every
time the conversation is opened.
"""
from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import logging
import re
import socket
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen

from iphonebridge import config

log = logging.getLogger(__name__)

# Match http(s) URLs in free text. Trailing sentence punctuation is stripped
# after the match so "see https://x.com/y." still resolves.
_URL_RE = re.compile(
    r"(https?://[^\s<>\"'\]\)\}>,]+)",
    re.IGNORECASE,
)
_TRAIL_PUNCT = ".,;:!?)]}'\"…"

# Chatty enough to get past the usual bot walls without looking like curl.
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# Facebook share/m.me links answer 400 to a browser UA and hand OG tags to
# the crawler UA they document for previews.
_UA_FACEBOOK = (
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
)

_HTML_LIMIT = 512 * 1024  # 512 KiB is plenty for head meta
_IMAGE_LIMIT = 2 * 1024 * 1024
_JSON_LIMIT = 64 * 1024
_FETCH_TIMEOUT = 8.0
# How long a successful preview is trusted without re-fetch.
_OK_TTL_SEC = 30 * 24 * 3600
# How long a miss / error is remembered so we don't thrash.
_FAIL_TTL_SEC = 6 * 3600

# Status values stored in the cache JSON.
STATUS_OK = "ok"
STATUS_FAIL = "fail"
STATUS_PENDING = "pending"

# Bump when the fetcher gains a better source for sites that used to
# soft-succeed with only a hostname (YouTube HTML has no og: tags for us).
# Entries below this are re-fetched even if still inside the TTL.
_CACHE_VERSION = 3

# Generic soft-fail titles from HTML shells (login walls, player shells).
# Treated as weak so a later oEmbed/crawler path can replace them.
_WEAK_TITLES = frozenset({
    "instagram",
    "facebook",
    "facebook.com",
    "spotify",
    "spotify – web player",
    "spotify - web player",
    "youtube",
    "youtube.com",
    "tiktok",
})

# Sites that ship no usable Open Graph to bare GETs. Hit their oEmbed
# endpoint instead — same metadata Messages shows, without rendering JS.
# Each entry: (host match, oEmbed URL builder).
def _oembed_youtube(url: str) -> str:
    return (
        "https://www.youtube.com/oembed?format=json&url="
        + quote(url, safe="")
    )


def _oembed_vimeo(url: str) -> str:
    return "https://vimeo.com/api/oembed.json?url=" + quote(url, safe="")


def _oembed_soundcloud(url: str) -> str:
    return (
        "https://soundcloud.com/oembed?format=json&url="
        + quote(url, safe="")
    )


def _oembed_spotify(url: str) -> str:
    return "https://open.spotify.com/oembed?url=" + quote(url, safe="")


def _oembed_tiktok(url: str) -> str:
    return "https://www.tiktok.com/oembed?url=" + quote(url, safe="")


def _oembed_instagram(url: str) -> str:
    # Trailing slash on the path matters; without it reels often 403.
    return "https://www.instagram.com/api/v1/oembed/?url=" + quote(url, safe="")


_OEMBED_HOSTS: list[tuple[re.Pattern[str], Any]] = [
    (re.compile(r"(?:^|\.)youtube\.com$|(?:^|\.)youtu\.be$", re.I), _oembed_youtube),
    (re.compile(r"(?:^|\.)vimeo\.com$", re.I), _oembed_vimeo),
    (re.compile(r"(?:^|\.)soundcloud\.com$", re.I), _oembed_soundcloud),
    (re.compile(r"(?:^|\.)open\.spotify\.com$|(?:^|\.)spotify\.link$", re.I),
     _oembed_spotify),
    (re.compile(r"(?:^|\.)tiktok\.com$|(?:^|\.)vm\.tiktok\.com$", re.I),
     _oembed_tiktok),
    (re.compile(r"(?:^|\.)instagram\.com$", re.I), _oembed_instagram),
]


def link_preview_dir() -> Path:
    root = config.STATE_DIR / "link_previews"
    (root / "meta").mkdir(parents=True, exist_ok=True)
    (root / "images").mkdir(parents=True, exist_ok=True)
    return root


def extract_urls(text: str | None) -> list[str]:
    """Return unique http(s) URLs found in `text`, in order of appearance."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _URL_RE.finditer(text):
        url = m.group(1).rstrip(_TRAIL_PUNCT)
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def first_url(text: str | None) -> str:
    """The URL a message should preview, or ""."""
    urls = extract_urls(text)
    return urls[0] if urls else ""


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _meta_path(url: str) -> Path:
    return link_preview_dir() / "meta" / f"{_cache_key(url)}.json"


def _is_weak_preview(entry: dict[str, Any]) -> bool:
    """True when the card is basically just a hostname — not worth keeping.

    Early fetches against YouTube/IG/Spotify shells "succeeded" with a
    brand name and no image, then poisoned the 30-day cache. Treat those
    as misses so a better source (oEmbed / crawler UA) can replace them.
    """
    title = (entry.get("title") or "").strip().lower()
    site = (entry.get("site") or "").strip().lower()
    has_image = bool(entry.get("image_path") or entry.get("image_url"))
    if int(entry.get("version") or 0) < _CACHE_VERSION:
        if not title:
            return True
        if title in _WEAK_TITLES and not has_image:
            return True
        if title in (site, site.removeprefix("www.")) and not has_image:
            return True
        # Hostname-as-title with no description is the soft HTML fallback.
        if title == site and not (entry.get("description") or "").strip():
            return True
    # Always re-fetch brand-shell titles without an image, even on the
    # current cache version — they are never a real preview.
    if title in _WEAK_TITLES and not has_image:
        return True
    return False


def load_cached(url: str) -> dict[str, Any] | None:
    """Return a non-expired cache entry, or None."""
    if not url:
        return None
    path = _meta_path(url)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    status = data.get("status")
    fetched = float(data.get("fetched_at") or 0)
    age = time.time() - fetched
    if status == STATUS_OK and age <= _OK_TTL_SEC:
        if _is_weak_preview(data):
            return None
        return data
    if status == STATUS_FAIL and age <= _FAIL_TTL_SEC:
        return data
    return None


def save_cached(entry: dict[str, Any]) -> None:
    url = entry.get("url") or ""
    if not url:
        return
    entry = dict(entry)
    entry.setdefault("fetched_at", time.time())
    path = _meta_path(url)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log.debug("could not cache link preview for %s: %s", url, e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _host_is_public(host: str) -> bool:
    """Reject loopback / link-local / private targets (basic SSRF guard)."""
    if not host or host == "localhost":
        return False
    # Strip brackets from IPv6 literals.
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        infos = socket.getaddrinfo(bare, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


def _safe_http_url(url: str) -> str | None:
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if p.scheme not in ("http", "https"):
        return None
    if not p.netloc or not _host_is_public(p.hostname or ""):
        return None
    return url


class _MetaParser(HTMLParser):
    """Pull og:/twitter: tags, <title>, and a few fallbacks from HTML head."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self.meta: dict[str, str] = {}
        self._done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._done:
            return
        tag = tag.lower()
        ad = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
            return
        if tag == "meta":
            # property=og:title / name=twitter:title / name=description
            key = (ad.get("property") or ad.get("name") or "").lower().strip()
            content = ad.get("content") or ""
            if key and content and key not in self.meta:
                self.meta[key] = content.strip()
            return
        if tag == "body":
            # Head is over; keep whatever we have.
            self._done = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.title:
            self.title = data.strip()


def parse_html_meta(raw: bytes, base_url: str) -> dict[str, str]:
    """Extract title / description / image / site from an HTML document."""
    # Charset: prefer declared, else utf-8 with replacement.
    text = raw.decode("utf-8", errors="replace")
    # Cheap charset sniff from a meta tag at the top.
    m = re.search(
        r'charset\s*=\s*["\']?([a-zA-Z0-9_\-]+)',
        text[:4000],
        re.I,
    )
    if m:
        enc = m.group(1).strip().lower()
        if enc and enc not in ("utf-8", "utf8"):
            try:
                text = raw.decode(enc, errors="replace")
            except LookupError:
                pass

    parser = _MetaParser()
    try:
        parser.feed(text[:_HTML_LIMIT])
        parser.close()
    except Exception:
        # Broken HTML still yields partial meta from whatever was fed.
        pass

    meta = parser.meta
    title = (
        meta.get("og:title")
        or meta.get("twitter:title")
        or parser.title
        or ""
    )
    description = (
        meta.get("og:description")
        or meta.get("twitter:description")
        or meta.get("description")
        or ""
    )
    image = (
        meta.get("og:image:secure_url")
        or meta.get("og:image")
        or meta.get("twitter:image")
        or meta.get("twitter:image:src")
        or ""
    )
    site = (
        meta.get("og:site_name")
        or meta.get("application-name")
        or ""
    )
    # Absolute-ify a relative image URL against the page we fetched.
    if image:
        image = urljoin(base_url, image)

    # Collapse whitespace; sites love newlines in descriptions.
    def clean(s: str) -> str:
        return re.sub(r"\s+", " ", html.unescape(s)).strip()

    if not site:
        host = urlparse(base_url).hostname or ""
        site = host[4:] if host.startswith("www.") else host

    return {
        "title": clean(title)[:300],
        "description": clean(description)[:500],
        "image_url": image,
        "site": clean(site)[:120],
    }


def _hostname(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _user_agent_for(url: str) -> str:
    """Pick a UA that actually gets metadata for this host.

    Facebook share links return HTTP 400 to a normal browser UA and only
    emit `og:*` tags for the documented crawler string.
    """
    host = _hostname(url)
    if host == "facebook.com" or host.endswith(".facebook.com") or host in (
        "fb.watch", "fb.com", "m.facebook.com",
    ):
        return _UA_FACEBOOK
    return _UA


def _http_get(url: str, *, limit: int, user_agent: str | None = None) -> tuple[bytes, str]:
    """GET `url`; returns (body, final_url). Raises on hard failure."""
    safe = _safe_http_url(url)
    if not safe:
        raise ValueError(f"blocked url: {url}")
    ua = user_agent or _user_agent_for(safe)
    req = Request(
        safe,
        headers={
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/json,image/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
        },
        method="GET",
    )
    # Scheme already restricted to http(s) above; urlopen is intentional.
    with urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
        final = resp.geturl() or safe
        # Re-check redirect target.
        if not _safe_http_url(final):
            raise ValueError(f"blocked redirect: {final}")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = resp.read(64 * 1024)
            if not block:
                break
            total += len(block)
            if total > limit:
                chunks.append(block[: max(0, limit - (total - len(block)))])
                break
            chunks.append(block)
        return b"".join(chunks), final


def _image_ext(url: str, data: bytes) -> str:
    path = urlparse(url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".img"


def _download_image(image_url: str, key: str) -> str:
    """Fetch preview image to the cache dir; return absolute path or ""."""
    if not image_url or not _safe_http_url(image_url):
        return ""
    try:
        data, final = _http_get(image_url, limit=_IMAGE_LIMIT)
    except Exception as e:
        log.debug("preview image fetch failed for %s: %s", image_url, e)
        return ""
    if not data or len(data) < 32:
        return ""
    ext = _image_ext(final, data)
    dest = link_preview_dir() / "images" / f"{key}{ext}"
    try:
        dest.write_bytes(data)
        return str(dest)
    except OSError as e:
        log.debug("could not write preview image: %s", e)
        return ""


def _oembed_endpoint(url: str) -> str | None:
    """oEmbed API URL for providers that block HTML scrapes, else None."""
    host = _hostname(url)
    if not host:
        return None
    for pat, builder in _OEMBED_HOSTS:
        if pat.search(host):
            return builder(url)
    return None


def _fetch_oembed(url: str) -> dict[str, str] | None:
    """Resolve title/site/thumbnail via a provider's oEmbed JSON endpoint."""
    endpoint = _oembed_endpoint(url)
    if not endpoint:
        return None
    try:
        # oEmbed endpoints want a normal browser UA, not the FB crawler.
        body, _ = _http_get(endpoint, limit=_JSON_LIMIT, user_agent=_UA)
        data = json.loads(body.decode("utf-8", errors="replace"))
    except Exception as e:
        log.debug("oEmbed failed for %s: %s", url, e)
        return None
    if not isinstance(data, dict):
        return None
    title = str(data.get("title") or "").strip()
    if not title:
        return None
    author = str(data.get("author_name") or "").strip()
    provider = str(data.get("provider_name") or "").strip()
    thumb = str(
        data.get("thumbnail_url") or data.get("thumbnail_url_with_play_button") or ""
    ).strip()
    # Prefer provider name ("YouTube") over the bare host.
    site = provider or _hostname(url) or ""
    if isinstance(site, str) and site.lower().startswith("www."):
        site = site[4:]
    return {
        "title": title[:300],
        "description": author[:500],
        "site": site[:120],
        "image_url": thumb,
    }


def fetch_preview(url: str, *, download_image: bool = True) -> dict[str, Any]:
    """Fetch and cache a link preview for `url`.

    Always returns a dict with at least `url` and `status`. On success also
    carries title, description, site, image_url, image_path.

    Prefer oEmbed for YouTube / Instagram / Spotify / etc. Facebook share
    links need the crawler UA on a plain HTML GET. Short links (spotify.link)
    are resolved, then oEmbed is retried against the final URL.
    """
    url = (url or "").strip()
    if not url:
        return {"url": "", "status": STATUS_FAIL}

    cached = load_cached(url)
    if cached is not None:
        return cached

    entry: dict[str, Any] = {
        "url": url,
        "status": STATUS_FAIL,
        "title": "",
        "description": "",
        "site": "",
        "image_url": "",
        "image_path": "",
        "fetched_at": time.time(),
        "version": _CACHE_VERSION,
    }

    if not _safe_http_url(url):
        save_cached(entry)
        return entry

    try:
        # 1) oEmbed first for providers that need it.
        oembed = _fetch_oembed(url)
        final = url
        html_body: bytes | None = None

        if not oembed:
            # 2) Follow redirects + pull HTML (FB crawler UA where needed).
            html_body, final = _http_get(url, limit=_HTML_LIMIT)
            entry["final_url"] = final
            # Short links (spotify.link → open.spotify.com/track/…) only
            # oEmbed once expanded.
            if final and final != url:
                oembed = _fetch_oembed(final)

        if oembed:
            entry.update(oembed)
            entry["status"] = STATUS_OK
            entry["source"] = "oembed"
            entry["url"] = url
        else:
            # 3) Open Graph / <title> scrape of the HTML we already have.
            if html_body is None:
                html_body, final = _http_get(url, limit=_HTML_LIMIT)
            meta = parse_html_meta(html_body, final)
            entry.update(meta)
            entry["url"] = url  # keep the original for cache key stability
            entry["final_url"] = final
            entry["source"] = "html"
            title = (entry.get("title") or "").strip()
            if (
                title
                or entry.get("description")
                or entry.get("image_url")
            ) and title.lower() not in _WEAK_TITLES:
                entry["status"] = STATUS_OK
            elif entry.get("image_url") and title:
                # Brand title but a real image is still useful.
                entry["status"] = STATUS_OK
            else:
                # Soft fallback — hostname only. Marked weak so a later
                # cache version / source can replace it.
                host = _hostname(final) or _hostname(url)
                entry["site"] = host
                entry["title"] = host or url
                entry["status"] = STATUS_OK

        if download_image and entry.get("image_url"):
            entry["image_path"] = _download_image(
                entry["image_url"], _cache_key(url)
            )
    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as e:
        log.debug("link preview fetch failed for %s: %s", url, e)
        entry["status"] = STATUS_FAIL
        entry["error"] = str(e)[:200]
    except Exception as e:
        # Never take down the UI for a card — any unexpected parse/IO
        # error is a soft miss.
        log.debug("link preview unexpected error for %s: %s", url, e)
        entry["status"] = STATUS_FAIL
        entry["error"] = str(e)[:200]

    save_cached(entry)
    return entry


def preview_for_ui(entry: dict[str, Any] | None) -> dict[str, str]:
    """Flatten a cache entry into the string fields the message model exposes.

    Empty strings when there is nothing to show (miss, fail, pending).
    """
    empty = {
        "linkUrl": "",
        "linkTitle": "",
        "linkDescription": "",
        "linkSite": "",
        "linkImage": "",
    }
    if not entry or entry.get("status") != STATUS_OK:
        return empty
    image_path = entry.get("image_path") or ""
    # Drop a stale image path whose file has been cleaned up.
    if image_path and not Path(image_path).is_file():
        image_path = ""
    return {
        "linkUrl": entry.get("url") or "",
        "linkTitle": entry.get("title") or "",
        "linkDescription": entry.get("description") or "",
        "linkSite": entry.get("site") or "",
        "linkImage": image_path,
    }
