import argparse
import fcntl
import hashlib
import json
import logging
import os
import smtplib
import sqlite3
import ssl
import time
from contextlib import closing, contextmanager
from datetime import datetime, time as wall_time, timedelta
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
from zoneinfo import ZoneInfo

from .portal import LoginRequired, Source, collect, goto, is_login, safe_error, tesla_url, validate_pdf

LOG = logging.getLogger("tesla_invoice")
CATEGORIES = {"supercharger": "Supercharger", "connectivity": "Premium Connectivity"}
WEEKDAYS = {day: index for index, day in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))}


@contextmanager
def locked_data():
    root = Path(os.getenv("DATA_DIR", "/data"))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield root


def database(root):
    db = sqlite3.connect(root / "invoices.sqlite3")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS invoices (
            digest TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            pdf BLOB NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'sending', 'sent', 'uncertain', 'baseline')),
            updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS portal_ids (
            category TEXT NOT NULL,
            invoice_id TEXT NOT NULL,
            PRIMARY KEY (category, invoice_id)
        );
        CREATE TABLE IF NOT EXISTS schedule (
            day TEXT PRIMARY KEY
        );
    """)
    with db:
        db.execute("UPDATE invoices SET status='uncertain' WHERE status='sending'")
    return db


def store_pdf(db, category, data, invoice_id=None):
    digest = hashlib.sha256(data).hexdigest()
    with db:
        result = db.execute(
            "INSERT OR IGNORE INTO invoices(digest, category, pdf) VALUES (?, ?, ?)",
            (digest, category, data),
        )
        if invoice_id:
            db.execute("INSERT OR IGNORE INTO portal_ids VALUES (?, ?)", (category, invoice_id))
    if result.rowcount:
        LOG.info("%s: PDF saved (%s)", category, digest[:12])


def update_status(db, digest, status):
    with db:
        db.execute(
            "UPDATE invoices SET status=?, updated=CURRENT_TIMESTAMP WHERE digest=?",
            (status, digest),
        )


def retry(action, label, attempts=3):
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except (LoginRequired, ValueError):
            raise
        except Exception as exc:
            if attempt == attempts or (isinstance(exc, smtplib.SMTPResponseException) and exc.smtp_code >= 500):
                raise
            delay = 5 * 2 ** (attempt - 1)
            LOG.warning("%s: Attempt %d/%d failed (%s). Retrying in %ds",
                        label, attempt, attempts, safe_error(exc), delay)
            time.sleep(delay)


def save_state(root, state):
    target = root / "session.json"
    temporary = root / "session.tmp"
    with temporary.open("w") as file:
        json.dump(state, file)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(target)


def import_pdfs(db, category, folder, limit):
    success = True
    for path in sorted(folder.glob("*.pdf")):
        try:
            with path.open("rb") as file:
                data = file.read(limit + 1)
            validate_pdf(data, limit)
            store_pdf(db, category, data)
        except Exception as exc:
            LOG.error("%s: PDF import failed (%s)", category, safe_error(exc))
            success = False
    return success


def check_portal(db, root, sources, limit):
    from playwright.sync_api import sync_playwright

    login_url = tesla_url(os.getenv("TESLA_LOGIN_URL", "https://www.tesla.com/de_DE/teslaaccount"))
    success = True
    with sync_playwright() as playwright, closing(playwright.chromium.launch()) as browser:
        for source in sources:
            def known(invoice_id):
                return db.execute(
                    "SELECT 1 FROM portal_ids WHERE category=? AND invoice_id=?",
                    (source.name, invoice_id),
                ).fetchone() is not None

            def attempt():
                state_path = root / "session.json"
                state = str(state_path) if state_path.is_file() else None
                with closing(browser.new_context(storage_state=state, accept_downloads=True)) as context:
                    context.set_default_timeout(15000)
                    context.set_default_navigation_timeout(30000)
                    collect(
                        context.new_page(), source, login_url, limit,
                        lambda data, invoice_id: store_pdf(db, source.name, data, invoice_id),
                        known,
                        lambda: save_state(root, context.storage_state(indexed_db=True)),
                    )

            try:
                retry(attempt, source.name)
                LOG.info("%s: Portal check complete", source.name)
            except Exception as exc:
                LOG.error("%s: Portal check failed (%s)", source.name, safe_error(exc))
                success = False
    return success


def download_invoices(db, root):
    limit = int(os.getenv("MAX_PDF_BYTES", "10000000"))
    if limit < 1:
        raise ValueError("MAX_PDF_BYTES must be positive")
    sources = []
    success = True
    for category in CATEGORIES:
        folder = Path(os.getenv("INBOX_DIR", "/inbox")) / category
        if not import_pdfs(db, category, folder, limit):
            success = False
        try:
            mode = os.getenv(f"TESLA_{category.upper()}_SOURCE", "portal")
            if mode == "portal":
                sources.append(Source.from_env(category))
            elif mode != "inbox":
                raise ValueError("Source must be portal or inbox")
        except (ValueError, KeyError) as exc:
            LOG.error("%s: %s", category, str(exc))
            success = False
    if sources:
        return check_portal(db, root, sources, limit) and success
    return success


class Mailer:
    def __init__(self):
        self.host = os.environ["SMTP_HOST"]
        self.port = int(os.getenv("SMTP_PORT", "587"))
        self.security = os.getenv("SMTP_SECURITY", "starttls")
        self.user = os.getenv("SMTP_USER")
        self.password = os.getenv("SMTP_PASSWORD")
        self.sender = os.environ["MAIL_FROM"]
        self.recipient = os.environ["MAIL_TO"]
        if not all((self.host, self.sender, self.recipient)):
            raise ValueError("SMTP_HOST, MAIL_FROM, and MAIL_TO are required")
        if bool(self.user) != bool(self.password):
            raise ValueError("Set both SMTP_USER and SMTP_PASSWORD")
        for address in (self.sender, self.recipient):
            Address(addr_spec=address)
            address.encode("ascii")
        if self.security not in ("starttls", "ssl"):
            raise ValueError("SMTP_SECURITY must be starttls or ssl")

    def connect(self):
        context = ssl.create_default_context()
        if self.security == "ssl":
            client = smtplib.SMTP_SSL(timeout=30, context=context)
        else:
            client = smtplib.SMTP(timeout=30)
        try:
            code, response = client.connect(self.host, self.port)
            if code != 220:
                raise smtplib.SMTPConnectError(code, response)
            if self.security == "starttls":
                client.starttls(context=context)
            if self.user:
                client.login(self.user, self.password)
            return client
        except Exception:
            client.close()
            raise

    def message(self, digest, category, pdf):
        name = CATEGORIES[category]
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = self.recipient
        message["Subject"] = f"Tesla invoice – {name}"
        message["Message-ID"] = f"<{digest}@tesla-invoice.local>"
        message.set_content(f"Your Tesla invoice for {name} is attached.")
        message.add_attachment(pdf, maintype="application", subtype="pdf",
                               filename=f"tesla-{category}-{digest[:12]}.pdf")
        return message


def send_pending(db):
    pending = db.execute(
        "SELECT digest, category FROM invoices WHERE status='pending' ORDER BY rowid"
    ).fetchall()
    if not pending:
        return True

    mailer = Mailer()
    success = True
    for digest, category in pending:
        pdf, = db.execute("SELECT pdf FROM invoices WHERE digest=?", (digest,)).fetchone()
        message = mailer.message(digest, category, pdf)

        def deliver():
            try:
                client = mailer.connect()
            except smtplib.SMTPAuthenticationError as exc:
                raise ValueError("SMTP authentication failed") from exc
            with closing(client):
                client.ehlo_or_helo_if_needed()
                for command, address, accepted in (
                    (client.mail, mailer.sender, (250,)),
                    (client.rcpt, mailer.recipient, (250, 251)),
                ):
                    code, response = command(address)
                    if code not in accepted:
                        raise smtplib.SMTPResponseException(code, response)
                update_status(db, digest, "sending")
                try:
                    code, response = client.data(message.as_bytes(policy=SMTP))
                    if code != 250:
                        raise smtplib.SMTPDataError(code, response)
                except smtplib.SMTPResponseException:
                    # A negative SMTP reply explicitly rejects the message.
                    update_status(db, digest, "pending")
                    raise
                except Exception:
                    update_status(db, digest, "uncertain")
                    LOG.error("SMTP acceptance is uncertain (%s). Check delivery before retrying", digest)
                    return False
                update_status(db, digest, "sent")
                return True

        try:
            if retry(deliver, "SMTP"):
                LOG.info("%s: Email accepted (%s)", category, digest[:12])
            else:
                success = False
        except Exception as exc:
            code = exc.smtp_code if isinstance(exc, smtplib.SMTPResponseException) else None
            LOG.error("SMTP delivery failed for %s (%s%s)",
                      digest[:12], safe_error(exc), f", SMTP {code}" if code else "")
            success = False
            if not code or code < 500:
                break
    return success


def run_once(db, root, download_only=False):
    try:
        downloaded = download_invoices(db, root)
    except Exception as exc:
        LOG.error("Could not start the Tesla check (%s)", safe_error(exc))
        downloaded = False
    if download_only:
        return downloaded
    try:
        sent = send_pending(db)
    except Exception as exc:
        LOG.error("Could not start email delivery (%s)", safe_error(exc))
        sent = False
    uncertain, = db.execute("SELECT count(*) FROM invoices WHERE status='uncertain'").fetchone()
    if uncertain:
        LOG.warning("%d deliveries have uncertain status. Use the status command to inspect them", uncertain)
    return downloaded and sent


def due_day(now, days, scheduled_time):
    for offset in range(8):
        day = now.date() - timedelta(days=offset)
        if day.weekday() in days and (offset or now.time() >= scheduled_time):
            return day.isoformat()
    raise ValueError("No valid weekday found")


def schedule():
    days = {WEEKDAYS[day.strip().lower()] for day in os.getenv("RUN_DAYS", "mon,wed,fri").split(",")}
    scheduled_time = wall_time.fromisoformat(os.getenv("RUN_TIME", "08:00"))
    if scheduled_time.tzinfo is not None:
        raise ValueError("RUN_TIME must be a local time without a UTC offset")
    zone = ZoneInfo(os.getenv("RUN_TIMEZONE", "Europe/Berlin"))
    LOG.info("Schedule active (%s, %s, %s)", os.getenv("RUN_DAYS", "mon,wed,fri"), scheduled_time, zone)
    while True:
        delay = 30
        day = due_day(datetime.now(zone), days, scheduled_time)
        try:
            with locked_data() as root, closing(database(root)) as db:
                completed = db.execute("SELECT 1 FROM schedule WHERE day=?", (day,)).fetchone()
                if not completed:
                    LOG.info("Checking invoices for scheduled date %s", day)
                    if run_once(db, root):
                        with db:
                            db.execute("INSERT INTO schedule(day) VALUES (?)", (day,))
                    else:
                        delay = 1800
                        LOG.warning("Check incomplete. Retrying in %d minutes", delay // 60)
        except BlockingIOError:
            LOG.info("Another process holds the data directory lock")
        time.sleep(delay)


def interactive_session(root):
    from playwright.sync_api import sync_playwright

    url = os.getenv("TESLA_LOGIN_URL", "https://www.tesla.com/de_DE/teslaaccount")
    with sync_playwright() as playwright, closing(playwright.chromium.launch(headless=False)) as browser:
        context = browser.new_context()
        page = context.new_page()
        goto(page, url)
        input("Sign in to Tesla in the browser, then press Enter here: ")
        tesla_url(page.url)
        if is_login(page) or not context.cookies():
            raise LoginRequired("The browser did not save a Tesla session")
        save_state(root, context.storage_state(indexed_db=True))


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--download-only", action="store_true")
    mode.add_argument("--baseline", action="store_true")
    for name in ("schedule", "status", "login", "import-session"):
        commands.add_parser(name)
    resolve = commands.add_parser("resolve")
    resolve.add_argument("digest")
    resolve.add_argument("status", choices=("sent", "pending"))
    args = parser.parse_args()
    try:
        if args.command == "schedule":
            schedule()
            return 0
        with locked_data() as root:
            if args.command == "login":
                interactive_session(root)
                return 0
            if args.command == "import-session":
                import sys

                state = json.load(sys.stdin)
                if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
                    raise ValueError("Expected a Playwright session file")
                save_state(root, state)
                return 0
            with closing(database(root)) as db:
                if args.command == "run":
                    if args.baseline:
                        if db.execute("SELECT 1 FROM invoices WHERE status IN ('sent', 'uncertain')").fetchone():
                            LOG.error("Run baseline before sending the first invoice")
                            return 1
                        if not run_once(db, root, download_only=True):
                            return 1
                        with db:
                            result = db.execute("UPDATE invoices SET status='baseline' WHERE status='pending'")
                        LOG.info("Saved %d existing PDFs as the baseline", result.rowcount)
                        return 0
                    return 0 if run_once(db, root, args.download_only) else 1
                if args.command == "status":
                    for row in db.execute("SELECT status, count(*) FROM invoices GROUP BY status"):
                        print(*row)
                    for row in db.execute("SELECT digest, category, updated FROM invoices WHERE status='uncertain'"):
                        print(*row)
                if args.command == "resolve":
                    with db:
                        changed = db.execute(
                            "UPDATE invoices SET status=?, updated=CURRENT_TIMESTAMP "
                            "WHERE digest=? AND status='uncertain'",
                            (args.status, args.digest),
                        )
                    if not changed.rowcount:
                        LOG.error("No invoice with that hash has uncertain delivery status")
                        return 1
        return 0
    except BlockingIOError:
        LOG.error("Another invoice check holds the data directory lock")
        return 1
    except (ValueError, KeyError) as exc:
        LOG.error("Invalid configuration or input (%s)", type(exc).__name__)
        return 1
    except Exception as exc:
        LOG.error("Command failed (%s)", safe_error(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
