import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tesla_invoice.fleet import CHARGING_URL, TOKEN_URL, Fleet
from tesla_invoice.portal import PortalError

PDF = b"%PDF-1.4\n%%EOF\n"


def history(*ids):
    return json.dumps({"data": [{"invoices": [{"contentId": value}]} for value in ids]}).encode()


class FleetTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict("os.environ", {
            "TESLA_FLEET_CLIENT_ID": "test-id",
            "TESLA_FLEET_CLIENT_SECRET": "test-secret",
        }, clear=True))
        self.fleet = Fleet(root)
        self.fleet.file.write_text(json.dumps({"client_id": "test-id", "refresh_token": "old"}))

    def test_rotates_token_and_downloads_new_pdf_from_next_page(self):
        responses = [
            b'{"access_token":"access","refresh_token":"rotated"}',
            history("known"), history("known"), history("new"), PDF, history(),
        ]
        store = Mock()
        with patch("tesla_invoice.fleet.request_bytes", side_effect=responses) as network:
            self.assertTrue(self.fleet.collect(1024, lambda key: key == "fleet:known", store, lambda f, _: f()))
        self.assertEqual(json.loads(self.fleet.file.read_text())["refresh_token"], "rotated")
        self.assertEqual(network.call_args_list[0].args[0], TOKEN_URL)
        for call in network.call_args_list[1:]:
            self.assertTrue(call.args[0].startswith(CHARGING_URL))
            self.assertEqual(call.kwargs["token"], "access")
        self.assertEqual(network.call_args_list[-2].args[0], CHARGING_URL + "/invoice/new")
        store.assert_called_once_with(PDF, "fleet:new")

    def test_invalid_pdf_does_not_block_other_invoices(self):
        self.fleet.access_token = "access"
        store = Mock()
        with patch("tesla_invoice.fleet.request_bytes", side_effect=[
            history("invalid", "valid"), b"<html>Error</html>", PDF, history(),
        ]):
            self.assertFalse(self.fleet.collect(1024, lambda _: False, store, lambda f, _: f()))
        store.assert_called_once_with(PDF, "fleet:valid")

    def test_repeated_pagination_is_reported(self):
        self.fleet.access_token = "access"
        with patch("tesla_invoice.fleet.request_bytes", return_value=history("same")):
            with self.assertRaisesRegex(PortalError, "repeated"):
                list(self.fleet.pages())
