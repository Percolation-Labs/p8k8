# Commerce Analytics (Platoon)

Percolate includes an integrated commerce analytics suite powered by [Platoon](https://github.com/Percolate-AI/p8-platoon). It gives agents the ability to run demand forecasting, inventory optimization, anomaly detection, basket analysis, cash flow projection, and staff scheduling — all from natural language questions.

## Getting Data In

Upload CSV or spreadsheet files to Percolate, then reference them by file ID. The tools accept either a **local file path** or an **uploaded file UUID** — the server resolves both transparently.

**Upload methods:**
- Percolate mobile app
- Drive sync (Google Drive, iCloud)
- `POST /content/` API endpoint
- `platoon_read_file` for local files (stdio/Claude Code only)

## Tools

| Tool | What it does |
|------|-------------|
| `get_file` | Read any uploaded file by ID — CSV rows or plain text |
| `platoon_forecast` | Demand forecasting (ETS, ARIMA, Croston) |
| `platoon_optimize` | EOQ, safety stock, reorder points, ABC, stockout risk |
| `platoon_detect_anomalies` | Spike/drop detection (z-score, IQR) |
| `platoon_basket_analysis` | Frequently-bought-together association rules |
| `platoon_cashflow` | Daily revenue/COGS/reorder cash projection |
| `platoon_schedule` | Demand-based staff shift assignment |

All `data_path` parameters accept either a local file path or an uploaded file UUID.

## End-to-End Case Study: Trailhead Nature Shop

This walkthrough uses a fictional outdoor/nature shop with 8 products — binoculars, bird feeders, field guides, trail boots, songbird seed, spotting scopes, field journals, and trail cameras. One month of February 2026 data with a Valentine's Day gift bump on optics and journals.

### Step 1: Upload Data

Upload four CSV files via the content API:

```bash
# Products catalog
curl -X POST https://api.percolationlabs.ai/content/ \
  -H "x-user-id: $USER_ID" \
  -F "file=@products.csv" -F "category=commerce"
# → file_id: 439af134-368a-5371-96fb-c2b8c88bbc6f

# 28 days of daily demand
curl -X POST https://api.percolationlabs.ai/content/ \
  -H "x-user-id: $USER_ID" \
  -F "file=@demand.csv" -F "category=commerce"
# → file_id: 72a09785-826d-500e-9105-173a4e1b442b

# 1,388 order line items (with line_total for ABC)
curl -X POST https://api.percolationlabs.ai/content/ \
  -H "x-user-id: $USER_ID" \
  -F "file=@orders.csv" -F "category=commerce"
# → file_id: d86f8a30-3d0e-5671-b847-b7ee26ff2a30

# Current stock levels
curl -X POST https://api.percolationlabs.ai/content/ \
  -H "x-user-id: $USER_ID" \
  -F "file=@inventory.csv" -F "category=commerce"
# → file_id: 85594c11-13cb-55ba-996c-53209cd7aeda
```

Files are stored in S3 and indexed. From here on, we reference them only by UUID.

### Step 2: Read Uploaded Data

Verify the upload by reading the products file:

```
get_file(file_id="439af134-368a-5371-96fb-c2b8c88bbc6f", head=3)
```

```json
{
  "status": "ok",
  "format": "csv",
  "name": "products",
  "columns": ["product_id", "sku", "cost", "price", "base_daily_demand", "lead_time_days"],
  "row_count": 3,
  "rows": [
    {"product_id": "BINOC-01", "sku": "BIN-PRO-8X42", "cost": "85.00", "price": "189.99", "base_daily_demand": "4", "lead_time_days": "10"},
    {"product_id": "FEEDER-01", "sku": "FDR-CEDAR-LG", "cost": "12.50", "price": "34.99", "base_daily_demand": "15", "lead_time_days": "5"},
    {"product_id": "GUIDE-01", "sku": "GDE-BIRDS-PNW", "cost": "8.00", "price": "24.95", "base_daily_demand": "8", "lead_time_days": "7"}
  ]
}
```

### Step 3: Forecast Demand

> "How much songbird seed will we sell in the next two weeks?"

```
platoon_forecast(
  data_path  = "72a09785-826d-500e-9105-173a4e1b442b",
  product_id = "SEED-01",
  horizon    = 14,
  holdout    = 0
)
```

**Result:**

| Field | Value |
|-------|-------|
| method | statsforecast:ets |
| predicted_sum | 356.4 units |
| predicted_mean | 25.46/day |
| trend | stable |
| seasonality_detected | yes |
| 95% CI | 15–36 units/day |

With only 30 units in stock and demand at ~25/day, we have **1.2 days of stock**. This is critical.

### Step 4: Inventory Optimization

> "Which products are at risk? What's the full picture?"

```
platoon_optimize(
  data_path      = "439af134-368a-5371-96fb-c2b8c88bbc6f",
  orders_path    = "d86f8a30-3d0e-5671-b847-b7ee26ff2a30",
  inventory_path = "85594c11-13cb-55ba-996c-53209cd7aeda"
)
```

**Result — ABC Summary:** 3 A-class, 2 B-class, 3 C-class

**All 8 products:**

| Product | SKU | ABC | Price | Daily Demand | Stock | Days Left | Risk | EOQ | Action |
|---------|-----|-----|------:|------------:|------:|----------:|-----:|----:|--------|
| Binoculars | BIN-PRO-8X42 | **A** | $189.99 | 4 | 8 | 2.0 | 100% | 83 | Reorder 83 |
| Spotting Scope | SCP-SPOT-20X | **A** | $279.99 | 2 | 22 | 11.0 | 83% | 49 | Reorder 49 |
| Trail Camera | CAM-TRAIL-HD | **A** | $149.99 | 3 | 5 | 1.7 | 100% | 89 | Reorder 89 |
| Cedar Feeder | FDR-CEDAR-LG | B | $34.99 | 15 | 180 | 12.0 | 0% | 419 | OK |
| Trail Boots | BT-TRAIL-WP | B | $129.99 | 3 | 45 | 15.0 | 19% | 99 | OK |
| Bird Guide PNW | GDE-BIRDS-PNW | C | $24.95 | 8 | 25 | 3.1 | 100% | 382 | Reorder 382 |
| Songbird Seed | SD-SONGBIRD-5LB | C | $12.99 | 25 | 30 | 1.2 | 100% | 901 | Reorder 901 |
| Field Journal | JRN-FIELD-LTH | C | $18.99 | 10 | 12 | 1.2 | 100% | 493 | Reorder 493 |

**6 of 8 products are at elevated stockout risk.** The three A-class items (Binoculars, Scope, Camera) drive the most revenue and should be prioritized.

### Step 5: Rush-Order Decision

> "Our binoculars are almost out. Next shipment is in 10 days. Rush-order?"

Combine forecast + optimize results:

```
current_stock           =     8 units
forecasted daily demand =  3.75 units/day
days_until_stockout     =  8 / 3.75  = 2.1 days
regular shipment ETA    =    10 days
coverage gap            =  10 - 2.1  = 7.9 days without stock
daily_revenue           =  $760/day
revenue at risk         =  7.9 × $760 = ~$6,004
restock cost (EOQ)      =  83 × $85  = $7,055
```

**Yes — rush-order immediately.** The binoculars are A-class and will stock out in ~2 days, leaving a 7.9-day gap costing ~$6,000 in lost revenue.

### Step 6: Budget Allocation

> "I have $15K for restocking. Where does it go?"

Using the alerts from Step 4, rank by daily revenue impact:

| # | Product | ABC | Units | Cost | Daily Rev | Why |
|---|---------|-----|------:|-----:|----------:|-----|
| 1 | Binoculars | A | 83 | $7,046 | $760 | 2 days of stock, A-class |
| 2 | Trail Camera | A | 89 | $4,908 | $450 | 1.7 days, A-class |
| 3 | Songbird Seed (partial) | C | 454 | $2,046 | $325 | 1.2 days, highest volume |

**Total: $14,000 → 626 units across 3 SKUs.** Remaining $1,000 held for Scope reorder when it drops below reorder point.

The A-class items get funded first despite the Seed having worse days-of-stock — because Binoculars generate $760/day vs Seed's $325/day.

## Decision Patterns

Most business questions require composing multiple tools:

| Question | Tools | Agent Adds |
|----------|-------|-----------|
| "How many will we sell?" | `forecast` | Narrates trend + seasonal pattern + planning range |
| "What should I reorder?" | `optimize` | Prioritizes by ABC class × risk, not just risk |
| "Should I rush-order?" | `forecast` + `optimize` | Days-to-stockout math + revenue-at-risk |
| "Where does my $N go?" | `optimize` | ROI ranking + greedy budget allocation |
| "Top products trending?" | `optimize` + `forecast` ×N | Cross-references ABC + trend + stock |
| "Cash flow outlook?" | `cashflow` | Identifies reorder spikes, sizes credit line |
| "Any anomalies?" | `detect_anomalies` | Clusters by season, distinguishes noise from signal |
| "Cross-sell opportunities?" | `basket_analysis` | Resolves IDs to names, suggests bundles |

## Tool Reference

### platoon_forecast

```
platoon_forecast(
  data_path  = "<file_id or path>",   # CSV: date, product_id, units_sold
  product_id = "PROD-1004",            # omit → auto-pick highest volume
  horizon    = 14,                     # days ahead
  method     = "auto",                 # auto | moving_average | exponential_smoothing | croston | arima | ets | theta
  holdout    = 30                      # days held out for MAE eval
)
→ {status, product_id, method, forecast, lower_bound, upper_bound,
   predicted_sum, predicted_mean, trend, seasonality_detected, mae}
```

### platoon_optimize

```
platoon_optimize(
  data_path      = "<file_id or path>",  # CSV: product_id, sku, cost, price, base_daily_demand, lead_time_days
  orders_path    = "<file_id or path>",  # optional — enables ABC classification (needs line_total column)
  inventory_path = "<file_id or path>",  # optional — enables stock levels + risk (needs closing_stock column)
  product_id     = "PROD-1004",          # omit for all products
  service_level  = 0.95
)
→ {status, product_count, abc_summary, results, alerts}
```

### platoon_detect_anomalies

```
platoon_detect_anomalies(
  data_path  = "<file_id or path>",  # CSV: date, product_id, units_sold
  product_id = "PROD-1004",
  method     = "zscore",              # zscore or iqr
  window     = 30,
  threshold  = 2.5
)
→ {status, product_id, anomaly_count, anomaly_rate, anomalies}
```

### platoon_basket_analysis

```
platoon_basket_analysis(
  orders_path    = "<file_id or path>",  # CSV: order_id, product_id, quantity, unit_price, order_date, line_total
  min_support    = 0.01,
  min_confidence = 0.3,
  max_rules      = 50
)
→ {status, total_orders, multi_item_orders, rule_count, rules}
```

### platoon_cashflow

```
platoon_cashflow(
  data_path      = "<file_id or path>",  # products CSV
  demand_path    = "<file_id or path>",  # demand CSV
  inventory_path = "<file_id or path>",  # inventory CSV
  horizon        = 30,
  product_id     = "PROD-1004"           # omit for all
)
→ {status, summary, reorder_events, periods}
```

### platoon_schedule

```
platoon_schedule(
  demand_path  = "<file_id or path>",
  staff_path   = "<file_id or path>",
  shift_hours  = 8,
  min_coverage = 1.0,
  horizon_days = 7
)
→ {status, total_cost, total_hours, demand_by_slot, coverage_gaps, schedule_grid}
```

## Data Format

### Products CSV
```csv
product_id,sku,cost,price,base_daily_demand,lead_time_days
BINOC-01,BIN-PRO-8X42,85.00,189.99,4,10
```

### Daily Demand CSV
```csv
date,product_id,units_sold
2026-02-01,BINOC-01,5
```

### Orders CSV (for ABC + basket analysis)
```csv
order_id,product_id,quantity,unit_price,order_date,line_total
ORD-01000,BINOC-01,2,189.99,2026-02-01,379.98
```

### Inventory CSV
```csv
product_id,closing_stock
BINOC-01,8
```

### Staff CSV (for scheduling)
```csv
name,hourly_rate,max_hours,available_days
Alice,18.50,40,"Monday,Tuesday,Wednesday,Thursday,Friday"
```

## Installation

Platoon is included in the p8k8 Docker image. For local development:

```bash
uv pip install "p8-platoon[learning]"
```

Optional extras:
- `[optimization]` — LP/MIP via OR-Tools
- `[search]` — web search enrichment via Tavily
- `[learning,optimization,search]` — everything
