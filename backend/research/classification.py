"""Source type classification (JAR-42) from the URL and title alone.

Nothing is fetched and nothing is resolved: the decision uses only the text of the URL (host
labels, host suffixes, path segments) and, last, the title. The rule table ``RULES`` is the
documentation. It is ordered: the first rule that matches decides, and the matching rule's
id is returned so the decision can be explained and recorded. Anything unmatched is
``unknown``; the classifier never guesses from popularity.

A classification is a heuristic about the kind of site a URL points to. It says nothing about
whether the page is correct. In particular ``docs`` means "looks like documentation", not
"documentation of the thing you asked about, written by its maker": a host such as
``docs.example.net`` is classified by its name pattern, and anyone can register one. The
``basis`` of the decision (``host``, ``path``, ``title`` or ``default``) is returned so
later steps can trust a host-table hit more than a title hit.

Extending: add a ``Rule`` (or an entry to one of its tuples) in ``RULES``; per-topic tables
(for example the official domains of the product a question is about) can be supplied by
the caller as ``rules=`` without touching this module.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from backend.research.models import MAX_TITLE_CHARS, Basis, SourceType

_MAX_URL_CHARS = 2048
_IPV4 = re.compile(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}")


@dataclass(frozen=True)
class Rule:
    """One classification rule: any one condition that matches makes the rule match.

    ``host_suffixes`` match the host itself or any subdomain of it (``"arxiv.org"`` matches
    ``export.arxiv.org``); a suffix starting with a dot (``".gov"``) matches only hosts
    that end with it. ``host_first_labels`` match the leftmost host label (``docs.``).
    ``path_segments`` match a whole path segment (``/docs/``). ``title_words`` match as
    case-folded substrings of the title and are only consulted when no rule matched the URL.
    """

    rule_id: str
    source_type: SourceType
    host_suffixes: tuple[str, ...] = ()
    host_first_labels: tuple[str, ...] = ()
    path_segments: tuple[str, ...] = ()
    title_words: tuple[str, ...] = ()
    path_only_on_hosts: tuple[str, ...] = ()  # restrict path_segments to these host suffixes


@dataclass(frozen=True)
class SourceClassification:
    source_type: SourceType
    rule_id: str
    basis: Basis


# Ordered. Specific, high-confidence tables come before the generic name patterns.
RULES: tuple[Rule, ...] = (
    # Personal pages hosted by a university are blogs, not institutional publications.
    Rule(
        "personal_academic_page",
        SourceType.BLOG,
        path_only_on_hosts=(".edu", ".ac.jp", ".ac.uk"),
        path_segments=("~",),
    ),
    Rule(
        "academic_host",
        SourceType.ACADEMIC,
        host_suffixes=(
            "arxiv.org",
            "doi.org",
            "dx.doi.org",
            "biorxiv.org",
            "medrxiv.org",
            "ssrn.com",
            "openreview.net",
            "aclanthology.org",
            "semanticscholar.org",
            "ncbi.nlm.nih.gov",
            "ieeexplore.ieee.org",
            "dl.acm.org",
            "link.springer.com",
            "sciencedirect.com",
            "jstage.jst.go.jp",
            "cir.nii.ac.jp",
            ".edu",
            ".ac.jp",
            ".ac.uk",
            ".edu.au",
        ),
    ),
    Rule(
        "official_host",
        SourceType.OFFICIAL,
        host_suffixes=(
            ".gov",
            ".gov.uk",
            ".gov.au",
            ".go.jp",
            ".lg.jp",
            ".mil",
            ".europa.eu",
            "w3.org",
            "ietf.org",
            "whatwg.org",
            "iso.org",
            "ecma-international.org",
            "unicode.org",
            "who.int",
            "un.org",
            "oecd.org",
            "imf.org",
            "worldbank.org",
        ),
    ),
    # Question-and-answer sites and discussion hubs people read for experience reports.
    Rule(
        "community_host",
        SourceType.COMMUNITY,
        host_suffixes=(
            "reddit.com",
            "stackoverflow.com",
            "stackexchange.com",
            "superuser.com",
            "serverfault.com",
            "askubuntu.com",
            "news.ycombinator.com",
            "quora.com",
            "teratail.com",
            "chiebukuro.yahoo.co.jp",
            "lobste.rs",
        ),
    ),
    Rule(
        "forum_host",
        SourceType.FORUM,
        host_suffixes=("5ch.net", "2ch.sc", "discourse.org"),
        host_first_labels=("forum", "forums", "discuss", "community", "bbs"),
    ),
    Rule(
        "news_host",
        SourceType.NEWS,
        host_suffixes=(
            "reuters.com",
            "apnews.com",
            "bbc.com",
            "bbc.co.uk",
            "nytimes.com",
            "washingtonpost.com",
            "theguardian.com",
            "bloomberg.com",
            "ft.com",
            "wsj.com",
            "cnn.com",
            "nhk.or.jp",
            "asahi.com",
            "mainichi.jp",
            "yomiuri.co.jp",
            "nikkei.com",
            "sankei.com",
            "jiji.com",
            "kyodonews.jp",
            "news.yahoo.co.jp",
            "itmedia.co.jp",
            "techcrunch.com",
            "theverge.com",
            "arstechnica.com",
            "wired.com",
            "zdnet.com",
            "cnet.com",
            "engadget.com",
            "gigazine.net",
            "impress.co.jp",
        ),
        host_first_labels=("news",),
    ),
    # Documentation by name pattern.
    Rule(
        "docs_host",
        SourceType.DOCS,
        host_suffixes=("readthedocs.io", "readthedocs.org", "gitbook.io", "docs.rs", "pkg.go.dev"),
        host_first_labels=(
            "docs",
            "doc",
            "documentation",
            "developer",
            "developers",
            "devdocs",
            "apidocs",
            "api-docs",
            "reference",
            "manual",
            "help",
        ),
    ),
    # Blogs by hosting platform and name pattern.
    Rule(
        "blog_host",
        SourceType.BLOG,
        host_suffixes=(
            "medium.com",
            "substack.com",
            "wordpress.com",
            "blogspot.com",
            "note.com",
            "hatenablog.com",
            "hatenablog.jp",
            "hateblo.jp",
            "ameblo.jp",
            "livedoor.blog",
            "qiita.com",
            "zenn.dev",
            "dev.to",
            "hashnode.dev",
        ),
        host_first_labels=("blog", "blogs"),
    ),
    Rule(
        "community_path",
        SourceType.COMMUNITY,
        path_segments=("issues", "discussions"),
        path_only_on_hosts=("github.com", "gitlab.com"),
    ),
    Rule(
        "docs_path",
        SourceType.DOCS,
        path_segments=("docs", "doc", "documentation", "reference", "manual", "guide", "guides"),
    ),
    Rule(
        "forum_path",
        SourceType.FORUM,
        path_segments=("forum", "forums", "threads", "thread", "topic", "topics"),
    ),
    Rule("blog_path", SourceType.BLOG, path_segments=("blog", "blogs", "posts")),
    # Title words are the weakest evidence (page text can be written by anyone).
    Rule(
        "docs_title",
        SourceType.DOCS,
        title_words=("documentation", "api reference", "ドキュメント", "リファレンス"),
    ),
)


def _host_matches(host: str, suffix: str) -> bool:
    if suffix.startswith("."):
        return host.endswith(suffix)
    return host == suffix or host.endswith("." + suffix)


def _segment_matches(segment: str, wanted: tuple[str, ...]) -> bool:
    """A whole segment match; the entry ``"~"`` stands for any ``~user`` segment."""
    return segment in wanted or (segment.startswith("~") and "~" in wanted)


def _parse(url: object) -> tuple[str, list[str]] | None:
    """Lower-cased host and path segments of an http(s) URL; ``None`` for anything else."""
    if not isinstance(url, str) or not url or len(url) > _MAX_URL_CHARS:
        return None
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return None
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".").casefold()
    except ValueError:
        return None
    if parts.scheme.casefold() not in {"http", "https"} or not host or _IPV4.fullmatch(host):
        return None
    if ":" in host:  # IPv6 literal
        return None
    segments = [segment.casefold() for segment in parts.path.split("/") if segment]
    return host, segments


def classify_source_detailed(
    url: str, title: str | None = None, *, rules: tuple[Rule, ...] = RULES
) -> SourceClassification:
    """Classify ``url`` (and, last, ``title``) with the ordered rule table."""
    parsed = _parse(url)
    if parsed is None:
        return SourceClassification(SourceType.UNKNOWN, "invalid_url", Basis.DEFAULT)
    host, segments = parsed
    first_label = host.split(".", 1)[0]
    for rule in rules:
        if any(_host_matches(host, suffix) for suffix in rule.host_suffixes):
            return SourceClassification(rule.source_type, rule.rule_id, Basis.HOST)
        if first_label in rule.host_first_labels and "." in host:
            return SourceClassification(rule.source_type, rule.rule_id, Basis.HOST)
        if rule.path_segments and (
            not rule.path_only_on_hosts
            or any(_host_matches(host, suffix) for suffix in rule.path_only_on_hosts)
        ):
            if any(_segment_matches(segment, rule.path_segments) for segment in segments):
                return SourceClassification(rule.source_type, rule.rule_id, Basis.PATH)
    if isinstance(title, str) and title:
        folded = title[:MAX_TITLE_CHARS].casefold()
        for rule in rules:
            if any(word.casefold() in folded for word in rule.title_words):
                return SourceClassification(rule.source_type, rule.rule_id, Basis.TITLE)
    return SourceClassification(SourceType.UNKNOWN, "no_rule", Basis.DEFAULT)


def classify_source(
    url: str, title: str | None = None, *, rules: tuple[Rule, ...] = RULES
) -> SourceType:
    """The ``SourceType`` of a URL; ``unknown`` when no rule applies. Never fetches anything."""
    return classify_source_detailed(url, title, rules=rules).source_type
