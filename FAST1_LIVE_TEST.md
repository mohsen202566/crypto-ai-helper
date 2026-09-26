# FAST1 virtual-live test build

Frozen strategy changes only:
- Entry: Peak -> configured pullback (default 3%) -> additional FAST drop 1% -> SHORT.
- No breathing/staleness timer.
- Conservative setup invalidation: +1% rebound above pullback trigger; after cancel/entry, a new peak is required before re-arm.
- Cooldown removed from entry and exit logic.
- Stop: exact modeled net loss = 38% of position margin.
- Trail activation: net profit = 30% of position margin.
- Trail retrace: 10% of position margin from best net profit.
- Default Top N = 4; default max concurrent positions = 4; leverage default remains 5x.
- Baseline modeled round-trip cost default = 0.20% (0.05% taker each side + 0.05% slippage each side); funding reserve default 0.
- Legacy dollar TP/SL/trail and staleness/cooldown commands are recognized but do not change the frozen FAST1 test parameters.
- Existing unrelated panels/commands and trading modes remain in place.

Important: existing runtime.db settings (e.g. top_n_count/max_positions/pullback/leverage/position_size) remain authoritative where those settings are user-configurable. For exact validated setup set: واچ 15, تاپ 4, برگشت 3, پوزیشن 4, اهرم 5, and your intended margin sizing before virtual test.
