"""Ambit's web surface: the landing page and the merchant console.

The console is **just another API client.** It reads the same `/agent`,
`/bound` and `/explain` routes an agent or a curl command would, and it has no
privileged path to the store, the chain or the decision engine. Revoking a
grant from the BOUND tab posts to the same endpoint anyone else would post to,
and gets checked the same way.

That is deliberate. A console with a back door is a console that can lie about
what the audit chain says. This one cannot: everything on screen came through
the public API, so anything it shows you, you can reproduce with `curl`.

There is no template engine here for the same reason there is no model in the
decision path: nothing needed one. The pages are static; the data is live.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

console_router = APIRouter(tags=["console"])

STATIC = Path(__file__).with_name("static")


def _page(name: str) -> HTMLResponse:
    return HTMLResponse((STATIC / name).read_text(encoding="utf-8"))


@console_router.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing() -> HTMLResponse:
    """What Ambit is, for a human who just found the repo."""
    return _page("landing.html")


@console_router.get("/console", response_class=HTMLResponse, include_in_schema=False)
def console() -> HTMLResponse:
    """The merchant console. Every number on it is fetched from the API."""
    return _page("console.html")
