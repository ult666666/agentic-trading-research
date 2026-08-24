# Broker Workflow

## Tool categories

Map available broker MCP tools into:

- Read account
- Read positions
- Read orders
- Read quotes or market data
- Check tradability
- Review or preview order
- Place order
- Modify order
- Cancel order

Tool names vary. Never guess that a tool is read-only from its name alone; inspect its description.

## Required sequence

1. Refresh account, positions, pending orders, and quote.
2. Run risk gate.
3. Present exact order proposal.
4. Obtain exact user confirmation.
5. Refresh state again.
6. Run risk gate again.
7. Review or preview order.
8. Compare review with confirmation.
9. Submit only the exact confirmed order.
10. Read order status.
11. Reconcile intent, submission, fill, positions, and buying power.
12. Pause on any mismatch.

## Confirmation validity

Confirmation is invalid when:

- Symbol, side, amount, quantity, type, or price changes.
- Quote becomes stale or market conditions materially change.
- Buying power or positions change.
- A new warning appears.
- The session is restarted or context is lost.

## Order preferences

- Prefer limit orders when practical.
- Never repeatedly raise a buy limit merely to force a fill.
- Never silently convert between dollar-based and share-based orders.
- Never assume a stop guarantees its stop price.
- Never assume fractional shares support every order type.
