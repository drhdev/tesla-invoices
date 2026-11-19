import base64
import io
import unittest
from unittest.mock import Mock

from tesla_invoice.browser_sync import import_connectivity
from tesla_invoice.portal import PortalError


class BrowserImportTest(unittest.TestCase):
    def test_imports_valid_pdf_from_completed_sync(self):
        pdf = b"%PDF-1.4\n%%EOF\n"
        store = Mock()
        import_connectivity(io.BytesIO(base64.b64encode(pdf) + b"\nEND\n"), len(pdf), store)
        store.assert_called_once_with(pdf)

    def test_reports_interrupted_sync(self):
        with self.assertRaisesRegex(PortalError, "did not complete"):
            import_connectivity(io.BytesIO(), 100, Mock())

    def test_rejects_non_pdf_payload(self):
        store = Mock()
        with self.assertRaises(PortalError):
            import_connectivity(io.BytesIO(base64.b64encode(b"html") + b"\nEND\n"), 100, store)
        store.assert_not_called()
