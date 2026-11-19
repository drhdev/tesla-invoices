import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

LOG = logging.getLogger(__name__)


class PortalError(Exception):
    pass


class LoginRequired(PortalError):
    pass


def tesla_url(value):
    parts = urlsplit(value)
    host = parts.hostname or ""
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
        or not (host == "tesla.com" or host.endswith(".tesla.com"))
    ):
        raise PortalError("Expected an HTTPS URL on tesla.com")
    return value


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    listing: str
    invoice: str
    empty: str
    next_page: str
    mode: str
    id_attribute: str

    @classmethod
    def from_env(cls, name):
        prefix = f"TESLA_{name.upper()}_"
        fields = [os.getenv(prefix + key, "") for key in ("URL", "LIST", "INVOICE", "EMPTY", "NEXT")]
        if not all(fields[:3]):
            raise ValueError(f"{prefix}URL, {prefix}LIST, and {prefix}INVOICE are required")
        tesla_url(fields[0])
        mode = os.getenv(prefix + "MODE", "download")
        if mode not in ("download", "href"):
            raise ValueError(f"{prefix}MODE must be download or href")
        return cls(name, *fields, mode, os.getenv(prefix + "ID_ATTRIBUTE", ""))


def goto(page, url):
    response = page.goto(tesla_url(url), wait_until="domcontentloaded")
    if response and response.status >= 400:
        if response.status in (401, 403):
            raise LoginRequired(f"Tesla denied access (HTTP {response.status})")
        raise PortalError(f"Tesla page returned HTTP {response.status}")
    tesla_url(page.url)


def is_login(page):
    host = urlsplit(page.url).hostname
    return host == "auth.tesla.com" or page.locator("input[type=password]").first.is_visible()


def sign_in(page, login_url):
    if urlsplit(page.url).hostname != "auth.tesla.com":
        goto(page, login_url)
    if urlsplit(page.url).hostname != "auth.tesla.com":
        raise LoginRequired("Could not verify the Tesla sign-in page")
    email, password = os.getenv("TESLA_EMAIL"), os.getenv("TESLA_PASSWORD")
    if not email or not password:
        raise LoginRequired("Tesla session expired. Export the session again or configure credentials")
    user = page.locator("input[type=email], input[name=identity], input[autocomplete=username]").first
    secret = page.locator("input[type=password]").first
    submit = page.locator("button[type=submit], input[type=submit]").first
    user.fill(email)
    if not secret.is_visible():
        submit.click()
    secret.wait_for(state="visible")
    if urlsplit(page.url).hostname != "auth.tesla.com":
        raise LoginRequired("Unexpected change of sign-in domain")
    secret.fill(password)
    submit.click()
    try:
        page.wait_for_url(
            lambda url: urlsplit(url).hostname not in ("auth.tesla.com", None),
            timeout=20000,
        )
    except Exception as exc:
        if type(exc).__name__ != "TimeoutError":
            raise
        raise LoginRequired("Tesla sign-in did not finish. Check credentials, MFA, or CAPTCHA") from None
    tesla_url(page.url)


def get_pdf(page, item, mode, limit):
    if mode == "download":
        with page.expect_download() as event:
            item.click()
        download = event.value
        try:
            with download.path().open("rb") as file:
                return file.read(limit + 1)
        finally:
            download.delete()

    href = item.get_attribute("href")
    if not href:
        raise PortalError("Invoice link has no href attribute")
    url = urljoin(page.url, href)
    for _ in range(5):
        response = page.request.get(tesla_url(url), max_redirects=0, fail_on_status_code=False)
        try:
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise PortalError("PDF redirect has no destination")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                if response.status in (401, 403):
                    raise LoginRequired(f"PDF access denied (HTTP {response.status})")
                raise PortalError(f"PDF download returned HTTP {response.status}")
            length = response.headers.get("content-length", "")
            if length.isdecimal() and int(length) > limit:
                raise PortalError("PDF exceeds MAX_PDF_BYTES")
            return response.body()
        finally:
            response.dispose()
    raise PortalError("Too many PDF download redirects")


def validate_pdf(data, limit):
    if len(data) > limit or not data.startswith(b"%PDF-") or not re.search(rb"%%EOF\s*$", data[-2048:]):
        raise PortalError("Download is an incomplete PDF or exceeds MAX_PDF_BYTES")


def collect(page, source, login_url, limit, store, known, save_session):
    goto(page, source.url)
    if is_login(page):
        sign_in(page, login_url)
        goto(page, source.url)
        if is_login(page):
            raise LoginRequired("Tesla invoicesbereich verlangt weiterhin eine Anmeldung")

    listing = page.locator(source.listing).first
    invoices = page.locator(source.invoice)
    empty = page.locator(source.empty).first if source.empty else None
    ready = invoices.first.or_(empty).first if empty is not None else invoices.first
    listing.wait_for(state="visible")
    ready.wait_for(state="visible")
    save_session()
    failures = 0

    for _ in range(100):
        count = invoices.count()
        if not count and (empty is None or not empty.is_visible()):
            raise PortalError("The invoice list shows neither invoices nor the configured empty state")
        for index in range(count):
            try:
                item = invoices.nth(index)
                invoice_id = item.get_attribute(source.id_attribute) if source.id_attribute else None
                if invoice_id and known(invoice_id):
                    continue
                data = get_pdf(page, item, source.mode, limit)
                validate_pdf(data, limit)
                store(data, invoice_id)
            except LoginRequired:
                raise
            except Exception as exc:
                failures += 1
                LOG.warning("%s: PDF %d failed (%s)", source.name, index + 1, safe_error(exc))

        next_button = page.locator(source.next_page).first if source.next_page else None
        if (
            next_button is None
            or not next_button.is_visible()
            or not next_button.is_enabled()
            or next_button.get_attribute("aria-disabled") == "true"
        ):
            if failures:
                raise PortalError(f"{failures} PDF downloads failed")
            return
        before = listing.inner_text()
        previous_url = page.url
        next_button.click()
        page.wait_for_function(
            """({selector, before, url}) => {
                const list = document.querySelector(selector);
                return list && (location.href !== url || list.innerText !== before);
            }""",
            arg={"selector": source.listing, "before": before, "url": previous_url},
        )
        ready.wait_for(state="visible")
        tesla_url(page.url)
    raise PortalError("Stopped after 100 invoice pages")


def safe_error(exc):
    # Playwright error messages can include signed URLs and user identifiers.
    if isinstance(exc, PortalError):
        return str(exc)
    return type(exc).__name__
