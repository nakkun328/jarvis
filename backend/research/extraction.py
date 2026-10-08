"""Main text and metadata extraction for fetched pages (JAR-39, JAR-40).

Only the standard library is used. The input is untrusted: nothing in it is executed or
interpreted, hidden and script content is dropped, and the returned text is inert data that a
caller must still treat as untrusted. Publication dates are never guessed: an absent or
unparseable date is ``None``, not the retrieval time.
"""

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

DEFAULT_MAX_TEXT_CHARS = 20_000
MAX_TITLE_CHARS = 300
MAX_AUTHOR_CHARS = 200
MAX_JSON_LD_CHARS = 200_000
MAX_JSON_LD_NODES = 200
MAX_DATE_CHARS = 64

# Dropped in every mode; nothing inside these is page content.
ALWAYS_SKIPPED = frozenset({"script", "style", "noscript", "template", "iframe", "svg", "object"})
# Dropped when boilerplate removal is on (the first pass).
BOILERPLATE_SKIPPED = frozenset({"nav", "footer", "aside", "button", "select", "dialog"})
BOILERPLATE_ROLES = frozenset({"navigation", "banner", "contentinfo", "complementary"})
VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param"}
    | {"source", "track", "wbr"}
)
BLOCK_TAGS = frozenset(
    {"p", "div", "section", "article", "main", "header", "h1", "h2", "h3", "h4", "h5", "h6"}
    | {"ul", "ol", "li", "dl", "dt", "dd", "table", "tr", "blockquote", "pre", "figure"}
    | {"figcaption", "hr", "details", "summary", "form", "fieldset", "address", "body"}
)
HEADINGS = {f"h{level}": "#" * level + " " for level in range(1, 7)}

_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?![.\d])"
    r"|font-size\s*:\s*0(?![.\d])|clip-path\s*:\s*inset\(\s*100%",
    re.I,
)
# Control and invisible formatting characters (bidi overrides, zero width, tag characters).
_INVISIBLE = re.compile(
    r"[\x00-\x08\x0e-\x1f\x7f-\x9f\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064"
    r"\u2066-\u206f\ufeff\ufff9-\ufffb\U000e0000-\U000e007f]"
)
_INLINE_SPACE = re.compile(r"[^\S\n]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*$")

DATE_META_KEYS = (
    "date",
    "dc.date",
    "dc.date.issued",
    "dcterms.date",
    "dcterms.created",
    "pubdate",
    "publishdate",
    "citation_publication_date",
    "citation_date",
)
AUTHOR_META_KEYS = (
    "author",
    "article:author",
    "og:article:author",
    "dc.creator",
    "citation_author",
)


class ExtractionError(Exception):
    """No usable text could be extracted."""


@dataclass(frozen=True)
class ExtractedPage:
    text: str
    title: str | None
    author: str | None
    published_at: datetime | None
    language: str | None
    truncated: bool


def extract_page(
    content: str,
    content_type: str,
    *,
    max_chars: int = DEFAULT_MAX_TEXT_CHARS,
    default_language: str | None = None,
) -> ExtractedPage:
    """Extract bounded text and metadata from already decoded content."""
    media_type = content_type.split(";", 1)[0].strip().lower()
    try:
        if media_type in {"text/html", "application/xhtml+xml"}:
            return _extract_html(content, max_chars, default_language)
        if media_type == "application/json":
            text = _json_text(content)
        elif media_type == "text/plain":
            text = _plain_text(content)
        else:
            raise ExtractionError("unsupported content type")
    except ExtractionError:
        raise
    except Exception:  # a parser limit such as recursion depth; never leak details
        raise ExtractionError("extraction failed") from None
    return _bounded(text, max_chars, None, None, None, _language(default_language))


def parse_published_at(value: object) -> datetime | None:
    """Parse an ISO 8601 or RFC 2822 date into UTC. Naive values are taken as UTC."""
    if not isinstance(value, str):
        return None
    text = _INVISIBLE.sub("", value).strip()
    if not text or len(text) > MAX_DATE_CHARS:
        return None
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        parsed = parsed.astimezone(UTC)
    except (OverflowError, ValueError):
        return None
    return parsed if 1970 <= parsed.year <= 2100 else None


def _extract_html(content: str, max_chars: int, default_language: str | None) -> ExtractedPage:
    parser = _PageParser(boilerplate=True, max_chars=max_chars)
    parser.run(content)
    if not parser.text():
        # A stray unclosed <nav>/<aside> can swallow the page; retry with minimal skipping.
        fallback = _PageParser(boilerplate=False, max_chars=max_chars)
        fallback.run(content)
        if fallback.text():
            parser = fallback
    return _bounded(
        parser.text(),
        max_chars,
        parser.title(),
        parser.author(),
        parser.published_at(),
        parser.language() or _language(default_language),
    )


def _bounded(
    text: str,
    max_chars: int,
    title: str | None,
    author: str | None,
    published_at: datetime | None,
    language: str | None,
) -> ExtractedPage:
    truncated = False
    if len(text) > max_chars:
        cut = text[:max_chars]
        boundary = max(cut.rfind(" "), cut.rfind("\n"))
        if boundary > max_chars - 200:
            cut = cut[:boundary]
        text = cut.rstrip()
        truncated = True
    if not text:
        raise ExtractionError("no text")
    return ExtractedPage(text, title, author, published_at, language, truncated)


def _clean_lines(text: str) -> str:
    text = _INVISIBLE.sub("", text)
    lines = (_INLINE_SPACE.sub(" ", line).strip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def _plain_text(content: str) -> str:
    return _clean_lines(content.replace("\r\n", "\n").replace("\r", "\n"))


def _json_text(content: str) -> str:
    try:
        rendered = json.dumps(json.loads(content), ensure_ascii=False, indent=2)
    except (ValueError, RecursionError):
        rendered = content  # truncated or invalid JSON is still returned as inert text
    # Keep the indentation that json.dumps produced; only drop invisible characters.
    return _INVISIBLE.sub("", rendered).replace("\r", "").strip()


def _collapse(value: str) -> str:
    return _INLINE_SPACE.sub(" ", _INVISIBLE.sub("", value).replace("\n", " ")).strip()


def _language(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().split(",", 1)[0].strip()
    if not _LANGUAGE.match(value):
        return None
    primary, *rest = value.replace("_", "-").split("-")
    parts = [primary.lower()]
    for part in rest:
        parts.append(part.upper() if len(part) == 2 and part.isalpha() else part.lower())
    return "-".join(parts)


class _PageParser(HTMLParser):
    def __init__(self, *, boilerplate: bool, max_chars: int) -> None:
        super().__init__(convert_charrefs=True)
        self._boilerplate = boilerplate
        self._skip_tag: str | None = None
        self._skip_depth = 0
        self._parts: list[str] = []
        self._blocks: list[tuple[bool, str]] = []  # (is list item, text)
        self._prefix = ""
        self._is_item = False
        self._pre_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self._json_ld: list[str] | None = None
        self._json_ld_chars = 0
        self._json_ld_docs: list[str] = []
        self._chars = 0
        self._collect_limit = max_chars + 2000
        self._lang: str | None = None
        self._meta: dict[str, str] = {}
        self._time_dates: list[tuple[int, str]] = []

    def run(self, content: str) -> None:
        try:
            self.feed(content)
            self.close()
        except Exception:  # malformed markup must never escape as a parser detail
            pass
        self._flush()

    # -- results -------------------------------------------------------------------------

    def text(self) -> str:
        out: list[str] = []
        previous_item = False
        for is_item, block in self._blocks:
            if out:
                out.append("\n" if is_item and previous_item else "\n\n")
            out.append(block)
            previous_item = is_item
        return "".join(out)

    def title(self) -> str | None:
        for candidate in (self._meta.get("og:title"), "".join(self._title_parts)):
            value = _collapse(candidate or "")[:MAX_TITLE_CHARS]
            if value:
                return value
        return None

    def language(self) -> str | None:
        return (
            _language(self._lang)
            or _language(self._meta.get("content-language"))
            or _language(self._meta.get("og:locale"))
        )

    def author(self) -> str | None:
        for key in AUTHOR_META_KEYS:
            value = _collapse(self._meta.get(key, ""))[:MAX_AUTHOR_CHARS]
            if value and not value.lower().startswith(("http://", "https://")):
                return value
        return self._json_ld_value("author")

    def published_at(self) -> datetime | None:
        candidates: list[object] = [
            self._meta.get("article:published_time"),
            self._meta.get("og:article:published_time"),
        ]
        candidates += [self._meta.get(key) for key in DATE_META_KEYS]
        candidates += [self._meta.get("datepublished")]
        candidates += [value for rank, value in self._time_dates if rank == 0]
        candidates += [self._json_ld_value("datePublished", date=True)]
        candidates += [value for rank, value in self._time_dates if rank == 1]
        for candidate in candidates:
            parsed = parse_published_at(candidate)
            if parsed is not None:
                return parsed
        return None

    # -- parser callbacks ----------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        if tag == "script" and attributes.get("type", "").strip().lower() == (
            "application/ld+json"
        ):
            self._json_ld = []
            self._json_ld_chars = 0
        if self._skip_tag is not None:
            if tag == self._skip_tag and tag not in VOID_TAGS:
                self._skip_depth += 1
            return
        if tag == "html":
            self._lang = self._lang or attributes.get("lang") or attributes.get("xml:lang")
        elif tag == "meta":
            self._record_meta(attributes)
            return
        elif tag == "time":
            self._record_time(attributes)
        elif tag == "title":
            self._in_title = True
            return
        if self._should_skip(tag, attributes):
            if tag not in VOID_TAGS:
                self._flush()
                self._skip_tag = tag
                self._skip_depth = 1
            return
        if tag in BLOCK_TAGS:
            self._flush()
            if tag in HEADINGS:
                self._prefix, self._is_item = HEADINGS[tag], False
            elif tag == "li":
                self._prefix, self._is_item = "- ", True
            elif tag == "pre":
                self._pre_depth += 1
        elif tag in {"td", "th", "br"}:
            self._parts.append("\n" if tag == "br" else " ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._json_ld is not None:
            self._json_ld_docs.append("".join(self._json_ld))
            self._json_ld = None
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth <= 0:
                    self._skip_tag = None
                    self._skip_depth = 0
            return
        if tag == "title":
            self._in_title = False
        elif tag in BLOCK_TAGS:
            self._flush()
            if tag in HEADINGS or tag == "li":
                self._prefix, self._is_item = "", False
            elif tag == "pre" and self._pre_depth:
                self._pre_depth -= 1
        elif tag in {"td", "th"}:
            self._parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._json_ld is not None:
            if self._json_ld_chars < MAX_JSON_LD_CHARS:
                self._json_ld.append(data)
                self._json_ld_chars += len(data)
            return
        if self._skip_tag is not None:
            return
        if self._in_title:
            if sum(map(len, self._title_parts)) < 4 * MAX_TITLE_CHARS:
                self._title_parts.append(data)
            return
        self._parts.append(data)

    # -- helpers -------------------------------------------------------------------------

    def _should_skip(self, tag: str, attributes: dict[str, str]) -> bool:
        if tag in ALWAYS_SKIPPED:
            return True
        if "hidden" in attributes or attributes.get("aria-hidden", "").strip().lower() == "true":
            return True
        if _HIDDEN_STYLE.search(attributes.get("style", "")):
            return True
        if self._boilerplate:
            role = attributes.get("role", "").strip().lower()
            return tag in BOILERPLATE_SKIPPED or role in BOILERPLATE_ROLES
        return False

    def _record_meta(self, attributes: dict[str, str]) -> None:
        content = attributes.get("content", "")
        if not content.strip():
            return
        for key_name in ("name", "property", "itemprop", "http-equiv"):
            key = attributes.get(key_name, "").strip().lower()
            if key:
                self._meta.setdefault(key, content)

    def _record_time(self, attributes: dict[str, str]) -> None:
        value = attributes.get("datetime", "")
        if not value.strip() or len(self._time_dates) >= 20:
            return
        itemprop = attributes.get("itemprop", "").lower()
        explicit = "datepublished" in itemprop or "pubdate" in attributes
        self._time_dates.append((0 if explicit else 1, value))

    def _flush(self) -> None:
        if not self._parts:
            return
        raw = "".join(self._parts)
        self._parts = []
        if self._chars > self._collect_limit:
            return  # enough text collected; later blocks cannot fit under the cap
        if self._pre_depth:
            block = _clean_lines(raw)
        else:
            block = _collapse_block(raw)
        if not block:
            return
        self._blocks.append((self._is_item, self._prefix + block))
        self._chars += len(block) + 2

    def _json_ld_value(self, key: str, *, date: bool = False) -> str | None:
        for document in self._json_ld_docs:
            try:
                data = json.loads(document)
            except (ValueError, RecursionError):
                continue
            for node in _walk_json_ld(data):
                if key not in node:
                    continue
                value = _json_ld_string(node[key], date=date)
                if value:
                    return value
        return None


def _collapse_block(raw: str) -> str:
    lines = (_collapse(line) for line in raw.split("\n"))
    return "\n".join(line for line in lines if line)


def _walk_json_ld(data: object) -> Iterator[dict[str, object]]:
    queue: list[tuple[object, int]] = [(data, 0)]
    seen = 0
    while queue and seen < MAX_JSON_LD_NODES:
        node, depth = queue.pop(0)
        seen += 1
        if isinstance(node, dict):
            yield node
            if depth < 4:
                queue += [
                    (child, depth + 1) for child in node.values() if isinstance(child, dict | list)
                ]
        elif isinstance(node, list) and depth < 4:
            queue += [(child, depth + 1) for child in node[:50]]


def _json_ld_string(value: object, *, date: bool) -> str | None:
    if isinstance(value, str):
        cleaned = _collapse(value)[:MAX_AUTHOR_CHARS]
        return cleaned or None
    if date:
        return None
    if isinstance(value, dict):
        return _json_ld_string(value.get("name"), date=False)
    if isinstance(value, list):
        names = [name for item in value[:3] if (name := _json_ld_string(item, date=False))]
        return ", ".join(names)[:MAX_AUTHOR_CHARS] or None
    return None
