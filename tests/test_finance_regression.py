import math
import sys
import types
import unittest

# Stub optional dependencies required when importing finance
sys.modules.setdefault('dotenv', types.SimpleNamespace(load_dotenv=lambda *args, **kwargs: None))

fake_client = types.SimpleNamespace(open_by_key=lambda *args, **kwargs: None)
fake_gspread = types.ModuleType('gspread')
fake_gspread.auth = types.SimpleNamespace(service_account=lambda *args, **kwargs: fake_client)
sys.modules.setdefault('gspread', fake_gspread)
sys.modules.setdefault('gspread.auth', fake_gspread.auth)

sys.modules.setdefault('discord', types.SimpleNamespace())

from tomcat.handlers import finance


class FinanceRegressionTests(unittest.TestCase):
    def _ts(self):
        return "2025-09-15T12:00:00Z"

    def test_venmo_receipt_amount_parsing(self):
        email = {
            "id": "email-venmo-petsmart",
            "subject": "Receipt from NNT PETSMART # 1550002564 - $32.46",
            "content": "Total: $32.46\nPurchase Amount: $32.46",
            "message_id": "msg-venmo-petsmart",
            "ts_received": self._ts(),
        }
        event, status = finance._classify_venmo(email)
        self.assertEqual(status, "expense")
        self.assertTrue(math.isclose(event.amount, 32.46, rel_tol=1e-6))
        self.assertIsNone(event.txn_id)

    def test_dominos_receipt_amount_parsing(self):
        email = {
            "id": "email-venmo-dominos",
            "subject": "Receipt from DOMINO'S 6901 - $60.57",
            "content": "DOMINO'S 6901 purchase\nTotal: $60.57",
            "message_id": "msg-venmo-dominos",
            "ts_received": self._ts(),
        }
        event, status = finance._classify_venmo(email)
        self.assertEqual(status, "expense")
        self.assertTrue(math.isclose(event.amount, 60.57, rel_tol=1e-6))

    def test_dues_message_id_short_circuits(self):
        finance._DUES_MESSAGE_IDS.clear()
        finance._DUES_MESSAGE_IDS.add("dues-msg")
        self.assertTrue(finance._is_dues_email(15.0, "", "dues-msg"))

    def test_paypal_transaction_id_extracted(self):
        email = {
            "id": "email-paypal-merchant",
            "subject": "Example Merchant: $36.77 USD",
            "content": "Thanks!\nTransaction ID: TXN-ABC123456789",
            "message_id": "msg-paypal",
            "ts_received": self._ts(),
        }
        event, status = finance._classify_paypal(email)
        self.assertEqual(status, "income")
        self.assertEqual(event.txn_id, "TXN-ABC123456789")


if __name__ == "__main__":
    unittest.main()
