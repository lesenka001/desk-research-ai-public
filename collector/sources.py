"""Source adapters for audience-reach desk research.

RSS is used only to discover publications.  For every discovered URL we fetch
the public HTML page as well, because feed descriptions rarely contain the
research base, methodology, or all reported figures.
"""

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup


USER_AGENT = "target-audience-navigator/0.1 (+public desk research)"
TIMEOUT = 30
MAX_TEXT_CHARS = 30_000

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
    }
)

CONTENT_SELECTORS = {
    "fom.ru": (".page_content", "#post", "article", ".article", ".material", ".content", "main"),
    "nafi.ru": ("article", ".detail", ".content", "main"),
    "datainsight.ru": (".content-section--text", ".content-section__inner", "article", ".node__content", ".field--name-body", "main"),
    "cbr.ru": ("main", ".content", ".landing-text"),
    "www.cbr.ru": ("main", ".content", ".landing-text"),
}

NOISE_TAGS = ("script", "style", "noscript", "svg", "form", "nav", "footer", "header")

CBR_RELEVANCE_PATTERNS = (
    r"\bопрос(?:а|ов|ы|е|ом|ами)?\b",
    r"\bисследован\w*\b",
    r"\bнаселени\w*\b",
    r"\bроссиян\w*\b",
    r"\bграждан\w*\b",
    r"\bдомохозяй\w*\b",
    r"\bпотребительские настроения\b",
    r"\bпользовател\w*\b",
    r"\bфинансовая грамотность\b",
    r"\bфинансовых услуг\b",
    r"\bбанковских услуг\b",
)

RU_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}


def fetch(url):
    response = SESSION.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return response.content, response.url, response.headers.get("Content-Type", "")


def canonical_url(url):
    """Remove fragments and common tracking parameters without changing identity."""
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def normalize_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def content_hash(text):
    normalized = normalize_text(text).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else None


def parse_date(value):
    if not value:
        return None
    value = value.strip()
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def parse_russian_date(value):
    match = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", (value or "").lower())
    if not match or match.group(2) not in RU_MONTHS:
        return None
    moment = datetime(
        int(match.group(3)), RU_MONTHS[match.group(2)], int(match.group(1)),
        tzinfo=timezone(timedelta(hours=3)),
    )
    return moment.isoformat()


def page_date(soup):
    for attrs in (
        {"property": "article:published_time"},
        {"name": "date"},
        {"itemprop": "datePublished"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and parse_date(tag.get("content")):
            return parse_date(tag.get("content"))
    time_tag = soup.find("time")
    if time_tag:
        return parse_date(time_tag.get("datetime"))
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(script.string or "null")
        except json.JSONDecodeError:
            continue
        records = payload if isinstance(payload, list) else [payload]
        for record in records:
            if isinstance(record, dict) and parse_date(record.get("datePublished")):
                return parse_date(record["datePublished"])
    return None


def extract_html_page(url):
    raw, final_url, _ = fetch(url)
    soup = BeautifulSoup(raw, "html.parser")
    published_at = page_date(soup)
    for tag in soup.find_all(NOISE_TAGS):
        tag.decompose()

    host = urlsplit(final_url).netloc.lower()
    root = None
    for selector in CONTENT_SELECTORS.get(host, ("article", "main")):
        candidate = soup.select_one(selector)
        if candidate and len(normalize_text(candidate.get_text(" ", strip=True))) >= 200:
            root = candidate
            break
    root = root or soup.body or soup
    text = normalize_text(root.get_text(" ", strip=True))[:MAX_TEXT_CHARS]
    title_tag = soup.find("h1") or soup.find("title")
    title = normalize_text(title_tag.get_text(" ", strip=True)) if title_tag else ""
    return {
        "url": canonical_url(final_url),
        "title": title,
        "text": text,
        "published_at": published_at,
        "content_hash": content_hash(text),
    }


def rss_items(url):
    raw, _, _ = fetch(url)
    root = ET.fromstring(raw)
    for item in root.iter("item"):
        link = normalize_text(item.findtext("link"))
        if not link:
            continue
        yield {
            "url": canonical_url(link),
            "title": normalize_text(item.findtext("title")),
            "description": normalize_text(item.findtext("description")),
            "published_at": parse_date(item.findtext("pubDate")),
        }


def read_rss_with_pages(url, prefilter=None):
    for discovered in rss_items(url):
        haystack = f"{discovered['title']} {discovered['description']}".lower()
        if prefilter and not prefilter(haystack):
            continue
        try:
            page = extract_html_page(discovered["url"])
        except requests.RequestException:
            page = {
                "url": discovered["url"],
                "title": discovered["title"],
                "text": discovered["description"],
                "published_at": None,
                "content_hash": content_hash(discovered["description"]),
            }
        # The feed title is normally more precise than the first H1 on sites
        # that repeat a generic section heading above every publication.
        page["title"] = discovered["title"] or page["title"]
        page["published_at"] = discovered["published_at"] or page["published_at"]
        yield page


def read_fom(url):
    yield from read_rss_with_pages(url)


def read_datainsight(url):
    yield from read_rss_with_pages(url)


def is_cbr_relevant(text):
    return any(re.search(pattern, text.lower()) for pattern in CBR_RELEVANCE_PATTERNS)


def read_cbr(url):
    yield from read_rss_with_pages(url, prefilter=is_cbr_relevant)


def read_nafi(url):
    raw, final_url, _ = fetch(url)
    soup = BeautifulSoup(raw, "html.parser")
    links = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        target = canonical_url(urljoin(final_url, anchor["href"]))
        path = urlsplit(target).path
        if not re.fullmatch(r"/polls/[^/]+/", path) or target in seen:
            continue
        seen.add(target)
        title_tag = anchor.select_one(".name")
        date_tag = anchor.select_one(".date")
        links.append(
            (
                target,
                normalize_text(title_tag.get_text(" ", strip=True)) if title_tag else "",
                parse_russian_date(date_tag.get_text(" ", strip=True)) if date_tag else None,
            )
        )
    for target, discovered_title, discovered_date in links:
        page = extract_html_page(target)
        page["title"] = discovered_title or page["title"]
        page["published_at"] = discovered_date or page["published_at"]
        yield page


READERS = {
    "fom_rss": read_fom,
    "nafi_html": read_nafi,
    "datainsight_rss": read_datainsight,
    "cbr_rss": read_cbr,
}
