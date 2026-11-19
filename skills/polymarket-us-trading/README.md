# Polymarket US Trading Skill

Research event contracts, calculate fee-adjusted entries, and submit exact orders after approval. This independent skill uses the official Polymarket US Python SDK. It is designed for the U.S. exchange, with market research and paper checks available without an account key.

## What it does

| Command | Result | Exchange access |
| --- | --- | --- |
| `markets` | Open market slugs, titles, and reported deadlines | Public |
| `quote` | Resolution terms, price tick, minimum size, and live book | Public |
| `paper` | A proposed size, price cap, fee estimate, and payoff using one independent forecast | Public |
| `scan` | Rank up to 20 reviewed forecasts against the current executable books | Public |
| `preview` | Exact, expiring order proposal validated through the exchange preview endpoint | Authenticated |
| `submit` | One approved limit fill-or-kill order | Authenticated |
| `status` | Exchange-reported state, filled size, average price, and commission | Authenticated |
| `reconcile` | Match an ambiguous attempt to a verified terminal exchange order | Authenticated |
| `ledger` | Local submission history | Local only |

The order path supports whole-contract purchases of the long outcome on markets with a verified standard binary payout. The skill leaves the position open for settlement after a fill. This release supports single approved entries. NO-side entries, early exits, combinations, and unattended live execution are outside its order path.

## Install

Copy or install the `polymarket-us-trading` folder into your Agent Skills directory, then invoke `$polymarket-us-trading` in Codex. The same folder works as a standalone CLI. Requires Python 3.10+.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python trade.py markets --count 5
.venv/bin/python trade.py quote YOUR-MARKET-SLUG
```

## Evaluate an entry

1. Run `quote` and read the full resolution rules, including the settlement source, expiry, tie handling, and exceptions. Use markets whose regular outcome pays $1 or $0.
2. Copy `forecast.example.json` and `limits.example.json` to private working files. Put the current `terms_sha256` from `quote` in the forecast. Replace the example probability, data source, rationale and timestamps with an independent, time-stamped estimate for the long outcome. Use a conservative lower bound, with a meaningful rationale or out-of-sample calibration. Copying the contract price into this field never establishes an edge.
3. Set a per-order cash cap and a UTC daily cash cap, plus a limit-price ceiling, acceptable spread, and required edge per contract. Example values are illustrations.
4. Run a paper check before using credentials.

```bash
.venv/bin/python trade.py paper forecast.json limits.json
# forecasts.json is an array of independently researched forecast objects.
.venv/bin/python trade.py scan forecasts.json limits.json
```

The paper result is a snapshot-based proposal. It reserves a conservative standard taker commission, uses visible depth at or under the limit, and weights entry size by the conservative expected payout minus maximum cash cost. A positive output depends on the forecast being sound. Record forecasts and later outcomes separately before using the method with money. Fees, timing, data quality, and short-run luck can all change results.

## Review and submit one order

Use API credentials from the [official Polymarket US developer page](https://polymarket.us/developer) only if your account and jurisdiction permit that access. Supply `POLYMARKET_KEY_ID` and `POLYMARKET_SECRET_KEY` in your local environment or secret manager. Keep the values out of chats, examples, terminal history, and version control. Public commands neither require nor read those variables.

```bash
.venv/bin/python trade.py preview forecast.json limits.json --save ./approval.json
# Review the exact proposed order and authorize it before running submit.
.venv/bin/python trade.py submit ./approval.json --confirm SHA256_FROM_PREVIEW
.venv/bin/python trade.py status EXCHANGE_ORDER_ID
```

Previews expire within 90 seconds or earlier if the forecast or market ends. Submission rechecks the book, fee bound, funding, and existing orders and positions. It sends one price-capped fill-or-kill order. The full order can fail to fill if quotes move. The local journal makes each approval single-use, applies the daily cost cap and a 30-second throttle, and blocks subsequent submissions until the previous attempt is reconciled. A position already held in the same market blocks another entry. Set a `HALT` file under `~/.local/state/polymarket-us-trading/` to stop new submissions. The cap applies to this installation's journal. Track other activity and account-wide exposure independently.

A response with an order ID means it was submitted. Use `status` to verify the exchange outcome and what filled. A network error or missing order ID can leave an unknown submission. Check `ledger` for exact terms, timestamp, approval digest, and any exchange request ID. If Polymarket US history shows a matching terminal order, run `reconcile APPROVAL_DIGEST EXCHANGE_ORDER_ID` with the same API key. If there is no verifiable exchange order, preserve the journal and contact Polymarket US support. The tool keeps submissions blocked and has no override for an unresolved attempt.

## Verification and sources

```bash
.venv/bin/python -m unittest -v test_trade.py
```

The tests use a fake exchange. They exercise price and fee caps, stale quotes, order payloads, replay protection, and ambiguous submissions without using account credentials. Live execution needs your own eligible credentials and separate verification. No historical profit or future return is claimed.

- [Polymarket US API](https://docs.polymarket.us/api-reference/introduction)
- [Standard fees and rebates](https://docs.polymarket.us/fees)
- [Official Polymarket US Python SDK](https://github.com/Polymarket/polymarket-us-python)
- [Polymarket US market integrity](https://integrity.polymarket.us/)
