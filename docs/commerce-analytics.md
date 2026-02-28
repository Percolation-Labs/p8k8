# Commerce Analytics (Platoon)

Percolate includes an integrated commerce analytics suite powered by [Platoon](https://github.com/Percolate-AI/p8-platoon). It gives agents the ability to run demand forecasting, inventory optimization, anomaly detection, basket analysis, cash flow projection, and staff scheduling — all from natural language questions.

## Getting Data In

Upload CSV or spreadsheet files to Percolate, then reference them by file ID. The tools accept either a **local file path** or an **uploaded file UUID** — the server resolves both transparently.

**Upload methods:**
- Percolate mobile app
- Drive sync (Google Drive, iCloud)
- `POST /content/` API endpoint
- `platoon_read_file` for local files (stdio/Claude Code only)

**Reading uploaded files:**
```
get_file(file_id="08fd3fd6-222e-5c3a-88c7-254755f962da")
→ {status, format, columns, row_count, rows}
```

Once a file is uploaded, pass its UUID directly to any analytics tool as the `data_path`:
```
platoon_optimize(data_path="08fd3fd6-222e-5c3a-88c7-254755f962da")
```

## Tools

### platoon_forecast

Predict future demand per product using auto-selected statistical models.

```
platoon_forecast(
  data_path  = "<file_id or path>",   # CSV: date, product_id, units_sold
  product_id = "PROD-1004",            # omit to auto-pick highest volume
  horizon    = 14,                     # days ahead
  method     = "auto",                 # auto | moving_average | exponential_smoothing | croston | arima | ets | theta
  holdout    = 30                      # days held out for MAE accuracy eval
)
→ {status, product_id, method, forecast, lower_bound, upper_bound,
   predicted_sum, predicted_mean, trend, seasonality_detected, mae}
```

**Auto-selection logic:**
- Regular demand (few zeros) → AutoETS / AutoARIMA / AutoTheta (statsforecast)
- Intermittent demand (>40% zeros) → Croston's method
- Fallback → exponential smoothing or moving average (zero-dependency)

**Key fields:**
- `forecast` — daily point predictions
- `lower_bound` / `upper_bound` — 95% confidence interval
- `predicted_sum` — total units over the horizon
- `trend` — "up", "down", or "stable"
- `seasonality_detected` — true if weekly pattern found
- `mae` — mean absolute error on holdout (only when holdout > 0)

### platoon_optimize

Compute EOQ, safety stock, reorder points, ABC classification, and stockout risk.

```
platoon_optimize(
  data_path      = "<file_id or path>",  # CSV: product_id, sku, cost, price, base_daily_demand, lead_time_days
  orders_path    = "<file_id or path>",  # optional, enables ABC classification
  inventory_path = "<file_id or path>",  # optional, enables stock levels + risk
  product_id     = "PROD-1004",          # omit for all products
  service_level  = 0.95
)
→ {status, product_count, abc_summary, results, alerts}
```

**Key fields per product:**
- `eoq` — Economic Order Quantity: √(2DS/H)
- `safety_stock` — buffer: z × σ × √(lead_time)
- `reorder_point` — (daily_demand × lead_time) + safety_stock
- `stockout_risk` — probability 0.0–1.0 via normal CDF
- `abc_class` — A (top 80% revenue), B (next 15%), C (bottom 5%)
- `daily_revenue`, `restock_cost`, `days_of_stock` — for business math

**Alerts** are products with >30% risk, sorted descending, with action recommendations.

### platoon_detect_anomalies

Detect demand spikes and drops using rolling-window statistics.

```
platoon_detect_anomalies(
  data_path  = "<file_id or path>",
  product_id = "PROD-1004",
  method     = "zscore",     # zscore or iqr
  window     = 30,
  threshold  = 2.5
)
→ {status, product_id, anomaly_count, anomaly_rate, anomalies}
```

Each anomaly includes date, value, expected, z_score, direction (spike/drop), severity (low/medium/high).

### platoon_basket_analysis

Find frequently-bought-together association rules from order data.

```
platoon_basket_analysis(
  orders_path    = "<file_id or path>",
  min_support    = 0.01,
  min_confidence = 0.3,
  max_rules      = 50
)
→ {status, total_orders, multi_item_orders, rule_count, rules}
```

Each rule has antecedent, consequent, support, confidence, lift. Lift > 1.0 means positive association.

### platoon_cashflow

Project daily revenue, COGS, and reorder costs over a planning horizon.

```
platoon_cashflow(
  data_path      = "<file_id or path>",
  demand_path    = "<file_id or path>",
  inventory_path = "<file_id or path>",
  horizon        = 30,
  product_id     = "PROD-1004"           # omit for all
)
→ {status, summary, reorder_events, periods}
```

Summary includes total_revenue, total_cogs, total_gross_profit, total_reorder_costs, net_cash.

### platoon_schedule

Assign staff to shifts based on demand signal and availability.

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

## Agent: commerce-analyst

The `commerce-analyst` agent schema (registered in the `schemas` table) wraps these tools with a system prompt that interprets raw results into business recommendations. It:

- Leads with the answer, not the data
- Shows math for derived metrics (days-to-stockout, ROI, revenue at risk)
- Uses tables for multi-product comparisons
- Flags uncertainty when confidence intervals are wide
- Recommends concrete actions with quantities and dollar amounts

Invoke via MCP:
```
ask_agent(agent_name="commerce-analyst", input_text="Which products should I reorder?")
```

## Decision Patterns

Most business questions require composing multiple tools:

### "Should I rush-order X?"
→ `forecast` (velocity) + `optimize` (stock + price) → days-until-stockout math

```
days_until_stockout = current_stock / predicted_mean
coverage_gap        = shipment_eta - days_until_stockout
revenue_at_risk     = coverage_gap × daily_revenue
```

### "Where does my $N budget go?"
→ `optimize` (alerts with daily_revenue + restock_cost) → ROI ranking + greedy allocation

```
ROI = (daily_revenue × lead_time_days) / restock_cost
```
Sort by absolute daily revenue, allocate greedily until budget exhausted.

### "Are my top products trending?"
→ `optimize` (ABC classification) + `forecast` × N → cross-reference trend with stock

### "What's our cash position?"
→ `cashflow` (revenue - COGS - reorders) → identify reorder spikes, size credit line

### "How should I staff?"
→ `schedule` (demand → shifts) → spot coverage gaps, size hires

## Worked Examples

### Forecasting

> "How many Bluetooth Speakers will we sell in the next two weeks?"

```
platoon_forecast(data_path="...", product_id="PROD-1004", horizon=14, holdout=0)
```

**Result:** ~331 units over 14 days (~24/day). Weekly cycle — peaks mid-week at ~29, dips to ~22 on weekends. Planning range: 137–525 total units (95% CI).

### Inventory Optimization

> "Which SKUs are most at risk of stocking out?"

```
platoon_optimize(
  data_path="...", orders_path="...", inventory_path="...", service_level=0.95
)
```

**Result:** 34 of 80 SKUs at elevated risk. ABC summary: 48 A-class, 20 B, 12 C. Zero-stock A-class items flagged as urgent with EOQ quantities.

### Rush-Order Decision (Compound)

> "Our webcam stock is getting low. Should I rush-order or wait 15 days?"

Agent calls both `forecast` and `optimize` in parallel:

```
current_stock           =    32 units
forecasted daily demand =   8.88 units/day
days_until_stockout     =  32 / 8.88  = 3.6 days
coverage gap            =  15 - 3.6   = 11.4 days without stock
revenue at risk         =  11.4 × $456 = ~$5,200
→ Rush-order 153 units (EOQ, cost $3,040)
```

### Budget Allocation

> "I have $10K for restocking. Where does it go?"

Agent ranks alerts by daily revenue impact, allocates greedily:

| # | Product | Units | Cost | Daily Rev | Why |
|---|---------|------:|-----:|----------:|-----|
| 1 | Bluetooth Speaker | 248 | $5,277 | $631 | Zero stock, 1.3× ROI |
| 2 | Webcam | 153 | $3,040 | $456 | Stocks out in 3.6 days |
| 3 | Keyboard (partial) | 68 | $1,683 | $159 | Remaining budget |

**Total: $10,000 → 469 units across 3 SKUs.** Speaker pays back in ~8.4 days.

### Anomaly Detection

> "Any unusual demand spikes for Bluetooth Speakers?"

```
platoon_detect_anomalies(data_path="...", product_id="PROD-1004")
```

**Result:** 12 anomalous days out of 731 (1.6%). All upward spikes. Medium-severity cluster in November/December — Black Friday and holiday effects, not data quality issues.

### Intermittent Demand

> "The Brocade Ring Purse sells sporadically. Can you forecast it?"

Auto-selects Croston's method (50% zero-demand days). Returns flat daily expected value of ~51 units. Agent explains flat ≠ bad — it's the expected value across sporadic bulk orders.

## Data Format

### Products CSV
```csv
product_id,sku,cost,price,base_daily_demand,lead_time_days
WIDGET-A,WA-001,8.50,24.99,50,7
```

### Daily Demand CSV
```csv
date,product_id,units_sold
2024-01-01,WIDGET-A,45
```

### Orders CSV (for ABC + basket analysis)
```csv
order_id,product_id,quantity,unit_price,order_date
ORD-001,WIDGET-A,3,24.99,2024-01-01
```

### Inventory CSV
```csv
product_id,current_stock
WIDGET-A,350
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
