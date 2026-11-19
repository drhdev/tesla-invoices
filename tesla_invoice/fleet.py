import json
import logging
import os
import re
import secrets
from getpass import getpass
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .portal import LoginRequired, PortalError, safe_error, validate_pdf

LOG = logging.getLogger(__name__)
FLEET_URL = "https://fleet-api.prd.eu.vn.cloud.tesla.com"
TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
AUTHORIZE_URL = "https://auth.tesla.com/oauth2/v3/authorize"
CHARGING_URL = FLEET_URL + "/api/1/dx/charging"
JSON_LIMIT = 4 * 1024 * 1024
TOKEN_FILE = "fleet-token.json"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_bytes(url, limit, *, token=None, form=None, accept="application/json"):
    headers = {"Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = urlencode(form).encode("ascii")
    try:
        with build_opener(NoRedirect()).open(Request(url, data=body, headers=headers), timeout=30) as response:
            data = response.read(limit + 1)
    except HTTPError as exc:
        exc.close()
        if exc.code in (401, 403) or (form is not None and exc.code == 400):
            raise LoginRequired(f"Tesla Fleet authorization rejected (HTTP {exc.code})") from None
        raise PortalError(f"Tesla Fleet request failed (HTTP {exc.code})") from None
    if len(data) > limit:
        raise PortalError("Tesla Fleet response exceeded the size limit")
    return data


class Fleet:
    @staticmethod
    def is_configured(root):
        return (
            bool(os.getenv("TESLA_FLEET_CLIENT_ID", "").strip())
            and bool(os.getenv("TESLA_FLEET_CLIENT_SECRET", "").strip())
            and (root / TOKEN_FILE).is_file()
        )

    def __init__(self, root):
        self.client_id = os.getenv("TESLA_FLEET_CLIENT_ID", "")
        self.client_secret = os.getenv("TESLA_FLEET_CLIENT_SECRET", "")
        if not self.client_id or not self.client_secret:
            raise ValueError("Set TESLA_FLEET_CLIENT_ID and TESLA_FLEET_CLIENT_SECRET")
        self.file = root / TOKEN_FILE
        self.access_token = None

    def exchange(self, **grant):
        tokens = json.loads(request_bytes(
            TOKEN_URL, JSON_LIMIT, form={"client_id": self.client_id, **grant}
        ))
        if not isinstance(tokens, dict) or not all(
            isinstance(tokens.get(key), str) and tokens[key] for key in ("refresh_token", "access_token")
        ):
            raise LoginRequired("Tesla Fleet did not return both tokens. Check offline_access consent")
        temporary = self.file.with_suffix(".tmp")
        with open(temporary, "w", opener=private_file) as file:
            json.dump({"client_id": self.client_id, "refresh_token": tokens["refresh_token"]}, file)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(self.file)
        directory = os.open(self.file.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        self.access_token = tokens["access_token"]

    def authorize(self):
        redirect_uri = os.getenv("TESLA_FLEET_REDIRECT_URI", "")
        redirect = urlsplit(redirect_uri)
        if (redirect.scheme != "https" or not redirect.hostname or redirect.username is not None
                or redirect.query or redirect.fragment):
            raise ValueError("Set TESLA_FLEET_REDIRECT_URI to your registered HTTPS callback URL without a query")
        state = secrets.token_urlsafe(32)
        params = {
            "client_id": self.client_id, "redirect_uri": redirect_uri, "response_type": "code",
            "scope": "openid offline_access vehicle_charging_cmds", "state": state,
            "require_requested_scopes": "true",
        }
        print(f"Open this URL in your browser and authorize your Tesla account:\n{AUTHORIZE_URL}?{urlencode(params)}",
              flush=True)
        callback = urlsplit(getpass("Paste the full callback URL here (hidden): "))
        if callback._replace(query="", fragment="") != redirect or callback.fragment:
            raise LoginRequired("The callback URL does not match the registered redirect")
        query = parse_qs(callback.query)
        if query.get("state") != [state] or len(query.get("code", [])) != 1:
            raise LoginRequired("Missing authorization code or mismatched OAuth state")
        self.exchange(grant_type="authorization_code", code=query["code"][0],
                      redirect_uri=redirect_uri, client_secret=self.client_secret, audience=FLEET_URL)
        LOG.info("Tesla Fleet refresh token saved")

    def refresh(self):
        if not self.file.is_file():
            raise LoginRequired("Tesla Fleet is not authorized. Run fleet-authorize once")
        with self.file.open() as file:
            saved = json.load(file)
        if saved.get("client_id") != self.client_id:
            raise LoginRequired("Tesla Fleet client changed. Run fleet-authorize again")
        self.exchange(grant_type="refresh_token", refresh_token=saved["refresh_token"])

    def get(self, path, limit, *, accept="application/json"):
        if self.access_token is None:
            self.refresh()
        try:
            return request_bytes(CHARGING_URL + path, limit, token=self.access_token, accept=accept)
        except LoginRequired:
            self.access_token = None
            raise

    def pages(self):
        seen = set()
        for number in range(1001):
            params = {"pageSize": 50}
            if number:
                params["pageNo"] = number
            body = json.loads(self.get(f"/history?{urlencode(params)}", JSON_LIMIT))
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                raise PortalError("Unexpected Tesla charging history response")
            records = body["data"]
            if not records:
                return
            signature = json.dumps(records, sort_keys=True)
            if signature in seen:
                # Some deployments use page 1 as the default, others start at page 0.
                if number == 1:
                    continue
                raise PortalError("Tesla charging history repeated a page")
            seen.add(signature)
            yield records
        raise PortalError("Tesla charging history exceeded 1000 pages")

    def collect(self, limit, known, store, retry):
        failed = False
        for records in self.pages():
            for record in records:
                if not isinstance(record, dict):
                    raise PortalError("Unexpected Tesla charging event")
                invoices = record.get("invoices")
                if invoices is None:
                    continue
                if not isinstance(invoices, list):
                    raise PortalError("Unexpected Tesla charging invoice list")
                for invoice in invoices:
                    invoice_id = invoice.get("contentId") if isinstance(invoice, dict) else None
                    if not isinstance(invoice_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", invoice_id):
                        LOG.error("Tesla Fleet listed an invoice without a valid content ID")
                        failed = True
                        continue
                    key = f"fleet:{invoice_id}"
                    if known(key):
                        continue
                    try:
                        def fetch():
                            data = self.get(f"/invoice/{invoice_id}", limit, accept="application/pdf")
                            validate_pdf(data, limit)
                            return data

                        store(retry(fetch, "Supercharger PDF"), key)
                    except LoginRequired:
                        raise
                    except Exception as exc:
                        LOG.error("Supercharger PDF download failed (%s)", safe_error(exc))
                        failed = True
        return not failed


def private_file(path, flags):
    return os.open(path, flags, 0o600)
