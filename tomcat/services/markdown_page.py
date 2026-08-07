"""Render a Markdown document as a standalone HTML page.

Deliberately tiny and dependency-free. This exists so docs/PRIVACY.md can be
served by the bot without keeping a second, hand-maintained HTML copy that
would drift from the Markdown the moment either one is edited.

Supports only what our docs actually use: ATX headings, bold, italic, inline
code, links, bullet lists, horizontal rules and paragraphs. Anything else is
passed through as escaped text rather than guessed at.
"""

from __future__ import annotations

import html
import os
import re
from typing import List

_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_CODE = re.compile(r"`([^`]+)`")


def _inline(text: str) -> str:
    """Escape, then apply inline formatting.

    Code spans are extracted first and reinserted last so their contents cannot
    be reinterpreted as bold or a link.
    """
    out = html.escape(text, quote=False)
    stash: List[str] = []

    def _hold(m: re.Match) -> str:
        stash.append(m.group(1))
        return "\x00%d\x00" % (len(stash) - 1)

    out = _CODE.sub(_hold, out)
    #Only http(s) and mailto targets become links; anything else stays as text.
    out = _LINK.sub(
        lambda m: (
            '<a href="%s">%s</a>' % (html.escape(m.group(2), quote=True), m.group(1))
            if m.group(2).startswith(("http://", "https://", "mailto:"))
            else "%s (%s)" % (m.group(1), m.group(2))
        ),
        out,
    )
    out = _BOLD.sub(lambda m: "<strong>%s</strong>" % m.group(1), out)
    out = _ITALIC.sub(lambda m: "<em>%s</em>" % m.group(1), out)
    for i, code in enumerate(stash):
        out = out.replace("\x00%d\x00" % i, "<code>%s</code>" % code)
    return out


def markdown_to_html(md: str) -> str:
    """Convert the supported Markdown subset to an HTML fragment."""
    lines = md.replace("\r\n", "\n").split("\n")
    parts: List[str] = []
    para: List[str] = []
    in_list = False

    def flush_para() -> None:
        if para:
            parts.append("<p>%s</p>" % _inline(" ".join(para).strip()))
            para.clear()

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            parts.append("</ul>")
            in_list = False

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            flush_para()
            close_list()
            continue

        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            flush_para()
            close_list()
            parts.append("<hr>")
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para()
            close_list()
            level = len(m.group(1))
            parts.append("<h%d>%s</h%d>" % (level, _inline(m.group(2)), level))
            continue

        m = re.match(r"^[-*]\s+(.*)$", stripped)
        if m:
            flush_para()
            if not in_list:
                parts.append("<ul>")
                in_list = True
            parts.append("<li>%s</li>" % _inline(m.group(1)))
            continue

        close_list()
        para.append(stripped)

    flush_para()
    close_list()
    return "\n".join(parts)


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    margin: 0 auto; padding: 2.5rem 1.25rem 4rem; max-width: 46rem;
    font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: #1a1a1a; background: #fff;
  }}
  h1 {{ font-size: 1.9rem; line-height: 1.25; margin: 0 0 .4rem; }}
  h2 {{ font-size: 1.25rem; margin: 2.2rem 0 .6rem; }}
  h3 {{ font-size: 1.05rem; margin: 1.6rem 0 .4rem; }}
  p, li {{ margin: 0 0 .85rem; }}
  ul {{ padding-left: 1.3rem; }}
  hr {{ border: 0; border-top: 1px solid #e2e2e2; margin: 2.2rem 0; }}
  a {{ color: #0b62c4; }}
  code {{
    background: #f2f3f5; padding: .12em .35em; border-radius: 3px;
    font-size: .9em; word-break: break-all;
  }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #e8e8e8; background: #16181c; }}
    hr {{ border-top-color: #333; }}
    a {{ color: #6fb2ff; }}
    code {{ background: #23262b; }}
  }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def render_markdown_file(path: str, title: str) -> str:
    """Read a Markdown file and return a complete HTML page."""
    with open(path, "r", encoding="utf-8") as f:
        return _PAGE.format(title=html.escape(title, quote=True),
                            body=markdown_to_html(f.read()))


def privacy_policy_path() -> str:
    """Absolute path to docs/PRIVACY.md, resolved from this file."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "..", "docs", "PRIVACY.md"))
