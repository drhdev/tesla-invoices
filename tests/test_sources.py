import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tesla_invoice.__main__ import download_invoices


class SourceRoutingTest(unittest.TestCase):
    def setUp(self):
        env = {}
        for category in ("SUPERCHARGER", "CONNECTIVITY"):
            prefix = f"TESLA_{category}_"
            env.update({
                prefix + "URL": "https://www.tesla.com/de_DE/teslaaccount",
                prefix + "LIST": "#list",
                prefix + "INVOICE": "#list a",
            })
        self.enterContext(patch.dict(os.environ, env, clear=True))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.portal = self.enterContext(patch("tesla_invoice.__main__.check_portal"))
        self.fleet = self.enterContext(patch("tesla_invoice.__main__.check_fleet"))
        self.calls = Mock()
        self.calls.attach_mock(self.portal, "web")
        self.calls.attach_mock(self.fleet, "fleet")

    def run_check(self):
        return download_invoices(Mock(), self.root, "test-run")

    def configure_fleet(self, authorize=True):
        os.environ["TESLA_FLEET_CLIENT_ID"] = "test-client"
        os.environ["TESLA_FLEET_CLIENT_SECRET"] = "test-secret"
        if authorize:
            (self.root / "fleet-token.json").write_text("{}")

    def test_portal_success_skips_fleet(self):
        self.configure_fleet()
        self.portal.return_value = {"supercharger", "connectivity"}
        self.assertTrue(self.run_check())
        self.fleet.assert_not_called()
        sources = self.portal.call_args.args[2]
        self.assertEqual([source.name for source in sources], ["supercharger", "connectivity"])

    def test_supercharger_failure_falls_back_after_web_checks(self):
        self.configure_fleet()
        self.portal.return_value = {"connectivity"}
        self.fleet.return_value = True
        self.assertTrue(self.run_check())
        self.assertEqual([call[0] for call in self.calls.mock_calls], ["web", "fleet"])

    def test_connectivity_failure_does_not_trigger_fleet(self):
        self.configure_fleet()
        self.portal.return_value = {"supercharger"}
        self.assertFalse(self.run_check())
        self.fleet.assert_not_called()

    def test_browser_failure_still_tries_supercharger_fallback(self):
        self.configure_fleet()
        self.portal.side_effect = RuntimeError("browser failed")
        self.fleet.return_value = True
        self.assertFalse(self.run_check())
        self.fleet.assert_called_once()

    def test_unconfigured_supercharger_web_source_uses_fleet(self):
        self.configure_fleet()
        del os.environ["TESLA_SUPERCHARGER_URL"]
        self.portal.return_value = {"connectivity"}
        self.fleet.return_value = True
        self.assertTrue(self.run_check())
        self.assertEqual([source.name for source in self.portal.call_args.args[2]], ["connectivity"])
        self.fleet.assert_called_once()

    def test_web_failure_skips_unconfigured_fleet(self):
        self.portal.return_value = {"connectivity"}
        self.assertFalse(self.run_check())
        self.fleet.assert_not_called()

    def test_web_failure_skips_fleet_without_saved_authorization(self):
        self.configure_fleet(authorize=False)
        self.portal.return_value = {"connectivity"}
        self.assertFalse(self.run_check())
        self.fleet.assert_not_called()

    def test_desktop_browser_mode_schedules_supercharger_only(self):
        os.environ["TESLA_CONNECTIVITY_SOURCE"] = "browser"
        self.portal.return_value = {"supercharger"}
        self.assertTrue(self.run_check())
        sources = self.portal.call_args.args[2]
        self.assertEqual([source.name for source in sources], ["supercharger"])
        self.fleet.assert_not_called()
