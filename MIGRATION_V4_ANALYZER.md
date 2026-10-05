# Analyzer V4 patch

- Top 24h is ranked only after filtering Toobit exchangeInfo metadata. Stock, Equity, Indices, Forex, TradFi, ETF/CFD and commodity-like contracts are excluded.
- Scanner remains Toobit USDT-M Futures only.
- Exact Peak is no longer an entry/signal prerequisite. The 36h pump peak is reference/context only.
- Reversal scoring can fire below the exact peak using local 1m deterioration plus the other independent layers.
- Being below Peak gives no reversal points by itself, preventing a stale/old peak from creating a false signal.
- Existing 5m / 15m / 1h support-resistance analysis and chart annotations are preserved.
