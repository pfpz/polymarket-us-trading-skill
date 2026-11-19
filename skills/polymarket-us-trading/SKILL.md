---
name: polymarket-us-trading
description: Research, paper-trade, preview, and submit individually approved Polymarket US event-contract buys via its official API. Use for Polymarket US, fee-adjusted trade evaluation, and order reconciliation.
---

# Polymarket US trading

Use [the operator guide](README.md) for commands and setup. This skill targets the U.S. exchange and ordinary binary YES purchases with a $1 winning payout. It needs no credentials for market research or paper trading.

Read the market's exact resolution rules, deadline, and executable book. Develop an independent probability range from dated public evidence or a tested forecasting model. Use its conservative YES probability. Prices alone do not establish a probability edge. If evidence is weak, show the market data and pass. Use `paper` or `scan` to check depth, cost, fee-adjusted edge, and payoff before considering a live preview.

Keep API credentials in the user's local environment. Use `preview` for one exact approved-to-review proposal. Show the market, quantity, limit, maximum cash at risk, forecast evidence, and expiry. Call `submit` only after the user approves that exact proposal. Submission rechecks terms and funding, uses a limit fill-or-kill order, and records the attempt before making a write. Report confirmed fills using `status`. An ambiguous outcome requires exchange reconciliation before further submission.

Use the user's risk limits and scope. Respect the venue's location, account, and market rules. Keep account details and local trading journals out of published examples. Avoid forecasts based on confidential or improperly obtained information. Keep research and proposed returns separate from actual results.
