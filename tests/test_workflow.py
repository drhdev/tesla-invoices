import hashlib
import os
import smtplib
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, time, timezone
from email import policy
from email.parser import BytesParser
from zoneinfo import ZoneInfo
from pathlib import Path
from unittest.mock import MagicMock, patch

from tesla_invoice.__main__ import archive_pdfs, database, due_day, run_once, send_pending, store_pdf
from tesla_invoice.portal import LoginRequired, PortalError, Source, collect, login_url_from_env, sign_in, tesla_url

PDF = b"%PDF-1.4\n%%EOF\n"
ENV = {
    "SMTP_HOST": "smtp.example.com",
    "MAIL_FROM": "sender@example.com",
    "MAIL_TO": "recipient@example.com",
}


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temp)
        self.db = self.enterContext(closing(database(self.root)))
        self.archive = MagicMock()
        self.archive.bucket = "shared-bucket"
        self.archive.save.side_effect = lambda category, digest, pdf: f"tesla-invoices/{category}/{digest}.pdf"
        self.enterContext(patch("tesla_invoice.__main__.Archive", return_value=self.archive))
        self.enterContext(patch.dict(os.environ, ENV, clear=True))
        self.upstream = []
        self.enterContext(patch("tesla_invoice.__main__.download_invoices", side_effect=self.download))

    def download(self, db, root, run_id):
        for category, pdf in self.upstream:
            store_pdf(db, category, pdf, run_id=run_id)
        return True

    def client(self):
        client = MagicMock()
        client.mail.return_value = (250, b"OK")
        client.rcpt.return_value = (250, b"OK")
        client.data.return_value = (250, b"OK")
        self.enterContext(patch("tesla_invoice.__main__.Mailer.connect", return_value=client))
        return client

    def statuses(self):
        return self.db.execute("SELECT status FROM invoices ORDER BY rowid").fetchall()

    def test_new_pdfs_only_are_sent(self):
        client = self.client()
        self.upstream.append(("connectivity", PDF))
        self.assertTrue(run_once(self.db, self.root, download_only=True))
        client.data.assert_not_called()
        self.assertTrue(run_once(self.db, self.root))
        self.upstream.extend([("connectivity", PDF), ("supercharger", PDF + b" ")])
        self.assertTrue(run_once(self.db, self.root))
        self.assertEqual(client.data.call_count, 2)
        message = BytesParser(policy=policy.default).parsebytes(client.data.call_args.args[0])
        attachment, = message.iter_attachments()
        retrieved_at, run_id = self.db.execute(
            "SELECT retrieved_at, run_id FROM retrievals WHERE digest=?", (hashlib.sha256(PDF + b" ").hexdigest(),)
        ).fetchone()
        self.assertEqual(attachment.get_payload(decode=True), PDF + b" ")
        self.assertIn(retrieved_at[:10], message["Subject"])
        self.assertIn(run_id, message["Subject"])
        self.assertIn("Hi Heinrich,", message.get_body().get_content())
        self.assertIn(run_id, message.get_body().get_content())
        self.assertIn(retrieved_at, message.get_body().get_content())
        self.assertEqual(self.archive.save.call_count, 2)
        self.assertEqual(self.statuses(), [("sent",), ("sent",)])

    def test_explicit_smtp_rejection_is_retried(self):
        client = self.client()
        client.data.side_effect = [(451, b"try again"), (250, b"OK")]
        store_pdf(self.db, "supercharger", PDF)
        self.assertTrue(archive_pdfs(self.db, self.archive))
        with patch("tesla_invoice.__main__.time.sleep") as sleep:
            self.assertTrue(send_pending(self.db, self.archive.bucket))
        sleep.assert_called_once_with(5)
        self.assertEqual(self.statuses(), [("sent",)])

    def test_disconnect_after_data_is_not_retried(self):
        client = self.client()
        client.data.side_effect = smtplib.SMTPServerDisconnected()
        store_pdf(self.db, "supercharger", PDF)
        self.assertTrue(archive_pdfs(self.db, self.archive))
        self.assertFalse(send_pending(self.db, self.archive.bucket))
        self.assertTrue(send_pending(self.db, self.archive.bucket))
        client.data.assert_called_once()
        self.assertEqual(self.statuses(), [("uncertain",)])

    def test_interrupted_send_is_recovered_as_uncertain(self):
        store_pdf(self.db, "supercharger", PDF, "stable-id")
        with self.db:
            self.db.execute("UPDATE invoices SET status='sending'")
        with closing(database(self.root)) as reopened:
            digest, status = reopened.execute("SELECT digest, status FROM invoices").fetchone()
            invoice_id, = reopened.execute("SELECT invoice_id FROM portal_ids").fetchone()
        self.assertEqual(digest, hashlib.sha256(PDF).hexdigest())
        self.assertEqual(status, "uncertain")
        self.assertEqual(invoice_id, "stable-id")

    def test_no_delivery_before_archive_and_retry_preserves_retrieval(self):
        client = self.client()
        store_pdf(self.db, "supercharger", PDF, run_id="original-run")
        self.archive.save.side_effect = OSError("temporary S3 failure")
        with patch("tesla_invoice.__main__.time.sleep"):
            self.assertFalse(run_once(self.db, self.root))
        client.data.assert_not_called()
        self.assertEqual(self.statuses(), [("pending",)])
        self.archive.save.side_effect = None
        self.archive.save.return_value = "tesla-invoices/supercharger/invoice.pdf"
        self.assertTrue(run_once(self.db, self.root))
        client.data.assert_called_once()
        self.assertIn(b"Run original-run", client.data.call_args.args[0])
        self.assertEqual(self.statuses(), [("sent",)])

    def test_schedule_follows_berlin_noon_in_winter_and_summer(self):
        zone = ZoneInfo("Europe/Berlin")
        days = set(range(5))
        for utc in (datetime(2026, 11, 18, 11, tzinfo=timezone.utc),
                    datetime(2026, 7, 15, 10, tzinfo=timezone.utc)):
            local = utc.astimezone(zone)
            self.assertIsNone(due_day(local.replace(hour=11), days, time(12)))
            self.assertEqual(due_day(local, days, time(12)), local.date().isoformat())
        self.assertIsNone(due_day(datetime(2026, 11, 21, 12, tzinfo=zone), days, time(12)))

    def test_portal_skips_known_id_and_stores_new_pdf(self):
        page = MagicMock()
        url = "https://www.tesla.com/de_DE/teslaaccount"
        page.url = url
        container, invoices, password = MagicMock(), MagicMock(), MagicMock()
        password.first.is_visible.return_value = False
        page.locator.side_effect = {
            "#invoices": container,
            "#invoices a": invoices,
            "input[type=password]": password,
        }.__getitem__
        invoices.count.return_value = 2
        items = [MagicMock(), MagicMock()]
        items[0].get_attribute.return_value = "known"
        items[1].get_attribute.return_value = "new"
        invoices.nth.side_effect = items.__getitem__
        source = Source("connectivity", url, "#invoices", "#invoices a", "", "", "href", "data-id")
        save_session, store = MagicMock(), MagicMock()
        with patch("tesla_invoice.portal.goto"), patch("tesla_invoice.portal.get_pdf", return_value=PDF) as fetch:
            collect(page, source, url, 1000, store, lambda value: value == "known", save_session)
        fetch.assert_called_once_with(page, items[1], "href", 1000)
        store.assert_called_once_with(PDF, "new")
        save_session.assert_called_once()

    def test_tesla_url_blocks_lookalike_and_cleartext_hosts(self):
        for url in ("https://tesla.com.example.org", "http://auth.tesla.com", "https://tesla.com@other.org"):
            with self.assertRaises(PortalError):
                tesla_url(url)
        self.assertEqual(tesla_url("https://auth.tesla.com"), "https://auth.tesla.com")

    def test_login_url_defaults_to_germany_and_accepts_an_override(self):
        self.assertEqual(login_url_from_env(), "https://www.tesla.com/de_de/teslaaccount")
        with patch.dict(os.environ, {"TESLA_LOGIN_URL": "https://www.tesla.com/fr_fr/teslaaccount"}):
            self.assertEqual(login_url_from_env(), "https://www.tesla.com/fr_fr/teslaaccount")

    def test_country_login_replaces_an_existing_auth_redirect(self):
        page = MagicMock(url="https://auth.tesla.com/en_us/oauth2/v1/authorize")
        url = "https://www.tesla.com/de_de/teslaaccount"
        with patch("tesla_invoice.portal.goto") as navigate:
            with self.assertRaises(LoginRequired):
                sign_in(page, url)
        navigate.assert_called_once_with(page, url)

    def test_login_url_rejects_external_hosts(self):
        with patch.dict(os.environ, {"TESLA_LOGIN_URL": "https://www.tesla.com.example.org/de_de/"}):
            with self.assertRaises(PortalError):
                login_url_from_env()
