"""
Built-in tools for Stirrup agent

These tools are fully standalone - no MCP server dependencies.
"""

import asyncio
import concurrent.futures
import functools
import ipaddress
import json
import socket
from itertools import zip_longest
from typing import Any

import httpx
import trafilatura
from loguru import logger
from openai.types.chat.chat_completion_tool_param import ChatCompletionToolParam

from .xml_format import format_web_fetch_result

WEB_FETCH_TIMEOUT = 60 * 3
# Cap the body we buffer before parsing so a hostile or misconfigured source
# can't blow the runner's memory. Well above MAX_LENGTH_WEB_FETCH_HTML (40k
# chars), which is what actually reaches the model after extraction.
MAX_WEB_FETCH_BYTES = 20 * 1024 * 1024
# Mirrors runner/utils/image_fetch.py: redirects are followed manually so every
# hop can be scheme-checked, since the URL is model-chosen.
MAX_WEB_FETCH_REDIRECTS = 10
# Connect gets its own, much smaller budget than the overall fetch. Passing a
# bare float to httpx sets connect/read/write/pool all to WEB_FETCH_TIMEOUT, so a
# blackholed address would burn the full 180s before failover tried the next one
# — and with two bad addresses that already exceeds the 300s tool_call_timeout.
# Sequential failover exists precisely because AAAA often sorts first while Modal
# egress is IPv4-oriented, so it has to be cheap to probe a dead address.
WEB_FETCH_CONNECT_TIMEOUT = 10.0
# Bounds a pathological PDF. See _PDF_EXECUTOR for what this does and does not
# fix: it fails the fetch fast, but cannot kill the worker thread.
PDF_PARSE_TIMEOUT = 30.0
# Bounds a hanging resolver; the lookup itself runs off the event loop.
DNS_RESOLUTION_TIMEOUT = 10.0
# Dedicated pool for DNS, deliberately NOT the loop's default executor.
#
# asyncio.wait_for cancels the *await*, but a thread running getaddrinfo cannot
# be cancelled — the work keeps occupying its worker until the resolver gives
# up. On the default executor those abandoned lookups would sit in the same pool
# as every asyncio.to_thread call in the container (including this module's PDF
# and HTML parses), so under @modal.concurrent a task prompt could aim the
# fetcher at a black-hole resolver repeatedly and starve unrelated trajectories.
# Isolating DNS caps the blast radius at these workers: saturating them degrades
# fetch_web_page (resolves queue, then time out and the fetch is refused with a
# clear reason) and nothing else.
_DNS_MAX_WORKERS = 4
_DNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=_DNS_MAX_WORKERS, thread_name_prefix="stirrup-webfetch-dns"
)
# Single worker on purpose: PyMuPDF is not thread-safe, and two trajectories in
# one @modal.concurrent container calling pymupdf.open/get_text at the same time
# can corrupt MuPDF's process-global C state and fault the shared container
# rather than failing one fetch. One worker serializes parses process-wide and,
# like the DNS pool, keeps them off the default executor. trafilatura's HTML
# extraction stays on the default pool — lxml is safe to run concurrently.
# Residual limitation, stated rather than papered over: PDF_PARSE_TIMEOUT fails
# the *fetch* fast but cannot cancel the thread, so a genuinely wedged parse keeps
# the single worker and later PDF fetches queue and then time out. That
# degradation is confined to PDF parsing — DNS, run_shell, MCP tools and HTML
# extraction all run elsewhere — and is the price of serializing a library that
# is not thread-safe. Killable parsing would need a subprocess pool.
_PDF_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="stirrup-webfetch-pdf"
)
# How many validated addresses to try before giving up. Bounds worst-case
# connect attempts at MAX_WEB_FETCH_REDIRECTS x this.
MAX_PINNED_CONNECT_ATTEMPTS = 4
_ALLOWED_FETCH_SCHEMES = ("http://", "https://")

# Identify ourselves honestly rather than impersonating a browser. The previous
# value claimed "Chrome/124.0.0.0" but sent none of the Sec-Fetch-*/sec-ch-ua
# client hints a real Chrome always sends, and WAFs reject that inconsistency:
# cpsc.gov returned 403 to the spoofed UA on every run of Project Balboa task
# 6034, while an honest UA, a bare `curl` UA, and no UA at all all returned 200.
# A declared agent string is also what public-sector robots policies expect.
DEFAULT_WEBFETCH_HEADERS = {
    "User-Agent": (
        "MercorRLStudio-ResearchAgent/1.0 "
        "(+https://studio.mercor.com; contact: studio-eng@mercor.com)"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "application/pdf;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# Content types we can turn into text for the model. Anything else gets an
# explicit error rather than a success envelope with an empty body.
_HTML_CONTENT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "application/xml",
    "text/xml",
)
_PDF_CONTENT_TYPES = ("application/pdf", "application/x-pdf")
_PLAIN_TEXT_CONTENT_TYPES = ("text/plain", "text/markdown", "application/json")
# Labels that carry no real type information, so magic-byte sniffing applies.
_GENERIC_CONTENT_TYPES = ("application/octet-stream", "binary/octet-stream")

FETCH_WEB_PAGE_TOOL: ChatCompletionToolParam = {
    "type": "function",
    "function": {
        "name": "fetch_web_page",
        "description": (
            "Fetch and extract the main content from a web page as markdown. "
            "Returns body text or error as XML."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Full HTTP or HTTPS URL of the web page to fetch and extract",
                },
            },
            "required": ["url"],
        },
    },
}

BUILTIN_TOOLS: dict[str, ChatCompletionToolParam] = {
    "fetch_web_page": FETCH_WEB_PAGE_TOOL,
}


class WebFetchError(Exception):
    """Raised when a page cannot be fetched or turned into text."""


def _content_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "").split(";")[0].strip().lower()


def _declared_charset(response: httpx.Response) -> str | None:
    """Charset from the Content-Type header, if the server declared one."""
    for param in response.headers.get("content-type", "").split(";")[1:]:
        name, _, value = param.partition("=")
        if name.strip().lower() == "charset":
            return value.strip().strip("\"'") or None
    return None


def _decode_text(raw: bytes, charset: str | None) -> str:
    """Decode a text body, honoring the server's declared charset.

    The pre-streaming code used ``response.text``, which respects the charset;
    hardcoding UTF-8 turns any non-UTF-8 page into mojibake. HTML does not come
    through here — trafilatura is handed raw bytes so it can also read
    ``<meta charset>``, which the header alone would miss.
    """
    for encoding in (charset, "utf-8"):
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    # latin-1 maps every byte, so this cannot raise.
    return raw.decode("latin-1")


def _is_non_public(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """Whether ``ip`` is anything other than a publicly-routable address.

    ``not is_global`` is the load-bearing clause: RFC 6598 shared/CGNAT space
    (``100.64.0.0/10``) is neither private nor global, so a rule built from
    ``is_private`` and friends alone let it through and it could be pinned for
    connect, reaching CGNAT-routed internal services. The explicit flags are kept
    alongside it so the intent stays readable and the check does not silently
    depend on how a given Python version defines ``is_global``.

    IPv4-mapped and 6to4 addresses are judged on the embedded IPv4 address, which
    is authoritative in *both* directions. Judging the v6 wrapper instead is wrong
    twice over: ``ip_address("::ffff:100.64.0.1").is_global`` is True (so CGNAT
    would slip through), while ``ip_address("::ffff:8.8.8.8").is_reserved`` is
    also True (so a legitimate public host resolving to a mapped address would be
    refused).
    """
    embedded = getattr(ip, "ipv4_mapped", None) or getattr(ip, "sixtofour", None)
    if embedded is not None:
        return _is_non_public(embedded)
    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def _is_blocked_address(host: str) -> str | None:
    """Return a reason when ``host`` resolves to a non-public address, else None."""
    return (await _resolve_public_addresses(host))[1]


async def _resolve_public_addresses(host: str) -> tuple[list[str], str | None]:
    """Resolve ``host``, returning ``(addresses, refusal_reason)``.

    Returns the resolved addresses so the caller can *connect to one of them*
    rather than let httpx resolve again. Validating and then handing the hostname
    back to httpx is a TOCTOU window: an attacker controlling DNS for the target
    can answer with a public address here and flip the record to
    169.254.169.254 before the connect, and the metadata response then lands in
    the trajectory body. See ``_fetch_body`` for the pinning.

    The URL is model-chosen and the model is steerable through task content, so
    without this a task prompt can point the fetcher at the cloud metadata
    endpoint (``169.254.169.254``) or an internal service and the response body
    is stored in the trajectory for anyone with read access. Every resolved
    address is checked, and any hit blocks the fetch.

    Residual risk: an attacker-controlled DNS server could return a public
    address here and a private one at connect time (DNS rebinding). Closing that
    needs connect-time pinning via a custom transport; the check below removes
    the direct path, which is what the reported finding described.
    """
    # Resolved off the event loop: socket.getaddrinfo is synchronous, and under
    # @modal.concurrent a blocking resolve freezes the shared loop for every
    # trajectory in the container — while it is blocked neither asyncio.wait_for
    # nor the run-level asyncio.timeout can fire, so one slow lookup stalls the
    # whole lane past its deadlines. Runs on _DNS_EXECUTOR rather than the
    # default pool so an abandoned lookup cannot starve unrelated to_thread work
    # (see the constant for why).
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.run_in_executor(
                _DNS_EXECUTOR,
                functools.partial(
                    socket.getaddrinfo, host, None, proto=socket.IPPROTO_TCP
                ),
            ),
            timeout=DNS_RESOLUTION_TIMEOUT,
        )
    except TimeoutError:
        return [], f"DNS lookup for host {host!r} timed out"
    except socket.gaierror as exc:
        return [], f"could not resolve host {host!r}: {exc}"

    addresses: list[str] = []
    for info in infos:
        # sockaddr[0] is typed ``str | int`` to cover non-IP families.
        address = str(info[4][0])
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return [], f"unparseable address {address!r} for host {host!r}"
        # _is_non_public unwraps IPv4-mapped / 6to4 forms itself, so
        # ::ffff:169.254.169.254 and ::ffff:100.64.0.1 are both caught here.
        if _is_non_public(ip):
            return (
                [],
                f"host {host!r} resolves to non-public address {address} — "
                "refusing to fetch internal or metadata endpoints",
            )
        addresses.append(address)
    if not addresses:
        return [], f"host {host!r} resolved to no usable addresses"
    return addresses, None


def _ascii_host(url: httpx.URL) -> str:
    """The IDNA/ASCII hostname httpx itself would put on the wire.

    ``httpx.URL.host`` returns *Unicode* for internationalized names — and even
    decodes an ``xn--`` input back to Unicode — while the wire form is
    ``raw_host``. Using ``.host`` for the Host header or for TLS SNI breaks IDN
    fetches that worked when httpx owned the URL end to end, so the ASCII form is
    used for resolution, Host, and SNI alike.
    """
    return url.raw_host.decode("ascii")


def _request_authority(url: httpx.URL) -> str:
    """The ``Host`` value httpx would have sent for ``url``.

    Two things ``httpx.URL.host`` would get wrong here: it never carries the
    port, so overriding Host with it drops ``:port`` for non-default ports and
    port-sensitive or virtual-hosted servers see the wrong authority even though
    the TCP connect used the right port; and it is Unicode for IDNs (see
    ``_ascii_host``). ``url.port`` is None for the scheme default, so the common
    case still sends a bare hostname. Built from parsed URL components, never raw
    argument text, so it cannot carry injected headers.
    """
    host = _ascii_host(url)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # IPv6 literal
    return host if url.port is None else f"{host}:{url.port}"


def _extract_pdf_text(raw: bytes) -> str:
    """Extract text from PDF bytes. CPU-bound; call via ``asyncio.to_thread``.

    ``trafilatura`` is an HTML extractor: handed a PDF it returns "" and the
    model receives ``<body></body>`` — indistinguishable from a blank page.
    ``pymupdf`` is already a runner dependency, so parse properly instead.
    """
    try:
        import pymupdf

        with pymupdf.open(stream=raw, filetype="pdf") as doc:
            # get_text()'s return type widens to list/dict for other output
            # modes; "text" always yields a str, so pin the mode and coerce.
            pages = [str(page.get_text("text") or "") for page in doc]
        text = "\n\n".join(p.strip() for p in pages if p.strip())
    except Exception as exc:
        raise WebFetchError(f"could not extract text from PDF: {exc}") from exc
    if not text.strip():
        raise WebFetchError(
            "PDF contained no extractable text (it may be a scanned image)"
        )
    return text


def _order_connect_candidates(addresses: list[str]) -> list[str]:
    """Dedupe and interleave address families for the pinned-connect attempts.

    Walking raw ``getaddrinfo`` order and truncating at
    ``MAX_PINNED_CONNECT_ATTEMPTS`` reintroduces the very failure the failover
    exists to fix: a dual-stack host answering with four or more AAAA records
    before its A record spends every attempt on IPv6, and on Modal's
    IPv4-oriented egress those can all be unreachable, so the fetch fails with a
    validated A record never tried.

    Interleaving starts with whichever family the resolver listed first, so this
    is not a blanket "prefer IPv4" override of system policy — it just guarantees
    the other family is reached by the second attempt however many records the
    first family returned. Order within each family is preserved.

    Pure: reorders and dedupes only, performs no I/O or name resolution, and
    parses the already-validated literals solely to read ``.version``. Every
    input has already passed ``_resolve_public_addresses``, so this cannot
    introduce or resurrect an unvalidated destination.
    """
    seen: set[str] = set()
    families: dict[int, list[str]] = {4: [], 6: []}
    order: list[int] = []
    for address in addresses:
        if address in seen:
            continue
        seen.add(address)
        version = ipaddress.ip_address(address).version
        families[version].append(address)
        if version not in order:
            order.append(version)

    if len(order) < 2:
        return families[order[0]] if order else []

    first, second = order
    ordered: list[str] = []
    for a, b in zip_longest(families[first], families[second]):
        if a is not None:
            ordered.append(a)
        if b is not None:
            ordered.append(b)
    return ordered


async def _stream_one_hop(
    client: httpx.AsyncClient, url: str, host: str, addresses: list[str]
) -> tuple[str, Any]:
    """Fetch one hop of ``url``, pinned to a validated address.

    Returns ``("redirect", next_url)`` or ``("body", (raw, media_type, charset))``.

    Tries each validated address in turn, falling through only on *connect*
    failures. Pinning to a single address removed httpx/anyio's Happy Eyeballs
    and multi-A failover: ``getaddrinfo`` commonly returns AAAA first on
    dual-stack hosts while Modal's public egress is IPv4-oriented, so one dead
    IPv6 connect would fail a fetch whose IPv4 address had already validated.

    Deliberately no failover on HTTP status errors or read timeouts — a 403 or a
    slow body is not a connectivity problem, and retrying read timeouts would
    multiply wall time by hops x addresses. Every candidate has already passed
    ``_resolve_public_addresses``, so failover cannot reach an unvalidated
    destination, and each attempt keeps ``sni_hostname`` on the real hostname so
    certificate verification is never relaxed.
    """
    authority = _request_authority(httpx.URL(url))
    connect_errors: list[str] = []
    candidates = _order_connect_candidates(addresses)[:MAX_PINNED_CONNECT_ATTEMPTS]
    for address in candidates:
        pinned = httpx.URL(url).copy_with(host=address)
        try:
            async with client.stream(
                "GET",
                pinned,
                headers={"Host": authority},
                extensions={"sni_hostname": host},
                timeout=httpx.Timeout(
                    WEB_FETCH_TIMEOUT, connect=WEB_FETCH_CONNECT_TIMEOUT
                ),
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise WebFetchError(
                            f"redirect from {url[:120]} has no Location header"
                        )
                    # Joined against the original URL, never the pinned one: a
                    # relative Location must resolve against the origin, and the
                    # caller re-resolves and re-pins that host.
                    return "redirect", str(httpx.URL(url).join(location))

                response.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_WEB_FETCH_BYTES:
                        raise WebFetchError(
                            f"response exceeds the {MAX_WEB_FETCH_BYTES}-byte cap"
                        )
                    chunks.append(chunk)
                return "body", (
                    b"".join(chunks),
                    _content_type(response),
                    _declared_charset(response),
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            connect_errors.append(f"{address}: {exc}")
            continue

    raise WebFetchError(
        f"could not connect to any validated address for host {host!r} "
        f"({'; '.join(connect_errors)})"
    )


async def _fetch_body(
    client: httpx.AsyncClient, url: str
) -> tuple[bytes, str, str | None]:
    """GET ``url``, following redirects manually and validating every hop.

    The URL is model-chosen, so redirects are followed here rather than by httpx
    (``follow_redirects=False`` at the call site): each hop is checked for an
    http(s) scheme — an https->file:// or https->gopher:// hop would otherwise
    slip past — and resolved to a publicly-routable address, which the connect is
    then pinned to. Same shape as ``runner/utils/image_fetch``'s
    ``_fetch_streaming``. The body is counted as it streams and the connection is
    dropped past ``MAX_WEB_FETCH_BYTES``.

    Returns ``(raw_bytes, media_type, declared_charset)``.
    """
    current = url
    for _ in range(MAX_WEB_FETCH_REDIRECTS + 1):
        if not current.lower().startswith(_ALLOWED_FETCH_SCHEMES):
            raise WebFetchError(f"refusing to fetch non-http(s) URL: {current[:120]}")
        # ASCII/IDNA form throughout: resolution, Host and SNI must all use the
        # wire hostname, not httpx's Unicode-decoded `.host`.
        if not httpx.URL(current).raw_host:
            raise WebFetchError(f"URL has no host: {current[:120]}")
        host = _ascii_host(httpx.URL(current))
        addresses, blocked = await _resolve_public_addresses(host)
        if blocked:
            raise WebFetchError(blocked)

        kind, payload = await _stream_one_hop(client, current, host, addresses)
        if kind == "redirect":
            current = payload
            continue
        return payload

    raise WebFetchError(f"too many redirects (>{MAX_WEB_FETCH_REDIRECTS})")


async def execute_fetch_web_page(url: str) -> str:
    """Execute fetch_web_page tool"""
    try:
        # follow_redirects=False so _fetch_body can scheme-check every hop.
        async with httpx.AsyncClient(
            headers=DEFAULT_WEBFETCH_HEADERS,
            follow_redirects=False,
            timeout=WEB_FETCH_TIMEOUT,
        ) as client:
            raw, content_type, charset = await _fetch_body(client, url)

        # Sniff the PDF magic whenever the label is missing *or* generic. S3/CDN
        # PDFs are routinely served as application/octet-stream, and treating
        # those as unsupported would undercut the whole PDF fix. Mirrors
        # image_fetch's sniff-on-generic-MIME behavior.
        looks_like_pdf = raw[:5] == b"%PDF-"
        if content_type in _PDF_CONTENT_TYPES or (
            looks_like_pdf
            and (not content_type or content_type in _GENERIC_CONTENT_TYPES)
        ):
            # to_thread for the same reason as the DNS resolve: parsing up to
            # MAX_WEB_FETCH_BYTES of PDF on the event loop freezes every other
            # trajectory in the container under @modal.concurrent, and blocks
            # asyncio.wait_for / the run-level asyncio.timeout from firing.
            # _PDF_EXECUTOR, not to_thread: PyMuPDF is not thread-safe, so
            # parses are serialized on a single dedicated worker rather than
            # racing on the shared default pool (see the constant).
            loop = asyncio.get_running_loop()
            try:
                text = await asyncio.wait_for(
                    loop.run_in_executor(_PDF_EXECUTOR, _extract_pdf_text, raw),
                    timeout=PDF_PARSE_TIMEOUT,
                )
            except TimeoutError:
                raise WebFetchError(
                    f"PDF parsing exceeded {PDF_PARSE_TIMEOUT:.0f}s "
                    "(the document may be pathological, or another parse is "
                    "still holding the single PDF worker)"
                ) from None
            return format_web_fetch_result(url, body=text)

        if content_type in _HTML_CONTENT_TYPES or not content_type:
            # Raw bytes, not a pre-decoded str: trafilatura detects the encoding
            # itself, including from <meta charset>, which the Content-Type
            # header alone would miss.
            # Same hazard as the PDF parse above — trafilatura on a multi-MB
            # document is CPU-bound, so keep it off the shared event loop.
            body_md = (
                await asyncio.to_thread(
                    trafilatura.extract, raw, output_format="markdown"
                )
                or ""
            )
            if not body_md.strip():
                # Distinguish "extractor found no article" from "page is blank";
                # silently returning an empty body reads to the model as the
                # latter and it moves on without retrying.
                raise WebFetchError(
                    "no main content could be extracted from the HTML "
                    "(the page may be a redirect stub, paywalled, or "
                    "JavaScript-rendered)"
                )
            return format_web_fetch_result(url, body=body_md)

        if content_type in _PLAIN_TEXT_CONTENT_TYPES:
            return format_web_fetch_result(url, body=_decode_text(raw, charset))

        raise WebFetchError(
            f"unsupported content type '{content_type}' — fetch_web_page "
            "handles HTML, PDF, and plain text"
        )

    except WebFetchError as exc:
        logger.warning(f"Web fetch failed for {url}: {exc}")
        return format_web_fetch_result(url, error=str(exc))
    except httpx.HTTPError as exc:
        logger.warning(f"HTTP error fetching {url}: {exc}")
        return format_web_fetch_result(url, error=str(exc))
    except Exception as exc:
        logger.warning(f"Error fetching {url}: {exc}")
        return format_web_fetch_result(url, error=str(exc))


def parse_builtin_tool_args(arguments: str) -> dict[str, Any]:
    """Parse tool arguments from JSON string"""
    try:
        return json.loads(arguments) if arguments else {}
    except json.JSONDecodeError:
        return {}
