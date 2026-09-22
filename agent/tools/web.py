"""web_search (one function, pluggable provider) and web_fetch (HTML -> markdown)."""
from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx

FETCH_CAP = 16 * 1024
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


class WebError(RuntimeError):
    pass


async def web_search(query: str, *, provider: str, api_key: str, client: httpx.AsyncClient | None = None) -> list[dict]:
    own = client is None
    client = client or httpx.AsyncClient(timeout=30, headers={"User-Agent": UA}, follow_redirects=True)
    try:
        if provider == "tavily":
            if not api_key:
                raise WebError("search is not configured (no SEARCH_API_KEY)")
            r = await client.post("https://api.tavily.com/search",
                                  headers={"Authorization": f"Bearer {api_key}"},
                                  json={"query": query, "max_results": 8})
            r.raise_for_status()
            items = [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
                     for x in r.json().get("results", [])]
        elif provider == "brave":
            if not api_key:
                raise WebError("search is not configured (no SEARCH_API_KEY)")
            r = await client.get("https://api.search.brave.com/res/v1/web/search",
                                 params={"q": query, "count": 8},
                                 headers={"X-Subscription-Token": api_key, "Accept": "application/json"})
            r.raise_for_status()
            items = [{"title": x.get("title", ""), "url": x.get("url", ""),
                      "snippet": _strip_tags(x.get("description", ""))}
                     for x in r.json().get("web", {}).get("results", [])]
        elif provider == "duckduckgo":
            r = await client.post("https://html.duckduckgo.com/html/", data={"q": query})
            r.raise_for_status()
            items = _parse_ddg(r.text)
        else:
            raise WebError(f"unknown search provider {provider!r}")
    except httpx.HTTPStatusError as e:
        raise WebError(f"search failed: HTTP {e.response.status_code}") from e
    except httpx.HTTPError as e:
        raise WebError(f"search failed: {type(e).__name__}") from e
    finally:
        if own:
            await client.aclose()
    return items[:8]


def _strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _parse_ddg(page: str) -> list[dict]:
    out = []
    links = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.S)
    for i, (href, title) in enumerate(links):
        href = html.unescape(href)
        if "uddg=" in href:
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
        out.append({"title": _strip_tags(title), "url": href,
                    "snippet": _strip_tags(snippets[i]) if i < len(snippets) else ""})
    return out


def html_to_markdown(page: str) -> str:
    from markdownify import markdownify

    page = re.sub(r"<(script|style|noscript|svg|iframe)\b.*?</\1>", "", page, flags=re.S | re.I)
    md = markdownify(page, heading_style="ATX", strip=["img"])
    return re.sub(r"\n{3,}", "\n\n", md).strip()


async def web_fetch(url: str, *, client: httpx.AsyncClient | None = None) -> dict:
    if urlparse(url).scheme not in ("http", "https"):
        raise WebError("only http(s) URLs are supported")
    own = client is None
    client = client or httpx.AsyncClient(timeout=30, headers={"User-Agent": UA}, follow_redirects=True)
    try:
        async with client.stream("GET", url) as r:
            body = bytearray()
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > 5 * 1024 * 1024:  # don't pull huge files into memory
                    break
            ctype = r.headers.get("content-type", "").lower()
            status, final_url = r.status_code, str(r.url)
    except httpx.HTTPError as e:
        raise WebError(f"fetch failed: {type(e).__name__}: {e}") from e
    finally:
        if own:
            await client.aclose()
    text = bytes(body).decode("utf-8", errors="replace")
    if "html" in ctype or (not ctype and text.lstrip()[:15].lower().startswith(("<!doctype", "<html"))):
        content = html_to_markdown(text)
    elif ctype.startswith("text/") or "json" in ctype or "xml" in ctype or not ctype:
        content = text
    else:
        content = f"[non-text content: {ctype}, {len(body)} bytes; download with shell (curl) if needed]"
    truncated = len(content) > FETCH_CAP
    return {"url": final_url, "status": status, "content_type": ctype,
            "content": content[:FETCH_CAP], "truncated": truncated}
