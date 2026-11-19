import copy
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import trade


def usd(value):
    return {"currency": "USD", "value": value}


class TradingTests(unittest.TestCase):
    def setUp(self):
        self.now = trade.utcnow()
        self.market = {
            "slug": "example-event", "title": "Example outcome", "description": "A clear binary event.",
            "endDate": (self.now + timedelta(hours=1)).isoformat(), "active": True, "closed": False,
            "minimumTradeQty": 1, "orderPriceMinTickSize": "0.01", "feeCoefficient": "0.095",
            "marketSides": [{"description": "No", "long": False}, {"description": "Yes", "long": True}],
        }
        self.book = {"marketSlug": "example-event", "state": "MARKET_STATE_OPEN",
                     "transactTime": self.now.isoformat(),
                     "bids": [{"px": usd("0.49"), "qty": "20"}],
                     "offers": [{"px": usd("0.50"), "qty": "3"}, {"px": usd("0.51"), "qty": "20"}]}
        self.forecast = {
            "market": "example-event", "terms_sha256": trade.digest(trade.market_terms(self.market)),
            "observed_at": self.now.isoformat(), "valid_until": (self.now + timedelta(minutes=10)).isoformat(),
            "fair_yes_lower_bound": "0.63", "source": "https://example.com/independent-evidence",
            "rationale": "Synthetic forecast used only for a test.", "standard_binary_payout_verified": True,
        }
        self.limits = {"max_order_cost": "5", "max_daily_cost": "10", "min_edge_per_contract": "0.03",
                       "max_contract_price": "0.52", "max_spread": "0.03"}
        self.client = SimpleNamespace(
            markets=SimpleNamespace(retrieve_by_slug=Mock(return_value={"market": self.market}),
                                    book=Mock(return_value={"marketData": self.book})),
            account=SimpleNamespace(balances=Mock(return_value={"balances": [{"currency": "USD", "buyingPower": "20"}]})),
            portfolio=SimpleNamespace(positions=Mock(return_value={"positions": {}, "eof": True})),
            orders=SimpleNamespace(list=Mock(return_value={"orders": []}), preview=Mock(),
                                   create=Mock(return_value={"id": "test-order-id"}), retrieve=Mock()),
        )

    def plan(self):
        return trade.build_plan(self.client, self.forecast, self.limits, self.now)

    def approve(self):
        plan = self.plan()
        self.client.orders.preview.return_value = {"order": trade.payload(plan)}
        result = {"account": "test-account", "plan": plan, "exchange_preview": trade.exchange_preview(self.client, plan)}
        return result, trade.digest(result)

    def test_plan_obeys_fee_inclusive_cost_and_price_limit(self):
        plan = self.plan()
        self.assertEqual((plan["side"], plan["quantity"], plan["limit_price"]), ("Yes", 9, "0.51"))
        self.assertEqual(plan["maximum_fee_estimate"], "0.22")
        self.assertEqual(plan["maximum_cost"], "4.81")
        self.assertEqual(trade.payload(plan)["tif"], trade.FOK)
        self.assertEqual(trade.fee_upper_bound(100, Decimal("0.60"), Decimal("0.095")), Decimal("2.38"))

    def test_stale_book_changed_terms_and_no_edge(self):
        original = self.book["transactTime"]
        self.book["transactTime"] = (self.now - timedelta(seconds=31)).isoformat()
        with self.assertRaisesRegex(ValueError, "stale"):
            self.plan()
        self.book["transactTime"] = original
        self.market["description"] = "Changed resolution terms"
        with self.assertRaisesRegex(ValueError, "differ"):
            self.plan()
        self.market["description"] = "A clear binary event."
        self.forecast["fair_yes_lower_bound"] = "0.52"
        with self.assertRaisesRegex(ValueError, "edge"):
            self.plan()

    def test_preview_rejects_mismatched_terms(self):
        plan = self.plan()
        request = trade.payload(plan)
        request["quantity"] += 1
        self.client.orders.preview.return_value = {"order": request}
        with self.assertRaisesRegex(ValueError, "exact order terms"):
            trade.exchange_preview(self.client, plan)

    def test_scan_separates_candidates_from_rejected_forecasts(self):
        weak = {**self.forecast, "fair_yes_lower_bound": "0.52"}
        strong = {**self.forecast, "fair_yes_lower_bound": "0.70"}
        result = trade.scan_forecasts(self.client, [self.forecast, weak, strong], self.limits)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(result["candidates"][0]["forecast"]["fair_yes_lower_bound"], "0.70")

    def test_preflight_rejects_existing_position_and_insufficient_funds(self):
        plan = self.plan()
        self.client.portfolio.positions.return_value = {"positions": {"example-event": {"netPositionDecimal": "-1"}}}
        with self.assertRaisesRegex(ValueError, "existing position"):
            trade.live_checks(self.client, plan)
        self.client.account.balances.return_value = {"balances": [{"currency": "USD", "buyingPower": "2"}]}
        with self.assertRaisesRegex(ValueError, "insufficient"):
            trade.live_checks(self.client, plan)

    def test_approval_tampering_halt_and_replay(self):
        approval, token = self.approve()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            altered = copy.deepcopy(approval)
            altered["plan"]["quantity"] += 1
            with self.assertRaisesRegex(ValueError, "does not match"):
                trade.submit(self.client, altered, token, "test-account", root)
            (root / "HALT").touch()
            with self.assertRaisesRegex(ValueError, "HALT"):
                trade.submit(self.client, approval, token, "test-account", root)
            (root / "HALT").unlink()
            result = trade.submit(self.client, approval, token, "test-account", root)
            self.assertEqual(result["submitted_order_id"], "test-order-id")
            with self.assertRaisesRegex(ValueError, "consumed"):
                trade.submit(self.client, approval, token, "test-account", root)
            self.client.orders.create.assert_called_once()

    def test_network_failure_leaves_persistent_unknown_state(self):
        approval, token = self.approve()
        self.client.orders.create.side_effect = TimeoutError
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "outcome unknown"):
                trade.submit(self.client, approval, token, "test-account", root)
            conn = trade.database(root)
            try:
                with self.assertRaisesRegex(ValueError, "earlier ambiguous"):
                    trade.reserve(conn, "different-approval", "another-key", approval["plan"], self.now)
            finally:
                conn.close()

    def test_status_records_terminal_exchange_result(self):
        approval, token = self.approve()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trade.submit(self.client, approval, token, "test-account", root)
            self.client.orders.retrieve.return_value = {"order": {"id": "test-order-id", "state": "ORDER_STATE_FILLED",
                                                                 "cumQuantity": 9, "avgPx": usd("0.50")}}
            result = trade.status(self.client, "test-order-id", "test-account", root)
            self.assertEqual(result["filled"], 9)
            conn = trade.database(root)
            try:
                self.assertEqual(conn.execute("SELECT state FROM attempts").fetchone()[0], "ORDER_STATE_FILLED")
            finally:
                conn.close()

    def test_reconcile_requires_exact_terminal_order(self):
        approval, token = self.approve()
        self.client.orders.create.side_effect = TimeoutError
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "outcome unknown"):
                trade.submit(self.client, approval, token, "test-account", root)
            conn = trade.database(root)
            try:
                started = conn.execute("SELECT started FROM attempts").fetchone()[0]
            finally:
                conn.close()
            order = {**trade.payload(approval["plan"]), "id": "test-order-id", "createTime": started,
                     "state": "ORDER_STATE_PENDING_NEW", "cumQuantity": 9}
            self.client.orders.retrieve.return_value = {"order": order}
            with self.assertRaisesRegex(ValueError, "exact terminal"):
                trade.reconcile(self.client, token, "test-order-id", "test-account", root)
            order["state"] = "ORDER_STATE_FILLED"
            result = trade.reconcile(self.client, token, "test-order-id", "test-account", root)
            self.assertEqual(result["filled"], 9)


if __name__ == "__main__":
    unittest.main()
