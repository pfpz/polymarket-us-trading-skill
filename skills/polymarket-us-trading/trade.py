#!/usr/bin/env python3
"""Fee-aware Polymarket US research, paper entries, and approved submissions."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path


UTC = timezone.utc
CENT = Decimal("0.01")
STANDARD_TAKER_CAP = Decimal("0.095")
THROTTLE_SECONDS = 30
FOK = "TIME_IN_FORCE_FILL_OR_KILL"
TERMINAL = {"ORDER_STATE_FILLED", "ORDER_STATE_REJECTED", "ORDER_STATE_CANCELED",
            "ORDER_STATE_EXPIRED"}
LOCAL_STATE = Path.home() / ".local/state/polymarket-us-trading"


def utcnow():
    return datetime.now(UTC)


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Use an ISO 8601 timestamp with a timezone")
    return result.astimezone(UTC)


def decimal(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Expected a finite decimal amount") from exc
    if not result.is_finite():
        raise ValueError("Expected a finite decimal amount")
    return result


def plain(value):
    return format(value, "f")


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"), parse_float=str)


def amount(obj):
    if not isinstance(obj, dict) or obj.get("currency") != "USD":
        raise ValueError("Expected a quoted USD amount")
    return decimal(obj.get("value"))


def market_slug(value):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,159}", value):
        raise ValueError("Invalid Polymarket US market slug")
    return value


def venue(client, slug):
    market = client.markets.retrieve_by_slug(slug)["market"]
    book = client.markets.book(slug)["marketData"]
    if market.get("slug") != slug or book.get("marketSlug") != slug:
        raise ValueError("Exchange market and book identifiers differ")
    return market, book


def market_terms(market):
    sides = market.get("marketSides") or []
    return {"slug": market["slug"], "question": market.get("question"),
            "title": market.get("title"), "rules": market.get("description"),
            "close": market.get("endDate"), "asset_price_terms": market.get("assetPriceTerms"),
            "outcomes": [{"label": x.get("description"), "long": x.get("long")}
                         for x in sides]}


def quote(client, slug):
    market, book = venue(client, slug)
    terms = market_terms(market)
    return {"terms": terms, "terms_sha256": digest(terms), "market_status": market.get("status"),
            "book_state": book.get("state"), "book_at": book.get("transactTime"),
            "minimum_contracts": market.get("minimumTradeQty"),
            "tick": market.get("orderPriceMinTickSize"),
            "bids": book.get("bids", [])[:5], "offers": book.get("offers", [])[:5]}


def fee_upper_bound(quantity, cap, coefficient):
    peak = min(Decimal("0.5"), cap)
    return (coefficient * quantity * peak * (1 - peak)).quantize(CENT, rounding=ROUND_CEILING)


def build_plan(client, forecast, limits, now):
    slug = market_slug(forecast["market"])
    if not forecast.get("source", "").startswith("https://") or not forecast.get("rationale", "").strip():
        raise ValueError("Supply an independent HTTPS source and a forecast rationale")
    if forecast.get("standard_binary_payout_verified") is not True:
        raise ValueError("Verify standard binary settlement with a $1 winning payout")
    observed = timestamp(forecast["observed_at"])
    valid_until = timestamp(forecast["valid_until"])
    if not observed <= now < valid_until:
        raise ValueError("Forecast is not current")
    prob = decimal(forecast["fair_yes_lower_bound"])
    budget = decimal(limits["max_order_cost"])
    day_cap = decimal(limits["max_daily_cost"])
    min_edge = decimal(limits["min_edge_per_contract"])
    max_price = decimal(limits["max_contract_price"])
    max_spread = decimal(limits["max_spread"])
    if not (0 < prob < 1 and 0 < budget <= day_cap and 0 < min_edge < 1 and
            0 < max_price < 1 and 0 < max_spread < 1):
        raise ValueError("Invalid probability, budget, price collar, spread, or edge limits")

    market, book = venue(client, slug)
    terms = market_terms(market)
    if not terms["rules"] or digest(terms) != forecast["terms_sha256"]:
        raise ValueError("Market terms are missing or differ from the reviewed terms")
    if (market.get("active") is not True or market.get("closed") is not False or
            book.get("state") != "MARKET_STATE_OPEN"):
        raise ValueError("Market is not an open standard binary market")
    if len(terms["outcomes"]) != 2 or {x["long"] for x in terms["outcomes"]} != {True, False}:
        raise ValueError("Cannot verify the two outcomes and the long side")
    if market.get("marketType") == "combo":
        raise ValueError("Combination contracts are outside this order path")
    close = timestamp(terms["close"])
    book_at = timestamp(book["transactTime"])
    checked_at = max(now, utcnow())
    if not checked_at < min(close, valid_until) or not -2 <= (checked_at - book_at).total_seconds() <= 30:
        raise ValueError("Market is closed or the executable book or forecast is stale")
    tick = decimal(market["orderPriceMinTickSize"])
    minimum = decimal(market["minimumTradeQty"])
    coefficient = max(STANDARD_TAKER_CAP, decimal(market.get("feeCoefficient", STANDARD_TAKER_CAP)))
    if tick <= 0 or minimum <= 0 or not 0 < coefficient < 1:
        raise ValueError("Venue sizing, tick or fee metadata is invalid")
    bids = [amount(x["px"]) for x in book.get("bids", []) if decimal(x["qty"]) > 0]
    offers = sorted((amount(x["px"]), decimal(x["qty"])) for x in book.get("offers", [])
                    if decimal(x["qty"]) > 0)
    if not bids or not offers:
        raise ValueError("A two-sided order book is required")
    floor = offers[0][0]
    if not 0 < max(bids) < floor < 1 or floor - max(bids) > max_spread:
        raise ValueError("Spread exceeds the approved limit or the book is crossed")

    planned = None
    depth = Decimal(0)
    for price, size in offers:
        if price > max_price or price >= prob or price % tick:
            break
        depth += size
        quantity = min(int(depth), int(budget / price))
        if quantity < minimum:
            continue
        peak = min(Decimal("0.5"), price)
        unit_fee_cap = coefficient * peak * (1 - peak)
        quantity = min(quantity, int(budget / (price + unit_fee_cap)))
        while quantity >= minimum and quantity * price + fee_upper_bound(quantity, price, coefficient) > budget:
            quantity -= 1
        if quantity < minimum:
            continue
        commission = fee_upper_bound(quantity, price, coefficient)
        cost = quantity * price + commission
        edge = quantity * prob - cost
        if cost <= budget and edge >= quantity * min_edge:
            if planned is None or edge > planned["edge"]:
                planned = {"quantity": quantity, "price": price, "fee": commission, "cost": cost, "edge": edge}
    if planned is None:
        raise ValueError("No size cleared the depth, cost and conservative edge checks")

    return {"market": slug, "title": terms["title"], "terms_sha256": digest(terms),
            "side": next(x["label"] for x in terms["outcomes"] if x["long"] is True),
            "quantity": planned["quantity"], "limit_price": plain(planned["price"]),
            "maximum_fee_estimate": plain(planned["fee"]), "maximum_cost": plain(planned["cost"]),
            "conservative_expected_net": plain(planned["edge"]), "daily_cost_cap": plain(day_cap),
            "book_at": book_at.isoformat(), "forecast": forecast, "limits": limits,
            "expires_at": min(checked_at + timedelta(seconds=90), close, valid_until).isoformat()}


def payload(plan):
    return {"marketSlug": plan["market"], "intent": "ORDER_INTENT_BUY_LONG",
            "type": "ORDER_TYPE_LIMIT", "price": {"value": plan["limit_price"], "currency": "USD"},
            "quantity": plan["quantity"], "tif": FOK, "synchronousExecution": True,
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC"}


def scan_forecasts(client, forecasts, limits):
    if not isinstance(forecasts, list) or not 1 <= len(forecasts) <= 20:
        raise ValueError("Provide a list of between 1 and 20 independently researched forecasts")
    candidates, skipped = [], []
    for record in forecasts:
        if not isinstance(record, dict):
            raise ValueError("Every forecast must be an object")
        try:
            candidates.append(build_plan(client, record, limits, utcnow()))
        except (ValueError, KeyError, TypeError) as exc:
            skipped.append({"market": record.get("market"), "reason": str(exc)})
        except Exception as exc:
            skipped.append({"market": record.get("market"), "reason": type(exc).__name__})
    candidates.sort(key=lambda plan: decimal(plan["conservative_expected_net"]), reverse=True)
    return {"mode": "paper", "candidates": candidates, "skipped": skipped}


def live_checks(client, plan):
    funds = [decimal(x.get("buyingPower")) for x in client.account.balances()["balances"]
             if x.get("currency") == "USD"]
    if len(funds) != 1 or funds[0] < decimal(plan["maximum_cost"]):
        raise ValueError("USD buying power is missing or insufficient")
    if client.orders.list({"slugs": [plan["market"]]})["orders"]:
        raise ValueError("An open order already exists for this market")
    positions = client.portfolio.positions({"market": plan["market"], "limit": 100})
    if positions.get("nextCursor") or positions.get("eof") is False:
        raise ValueError("The market position could not be fully reconciled")
    for pos in positions["positions"].values():
        if pos.get("expired") is not True:
            units = decimal(pos.get("netPositionDecimal", pos.get("netPosition")))
            if units != 0:
                raise ValueError("An existing position already exposes the account to this market")


def exchange_preview(client, plan):
    order = client.orders.preview({"request": payload(plan)})["order"]
    if (order.get("marketSlug") != plan["market"] or order.get("intent") != "ORDER_INTENT_BUY_LONG" or
            decimal(order.get("quantity")) != plan["quantity"] or
            amount(order.get("price")) != decimal(plan["limit_price"]) or order.get("tif") != FOK or
            order.get("state") in {"ORDER_STATE_REJECTED", "ORDER_STATE_CANCELED", "ORDER_STATE_EXPIRED"}):
        raise ValueError("Exchange preview did not validate the exact order terms")
    return {"state": order.get("state"), "market": order["marketSlug"], "tif": order["tif"]}


def database(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(path / "orders.sqlite3", timeout=10, isolation_level=None)
    conn.execute("""CREATE TABLE IF NOT EXISTS attempts (
        token TEXT PRIMARY KEY, account TEXT NOT NULL, market TEXT NOT NULL,
        started TEXT NOT NULL, cost TEXT NOT NULL, state TEXT NOT NULL, order_id TEXT,
        quantity INTEGER NOT NULL, limit_price TEXT NOT NULL, request_id TEXT
    )""")
    return conn


def reserve(conn, token, account, plan, now):
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM attempts WHERE token = ?", (token,)).fetchone():
            raise ValueError("This approval was already consumed")
        if conn.execute("SELECT 1 FROM attempts WHERE state='UNKNOWN'").fetchone():
            raise ValueError("Resolve the earlier ambiguous exchange order before another submission")
        if conn.execute("SELECT 1 FROM attempts WHERE state='SUBMITTED'").fetchone():
            raise ValueError("Check the earlier exchange order status before another submission")
        recent = conn.execute("SELECT started, cost FROM attempts ORDER BY started DESC").fetchall()
        if recent and (now - timestamp(recent[0][0])).total_seconds() < THROTTLE_SECONDS:
            raise ValueError("The 30-second order throttle is active")
        day = sum((decimal(c) for t, c in recent if timestamp(t).date() == now.date()), Decimal(0))
        if day + decimal(plan["maximum_cost"]) > decimal(plan["daily_cost_cap"]):
            raise ValueError("Approved UTC daily order-cost cap would be exceeded")
        conn.execute("INSERT INTO attempts VALUES (?,?,?,?,?,'UNKNOWN',NULL,?,?,NULL)",
                     (token, account, plan["market"], now.isoformat(), plan["maximum_cost"],
                      plan["quantity"], plan["limit_price"]))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def submit(client, approval, token, account, path):
    if digest(approval) != token or approval.get("account") != account:
        raise ValueError("The exact approval or account does not match")
    plan = approval["plan"]
    if utcnow() >= timestamp(plan["expires_at"]):
        raise ValueError("Approval expired; prepare a fresh preview")
    if (path / "HALT").exists():
        raise ValueError("Local HALT switch is enabled")
    fresh = build_plan(client, plan["forecast"], plan["limits"], utcnow())
    if (fresh["market"] != plan["market"] or fresh["quantity"] != plan["quantity"] or
            decimal(fresh["limit_price"]) != decimal(plan["limit_price"]) or
            decimal(fresh["maximum_cost"]) != decimal(plan["maximum_cost"])):
        raise ValueError("Executable terms changed; prepare a fresh preview")
    live_checks(client, plan)
    exchange_preview(client, plan)
    now = utcnow()
    if now >= timestamp(plan["expires_at"]) or (path / "HALT").exists():
        raise ValueError("Approval expired or the local HALT switch was enabled")
    with database(path) as conn:
        reserve(conn, token, account, plan, now)
        if (path / "HALT").exists():
            conn.execute("UPDATE attempts SET state='HALTED' WHERE token=?", (token,))
            raise ValueError("Local HALT switch was enabled before submission")
        dispatch = utcnow()
        if dispatch >= timestamp(plan["expires_at"]) or (dispatch - timestamp(fresh["book_at"])).total_seconds() > 30:
            conn.execute("UPDATE attempts SET state='STALE_NOT_SENT' WHERE token=?", (token,))
            raise ValueError("Approval or book expired before submission")
        try:
            response = client.orders.create(payload(plan))
        except Exception as exc:
            request = getattr(exc, "request_id", None)
            if isinstance(request, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request):
                conn.execute("UPDATE attempts SET request_id=? WHERE token=?", (request, token))
            raise ValueError("Submission outcome unknown. Check exchange orders and fills; do not retry.") from exc
        order_id = response.get("id")
        if not isinstance(order_id, str) or not order_id:
            raise ValueError("Exchange returned no order ID. Reconcile the submission before trading again.")
        conn.execute("UPDATE attempts SET state='SUBMITTED',order_id=? WHERE token=?", (order_id, token))
    return {"submitted_order_id": order_id, "next": "Use status to verify the exchange's actual result."}


def status(client, order_id, account, path):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", order_id):
        raise ValueError("Invalid exchange order ID")
    order = client.orders.retrieve(order_id)["order"]
    if order.get("id") != order_id:
        raise ValueError("Exchange returned a different order ID")
    state = order.get("state")
    if state in TERMINAL:
        with database(path) as conn:
            conn.execute("UPDATE attempts SET state=? WHERE order_id=? AND account=?", (state, order_id, account))
    return {"order_id": order_id, "state": state, "market": order.get("marketSlug"),
            "filled": order.get("cumQuantity"), "average_price": order.get("avgPx"),
            "commission": order.get("commissionNotionalTotalCollected")}


def reconcile(client, token, order_id, account, path):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", order_id):
        raise ValueError("Invalid exchange order ID")
    order = client.orders.retrieve(order_id)["order"]
    with database(path) as conn:
        row = conn.execute("SELECT market,started,quantity,limit_price FROM attempts "
                           "WHERE token=? AND account=? AND state='UNKNOWN'", (token, account)).fetchone()
        if row is None:
            raise ValueError("No matching ambiguous submission exists for this API key")
        market, started, quantity, price = row
        created = timestamp(order.get("createTime") or order["insertTime"])
        dt = (created - timestamp(started)).total_seconds()
        if (order.get("id") != order_id or order.get("marketSlug") != market or
                order.get("intent") != "ORDER_INTENT_BUY_LONG" or order.get("tif") != FOK or
                decimal(order.get("quantity")) != quantity or amount(order.get("price")) != decimal(price) or
                not -2 <= dt <= 30 or order.get("state") not in TERMINAL):
            raise ValueError("Exchange order does not match the exact terminal submission")
        cursor = conn.execute("UPDATE attempts SET state=?,order_id=? "
                              "WHERE token=? AND state='UNKNOWN' "
                              "AND NOT EXISTS(SELECT 1 FROM attempts WHERE order_id=?)",
                              (order["state"], order_id, token, order_id))
        if cursor.rowcount != 1:
            raise ValueError("The order is already accounted for or the local state changed")
    return {"order_id": order_id, "state": order["state"], "filled": order.get("cumQuantity")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    markets = sub.add_parser("markets", help="List open markets using the unauthenticated API")
    markets.add_argument("--count", type=int, choices=range(1, 101), default=10, metavar="1-100")
    m = sub.add_parser("quote", help="Read one market's rules and live order book")
    m.add_argument("market", type=market_slug)
    m = sub.add_parser("scan", help="Rank up to 20 reviewed forecasts against current executable books")
    m.add_argument("forecasts", type=Path)
    m.add_argument("limits", type=Path)
    for mode in ("paper", "preview"):
        m = sub.add_parser(mode, help="Evaluate an order" if mode == "paper" else "Request an authenticated order preview")
        m.add_argument("forecast", type=Path)
        m.add_argument("limits", type=Path)
        if mode == "preview":
            m.add_argument("--save", required=True, type=Path)
    m = sub.add_parser("submit", help="Submit the exact confirmed, unexpired preview once")
    m.add_argument("approval", type=Path)
    m.add_argument("--confirm", required=True)
    m = sub.add_parser("status", help="Verify an order by its exchange ID")
    m.add_argument("order_id")
    m = sub.add_parser("reconcile", help="Match an ambiguous attempt to an exact terminal exchange order")
    m.add_argument("approval_digest")
    m.add_argument("order_id")
    sub.add_parser("ledger", help="Read this installation's local order-attempt journal")
    args = parser.parse_args()
    local = LOCAL_STATE
    try:
        if args.mode == "ledger":
            with database(local) as conn:
                conn.row_factory = sqlite3.Row
                result = [dict(row) for row in conn.execute(
                    "SELECT token,market,started,cost,quantity,limit_price,state,order_id,request_id "
                    "FROM attempts ORDER BY started DESC")]
        else:
            from polymarket_us import PolymarketUS
            private = args.mode in {"preview", "submit", "status", "reconcile"}
            key = os.environ.get("POLYMARKET_KEY_ID") if private else None
            secret = os.environ.get("POLYMARKET_SECRET_KEY") if private else None
            if private and (not key or not secret):
                raise ValueError("Set the Polymarket US API credentials locally before using an authenticated command")
            account = digest(key) if key else None
            with PolymarketUS(key_id=key, secret_key=secret, timeout=8, max_retries=0) as client:
                if args.mode == "markets":
                    found = client.markets.list({"active": True, "closed": False, "limit": args.count})["markets"]
                    result = [{"market": x.get("slug"), "title": x.get("title"), "end": x.get("endDate")}
                              for x in found]
                elif args.mode == "quote":
                    result = quote(client, args.market)
                elif args.mode == "scan":
                    result = scan_forecasts(client, read_json(args.forecasts), read_json(args.limits))
                elif args.mode in {"paper", "preview"}:
                    plan = build_plan(client, read_json(args.forecast), read_json(args.limits), utcnow())
                    if args.mode == "paper":
                        result = {"mode": "paper", "proposed_entry": plan}
                    else:
                        live_checks(client, plan)
                        approval = {"account": account, "plan": plan, "exchange_preview": exchange_preview(client, plan)}
                        with args.save.open("x", encoding="utf-8") as file:
                            json.dump(approval, file, indent=2)
                        result = {"mode": "preview", "proposed_entry": plan, "approval_digest": digest(approval)}
                elif args.mode == "submit":
                    result = submit(client, read_json(args.approval), args.confirm, account, local)
                elif args.mode == "reconcile":
                    result = reconcile(client, args.approval_digest, args.order_id, account, local)
                else:
                    result = status(client, args.order_id, account, local)
        print(json.dumps(result, indent=2))
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"{exc}\n")
    except Exception as exc:
        parser.exit(1, f"Operation failed ({type(exc).__name__}). Check the venue from the official interface.\n")


if __name__ == "__main__":
    main()
