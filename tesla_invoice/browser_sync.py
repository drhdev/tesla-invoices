import base64
import binascii
import os
import sys
from contextlib import closing

from .portal import LoginRequired, PortalError, Source, collect_visible, goto, is_login, login_url_from_env, tesla_url, validate_pdf

INVOICE_ROWS = (
    ':is(tr, [role="row"], li):text-matches("premium[ -](connectivity|konnektivit.t)", "i") '
    'a[href*=".pdf" i]'
)
END = b"END\n"


def capture_connectivity(root, limit, save_session):
    from playwright.sync_api import sync_playwright

    state = root / "session.json"
    login_url = login_url_from_env()
    invoice_url = os.getenv("TESLA_CONNECTIVITY_URL", "")
    with sync_playwright() as playwright, closing(playwright.chromium.launch(headless=False)) as browser:
        with closing(browser.new_context(
            storage_state=str(state) if state.is_file() else None, accept_downloads=True,
        )) as context:
            context.set_default_timeout(15000)
            page = context.new_page()
            goto(page, invoice_url or login_url)
            print("Sign in and open the Premium Connectivity invoice list, then press Enter here.",
                  file=sys.stderr, flush=True)
            if not sys.stdin.readline():
                raise PortalError("Browser sync requires interactive confirmation")
            tesla_url(page.url)
            if is_login(page):
                raise LoginRequired("Finish signing in before syncing Premium Connectivity")

            source = Source(
                "connectivity", page.url,
                os.getenv("TESLA_CONNECTIVITY_LIST") or "body",
                os.getenv("TESLA_CONNECTIVITY_INVOICE") or INVOICE_ROWS,
                os.getenv("TESLA_CONNECTIVITY_EMPTY", ""),
                os.getenv("TESLA_CONNECTIVITY_NEXT", ""),
                os.getenv("TESLA_CONNECTIVITY_MODE") or "href",
                "",
            )
            if source.mode not in ("href", "download"):
                raise PortalError("TESLA_CONNECTIVITY_MODE must be download or href")

            output = sys.stdout.buffer

            def emit(pdf, invoice_id):
                output.write(base64.b64encode(pdf) + b"\n")
                output.flush()

            collect_visible(
                page, source, limit, emit, lambda _: False,
                lambda: save_session(root, context.storage_state(indexed_db=True)),
            )
            output.write(END)
            output.flush()


def import_connectivity(stream, limit, store):
    line_limit = 4 * ((limit + 2) // 3) + 2
    while line := stream.readline(line_limit):
        if line == END:
            if stream.read(1):
                raise PortalError("Unexpected content after the completed browser sync")
            return
        if len(line) >= line_limit or not line.endswith(b"\n"):
            raise PortalError("Browser sync invoice record is incomplete or too large")
        try:
            pdf = base64.b64decode(line[:-1], validate=True)
        except (ValueError, binascii.Error):
            raise PortalError("Browser sync invoice record is invalid") from None
        validate_pdf(pdf, limit)
        store(pdf)
    raise PortalError("Browser sync did not complete")
