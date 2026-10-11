"""Four tools over plain HTTP: view (GET a page, dump all of it), submit (send one of its forms), record, gaps.
main.py calls them in-process (its own model loop); `python web_tools.py` still serves them over MCP.
Numbers whose format the page marks ("64,853 (en-US)", or a page-wide "Number format: en-US") reach the model as
plain numbers: models misread thousands separators, code doesn't.
SITES_JSON, STORE_PATH and COOKIE_DIR configure it. No browser, no JavaScript, no HTML library: the standard library's
HTMLParser numbers the links and forms, urllib sends the requests with each site's cookies."""
from __future__ import annotations

import asyncio
import contextlib
import difflib
import fcntl
import functools
import io
import json
import os
import re
import sys
import types
import typing
import uuid
import zipfile
from collections import defaultdict
from decimal import Decimal
from html import escape, unescape
from html.parser import HTMLParser
from http.cookiejar import LWPCookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

sys.path.insert(0, os.environ.get("EVIDENCE_DIR", "."))

from mcp.server.mcpserver import MCPServer  # noqa: E402
from pydantic import BaseModel, ValidationError  # noqa: E402

import evidence as ev  # noqa: E402
from resolver import resolve  # noqa: E402

SITES = {s["siteId"]: s["url"].rstrip("/") for s in json.loads(os.environ.get("SITES_JSON", "[]"))}
STORE = Path(os.environ.get("STORE_PATH", "evidence.json"))
COOKIES = Path(os.environ.get("COOKIE_DIR", "."))
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"

server = MCPServer("web", instructions="Four tools: view, submit, record, gaps. Plain HTTP page dumps and form posts.")

# --- HTTP: page dump and form submission -------------------------------------------------------------------------------

ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"))
NUMBERED = {"a": "href", "form": None, "input": None, "select": None, "textarea": None, "button": None,
            "iframe": "src", "frame": "src"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
DROP = {"script", "style", "template"}
SKIP = {"meta", "link"}   # head noise (icons, viewport); the title stays
# attributes that carry content or form state; style/class/autocomplete and the like are layout only
KEEP = {"href", "src", "action", "method", "enctype", "name", "value", "type", "checked", "selected", "disabled",
        "hidden", "readonly", "multiple", "required", "alt", "title", "role", "id", "for", "form", "colspan",
        "rowspan", "datetime", "lang", "placeholder", "min", "max", "formaction", "formmethod", "formenctype", "open",
        "download", "inert"}


class Page(HTMLParser):
    """Re-serializes the HTML without scripts/styles, numbers links, frames, forms and fields (data-ref), and keeps
    what a form submission needs: every form with its fields in document order."""

    def __init__(self, url: str):
        super().__init__(convert_charrefs=True)
        self.base, self.out, self.refs, self.forms = url, [], {}, []
        self.drop = 0
        self.form = None            # the open <form>
        self.text_for = None        # the open <textarea>/<option>/<button> that collects its text
        self.select = None
        self.by_id = {}
        self.orphans = []           # fields with form="id", attached at the end
        self.stack = []             # open elements: (tag, label, hidden), for the link index's context

    def handle_starttag(self, tag, attrs):
        if self.drop or tag in DROP:
            self.drop += tag not in VOID
            return
        a = {k: (v if v is not None else "") for k, v in attrs}
        if tag == "base" and a.get("href"):
            self.base = urljoin(self.base, a["href"])
        if tag in SKIP:
            return
        if tag not in VOID:
            label = a.get("aria-label") or a.get("title") or ""
            hidden = "hidden" in a or bool(re.search(r"display\s*:\s*none", a.get("style", ""))) or (
                tag == "dialog" and "open" not in a)
            self.stack.append((tag, label.strip(), hidden))
        shown = [(k, v) for k, v in attrs if k in KEEP or k.startswith(("aria-", "data-"))]
        if re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", a.get("style", "")) and "hidden" not in a:
            shown.append(("hidden", None))   # hidden by its inline style
        if tag in NUMBERED and (NUMBERED[tag] is None or a.get(NUMBERED[tag])):
            n = str(len(self.refs) + 1)
            shown.append(("data-ref", n))
            item = {"tag": tag, "attrs": a, "ref": n, "text": ""}
            outer = self.stack[:-1] if tag not in VOID else self.stack
            item["context"] = next((label for _, label, _ in reversed(outer) if label), "")
            item["collapsed"] = any(h for _, _, h in outer)
            self.refs[n] = item
            if tag == "form":
                item["fields"] = []
                self.form = item
                self.forms.append(item)
                if a.get("id"):
                    self.by_id[a["id"]] = item
            elif tag in ("input", "select", "textarea", "button"):
                # only the field's own attribute counts: a disabled <fieldset> is a lock the page's script opens
                item["disabled"] = "disabled" in a
                if tag == "select":
                    item["options"] = []
                    self.select = item
                if "form" in a:
                    self.orphans.append(item)
                elif self.form is not None:
                    self.form["fields"].append(item)
                if tag in ("textarea", "button"):
                    self.text_for = item
            elif tag == "a":
                self.text_for = item
        if tag == "option" and self.select is not None:
            option = {"value": a.get("value"), "selected": "selected" in a, "text": "",
                      "disabled": "disabled" in a}
            self.select["options"].append(option)
            self.text_for = option
        self.out.append("<" + tag + "".join(f' {k}="{escape(v or "", quote=True)}"' if v is not None else f" {k}"
                                            for k, v in shown) + ">")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and not (self.drop or tag in DROP):
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.drop:
            if tag in DROP:
                self.drop -= 1
            return
        if any(t == tag for t, _, _ in self.stack):
            while self.stack and self.stack.pop()[0] != tag:
                pass
        if tag == "form":
            self.form = None
        elif tag == "select":
            self.select = None
        if tag in ("textarea", "option", "button", "a"):
            self.text_for = None
        if tag not in VOID:
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self.drop:
            return
        if self.text_for is not None:
            self.text_for["text"] += data
        self.out.append(escape(data, quote=False))

    def html(self) -> str:
        text = "".join(self.out).translate(ZERO_WIDTH)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        return re.sub(r"\s*\n\s*", "\n", text).strip()

    def finish_feed(self, text: str) -> str:
        self.feed(text)
        self.close()
        return self.finish().html()

    def finish(self):
        for item in self.orphans:
            owner = self.by_id.get(item["attrs"].get("form"))
            if owner is not None:
                owner["fields"].append(item)
        return self


openers: dict = {}
current: dict = {}   # site -> (url, Page)
visited: dict = {}   # site -> every URL a dump came from
params: dict = {}    # site -> every query parameter name a link, frame, form or field of a dump uses


def learn_params(site: str, page: Page) -> None:
    names = params.setdefault(site, set())
    names.update(k for k, _ in parse_qsl(urlsplit(page.base).query, keep_blank_values=True))
    for item in page.refs.values():
        target = item["attrs"].get("href") or item["attrs"].get("src") or item["attrs"].get("action") or ""
        names.update(k for k, _ in parse_qsl(urlsplit(target).query, keep_blank_values=True))
        if item["attrs"].get("name"):
            names.add(item["attrs"]["name"])


def invented_params(site: str, url: str) -> list[str]:
    """Query parameters of a typed URL that no link, frame, form or field of this site has used: made up."""
    known = params.get(site)
    if not known:
        return []
    return sorted({k for k, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)} - known)


def link_index(site: str, page: Page) -> str:
    """Every link and frame of the page in one list: ref, text, target, the nearest labelled container, and marks for
    links a browser shows only after a click (collapsed menus, closed dialogs) and for pages already opened."""
    seen, rows = visited.get(site, set()), []
    for item in page.refs.values():
        if item["tag"] not in ("a", "iframe", "frame"):
            continue
        target = urljoin(page.base, item["attrs"].get("href") or item["attrs"].get("src") or "").split("#")[0]
        if not target or target.startswith(("javascript:", "mailto:")):
            continue
        text = re.sub(r"\s+", " ", item["text"] or item["attrs"].get("aria-label") or item["attrs"].get("title")
                      or "").strip()[:80]
        path = urlsplit(target)._replace(scheme="", netloc="").geturl()
        marks = [m for m, on in (("in a collapsed menu: works like any link", item["collapsed"]),
                                 ("visited", target in seen)) if on]
        rows.append(f"{item['ref']}: {text or '(no text)'} -> {path}"
                    + (f" [{item['context'][:80]}]" if item["context"] and item["context"] != text else "")
                    + (f" ({'; '.join(marks)})" if marks else ""))
    if not rows:
        return "Links: none"
    return "Links (ref: text -> target [where] (marks)); view(site, link=ref) follows one:\n" + "\n".join(rows[:400])


def opener(site: str):
    if site not in openers:
        jar = LWPCookieJar(str(COOKIES / f"cookies-{site}.txt"))
        with contextlib.suppress(OSError):
            jar.load(ignore_discard=True, ignore_expires=True)
        openers[site] = (build_opener(HTTPCookieProcessor(jar)), jar)
    return openers[site]


def decode(body: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type, re.I) or re.search(rb'<meta[^>]+charset="?([\w-]+)', body[:4000],
                                                                         re.I)
    charset = m.group(1) if m else "utf-8"
    charset = charset.decode() if isinstance(charset, bytes) else charset
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def document(name: str, body: bytes, content_type: str) -> str:
    """A non-HTML response: text as it is; a ZIP file entry by entry; anything else only described."""
    if body[:2] == b"PK":
        parts = []
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                data = z.read(info)
                parts.append(f"--- {info.filename} ({info.file_size} bytes)\n" + document(info.filename, data, ""))
        return "\n".join(parts)
    if body[:4] == b"%PDF":
        return f"(a PDF document, {len(body)} bytes: its text cannot be read here)"
    if "html" in content_type or re.search(rb"<(html|body|div|table|p)\b", body[:2000], re.I):
        return Page("").finish_feed(decode(body, content_type))
    return decode(body, content_type)


MARKED = re.compile(r"(?<![\d.,])(\d[\d.,]*\d|\d)(\s*\(((de|en)(?:-[A-Za-z]{2})?)\))")
DECIMAL_COMMA = re.compile(r"(?<![\d.,/])\d+,\d+(?![\d.,/])")
AMBIGUOUS = re.compile(r"(?<![\d.,/])\d{1,3}(?:[.,]\d{3})+(?:[.,]\d+)?(?![\d.,/])")
PAGE_FORMAT = re.compile(r"(?:Zahlenformat|Number ?format|Numbers?)\s*:?\s*(de|en)(?:-[A-Za-z]{2})?\b", re.I)


def plain_number(raw: str, fmt: str) -> str:
    """'64,853' en -> '64853'; '2.743,58' de -> '2743.58'."""
    thousands, decimal = (",", ".") if fmt == "en" else (".", ",")
    return re.sub(r"[\s'\u00a0\u202f]", "", raw).replace(thousands, "").replace(decimal, ".")


def normalize(text: str) -> str:
    """Marked numbers become plain numbers: inline markers ("64,853 (en-US)" -> "64853 (en-US)") and, on a page
    that states one format for all, its numbers with separators. The marker stays, so the model sees it was done."""
    text = MARKED.sub(lambda m: plain_number(m.group(1), m.group(4).lower()) + m.group(2), text)
    page = PAGE_FORMAT.search(text)
    if page:
        fmt = page.group(1).lower()
        text = AMBIGUOUS.sub(lambda m: plain_number(m.group(0), fmt), text)
        if fmt == "de":
            text = DECIMAL_COMMA.sub(lambda m: m.group(0).replace(",", "."), text)
    return text


def request(site: str, url: str, data: bytes | None = None, content_type: str = "") -> str:
    return normalize(_request(site, url, data, content_type))


def _request(site: str, url: str, data: bytes | None = None, content_type: str = "") -> str:
    """One GET (or POST with data) with the site's cookies; dumps the whole response. Transient failures of a GET
    (connection errors, 502/503/504) are retried twice."""
    open_, jar = opener(site)
    url = quote(url, safe=":/?#[]@!$&'()*+,;=%~")   # what a browser does with spaces and umlauts in a typed URL
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
               "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}
    if site in current:
        headers["Referer"] = current[site][0]
    if content_type:
        headers["Content-Type"] = content_type
    for attempt in range(1 if data is not None else 3):
        try:
            response = open_.open(Request(url, data=data, headers=headers), timeout=30)
        except HTTPError as error:
            response = error   # error pages and rejected forms still have a body worth reading
            if error.code in (502, 503, 504) and data is None and attempt < 2:
                continue
        except (URLError, OSError) as error:
            if data is None and attempt < 2:
                continue
            return f"{site}: {url} failed: {error}"
        break
    with response:
        body = response.read()
        final, status = response.geturl(), response.status if hasattr(response, "status") else response.code
        ctype = response.headers.get("Content-Type", "")
        disposition = response.headers.get("Content-Disposition", "")
    jar.save(ignore_discard=True, ignore_expires=True)
    head = f"{site}: {final} (HTTP {status}" + (f", {ctype}" if ctype and "html" not in ctype else "") + ")"
    if "html" in ctype or (not ctype and b"<html" in body[:2000].lower()):
        if "attachment" not in disposition:
            page = Page(final)
            page.feed(decode(body, ctype))
            page.close()
            current[site] = (final, page.finish())
            visited.setdefault(site, set()).add(final.split("#")[0])
            learn_params(site, page)
            return f"{head}\n{page.html()}\n{link_index(site, page)}"
    return f"{head}\n{document(final, body, ctype)}"


def guard(fn):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except Exception as error:  # noqa: BLE001 - the model reads the reason and decides
            return f"ERROR: {type(error).__name__}: {error}"
    return wrapper


@server.tool()
@guard
def view(site: str, url: str = "", link: int = 0) -> str:
    """GET a page and dump all of it: the server's HTML without scripts and styles, no length limit. Links, frames,
    forms and fields carry data-ref numbers. url is absolute or relative to the current page; empty url reloads the
    current page (or opens the site's start page); link=N follows link or frame N of the last dump. Cookies persist
    per site. ZIP files are unpacked, text files shown."""
    if site not in SITES:
        raise LookupError(f"Unknown site {site!r}; use one of {', '.join(SITES)}")
    page_url, page = current.get(site, (SITES[site] + "/", None))
    if link:
        item = page.refs.get(str(link)) if page else None
        if not item or item["tag"] not in ("a", "iframe", "frame"):
            raise LookupError(f"{link} is not a link or frame of the last dump of {site}")
        url = item["attrs"].get("href") or item["attrs"].get("src")
    base = page.base if page else page_url
    target = urljoin(base, url) if url else page_url
    if not link and (made_up := invented_params(site, target)):
        raise LookupError(f"no link, form or field of {site} uses the parameter(s) {', '.join(made_up)}: made-up "
                          "parameters don't change a page. Open a link of the dump or send a form instead; to "
                          "re-read a page you already opened, record what you need from it the first time")
    return request(site, target)


def form_pairs(form: dict, pressed: dict | None, values: dict) -> tuple[list[tuple[str, str]], list[str]]:
    """The name=value pairs a browser sends for this form (HTML's form data set), with the model's values applied."""
    values = {str(k): v for k, v in values.items()}
    by_ref = {f["ref"]: f for f in form["fields"]}
    for key in list(values):             # field refs -> field names
        if key in by_ref and by_ref[key]["attrs"].get("name"):
            values[by_ref[key]["attrs"]["name"]] = values.pop(key)
    names = {f["attrs"].get("name") for f in form["fields"]}
    pairs, used = [], set()
    for f in form["fields"]:
        a, tag = f["attrs"], f["tag"]
        # no type: an <input> is text, a <button> submits; <select> and <textarea> are never buttons
        name, kind = a.get("name"), a.get("type", "submit" if tag == "button" else "text").lower()
        if not name or (f["disabled"] and name not in values):
            continue
        given = values.get(name)
        if tag == "button" or kind in ("submit", "image", "button", "reset"):
            if f is pressed and kind not in ("button", "reset"):
                if kind == "image":
                    pairs += [(name + ".x", "1"), (name + ".y", "1")]
                else:
                    pairs.append((name, a.get("value", "")))
            continue
        if kind == "file":
            continue
        if tag == "select":
            options = [o for o in f["options"] if not o["disabled"]]
            option_value = lambda o: o["value"] if o["value"] is not None else o["text"].strip()  # noqa: E731
            if name in values:
                wanted = given if isinstance(given, list) else [given]
                chosen = [o for o in options if any(str(w) == option_value(o) for w in wanted)] or \
                         [o for o in options if any(str(w).strip().lower() == o["text"].strip().lower() for w in wanted)]
                if not chosen:
                    raise ValueError(f"{name}: no option {given!r}; options: "
                                     + ", ".join(f"{option_value(o)!r} ({o['text'].strip()})" for o in options[:40]))
                used.add(name)
            else:
                chosen = [o for o in options if o["selected"]] or (options[:1] if "multiple" not in a else [])
            pairs += [(name, option_value(o)) for o in chosen]
            continue
        if kind in ("checkbox", "radio"):
            own = a.get("value", "on")
            if name in values:
                used.add(name)
                if isinstance(given, bool):
                    on = given
                elif isinstance(given, list):
                    on = own in map(str, given)
                else:
                    on = own == str(given)
            else:
                on = "checked" in a
            if on:
                pairs.append((name, own))
            continue
        current_value = f["text"] if tag == "textarea" else a.get("value", "")
        if name in values:
            used.add(name)
            if isinstance(given, list):     # a repeated field: one value per occurrence
                value = given.pop(0) if given else ""
                if given:
                    used.discard(name)
            else:
                value = given
            current_value = "" if value is None else ("on" if value is True else str(value))
        pairs.append((name, current_value))
    extra = [k for k in values if k not in names]
    for k in extra:                       # fields the page asks for that its form does not hold
        v = values[k]
        pairs += [(k, str(x)) for x in v] if isinstance(v, list) else [(k, str(v))]
    return pairs, extra


@server.tool()
@guard
def submit(site: str, form: int, button: str = "", values: dict | None = None) -> str:
    """Send form N of the last dump of this site as the browser would (GET or POST, URL-encoded or multipart), then
    dump the whole response. Hidden fields, defaults, checked boxes and selected options are sent as the page has them;
    values (field name or field ref -> value) replace them: text for inputs, the option value or text for selects,
    true/false or the value for checkboxes and radios, a list for repeated fields. A name the form doesn't hold is
    added as an extra field. button: the submit button's ref, name, value or text; its name=value is sent."""
    if site not in current:
        raise LookupError(f"Call view({site!r}) first")
    page_url, page = current[site]
    chosen = page.refs.get(str(form))
    if not chosen or chosen["tag"] != "form":
        raise LookupError(f"{form} is not a form of the last dump of {site}; forms: "
                          + ", ".join(f["ref"] for f in page.forms))
    buttons = [f for f in chosen["fields"] if (
        (f["tag"] == "button" and f["attrs"].get("type", "submit").lower() == "submit")
        or (f["tag"] == "input" and f["attrs"].get("type", "").lower() in ("submit", "image")))]
    pressed = None
    if button:
        b = str(button).strip()
        matches = [f for f in buttons if b in (f["ref"], f["attrs"].get("name"), f["attrs"].get("value"),
                                               re.sub(r"\s+", " ", f["text"]).strip())]
        if len(matches) != 1:
            raise ValueError(f"button {button!r} matches {len(matches)} submit buttons of form {form}; buttons: "
                             + "; ".join(f"ref {f['ref']} name={f['attrs'].get('name')!r} value="
                                         f"{f['attrs'].get('value')!r} text={f['text'].strip()[:40]!r}"
                                         for f in buttons))
        pressed = matches[0]
    pairs, extra = form_pairs(chosen, pressed, dict(values or {}))
    fa, ba = chosen["attrs"], (pressed or {}).get("attrs", {})
    action = urljoin(page.base, ba.get("formaction") or fa.get("action") or page_url)
    method = (ba.get("formmethod") or fa.get("method") or "get").lower()
    enctype = (ba.get("formenctype") or fa.get("enctype") or "application/x-www-form-urlencoded").lower()
    note = f"(sent extra fields not in the form: {', '.join(extra)})\n" if extra else ""
    if method != "post":
        parts = urlsplit(action)
        return note + request(site, urlunsplit(parts._replace(query=urlencode(pairs), fragment="")))
    if enctype.startswith("multipart/"):
        boundary = uuid.uuid4().hex
        body = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                        for k, v in pairs) + f"--{boundary}--\r\n".encode()
        return note + request(site, action, body, f"multipart/form-data; boundary={boundary}")
    return note + request(site, action, urlencode(pairs).encode(), "application/x-www-form-urlencoded")


# --- evidence ----------------------------------------------------------------------------------------------------------

KINDS: dict[str, type[BaseModel]] = {
    kind: typing.get_args(ev.Evidence.model_fields[kind].annotation)[0] for kind in ev._KEYS}
KEYS = dict(ev._KEYS)
ATOMIC = {"price_conditions"}
NESTED_KEYS = {"requirements": "id", "price_conditions": "valid_from", "scales": "min_quantity"}
case: dict = {}
store: dict[str, dict[str, dict]] = {kind: {} for kind in KINDS}
picks: dict = {}


def nested_model(annotation):
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in typing.get_args(annotation):
        found = nested_model(arg)
        if found:
            return found
    return None


def problems(model: type[BaseModel], data, path: str = "") -> list[str]:
    """Unknown field names (the schema would drop them silently) and numbers that need their format stated."""
    if not isinstance(data, dict):
        return []
    fields, out = model.model_fields, []
    for key, value in data.items():
        if key not in fields:
            near = difflib.get_close_matches(key, fields, n=1, cutoff=0.6)
            out.append(f"{path}{key}: unknown field" + (f" (did you mean {near[0]!r}?)" if near else "")
                       + f"; allowed: {', '.join(fields)}")
            continue
        sub = nested_model(fields[key].annotation)
        if sub and isinstance(value, list):
            for i, item in enumerate(value):
                out += problems(sub, item, f"{path}{key}[{i}].")
        elif sub:
            out += problems(sub, value, f"{path}{key}.")
        elif (isinstance(value, float) and re.fullmatch(r"-?\d+\.\d{3}", repr(value))) or (
                isinstance(value, str) and re.fullmatch(r"\s*-?\d{1,3}([.,]\d{3})+\s*", value)):
            out.append(f"{path}{key}: {value} is ambiguous (thousands separator or decimals?): send it as "
                       f'{{"raw": "<exactly as shown>", "format": "de" or "en"}}')
    return out


def numbers_from_raw(data):
    """{"raw": "1.234,56", "format": "de"} -> "1234.56", anywhere in a record."""
    if isinstance(data, dict):
        if set(data) <= {"raw", "format"} and "raw" in data:
            raw = str(data["raw"]).strip()
            fmt = str(data.get("format") or "").lower()[:2]
            m = re.search(r"-?\d[\d.,  ' ]*", raw)
            if not m:
                return raw
            return plain_number(m.group(0).strip(), fmt if fmt in ("de", "en") else "en")
        return {k: numbers_from_raw(v) for k, v in data.items()}
    if isinstance(data, list):
        return [numbers_from_raw(v) for v in data]
    return data


def empty(value) -> bool:
    return value is None or value == "" or value == [] or value == {} or (
        isinstance(value, str) and value.strip().lower() in ("unknown", "n/a", "none found"))


def merge_record(old, new):
    """Field by field: a later record adds and corrects values, but never wipes a known value with an empty one.
    Nested lists with keys (requirements, price conditions, scales) are merged per key."""
    if isinstance(old, dict) and isinstance(new, dict):
        out = dict(old)
        for key, value in new.items():
            if empty(value):
                continue
            if key in NESTED_KEYS and isinstance(old.get(key), list) and isinstance(value, list):
                k = NESTED_KEYS[key]
                rows = {str(r.get(k)): r for r in old[key] if isinstance(r, dict)}
                for r in value:
                    if isinstance(r, dict):
                        # a price condition is one unit: its prices only mean something with its own price unit,
                        # currency and scales, so a resent condition replaces the old one instead of mixing with it
                        rows[str(r.get(k))] = r if key in ATOMIC else merge_record(rows.get(str(r.get(k))), r)
                out[key] = list(rows.values())
            else:
                out[key] = merge_record(old.get(key), value)
        return out
    return new


@contextlib.contextmanager
def shared_store():
    """Several sessions (one per website) record into the same store at once: reload it under a file lock first."""
    with open(str(STORE) + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            load()
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def save() -> None:
    STORE.write_text(json.dumps({"case": case, "picks": picks, **{k: list(v.values()) for k, v in store.items()}},
                                ensure_ascii=False, indent=1, default=str))


def evidence() -> ev.Evidence:
    return ev.Evidence.model_validate({"case": case, "picks": picks, **{k: list(v.values()) for k, v in store.items()}})


def compact_schema(model: type[BaseModel], indent: str = "  ") -> str:
    """A model's fields as short lines for the model to read: name, type, allowed values, required, description.
    Same content as the JSON schema without its boilerplate (titles, anyOf, $defs)."""
    out = []
    for name, field in model.model_fields.items():
        ann, is_list = field.annotation, False
        while True:                          # unwrap Optional[...] and list[...]
            if typing.get_origin(ann) in (list, tuple, set):
                is_list, ann = True, typing.get_args(ann)[0]
                continue
            opts = [a for a in typing.get_args(ann) if a is not type(None)]
            if typing.get_origin(ann) in (typing.Union, types.UnionType) and len(opts) == 1:
                ann = opts[0]
                continue
            break
        args = [ann]
        sub = nested_model(ann)
        lit = typing.get_args(ann) if typing.get_origin(ann) is typing.Literal else None
        if lit:
            kind = "/".join(map(str, lit))
        elif sub:
            kind = "object"
        else:
            kind = getattr(ann, "__name__", str(ann))
        kind = ("list of " if is_list else "") + kind
        req = "*" if field.is_required() else ""
        desc = f" - {field.description}" if field.description else ""
        out.append(f"{indent}{name}{req}: {kind}{desc}")
        if sub and sub is not model:
            out.append(compact_schema(sub, indent + "  "))
    return "\n".join(out)


SCHEMAS = "\n".join(f"- {kind} (one record per {KEYS[kind]}):\n" + compact_schema(model) for kind, model in KINDS.items())
CASE_SCHEMA = "\n" + compact_schema(ev.Case)


@server.tool(description=(
    "Save evidence records of one kind as soon as you have read them (several records per call is best). A record "
    "with the same key as an earlier one is merged field by field: send only what is new or corrected; empty values "
    "never erase known ones. Use exactly the schema's field names (unknown names are rejected with the nearest "
    "correct one). Numbers: never convert them yourself; if the page writes a number with thousands separators or a "
    'decimal comma, send {"raw": "<exactly as shown>", "format": "de"} (de: 1.234,5) or "en" (en: 1,234.5). '
    "kind 'case' takes one record with the case fields. Invalid records come back with the reason: fix and resend.\n"
    "Fields per kind (* = required; nested objects indented):\n"
    f"- case: {CASE_SCHEMA}\n" + SCHEMAS))
def record(kind: str, records: list[dict]) -> str:
    with shared_store():
        return _record(kind, records)


def _record(kind: str, records: list[dict]) -> str:
    if kind == "case":
        update = {k: v for r in records for k, v in numbers_from_raw(r).items() if not empty(v)}
        bad = problems(ev.Case, update)
        if bad:
            return "Not saved: " + "; ".join(bad)
        case.update(update)
        save()
        return f"case: {case}"
    if kind == "picks":
        picks.update({k: v for r in records for k, v in r.items() if not empty(v)})
        save()
        return "picks saved"
    if kind not in KINDS:
        return f"Unknown kind {kind!r}; use one of case, {', '.join(KINDS)}"
    saved, errors = 0, []
    for n, raw in enumerate(records):
        bad = problems(KINDS[kind], raw)
        if bad:
            errors.append(f"record {n}: " + "; ".join(bad))
            continue
        key = str(raw.get(KEYS[kind]) or "")
        if not key:
            errors.append(f"record {n}: {KEYS[kind]} is required")
            continue
        merged = merge_record(store[kind].get(key), numbers_from_raw(raw))
        try:
            clean = KINDS[kind].model_validate(merged).model_dump(mode="json", exclude_none=True)
        except ValidationError as error:
            errors.append(f"record {n} ({key}): " + "; ".join(
                f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors(include_url=False)))
            continue
        store[kind][key] = merge_record(store[kind].get(key), clean)
        saved += 1
    save()
    return (f"Saved {saved} {kind} ({len(store[kind])} in total)." + (f" NOT saved: {errors}" if errors else "")
            + unstructured_note(kind, records))


STRUCTURED = {"qa": ("quantity_changes", "amendments", "withdraws", "assignments"),
              "messages": ("discount_percent", "min_order_value_eur", "condition_rules", "metal_index_rates",
                           "technical_releases", "packaging_tariffs", "delivery_commitments", "fx_rates",
                           "setup_pools", "bonus_plans")}


def unstructured_note(kind: str, records: list[dict]) -> str:
    """Code reads only the structured fields, never the text: point out records that carry none of them."""
    if kind not in STRUCTURED:
        return ""
    bare = [str(r.get(KEYS[kind])) for r in records if isinstance(r, dict)
            and (kind != "messages" or r.get("kind") not in (None, "other", "signing_tan"))
            and not any(not empty(r.get(f)) for f in STRUCTURED[kind])]
    if not bare:
        return ""
    fields = ", ".join(STRUCTURED[kind])
    return (f" NOTE: {len(bare)} of these ({', '.join(bare[:12])}) have none of the fields {fields}. The text is "
            f"never evaluated: if a record changes a quantity, an attribute, withdraws an earlier record, assigns a key "
            f"or states values, put that into those fields (send the record again with them).")


@server.tool()
def gaps(kind: str = "") -> str:
    """Ask the bid rules what evidence is still missing. kind optionally shows records of that kind."""
    with shared_store():
        return _gaps() if not kind else _peek(kind)


def norm_key(value) -> str:
    return re.sub(r"\s+", "", str(value or "").translate(ZERO_WIDTH)).upper()


def open_keys(e: ev.Evidence) -> list[str]:
    """Every key the evidence already names that no record resolves yet, whatever stage the rules are at: so each
    website can work on it now instead of waiting for the rules to get there."""
    out: list[str] = []
    searched = {norm_key(s.catalog_group): s for s in e.erp_searches}
    recorded = {norm_key(m.number) for m in e.materials}
    groups: dict[str, list[str]] = {}
    for rev in e.revisions:
        for req in rev.requirements:
            group = norm_key(req.catalog_group)
            if group and (group not in searched or not searched[group].complete):
                groups.setdefault(req.catalog_group, []).append(f"{req.id} of revision {rev.id}")
    for group, where in groups.items():
        out.append(f"Search the ERP for catalogue group {group} (named by requirement {', '.join(where[:4])}); open "
                   "every listed material, record them and the search.")
    for s in e.erp_searches:
        for number in s.material_numbers:
            if norm_key(number) not in recorded:
                out.append(f"Open ERP material {number} (listed for group {s.catalog_group}) and record it.")
    defined: dict[str, set[str]] = defaultdict(set)
    for m in e.messages:
        for field, entries in (("condition_group", m.condition_rules), ("packaging_tariff", m.packaging_tariffs),
                               ("delivery_commitment", m.delivery_commitments), ("setup_pool", m.setup_pools),
                               ("bonus_plan", m.bonus_plans), ("end_reserve", m.technical_releases),
                               ("fx", m.fx_rates)):
            defined[field] |= {norm_key(x.key) for x in entries}
        defined["metal_index"] |= {norm_key(x.code) for x in m.metal_index_rates}
    asked: set[tuple[str, str]] = set()

    def need(field: str, key, what: str):
        k = norm_key(key)
        if k and k not in defined[field] and (field, k) not in asked:
            asked.add((field, k))
            out.append(f"Find the latest mail for this exact tender reference that defines {what} {key} (see the "
                       "brief's rules for its sender and subject; check inbox and archive) and record it.")
    for qa in e.qa:
        for a in qa.assignments:
            need("condition_group", a.condition_group, "condition group (Konditionsgruppe)")
            need("packaging_tariff", a.packaging_tariff, "packaging tariff")
            need("delivery_commitment", a.delivery_commitment, "delivery commitment")
            need("setup_pool", a.setup_pool, "setup pool")
            need("bonus_plan", a.bonus_plan, "project bonus plan")
    for rev in e.revisions:
        for req in rev.requirements:
            need("end_reserve", req.end_reserve_key, f"the end reserve (technical release) for revision {rev.id}, key")
    for m in e.materials:
        need("metal_index", m.metal_index, "metal index rates for code")
        for c in m.price_conditions:
            if (c.currency or "EUR").upper() != "EUR":
                need("fx", c.fx_key, f"the {c.currency} exchange rate, key")
    if not any(m.kind == "discount_approval" for m in e.messages):
        out.append("Find the sales director's discount approvals for this exact tender reference (inbox and "
                   "archive, every page) and record them, also superseded ones.")
    return out


def implausible_prices(e: ev.Evidence, factor: Decimal = Decimal(20)) -> list[str]:
    """Per-piece prices that are far off the same material's other conditions or its catalogue group's other
    materials (same currency): the typical result of a misread thousands separator or price unit."""
    rows = []   # (material, group, condition, currency, per-piece price of the smallest scale)
    for m in e.materials:
        for c in m.price_conditions:
            if not c.scales or not c.price_unit:
                continue
            first = min(c.scales, key=lambda x: x.min_quantity)
            if first.price and first.price > 0:
                rows.append((m.number, norm_key(m.catalog_group), c, (c.currency or "EUR").upper(),
                             Decimal(first.price) / Decimal(c.price_unit)))
    out = []
    for number, group, c, cur, unit in rows:
        peers = [u for n, g, _, cu, u in rows if cu == cur and (n == number or (group and g == group))
                 and not (n == number and _ is c)]
        if not peers:
            continue
        mid = sorted(peers)[len(peers) // 2]
        if unit > mid * factor or unit * factor < mid:
            out.append(f"Re-read material {number}'s price condition valid from {c.valid_from} exactly as the page "
                       f"shows it: it gives {unit:.4f} {cur} per piece, its other conditions/group give about "
                       f"{mid:.4f}. Check thousands separators vs decimals (a marker like (en-US) means a comma "
                       f"separates thousands) and the price unit, then record it again.")
    return out


def _gaps() -> str:
    if not case.get("tender_reference") or not case.get("scenario_date"):
        return "Record the case first (kind 'case': tender reference and scenario date from the brief, the domain " \
               "of the bidder's own mailbox)."
    try:
        resolution = resolve(evidence())
    except ValidationError as error:
        return "The evidence does not validate: " + "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors(include_url=False)[:10])
    except Exception as error:  # noqa: BLE001
        return f"The rules could not run: {error!r}"
    lines = [f"- {g.question}" for g in resolution.gaps] + [f"- {q}" for q in implausible_prices(evidence())]
    priced = len((resolution.answer or {}).get("positions") or [])
    head = f"{priced} positions can be priced now."
    text = head + ("\nStill missing:\n" + "\n".join(lines) if lines else "\nNothing is missing.")
    if lines:
        asked = {g.question for g in resolution.gaps}
        extra = [q for q in open_keys(evidence()) if q not in asked]
        if extra:
            text += "\nAlso open already (work on those your website holds, now):\n" + "\n".join(
                f"- {q}" for q in extra[:40])
    return text


def _peek(kind: str = "") -> str:
    """What has been recorded so far: one line per record of a kind, or a count per kind."""
    if not kind:
        return "case: " + json.dumps(case, ensure_ascii=False) + "\n" + "\n".join(
            f"{k}: {len(v)} records" for k, v in store.items())
    if kind not in store:
        return f"Unknown kind {kind!r}; use one of {', '.join(store)}"
    rows = [json.dumps(r, ensure_ascii=False, separators=(",", ":"))[:600] for r in store[kind].values()]
    return f"{kind}: {len(rows)} records\n" + "\n".join(rows)[:12000]


def load() -> None:
    """A follow-up session continues on the evidence the earlier ones saved."""
    if STORE.exists():
        try:
            data = json.loads(STORE.read_text())
        except ValueError:
            return
        case.update(data.get("case") or {})
        picks.update(data.get("picks") or {})
        for kind in KINDS:
            store[kind] = {str(r.get(KEYS[kind])): r for r in data.get(kind) or []}


load()

if __name__ == "__main__":
    server.run()
