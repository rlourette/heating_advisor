# Heating Advisor — Fairport, NY

Helps decide whether to run an inverter heat pump or a natural gas boiler on any given day, based on real energy costs and outdoor temperature. Built specifically for a Fairport, NY home served by **Fairport Electric** (municipal utility) in **NYISO Zone C (CENTRL)**.

## The Core Idea

The decision is not about grid prices — it's about delivered heat cost. At any outdoor temperature, you are comparing:

```
Electric heat cost  =  retail_rate ($/kWh) ÷ COP(temp)
Gas heat cost       =  gas_price ($/therm) ÷ (29.31 kWh/therm × AFUE)
```

The **heat pump's COP drops as it gets colder outside**. That means there is a **breakeven temperature** — the outdoor °F where both systems cost the same per unit of heat. Above it, run the heat pump. Below it, run the gas boiler.

> With Fairport Electric's actual blended rate (~$0.055–$0.065/kWh), the LG LGRED COP curve, and National Fuel Gas (~$0.92/therm), the breakeven lands around **10–15°F**. Below that temperature, gas is cheaper. Above it — which covers the vast majority of the heating season in Fairport — the heat pump wins.

NYISO real-time and day-ahead LMP prices are fetched as an informational grid-stress signal, not as the primary decision driver. Fairport Electric customers pay a fixed retail rate regardless of what the wholesale grid is doing.

---

## Modes

### 1. Retrospective Analysis
Pulls NYISO Real-Time 5-min LMP and Open-Meteo historical temperatures over a configurable date range. For each day, computes the average outdoor temperature, interpolates the heat pump COP, calculates both delivered-heat costs, and prints a recommendation with a CSV export.

### 2. Tomorrow's Forecast
Pulls the NYISO **Day-Ahead hourly LMP** (published the evening before delivery) and the Open-Meteo **48-hour temperature forecast**. Produces an hour-by-hour table and a single daily recommendation. Best run each evening after ~7 PM when NYISO posts next-day prices.

---

## Setup

**Python 3.10+ required** (uses `float | None` type union syntax).

```bash
pip install gridstatus pandas requests
```

No API keys needed. Open-Meteo is free and unauthenticated. NYISO data is public.

---

## Configuration

All tunable values are at the top of `heating_advisor.py`. Update these before running:

| Variable | Recommended value | Notes |
|---|---|---|
| `ELECTRIC_RETAIL_RATE_PER_KWH` | `0.060` | See rate structure below — use blended rate from actual bill |
| `GAS_PRICE_PER_THERM` | `0.92` | National Fuel Gas bill — all-in supply + delivery |
| `BOILER_AFUE` | `0.82` | Boiler nameplate or installation manual |
| `COP_CURVE` | LG LGRED (Hyper Heat) data | Verify against spec sheet at lg-dfs.com if model number is known |
| `HIST_START` / `HIST_END` | Jan–Apr 2026 | Adjust for any date range |

### Fairport Electric rate structure (effective December 1, 2025)

Fairport Electric bills customers in two parts: a fixed **base rate** plus a variable **Purchased Power Adjustment Clause (PPAC)** adder. The PPAC is the pass-through cost of incremental power purchases above Fairport's hydro allocation, and it fluctuates — it rises in cold winters when more expensive supplemental power is needed, and is lower in mild months.

**Base rate (from the published December 2025 rate sheet):**
- Customer charge: $5.13/month
- Energy charge: $0.0448/kWh for first 1,000 kWh (winter and non-winter)
- Energy charge: $0.0673/kWh for usage **over** 1,000 kWh in winter (December–March)

**PPAC adder:** Historically adds roughly $0.01–$0.02/kWh in normal conditions, higher during cold snaps. The current PPAC statement is public — Fairport Electric links to it from their FAQ page at [village.fairport.ny.us](https://www.village.fairport.ny.us/departments/electric_department/faqs.php), which routes to the NY PSC filing at dps.ny.gov.

**Best approach for `ELECTRIC_RETAIL_RATE_PER_KWH`:** Take a recent winter bill, divide total charges (including PPAC, customer charge pro-rated) by total kWh. This blended number is what the script needs. If your son's winter usage is typically over 1,000 kWh/month, use a value closer to `0.070` to account for the higher tier.

The `COP_CURVE` is a list of `(outdoor_temp_°F, COP)` pairs. The script interpolates linearly between points. The current curve is based on LG LGRED (Hyper Heat) published data:

| Outdoor °F | COP |
|---|---|
| -13 | 1.3 |
| 5 | 2.0 |
| 17 | 2.5 |
| 27 | 3.1 |
| 35 | 3.6 |
| 47 | 4.2 |
| 60 | 4.5 |

The 5°F and 17°F values are adjusted down ~0.1 from LG's published figures to account for the drain pan heater (~120W) that runs continuously below 32°F but is excluded from LG's low-temperature test submissions. For comparison, the older Mitsubishi H2i curve used in the first version of this script had COP ~1.9 at 17°F and ~1.6 at 5°F — the LG LGRED is meaningfully better at the temperatures that matter most for this decision.

To get the most accurate curve, look up the exact model number on the unit's nameplate and download the submittal sheet from [lg-dfs.com](https://www.lg-dfs.com) or check the [NEEP cold-climate heat pump database](https://ashp.neep.org/).

---

## Usage

```bash
python heating_advisor.py
```

The script first prints a **configuration summary** including the computed breakeven temperature and a COP table, then prompts:

```
Select mode:
  1) Retrospective analysis (Jan–Apr 2026)
  2) Tomorrow's forecast + recommendation
  3) Both

Enter 1, 2, or 3 [default=3]:
```

### Example retrospective output

```
Date         AvgTemp   COP  Elec¢/kWh-h  Gas¢/kWh-h   AvgLMP   MaxLMP  Recommendation
------------ -------- ----- ------------ -----------  -------- --------  ---------------
2026-01-15      18.3  2.53         2.37        3.83      62.1    104.3  🟢 ELECTRIC
2026-01-28      34.7  3.58         1.68        3.83      44.8     71.2  🟢 ELECTRIC
2026-02-04       8.2  2.14         2.80        3.83      58.3     88.6  🟢 ELECTRIC [⚠ moderate grid]
2026-01-22      -2.1  1.43         4.20        3.83      71.4    118.7  🔴 GAS [⚡ grid stress]
```

### Example forecast output

```
Hour    Temp°F   COP  Elec¢/kWh-h  Gas¢/kWh-h   DA-LMP  Recommendation
------ -------- ----- ------------ -----------  --------  ---------------
00:00     28.4  3.14         1.91        3.83      51.2  🟢 ELECTRIC
06:00     21.0  2.63         2.28        3.83      68.4  🟢 ELECTRIC
12:00     35.2  3.62         1.66        3.83      43.1  🟢 ELECTRIC
18:00      4.1  2.06         2.91        3.83      55.7  🟢 ELECTRIC
```

---

## How the Math Works

**Electric delivered-heat cost:**
```
cost_e = retail_rate / COP(T)
```
A heat pump at 30°F with COP 3.1 (LG LGRED) and a $0.060/kWh blended rate delivers heat at $0.0194/kWh-heat.

**Gas delivered-heat cost:**
```
cost_g = gas_price / (29.31 kWh/therm × AFUE)
```
At $0.92/therm and 82% AFUE, that's $0.0383/kWh-heat — fixed, regardless of temperature.

**Breakeven temperature:**
Solve `retail_rate / COP(T) = cost_g` for T. The script walks the COP curve to find this crossing point and reports it on startup.

**LMP stress flags** (informational only):
- `[⚠ moderate grid]` — Day-Ahead LMP > $85/MWh
- `[⚡ grid stress]` — Day-Ahead LMP > $110/MWh

These don't change the recommendation but are worth noting. A high-LMP event could prompt conservation regardless of heating source.

---

## Output Files

Retrospective mode saves a CSV to the working directory:

```
heating_retrospective_YYYYMMDD_HHMM.csv
```

Columns: `Date, Avg_Temp_F, COP, Elec_c_kwh_heat, Gas_c_kwh_heat, Avg_LMP, Max_LMP, Rec`

---

## Data Sources

| Source | Data | Cost |
|---|---|---|
| [NYISO via gridstatus](https://github.com/gridstatus/gridstatus) | Real-time and day-ahead LMP, Zone C | Free |
| [Open-Meteo Forecast API](https://open-meteo.com/) | Hourly temperature forecast | Free, no key |
| [Open-Meteo Archive API](https://archive-api.open-meteo.com/) | Historical hourly temperature | Free, no key |

---

## Limitations

- **Fairport Electric rate is assumed flat.** The script uses a single blended rate. In reality, winter usage over 1,000 kWh/month is billed at a higher tier ($0.0673/kWh base vs $0.0448). Heavy electric-heat users may want to model a higher effective rate for peak winter months. If the utility ever moves to time-of-use pricing, the cost model would need to become hour-aware on the electric side.
- **COP curve is a model, not a measurement.** The LG LGRED curve is based on published data with a pan heater adjustment applied below 32°F. Actual efficiency also depends on installation quality, refrigerant charge, duct/coil condition, and defrost cycling frequency. If the exact model number is available, replace `COP_CURVE` with data from the unit's submittal sheet for best accuracy.
- **Day-ahead LMP is not available before ~7 PM the prior day.** Running the forecast mode in the morning will show `N/A` for LMP; recommendations will still be made from temperature alone.
- **Boiler startup costs and thermal lag** are not modeled. Switching sources mid-day has a real-world friction cost not captured here.
