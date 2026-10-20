"""Read explicit per-track instrumental flags from an independently held release.

This is a corroborating source only. No missing page, album genre, or absent
lyric is a positive flag. The caller must separately verify the exact recording.
"""
from __future__ import annotations

import hashlib
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

from ..text import normalize_text


BASE = "https://www.musik-sammler.de"


class ReleaseParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.artist: list[str] = []
        self.album: list[str] = []
        self.rows: list[list[str]] = []
        self.links: list[str] = []
        self.heading = False
        self.artist_link = False
        self.small = False
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "h1":
            self.heading = True
        if self.heading and tag == "small":
            self.small = True
        if tag == "a":
            href = str(attributes.get("href") or "")
            link = urllib.parse.urlparse(urllib.parse.urljoin(BASE, href))
            same_site = link.scheme == "https" and link.netloc == "www.musik-sammler.de"
            if same_site and re.fullmatch(r"/release/(?:[a-z0-9-]+-)?\d+/", link.path):
                self.links.append(urllib.parse.urlunparse(link))
            if self.heading and same_site and link.path.startswith("/artist/"):
                self.artist_link = True
        if tag == "tr":
            self.row = []
        if tag in {"td", "th"} and self.row is not None:
            self.cell = []

    def handle_data(self, data: str) -> None:
        if self.heading and not self.small:
            (self.artist if self.artist_link else self.album).append(data)
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1":
            self.heading = False
        if tag == "small":
            self.small = False
        if tag == "a":
            self.artist_link = False
        if tag in {"td", "th"} and self.cell is not None:
            if self.row is not None:
                self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        if tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def parse_release(html: str, *, title: str, album: str, artist: str,
                  url: str) -> dict[str, Any] | None:
    parser = ReleaseParser()
    parser.feed(html)
    release_artist = " ".join("".join(parser.artist).split())
    release_album = " ".join("".join(parser.album).split())
    if (normalize_text(release_album) != normalize_text(album)
            or normalize_text(release_artist) != normalize_text(artist)):
        return None
    for row in parser.rows:
        if (len(row) != 4 or not re.fullmatch(r"\d+\.", row[0])
                or not re.fullmatch(r"\d{1,3}:\d{2}", row[2])
                or normalize_text(row[1]) != normalize_text(title)):
            continue
        minutes, seconds = map(int, row[2].split(":"))
        if seconds >= 60:
            continue
        return {
            "provider": "musiksammler", "url": url,
            "title": row[1], "artist": release_artist, "album": release_album,
            "duration": minutes * 60 + seconds, "track_number": int(row[0][:-1]),
            "instrumental": "I" in row[3].split(),
            "source_sha256": hashlib.sha256(html.encode()).hexdigest(),
        }
    return None


def _request_html(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "www.musik-sammler.de":
        raise ValueError("unexpected release source URL")
    request = urllib.request.Request(url, headers={
        "User-Agent": "WordLyrics/1.0 (personal NAS recording verification)",
        "Accept": "text/html",
    })
    with urllib.request.urlopen(request, timeout=10) as response:
        final = urllib.parse.urlparse(response.geturl())
        if final.scheme != "https" or final.netloc != parsed.netloc:
            raise ValueError("release source redirected to another domain")
        data = response.read(1_000_001)
    if len(data) > 1_000_000:
        raise ValueError("release source is unexpectedly large")
    return data.decode("utf-8", "strict")


def get_record(*, title: str, album: str, artist: str) -> dict[str, Any] | None:
    if not all(normalize_text(value) for value in (title, album, artist)):
        return None
    search_url = BASE + "/search/?" + urllib.parse.urlencode({"query": artist + " " + album})
    parser = ReleaseParser()
    parser.feed(_request_html(search_url))
    for url in list(dict.fromkeys(parser.links))[:3]:
        record = parse_release(_request_html(url), title=title, album=album, artist=artist, url=url)
        if record is not None:
            return record
    return None
