# EVE Trader

Automated Jita 4-4 market-making and swing-trading engine for EVE Online. Combines historical DuckDB market analytics, live ESI order-book telemetry, strict post-fee margin gating, and a walk-forward retrospective backtester into a containerized FastAPI service and daily ledger pipeline.

---

## Trading Architecture: The 3-Desk Waterfall

Capital is allocated across three specialized desks in priority order (**Hold → Tactical → Evergreen**). Any unallocated budget slack cascades downstream so idle ISK is put to work without lowering upstream quality standards.

| Desk | Base Allocation | Min Net Margin | Horizon / Cadence | Strategy Profile |
| :--- | :--- | :--- | :--- | :--- |
| **Hold** | `20%` | `18.0%` | Multi-week (`21d+`) | Accumulates cyclical saturation dips (`live_ask < 180d VWAP * 0.90`) for mean-reversion exits. |
| **Tactical** | `45%` | `9.0%` | Swing (`≤ 10d`) | Captures high-dump-frequency (`≥ 25%`) volatility spreads sized to `15%` of daily volume (`vol_pct = 0.15`). |
| **Evergreen** | `35%` | `5.0%` | Intraday / Short | High-turnover, steady-spread market making on liquid Jita workhorses. |

---

## Key Risk & Execution Controls

* **Live Executable Margin Gating (`live_exec_margin`):** On Tactical and Evergreen desks, projected exit targets are capped against live Jita `lowest_ask` (`min(target_sell, live_ask)`), blocking new buy orders whenever historical 30-day VWAPs mask a collapsed live spread.
* **Sticky Inventory Carry-Over & `allow_new_buy` Gating:** Active sell orders and hangar inventory remain pinned to their assigned desk across daily rebuilds so exit pricing continues uninterrupted—while `allow_new_buy=False` suppresses additional buy recommendations until spreads recover.
* **Liquidity- & Bid-Share-Aware Position Sizing:** Line quantities scale against inferred Jita bid-side fill share (`bid_share_30d`), desk volume caps (`vol_pct`), and per-line ISK limits (`max_line_cap`).
* **Centralized EVE Accounting & Tick Precision:** Enforces EVE 4-significant-figure tick rounding (`round_up_eve`, `round_down_eve`), broker/SCC buy surcharges, exit sales taxes, and FIFO/weighted cost-basis tracking.
* **Live Order & Buy Advisors:**
  * **Buy Advisor (`workflows/buy_orders.py`):** Evaluates unplaced desk lines against live Jita books, clamps bids at `MARGIN CAP` (`max_buy_floor`) to prevent overbidding, filters out `allow_new_buy=False` sticky rows, and generates EVE Multibuy strings.
  * **Order Actions (`workflows/order_actions.py`):** Flags `BUMP_VIABLE` undercuts, `TOP_BOOK` winning bids, `PATIENT` holds, and `CANCEL_OVERPRICED` orders whose live post-fee margin has dropped below `cancel_floor`.

---

## Project Structure

```text
eve-trader/
├── services/
│   ├── accounting.py       # EVE tick math, fees, net proceeds, projected margin formulas
│   ├── blacklist.py        # Item and market-group exclusion filters
│   ├── esi.py              # Live Jita 4-4 order book & character order/asset telemetry
│   ├── ledgers.py          # DeskProfiles, candidate selection, pricing, sizing & order annotation
│   ├── market.py           # Market price cache & DuckDB/SDE analytics queries
│   └── retrospective/      # Walk-forward simulator, optimizer & AI autopilot PR pipeline
├── workflows/
│   ├── buy_orders.py       # Missing buy-order audit, margin cap clamping & Multibuy export
│   ├── order_actions.py    # Live BUY/SELL bump and cancel evaluation
│   └── sell_advisor.py     # Hangar inventory & active sell order pricing advisor
├── scripts/
│   ├── generate_ledgers.py # Multi-desk waterfall CLI & DuckDB ledger_history persistence
│   └── run_daily_ledger.sh # Automated git sync, pytest gate (with auto-revert) & ledger rebuild
├── tests/                  # Pytest suite verifying domain math, stability & strategy invariants
├── ledgers/                # Generated CSV ledgers (hold, tactical, evergreen) & AI proposals
└── data/                   # DuckDB analytics store (eve_analytics.duckdb) & SQLite SDE
```

---

## Quickstart & Daily Operations

### 1. Start the Service
```bash
docker compose up -d --build
```
The API and dashboard run on `http://localhost:8000`.

### 2. Run the Test Suite
```bash
docker compose exec -T eve-trading-desk pytest -q
```

### 3. Generate Daily Desk Ledgers
```bash
./scripts/run_daily_ledger.sh
```
`run_daily_ledger.sh` pulls the latest changes on the active branch, runs `pytest` (automatically reverting if any invariant test fails), and writes updated `hold_ledger.csv`, `tactical_ledger.csv`, and `evergreen_ledger.csv` files alongside timestamped snapshots in DuckDB (`ledger_history`).

---

## API Reference

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET / POST` | `/api/orders/actions` | Evaluates active BUY/SELL orders for viable bumps, holds, and cancellations. |
| `POST` | `/api/retrospective/analyze` | Runs a walk-forward backtest across historical DuckDB checkpoints, tunes `DeskProfile` parameters via coordinate descent, and audits spread-compression prevention. |
| `POST` | `/api/retrospective/deep-review` | Runs Stage 2 automated architectural & parameter review against retrospective telemetry and opens a PR on `ai-autopilot`. |

---

## Branching & Automated Retrospective Workflow

* **`main`:** Stable, manually reviewed production baseline. Automated PRs never target `main`.
* **`ai-autopilot`:** Active experimentation and tuning branch. Retrospective reviews open pull requests exclusively against `ai-autopilot`, where every change must pass the full `pytest` invariant suite before ledger generation.
