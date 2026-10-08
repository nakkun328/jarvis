# Research page reader

`backend/research/reader.py` and `backend/research/extraction.py` fetch one web page and turn it into bounded, inert text plus source metadata (JAR-38, JAR-39, JAR-40). Everything a page contains is untrusted data: it is never executed, never treated as instructions, and never used to decide what to fetch next beyond the policy below.

## Usage

```python
reader = PageReader()                 # real httpx transport and system DNS
page = await reader.read(url)         # FetchedPage, or raises ReaderError(reason)
```

`PageReader(transport, resolver, policy, clock)` accepts a `PageTransport`, an async `Resolver`, a `ReaderPolicy`, and a clock. Tests inject fakes; the real `HttpxTransport` is only exercised through `httpx.MockTransport`.

`FetchedPage` carries `requested_url`, `final_url` (fragment removed), `status_code`, `content_type` (media type only), `retrieved_at` (UTC), `body_sha256` (over the bytes actually read), `text`, `title`, `author`, `published_at` (UTC or `None`), `language`, and `truncated` (true when the body or the extracted text hit a cap).

## Failure codes

`ReaderError.reason` is a `ReadFailure`. The exception carries only that fixed code: no upstream messages, headers, or bodies, and no exception chaining. The reader logs only the code, never URLs, queries, headers, or text.

| Code | Meaning |
| --- | --- |
| `blocked_scheme` | Not a well-formed absolute http/https URL, or it contains userinfo, whitespace, or is over 2048 characters |
| `blocked_host` | Non-public host or address, numeric IP spelling a resolver might reinterpret, or a port outside the allowed set |
| `redirect_limit` | More than 5 redirects |
| `too_large` | The declared Content-Length exceeds the hard cap (16 MiB) |
| `timeout` | Per-request or total (20 s) deadline exceeded |
| `bad_content_type` | Not in the allowlist, missing, labelled text but binary, or compressed |
| `http_error` | Status other than 200, or a redirect without Location |
| `extraction_failed` | No text remained after extraction |
| `network_error` | DNS failure, connection failure, TLS failure, or any other transport error |

## Network safety policy

- Only `http` and `https`. Userinfo (`user:pass@host`) is rejected. Only ports 80 and 443 by default (`ReaderPolicy.allowed_ports`).
- Host names are normalised with IDNA (full-width digits become ASCII). `localhost`, `*.localhost`, `*.local`, `*.internal`, `*.localdomain`, `*.home.arpa`, `*.lan`, `*.intranet`, and single-label names are refused. Non-canonical numeric hosts (decimal, octal, hex, short forms such as `127.1`) are refused outright; they are never reinterpreted.
- Resolved addresses must all be public unicast. Blocked: loopback, RFC 1918 private, link local (including the cloud metadata address 169.254.169.254), carrier-grade NAT 100.64.0.0/10, multicast, reserved and documentation ranges, IPv6 unique local (fc00::/7), link local, multicast, IPv4-mapped IPv6 (judged by the embedded IPv4 address), NAT64, 6to4, and Teredo. A single non-public answer rejects the whole name.
- Redirects are followed by the reader, not the HTTP client. Every hop is validated and resolved again, relative `Location` values are resolved against the current URL, and at most 5 redirects are followed.
- No cookies (a fresh client per request and `Set-Cookie` is dropped), no credentials, no `Authorization`, `Proxy-Authorization`, or `Referer` headers, and no proxy or `.netrc` settings from the environment (`trust_env=False`). The `User-Agent` is the fixed `JarvisResearchReader/0.1 (personal research assistant)`.
- The body is streamed and capped at 2 MiB (`truncated` is set when the cap is hit; reading stops at the cap). `Accept-Encoding: identity` is sent and any `Content-Encoding` other than identity is refused, so a small compressed response cannot expand beyond the cap.
- Content types allowed: `text/html`, `application/xhtml+xml`, `text/plain`, `application/json`. PDFs and other binaries are rejected, including a PDF or NUL-containing body that is labelled as text.
- Charset comes from the Content-Type parameter, a BOM, or an early `<meta charset>`; decoding uses `errors="replace"`. Unknown labels, text-transform codecs such as rot13, and UTF-7 fall back to UTF-8.

### DNS rebinding

The reader resolves the host itself, validates every answer, and passes the validated IP to the transport (`TransportRequest.address`). `HttpxTransport` connects to that IP, sends the original `Host` header, and sets the TLS SNI and certificate-verification name to the original host, so no second lookup can return a different address. Limits: a custom transport that ignores `request.address` loses this guarantee; and the reader resolves again on each redirect hop (a name may legitimately change between hops, and each hop is validated afresh). Connecting to an IP literal with a hostname certificate relies on httpx and httpcore honouring the `sni_hostname` request extension. That path is covered here only with a mock transport and should be confirmed once against a real HTTPS server before relying on it.

## Extraction

Standard library `html.parser` only.

- Dropped: `script`, `style`, `noscript`, `template`, `iframe`, `svg`, `object`; `nav`, `footer`, `aside`, `button`, `select`, `dialog`; elements with `role` navigation, banner, contentinfo, or complementary; and hidden content (`hidden` attribute, `aria-hidden="true"`, inline `display:none`, `visibility:hidden`, `opacity:0`, `font-size:0`). Hidden text is a common carrier for prompt injection, so it is dropped rather than passed on. Comments are dropped.
- If the first pass yields no text (for example an unclosed `<nav>` swallowed the page), a second pass skips only the always-dropped and hidden elements.
- Kept: headings (`# ` by level), paragraphs, list items (`- `), table cells, and `pre` text, with whitespace collapsed and blank lines between blocks. Control characters, zero width characters, bidi overrides, and Unicode tag characters are removed.
- The text is capped at 20,000 characters, cut at a word boundary. Title is capped at 300 characters and author at 200.
- `text/plain` gets whitespace normalisation. `application/json` is pretty-printed (invalid or truncated JSON is returned as raw text).
- Prompt-injection text that is visible on the page is preserved verbatim as data. Callers must still treat `text`, `title`, and `author` as untrusted when building prompts.

### Metadata precedence

- Title: `og:title`, then `<title>`.
- Author: `meta name=author`, `article:author` (ignored when it is a URL), `dc.creator`, `citation_author`, then simple JSON-LD `author` (string, `{"name": ...}`, or up to three names from a list).
- Published date, first parseable candidate wins: `article:published_time`; `<meta>` date names (`date`, `dc.date`, `dc.date.issued`, `dcterms.*`, `pubdate`, `citation_publication_date`, ...); `itemprop="datePublished"`; `<time itemprop="datePublished">` or `<time pubdate>`; JSON-LD `datePublished`; then the first plain `<time datetime>`. ISO 8601 and RFC 2822 are accepted; naive values are read as UTC; the result is timezone-aware UTC. An unparseable or implausible (before 1970 or after 2100) value is skipped, and when nothing parses `published_at` is `None`. It is never filled with the retrieval time.
- Language: `<html lang>` (or `xml:lang`), `<meta http-equiv="content-language">`, `og:locale`, normalised to a BCP 47 style tag.

Search result sources are not trusted to name pages to read: a caller should pass only URLs it chose to read, and every one is subject to this policy.
