"""Open-Graph link previews — URL extraction, HTML parse, disk cache."""
from __future__ import annotations

import json

import pytest

from iphonebridge import link_preview as lp
from iphonebridge.emoji_text import body_markup, markup


@pytest.fixture()
def preview_root(tmp_path, monkeypatch):
    """Point the cache at a temp dir so tests never touch real state."""
    root = tmp_path / "link_previews"
    (root / "meta").mkdir(parents=True)
    (root / "images").mkdir(parents=True)
    monkeypatch.setattr(lp, "link_preview_dir", lambda: root)
    return root


# ---- URL extraction -------------------------------------------------------


def test_extract_urls_order_and_dedupe():
    text = "see https://a.example/x and https://b.example/y plus https://a.example/x again"
    assert lp.extract_urls(text) == [
        "https://a.example/x",
        "https://b.example/y",
    ]


def test_extract_urls_strips_trailing_punctuation():
    assert lp.extract_urls("go https://ex.com/path.") == ["https://ex.com/path"]
    assert lp.extract_urls("(https://ex.com/)") == ["https://ex.com/"]
    assert lp.first_url("end: https://x.co/z!") == "https://x.co/z"


def test_extract_urls_ignores_non_http():
    assert lp.extract_urls("ftp://files.example/a javascript:alert(1)") == []
    assert lp.extract_urls("") == []
    assert lp.extract_urls(None) == []
    assert lp.first_url("no links here") == ""


# ---- HTML meta parsing ----------------------------------------------------


def test_parse_og_tags():
    html = b"""<!doctype html><html><head>
      <meta property="og:title" content="Hello &amp; Welcome" />
      <meta property="og:description" content="A short blurb." />
      <meta property="og:image" content="/img/hero.jpg" />
      <meta property="og:site_name" content="Example" />
      <title>Fallback Title</title>
    </head><body></body></html>"""
    meta = lp.parse_html_meta(html, "https://example.com/page")
    assert meta["title"] == "Hello & Welcome"
    assert meta["description"] == "A short blurb."
    assert meta["image_url"] == "https://example.com/img/hero.jpg"
    assert meta["site"] == "Example"


def test_parse_falls_back_to_title_and_host():
    html = b"<html><head><title>Just a title</title></head><body>hi</body></html>"
    meta = lp.parse_html_meta(html, "https://www.news.example/story")
    assert meta["title"] == "Just a title"
    assert meta["site"] == "news.example"
    assert meta["image_url"] == ""


def test_parse_twitter_tags():
    html = b"""<html><head>
      <meta name="twitter:title" content="Tweet title" />
      <meta name="twitter:image" content="https://cdn.example/i.png" />
      <meta name="description" content="plain desc" />
    </head></html>"""
    meta = lp.parse_html_meta(html, "https://example.com/")
    assert meta["title"] == "Tweet title"
    assert meta["image_url"] == "https://cdn.example/i.png"
    assert meta["description"] == "plain desc"


# ---- disk cache -----------------------------------------------------------


def test_cache_roundtrip(preview_root):
    entry = {
        "url": "https://example.com/a",
        "status": lp.STATUS_OK,
        "title": "A",
        "description": "B",
        "site": "example.com",
        "image_url": "",
        "image_path": "",
        "fetched_at": 9_999_999_999,  # far future → not expired
    }
    lp.save_cached(entry)
    loaded = lp.load_cached("https://example.com/a")
    assert loaded is not None
    assert loaded["title"] == "A"
    assert lp.preview_for_ui(loaded)["linkTitle"] == "A"


def test_failed_cache_is_remembered(preview_root):
    entry = {
        "url": "https://down.example/",
        "status": lp.STATUS_FAIL,
        "fetched_at": 9_999_999_999,
    }
    lp.save_cached(entry)
    loaded = lp.load_cached("https://down.example/")
    assert loaded is not None
    assert loaded["status"] == lp.STATUS_FAIL
    # UI gets empty strings — no phantom card for a hard miss.
    fields = lp.preview_for_ui(loaded)
    assert fields["linkTitle"] == ""
    assert fields["linkUrl"] == ""


def test_expired_ok_is_ignored(preview_root, monkeypatch):
    entry = {
        "url": "https://old.example/",
        "status": lp.STATUS_OK,
        "title": "Stale",
        "fetched_at": 1.0,  # epoch-ish → way past TTL
    }
    path = preview_root / "meta" / (
        __import__("hashlib").sha256(b"https://old.example/").hexdigest() + ".json"
    )
    path.write_text(json.dumps(entry))
    assert lp.load_cached("https://old.example/") is None


def test_ssrf_blocks_private_hosts():
    assert lp._safe_http_url("http://127.0.0.1/") is None
    assert lp._safe_http_url("http://localhost/x") is None
    assert lp._safe_http_url("ftp://example.com/") is None
    # Public host should pass the scheme/host check; DNS may still fail later.
    # We only assert private addresses are rejected.
    assert lp._host_is_public("127.0.0.1") is False


# ---- rich-text linkification ---------------------------------------------


def test_markup_linkifies_urls():
    rich = markup("see https://example.com/x please", 13, link_color="#abc")
    assert 'href="https://example.com/x"' in rich
    assert "color:#abc" in rich
    assert "see " in rich


def test_body_markup_with_url_is_not_jumbo():
    rich, jumbo = body_markup("https://example.com/", 13)
    assert jumbo is False
    assert "href=" in rich


def test_body_markup_emoji_only_still_jumbo():
    rich, jumbo = body_markup("🔥🔥", 13)
    assert jumbo is True
    assert rich  # sized span


# ---- oEmbed / weak cache --------------------------------------------------


def test_youtube_uses_oembed_endpoint():
    assert "oembed" in (lp._oembed_endpoint("https://youtu.be/dQw4w9WgXcQ") or "")
    assert "oembed" in (
        lp._oembed_endpoint("https://www.youtube.com/watch?v=dQw4w9WgXcQ") or ""
    )
    assert lp._oembed_endpoint("https://example.com/") is None


def test_instagram_uses_oembed_endpoint():
    ep = lp._oembed_endpoint("https://www.instagram.com/p/DbT-CQApjfU/")
    assert ep is not None
    assert "instagram.com/api/v1/oembed" in ep


def test_facebook_uses_crawler_user_agent():
    assert "facebookexternalhit" in lp._user_agent_for(
        "https://www.facebook.com/share/1EPgxj9m79/"
    )
    assert "facebookexternalhit" not in lp._user_agent_for(
        "https://example.com/"
    )


def test_weak_hostname_only_preview_is_ignored(preview_root):
    """A brand-shell miss that only stored the host must re-fetch, not stick."""
    entry = {
        "url": "https://youtu.be/abc123",
        "status": lp.STATUS_OK,
        "title": "youtube.com",
        "site": "youtube.com",
        "description": "",
        "image_url": "",
        "image_path": "",
        "fetched_at": 9_999_999_999,
        "version": 1,
    }
    lp.save_cached(entry)
    assert lp.load_cached("https://youtu.be/abc123") is None


def test_weak_instagram_shell_is_ignored(preview_root):
    entry = {
        "url": "https://www.instagram.com/p/x/",
        "status": lp.STATUS_OK,
        "title": "Instagram",
        "site": "Instagram",
        "description": "",
        "image_url": "",
        "image_path": "",
        "fetched_at": 9_999_999_999,
        "version": 3,
    }
    lp.save_cached(entry)
    assert lp.load_cached("https://www.instagram.com/p/x/") is None


def test_fetch_prefers_oembed(preview_root, monkeypatch):
    oembed_json = json.dumps({
        "title": "Never Gonna Give You Up",
        "author_name": "Rick Astley",
        "provider_name": "YouTube",
        "thumbnail_url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
    }).encode()

    def fake_get(url, *, limit, user_agent=None):
        if "oembed" in url:
            return oembed_json, url
        raise AssertionError(f"should not HTML-scrape, got {url}")

    monkeypatch.setattr(lp, "_http_get", fake_get)
    monkeypatch.setattr(lp, "_download_image", lambda *a, **k: "/tmp/thumb.jpg")
    monkeypatch.setattr(lp, "_safe_http_url", lambda u: u)

    entry = lp.fetch_preview(
        "https://youtu.be/dQw4w9WgXcQ", download_image=True
    )
    assert entry["status"] == lp.STATUS_OK
    assert entry["title"] == "Never Gonna Give You Up"
    assert entry["site"] == "YouTube"
    assert entry["description"] == "Rick Astley"
    assert entry["source"] == "oembed"
    assert entry["image_path"] == "/tmp/thumb.jpg"
    # Cached as a real preview — not weak.
    assert lp.load_cached("https://youtu.be/dQw4w9WgXcQ")["title"] == (
        "Never Gonna Give You Up"
    )
