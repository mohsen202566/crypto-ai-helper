# Movement Hunter v1.0 Locked Architecture

Final locked architecture for the crypto futures bot.

## Locked rules

- AI is the final authority.
- Classic technical modules are sensors only.
- Final decisions are only REAL / GHOST / REJECT.
- No Paper mode.
- No Setup flow.
- Real trading uses Toobit safety path only.
- Isolated-only futures trading.
- Leverage, margin, quantity, min-notional, TP/SL, duplicate orders, emergency stop, and max positions are checked before real order.
- REAL and GHOST outcomes feed learning.
- Movement memory stores pre-move behavior so AI hunts fresh pump/dump moves.
- Learning/movement/ghost storage is bounded to 20,000 records for VPS stability.

## Passed audits

- 36 Cross File Audit: PASS
- 37 Real Trading Safety Audit: PASS_WITH_NOTES
- 38 AI Logic Audit: PASS
- 39 Performance Runtime Audit: PASS_WITH_NOTES
- 40 Final Lock: PASS

## Deployment reminder

Use this folder as the clean baseline. Before VPS real trading, set environment variables only on VPS:

- BOT_TOKEN
- TOOBIT_API_KEY
- TOOBIT_API_SECRET
- OWNER_ID

Never commit secrets to GitHub.
