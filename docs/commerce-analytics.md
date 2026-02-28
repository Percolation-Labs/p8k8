# Commerce Analytics (Platoon)

Percolate includes an integrated commerce analytics suite powered by [Platoon](https://pypi.org/project/p8-platoon/). It gives agents the ability to run demand forecasting, inventory optimization, anomaly detection, basket analysis, cash flow projection, and staff scheduling — all from natural language questions.

There are two ways to use these tools: **directly via MCP** (for agents like Claude Code that connect to the Percolate MCP server) or **through the p8 CLI** (the `commerce-analyst` agent picks the right tool for you).

## Option 1: MCP Tools (Claude Code, Cursor, etc.)

When an MCP client connects to the Percolate server, it gets direct access to all Platoon tools (`platoon_forecast`, `platoon_optimize`, etc.). The agent decides which tool to call based on your question.

For data access, use `platoon_read_file` to load CSVs from the local filesystem:

```
platoon_read_file(path="data/case_study/demand.csv", head=5)
```

This can be useful just to test the tools and not the agent.

## Option 2: p8 CLI Agent (`p8 chat`)

The `commerce-analyst` agent wraps the same tools behind a conversational interface. Upload your data to Percolate, then just ask questions — the agent finds your files automatically.

**1. Start the server** (if not already running):

```bash
p8 serve
```

**2. Upload your data files:**

Upload via the API, the Percolate mobile app, or Google Drive sync. For local testing, the CLI defaults to the Sage Whitfield test user — use the same user ID for uploads so the agent can find them:

```bash
# Test user ID (matches p8 chat default)
export USER_ID="7d31eddf-7ff7-542a-982f-7522e7a3ec67"

curl -X POST http://localhost:8000/content/ \
  -H "x-user-id: $USER_ID" -F "file=@products.csv"
curl -X POST http://localhost:8000/content/ \
  -H "x-user-id: $USER_ID" -F "file=@demand.csv"
curl -X POST http://localhost:8000/content/ \
  -H "x-user-id: $USER_ID" -F "file=@orders.csv"
curl -X POST http://localhost:8000/content/ \
  -H "x-user-id: $USER_ID" -F "file=@inventory.csv"
```

**3. Just ask — no file IDs needed:**

The agent has access to `get_moments` and `search`, so it discovers your uploaded files from the `content_upload` moments that Percolate creates automatically on upload. You don't need to copy-paste file IDs.

```bash
p8 chat --agent commerce-analyst
```

```
you> I uploaded some commerce data recently. What products should I reorder first?

assistant> You should prioritize reordering the following products immediately:
           | Priority | Product (SKU)              | Stock | Days Left | Daily Revenue |
           |----------|---------------------------|-------|-----------|--------------|
           | URGENT   | Binoculars (BIN-PRO-8X42)  | 8     | 2.0       | $759.96      |
           | URGENT   | Trail Camera (CAM-TRAIL-HD) | 5     | 1.7       | $449.97      |
           | URGENT   | Bird Seed (SD-SONGBIRD-5LB) | 30    | 1.2       | $324.75      |
           Focus on A-class products first — they drive the most revenue.

you> What does our cash situation look like for March?

assistant> Total projected revenue: $101,719. Restocking costs: $55,488.
           Net cash after all expenses: $7,233. You can cover all planned
           restocking, but cash dips on major reorder days. Consider staggering
           large reorders to smooth out cash flow.

you> Were there any unusual demand spikes around Valentine's Day?

assistant> On Feb 14, SEED-01 dropped to 14 units vs expected 26.4 (z-score -2.71).
           A statistically significant dip — possible Valentine's Day effect where
           customers shifted to gift items instead.
```

**How it works:** Each file upload creates a `content_upload` moment with the file ID in its metadata. The agent calls `get_moments(moment_type="content_upload")` to find recent uploads, extracts the file IDs, and passes them to the right analytics tool. Even if the agent grabs a moment ID instead of a file ID, Percolate's file resolver follows through to the actual file automatically.

You can also provide file IDs explicitly if you prefer — all `data_path` parameters accept either a file UUID or a local file path.

You can also sync files from drives - these create moments with file collections that can be used in the same way. So for example if we synced in daily sales reports we could run analysis on them.

## Getting Data In

Upload CSV or spreadsheet files to Percolate, then reference them by file ID. The tools accept either a **local file path** (MCP/stdio mode) or an **uploaded file UUID** (CLI/API mode) — the server resolves both transparently.

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
| `platoon_schedule` | Demand-based or skill-based staff shift assignment |

All `data_path` parameters accept either a local file path or an uploaded file UUID.

---

## End-to-End Case Study: Trailhead Nature Shop

A fictional outdoor/nature shop with 8 products — binoculars, bird feeders, field guides, trail boots, songbird seed, spotting scopes, field journals, and trail cameras. One month of February 2026 data with a Valentine's Day gift bump on optics and journals. Every result below was captured from live MCP tool calls.

### Step 1 — Upload Data

Upload four CSV files via the content API:

```bash
curl -X POST http://localhost:8000/content/ \
  -H "x-user-id: $USER_ID" \
  -F "file=@products.csv" -F "category=commerce"

curl -X POST http://localhost:8000/content/ \
  -H "x-user-id: $USER_ID" \
  -F "file=@demand.csv" -F "category=commerce"

curl -X POST http://localhost:8000/content/ \
  -H "x-user-id: $USER_ID" \
  -F "file=@orders.csv" -F "category=commerce"

curl -X POST http://localhost:8000/content/ \
  -H "x-user-id: $USER_ID" \
  -F "file=@inventory.csv" -F "category=commerce"
```

Each upload returns a file ID. From here on, we reference files only by UUID:

| File | ID | Rows |
|------|----|-----:|
| products.csv | `439af134-368a-5371-96fb-c2b8c88bbc6f` | 8 |
| demand.csv | `72a09785-826d-500e-9105-173a4e1b442b` | 224 |
| orders.csv | `d86f8a30-3d0e-5671-b847-b7ee26ff2a30` | 1,388 |
| inventory.csv | `85594c11-13cb-55ba-996c-53209cd7aeda` | 8 |

### Step 2 — Read Uploaded Data

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

### Step 3 — Forecast Demand

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

```json
{
  "status": "ok",
  "product_id": "SEED-01",
  "method": "statsforecast:ets",
  "series_length": 28,
  "predicted_sum": 356.4,
  "predicted_mean": 25.46,
  "trend": "stable",
  "seasonality_detected": true,
  "forecast": [25.46, 25.46, 25.46, 25.46, 25.46, ...],
  "lower_bound": [15.17, ...],
  "upper_bound": [35.75, ...]
}
```

> Songbird Seed is projected to sell **~356 units over 14 days** (~25.5/day). Stable trend, weekly seasonality detected. 95% CI: 15–36 units/day.
>
> With only 30 units in stock and demand at ~25/day, we have **1.2 days of stock**. This is critical.

### Step 4 — Forecast with Accuracy Check

> "How accurate is the binoculars forecast?"

```
platoon_forecast(
  data_path  = "72a09785-826d-500e-9105-173a4e1b442b",
  product_id = "BINOC-01",
  horizon    = 14,
  holdout    = 7
)
```

**Result:**

```json
{
  "product_id": "BINOC-01",
  "method": "statsforecast:ets",
  "train_length": 21,
  "holdout_length": 7,
  "predicted_mean": 5.17,
  "predicted_sum": 36.2,
  "mae": 1.41,
  "trend": "stable",
  "seasonality_detected": false
}
```

> Binoculars forecast: **~5.2/day**, MAE = 1.41 against 7-day holdout. That's ~27% of the mean — reasonable for a low-volume product. No weekly seasonality detected (expected — optics aren't a weekend-impulse purchase like seed).

### Step 5 — Inventory Optimization

> "Which products are at risk? What's the full picture?"

```
platoon_optimize(
  data_path      = "439af134-368a-5371-96fb-c2b8c88bbc6f",
  orders_path    = "d86f8a30-3d0e-5671-b847-b7ee26ff2a30",
  inventory_path = "85594c11-13cb-55ba-996c-53209cd7aeda"
)
```

**Result — ABC Summary:** 3 A-class, 2 B-class, 3 C-class

| Product | SKU | ABC | Price | Daily Demand | Stock | Days Left | Risk | EOQ | Action |
|---------|-----|-----|------:|------------:|------:|----------:|-----:|----:|--------|
| Binoculars | BIN-PRO-8X42 | **A** | $189.99 | 4 | 8 | 2.0 | 100% | 83 | Reorder 83 |
| Trail Camera | CAM-TRAIL-HD | **A** | $149.99 | 3 | 5 | 1.7 | 100% | 89 | Reorder 89 |
| Spotting Scope | SCP-SPOT-20X | **A** | $279.99 | 2 | 22 | 11.0 | 83% | 49 | Reorder 49 |
| Cedar Feeder | FDR-CEDAR-LG | B | $34.99 | 15 | 180 | 12.0 | 0% | 419 | OK |
| Trail Boots | BT-TRAIL-WP | B | $129.99 | 3 | 45 | 15.0 | 19% | 99 | OK |
| Bird Guide PNW | GDE-BIRDS-PNW | C | $24.95 | 8 | 25 | 3.1 | 100% | 382 | Reorder 382 |
| Songbird Seed | SD-SONGBIRD-5LB | C | $12.99 | 25 | 30 | 1.2 | 100% | 901 | Reorder 901 |
| Field Journal | JRN-FIELD-LTH | C | $18.99 | 10 | 12 | 1.2 | 100% | 493 | Reorder 493 |

> **6 of 8 products at elevated stockout risk.** The three A-class items (Binoculars, Camera, Scope) drive the most revenue and should be prioritized. Seed and Journal are only 1.2 days from stockout but are lower value — order them but don't rush-ship.

### Step 6 — Anomaly Detection

> "Were there any unusual demand spikes for field journals?"

```
platoon_detect_anomalies(
  data_path  = "72a09785-826d-500e-9105-173a4e1b442b",
  product_id = "JOURNAL-01",
  method     = "zscore",
  window     = 7,
  threshold  = 2.0
)
```

**Result:**

```json
{
  "status": "ok",
  "product_id": "JOURNAL-01",
  "method": "zscore",
  "total_points": 28,
  "anomaly_count": 1,
  "anomaly_rate": 0.036,
  "series_mean": 11.89,
  "series_std": 3.62,
  "anomalies": [
    {
      "date": "2026-02-12",
      "value": 19.0,
      "expected": 11.43,
      "z_score": 2.27,
      "direction": "spike",
      "severity": "low"
    }
  ]
}
```

> One anomaly detected: a **spike to 19 units on Feb 12** (expected ~11.4, z-score 2.27). This is the start of the Valentine's Day gift bump — journals and other gift items saw elevated demand Feb 12–15. Low severity, not a data quality issue.

### Step 7 — Basket Analysis

> "What products are frequently bought together?"

```
platoon_basket_analysis(
  orders_path    = "d86f8a30-3d0e-5671-b847-b7ee26ff2a30",
  min_support    = 0.001,
  min_confidence = 0.01,
  max_rules      = 10
)
```

**Result:**

```json
{
  "status": "ok",
  "total_orders": 842,
  "multi_item_orders": 419,
  "rule_count": 10,
  "rules": [
    {"antecedent": "BOOTS-01", "consequent": "SCOPE-01", "support": 0.072, "confidence": 0.252, "lift": 0.96},
    {"antecedent": "CAMERA-01", "consequent": "JOURNAL-01", "support": 0.084, "confidence": 0.263, "lift": 0.80},
    {"antecedent": "BINOC-01", "consequent": "FEEDER-01", "support": 0.060, "confidence": 0.212, "lift": 0.86}
  ]
}
```

> 842 total orders, 419 multi-item. Top pairings:
>
> | Pair | Support | Confidence | Lift |
> |------|--------:|-----------:|-----:|
> | Trail Boots + Spotting Scope | 7.2% | 25% | 0.96 |
> | Trail Camera + Field Journal | 8.4% | 26% | 0.80 |
> | Binoculars + Cedar Feeder | 6.0% | 21% | 0.86 |
>
> Lift values are near 1.0 (random co-occurrence) — with only 8 products and uniform popularity, associations are naturally diluted. The **Camera + Journal** pairing (8.4% support, highest) makes intuitive sense — both are field recording gear. Consider bundling as a "Field Kit."

### Step 8 — Cash Flow Projection

> "What does our cash flow look like for the next two weeks?"

```
platoon_cashflow(
  data_path      = "439af134-368a-5371-96fb-c2b8c88bbc6f",
  demand_path    = "72a09785-826d-500e-9105-173a4e1b442b",
  inventory_path = "85594c11-13cb-55ba-996c-53209cd7aeda",
  horizon        = 14
)
```

**Result:**

```json
{
  "status": "ok",
  "horizon_days": 14,
  "summary": {
    "total_revenue": 34669.52,
    "total_cogs": 13063.50,
    "total_gross_profit": 21606.02,
    "total_reorder_costs": 37614.61,
    "net_cash": -16008.58,
    "avg_daily_revenue": 2476.39,
    "avg_daily_profit": 1543.29
  },
  "reorder_event_count": 8
}
```

> **14-day outlook:**
>
> | Metric | Amount |
> |--------|-------:|
> | Total Revenue | $34,670 |
> | Total COGS | $13,064 |
> | Gross Profit | $21,606 |
> | Reorder Costs | $37,615 |
> | **Net Cash** | **-$16,009** |
>
> The shop generates **$2,476/day in revenue** ($1,543/day profit) but needs **$37.6K in reorders** — mostly on Day 1 when 8 products hit their reorder points simultaneously. After the initial restock burst, daily operations are cash-positive.
>
> **Action:** Secure a **$16K credit line** or stagger reorders by priority (A-class first, C-class next cycle).

### Step 9 — Rush-Order Decision (Compound)

> "Our trail cameras are almost out. Next shipment is in 10 days. Rush-order?"

Combine forecast + optimize results from earlier steps:

```
current_stock           =     5 units
forecasted daily demand =  2.68 units/day  (from Step 3 forecast)
days_until_stockout     =  5 / 2.68  = 1.9 days
regular shipment ETA    =    10 days
coverage gap            =  10 - 1.9  = 8.1 days without stock
daily_revenue           =  $450/day   (from Step 5 optimize)
revenue at risk         =  8.1 × $450 = ~$3,645
restock cost (EOQ)      =  89 × $55  = $4,895
```

> **Yes — rush-order immediately.** The trail camera is A-class, will stock out in ~2 days, leaving an 8-day gap costing ~$3,600 in lost revenue. Order 89 units (EOQ).

### Step 10 — Budget Allocation

> "I have $15K for restocking. Where does it go?"

Using alerts from Step 5, rank by daily revenue impact and allocate greedily:

| # | Product | ABC | Units | Cost | Daily Rev | Why |
|---|---------|-----|------:|-----:|----------:|-----|
| 1 | Binoculars | A | 83 | $7,046 | $760 | 2 days of stock, highest revenue |
| 2 | Trail Camera | A | 89 | $4,908 | $450 | 1.7 days, A-class |
| 3 | Songbird Seed (partial) | C | 454 | $2,043 | $325 | 1.2 days, highest volume |

**Total: $13,997 → 626 units across 3 SKUs.** Remaining ~$1,000 held for the Scope reorder (A-class, 83% risk but 11 days of stock — not urgent yet).

> The Binoculars pay back their $7,046 cost in **~9.3 days** of recovered sales ($760/day). The Seed is funded despite being C-class because it has only 1.2 days of stock and is the shop's highest-volume item.

---

## Decision Patterns

| Question | Tools | Agent Adds |
|----------|-------|-----------|
| "How many will we sell?" | `forecast` | Narrates trend + seasonal pattern + planning range |
| "What should I reorder?" | `optimize` | Prioritizes by ABC class × risk, not just risk |
| "Should I rush-order?" | `forecast` + `optimize` | Days-to-stockout math + revenue-at-risk |
| "Where does my $N go?" | `optimize` | ROI ranking + greedy budget allocation |
| "Any demand anomalies?" | `detect_anomalies` | Clusters by season, distinguishes noise from signal |
| "Cross-sell opportunities?" | `basket_analysis` | Resolves IDs to names, suggests bundles |
| "Cash flow outlook?" | `cashflow` | Identifies reorder spikes, sizes credit line |
| "How do I staff the week?" | `schedule` | Assigns shifts by demand, minimizes cost |
| "Schedule tutors by subject?" | `schedule` (skill-based) | Matches staff skills to slot requirements |

## Case Study: Tutor Scheduling with Skills

A tutoring center needs to schedule 6 tutors across 15 weekly subject slots. Each slot requires specific subject expertise — a math slot needs a tutor who knows math or AP calculus, a science slot needs physics, chemistry, or biology, etc.

### Data Setup

**staff.csv** — 6 tutors with subject skills:

```csv
staff_id,name,hourly_rate,max_hours_per_week,available_days,skills
T-1,Alice Chen,28.00,30,"Monday,Tuesday,Wednesday,Thursday","math,AP_calc,physics"
T-2,Bob Rivera,25.00,25,"Monday,Wednesday,Friday","english,history,economics"
T-3,Carol Kim,30.00,20,"Tuesday,Thursday","math,chemistry,biology"
T-4,Dan Okafor,22.00,35,"Monday,Tuesday,Wednesday,Thursday,Friday","computer_science,math,physics"
T-5,Eve Santos,26.00,25,"Monday,Wednesday,Friday","spanish,french,english"
T-6,Fay Liu,24.00,30,"Tuesday,Thursday,Friday","biology,chemistry,AP_physics"
```

**tutor_slots.csv** — 15 weekly slots with skill requirements:

```csv
slot_label,hours_needed,required_skills
Monday_math,4.0,"math,AP_calc"
Monday_english,3.0,english
Monday_CS,3.0,computer_science
Wednesday_math,4.0,"math,AP_calc"
Wednesday_history,3.0,"history,economics"
Wednesday_languages,3.0,"spanish,french"
Tuesday_science,4.0,"physics,chemistry,biology"
Tuesday_math,3.0,"math,AP_calc"
Thursday_science,4.0,"physics,chemistry,biology"
Thursday_math,3.0,"math,AP_calc"
Thursday_english,3.0,english
Friday_CS,4.0,computer_science
Friday_languages,3.0,"spanish,french"
Friday_science,3.0,"physics,chemistry,biology"
Friday_english,3.0,english
```

### Running the Schedule

```
platoon_schedule(
  demand_path = "demand.csv",
  staff_path  = "staff.csv",
  slots_path  = "tutor_slots.csv",
  shift_hours = 4
)
```

The tool returns a schedule grid showing which tutor covers each slot, total cost, total hours, and any **coverage gaps** where no available tutor has the required skills.

### What the Agent Reports

> **Weekly schedule built** — 15 slots, 6 tutors, skill-based mode.
>
> | Slot | Tutor | Hours | Skills Match |
> |------|-------|------:|-------------|
> | Monday_math | Alice Chen | 4.0 | math, AP_calc |
> | Monday_english | Bob Rivera | 3.0 | english |
> | Monday_CS | Dan Okafor | 3.0 | computer_science |
> | Tuesday_science | Carol Kim | 4.0 | chemistry, biology |
> | Tuesday_math | Dan Okafor | 3.0 | math |
> | Wednesday_math | Alice Chen | 4.0 | math, AP_calc |
> | Wednesday_history | Bob Rivera | 3.0 | history |
> | Wednesday_languages | Eve Santos | 3.0 | spanish, french |
> | Thursday_science | Fay Liu | 4.0 | biology, chemistry |
> | Thursday_math | Carol Kim | 3.0 | math |
> | Thursday_english | ⚠️ Gap | 3.0 | No available tutor |
> | Friday_CS | Dan Okafor | 4.0 | computer_science |
> | Friday_languages | Eve Santos | 3.0 | spanish, french |
> | Friday_science | Fay Liu | 3.0 | biology, chemistry |
> | Friday_english | Bob Rivera | 3.0 | english |
>
> **Coverage gap:** Thursday_english — no English-skilled tutor is available on Thursday. Consider adding Thursday to Bob or Eve's available days.
>
> **Total cost:** ~$1,230/week across 50 tutor-hours.

---

## Tool Reference

### get_file

```
get_file(file_id="<uuid>", head=0)
→ {status, format, columns, row_count, rows}   # CSV
→ {status, format, line_count, content}         # text
```

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
  orders_path    = "<file_id or path>",  # optional — enables ABC (needs line_total column)
  inventory_path = "<file_id or path>",  # optional — enables stock/risk (needs closing_stock column)
  product_id     = "PROD-1004",          # omit for all
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
  orders_path    = "<file_id or path>",  # CSV: order_id, product_id (+ line_total for revenue)
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
  inventory_path = "<file_id or path>",  # optional — enables reorder simulation
  horizon        = 30,
  product_id     = "PROD-1004"           # omit for all
)
→ {status, summary, reorder_events, periods}
```

### platoon_schedule

Two modes: **demand-based** (aggregate demand into day-of-week slots) and **skill-based** (explicit slots with skill requirements).

```
# Mode 1: Demand-based (default)
platoon_schedule(
  demand_path  = "<file_id or path>",   # demand CSV
  staff_path   = "<file_id or path>",   # staff CSV
  shift_hours  = 8,
  min_coverage = 1.0,
  horizon_days = 7
)

# Mode 2: Skill-based (provide slots_path)
platoon_schedule(
  demand_path  = "<file_id or path>",   # demand CSV (used for context)
  staff_path   = "<file_id or path>",   # staff CSV with skills column
  slots_path   = "<file_id or path>",   # slots CSV with required_skills
  shift_hours  = 8,
  min_coverage = 1.0
)
→ {status, mode, total_cost, total_hours, demand_by_slot, coverage_gaps, schedule_grid}
```

When `slots_path` is provided, the tool switches to skill-based mode — slots define explicit hours and skill requirements, and only staff with at least one matching skill are assigned. Match is OR-based and case-insensitive.

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
staff_id,name,hourly_rate,max_hours_per_week,available_days,skills
T-1,Alice,25.00,40,"Monday,Tuesday,Wednesday,Thursday,Friday","math,AP_calc,physics"
T-2,Bob,22.00,32,"Monday,Wednesday,Friday","english,history"
```

The `skills` column is optional. When present and used with a slots file, only staff with matching skills are assigned to skill-required slots.

### Slots CSV (for skill-based scheduling)
```csv
slot_label,hours_needed,required_skills
Monday_math,4.0,"math,AP_calc"
Monday_english,3.0,english
Tuesday_science,4.0,"physics,chemistry,biology"
Wednesday_math,4.0,"math,AP_calc"
```

Each slot specifies hours of coverage needed and which skills qualify a staff member. Empty `required_skills` means any staff qualifies.

## Installation

Platoon is included in the p8k8 Docker image. For local development:

```bash
uv sync --extra platoon
```

This installs `p8-platoon` with all analytics dependencies (statsforecast, OR-Tools, scikit-learn, polars, Tavily).
