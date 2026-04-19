"""
heating_advisor.py
==================
Helps determine whether to use an inverter heat pump or natural gas boiler
baseboard heat on any given day in Fairport, NY (NYISO Zone C - CENTRL).

Two operating modes:
  1. RETROSPECTIVE  -- Analyze a historical date range (how did we do?)
  2. FORECAST       -- Pull NYISO Day-Ahead prices + weather forecast for
                       tomorrow and give an hour-by-hour recommendation.

Key design decisions
--------------------
* Fairport has a MUNICIPAL electric utility (Fairport Electric). Customers
  pay a fixed retail rate, NOT real-time LMP.  The LMP is used only as a
  congestion/stress signal and a rough upper-bound proxy.

* Fairport Electric has a two-tier winter rate (December–March).  Usage up
  to 1,000 kWh/month is billed at the Tier 1 base rate; usage above that
  is billed at the higher Tier 2 rate.  If a Sense Energy Monitor is
  configured, the program reads current-month kWh automatically and applies
  the correct marginal rate.  Without Sense, it falls back to the flat
  ELECTRIC_RETAIL_RATE_PER_KWH value.

* Heat-pump COP degrades with outdoor temperature.  The COP_CURVE is modeled
  on LG LGRED (Hyper Heat) published data, adjusted down ~0.1 below 32°F to
  account for drain pan heater draw that LG omits from their test submissions.
  If the exact model number is known, pull the submittal sheet from lg-dfs.com
  and replace the curve points below.

* Natural gas cost is in $/therm.  Rochester-area National Fuel Gas rates
  are typically $0.80 - $1.10/therm for residential supply + delivery.

Dependencies
------------
    pip install gridstatus pandas requests sense-energy
"""

import warnings
warnings.filterwarnings("ignore", category=FutureWarning)

from datetime import datetime, timedelta, date

import requests
import pandas as pd
import gridstatus

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION  ← edit these
# ─────────────────────────────────────────────────────────────────────────────

# ── Sense Energy Monitor (optional) ──────────────────────────────────────────
# Leave SENSE_EMAIL blank to disable.  When configured, the program fetches
# current-month kWh from Sense and uses the correct billing tier automatically.
# It will also report whether the LG heat pump is currently running.
SENSE_EMAIL    = ""          # e.g. "user@example.com"
SENSE_PASSWORD = ""          # Sense account password

# ── Fairport Electric tiered rates (effective December 1, 2025) ──────────────
# Base rates before PPAC.  PPAC is added on top of whichever tier applies.
ELECTRIC_TIER1_RATE  = 0.0448   # $/kWh — first 1,000 kWh/month (all seasons)
ELECTRIC_TIER2_RATE  = 0.0673   # $/kWh — above 1,000 kWh in winter only
ELECTRIC_TIER1_LIMIT = 1000     # kWh/month threshold for tier 2
ELECTRIC_PPAC_EST    = 0.015    # $/kWh — PPAC adder estimate; check dps.ny.gov
                                 # for the current filed statement

# ── Fallback flat rate ────────────────────────────────────────────────────────
# Used when Sense is not configured, or as a sanity-check override.
# Best value: divide total winter bill (all charges) by kWh used.
ELECTRIC_RETAIL_RATE_PER_KWH = 0.060   # ← UPDATE from actual bill if no Sense

# ── Natural gas ───────────────────────────────────────────────────────────────
# National Fuel Gas (or local supplier) all-in rate, $/therm.
# Current NFG residential rate in Monroe County ≈ $0.85-$1.05/therm.
GAS_PRICE_PER_THERM = 0.92             # ← UPDATE from actual bill

# ── Boiler ────────────────────────────────────────────────────────────────────
# Annual Fuel Utilization Efficiency of the gas boiler (0–1).
# Older cast-iron boilers: ~0.80.  Mid-efficiency: ~0.85.  High-eff: ~0.92+
BOILER_AFUE = 0.82

# ── Heat pump COP curve ───────────────────────────────────────────────────────
# Piecewise-linear COP vs outdoor °F.
# Source: LG LGRED (Hyper Heat) published AHRI/NEEP data.
# Values at 5°F and 17°F adjusted down ~0.1 from published figures to account
# for drain pan heater draw (~120W) that LG omits from low-temp test submissions.
# Format: [(outdoor_temp_F, COP), ...] — must be sorted ascending.
COP_CURVE = [
    (-13, 1.3),
    (  5, 2.0),
    ( 17, 2.5),
    ( 27, 3.1),
    ( 35, 3.6),
    ( 47, 4.2),
    ( 60, 4.5),
]

# ── Location (Fairport, NY) ───────────────────────────────────────────────────
LATITUDE  = 43.1009
LONGITUDE = -77.4419

# ── LMP thresholds (informational, not used in cost calc) ────────────────────
LMP_MODERATE_THRESHOLD  =  85   # $/MWh — watch closely
LMP_HIGH_THRESHOLD      = 110   # $/MWh — strong grid stress signal

# ── Historical date range ─────────────────────────────────────────────────────
HIST_START = "2026-01-01"
HIST_END   = "2026-04-19"

# ── Winter months (tier 2 rate applies) ──────────────────────────────────────
WINTER_MONTHS = {12, 1, 2, 3}

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
BTU_PER_THERM = 100_000
KWH_PER_THERM = BTU_PER_THERM / 3_412  # ≈ 29.31 kWh of heat per therm


# ─────────────────────────────────────────────────────────────────────────────
# SENSE INTEGRATION
# ─────────────────────────────────────────────────────────────────────────────

def fetch_sense_data() -> dict | None:
    """
    Connect to the Sense Energy Monitor API and return a summary dict:
      {
        "monthly_kwh":    float,  # kWh used so far this billing month
        "active_devices": list,   # device names currently drawing power
        "heat_pump_on":   bool,   # True if an LG/heat pump device is active
        "total_watts":    float,  # whole-home watts right now
      }

    Returns None if Sense is not configured or the connection fails.
    Requires:  pip install sense-energy
    """
    if not SENSE_EMAIL or not SENSE_PASSWORD:
        return None

    try:
        from sense_energy import Senseable
    except ImportError:
        print("  ⚠  sense-energy not installed.  Run: pip install sense-energy")
        return None

    try:
        print("  Connecting to Sense Energy Monitor…", end="", flush=True)
        sense = Senseable()
        sense.authenticate(SENSE_EMAIL, SENSE_PASSWORD)
        sense.update_realtime()

        monthly_kwh    = sense.monthly_usage
        active_devices = sense.active_devices      # list of device name strings
        total_watts    = sense.active_power

        # Sense learns device names over time; heat pumps may appear as
        # "Heat Pump", "Mini Split", "LG", or a custom name the user set.
        heat_pump_keywords = {"heat pump", "mini split", "lg", "hvac"}
        heat_pump_on = any(
            any(kw in d.lower() for kw in heat_pump_keywords)
            for d in active_devices
        )

        print(f" connected.  Monthly usage: {monthly_kwh:.0f} kWh  |  "
              f"Now: {total_watts:.0f}W  |  "
              f"HP running: {'yes' if heat_pump_on else 'no'}")

        return {
            "monthly_kwh":    monthly_kwh,
            "active_devices": active_devices,
            "heat_pump_on":   heat_pump_on,
            "total_watts":    total_watts,
        }

    except Exception as exc:
        print(f"\n  ⚠  Sense connection failed: {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# RATE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def marginal_electric_rate(monthly_kwh: float | None, month: int) -> float:
    """
    Return the marginal electric rate ($/kWh) given:
      monthly_kwh -- kWh consumed so far this billing month (from Sense or None)
      month       -- calendar month (1–12)

    If monthly_kwh is None, falls back to ELECTRIC_RETAIL_RATE_PER_KWH.

    Tier 2 only applies in winter months (Dec–Mar) and only when the monthly
    accumulation has already crossed ELECTRIC_TIER1_LIMIT.
    """
    if monthly_kwh is None:
        return ELECTRIC_RETAIL_RATE_PER_KWH

    in_winter = month in WINTER_MONTHS
    base = ELECTRIC_TIER2_RATE if (in_winter and monthly_kwh >= ELECTRIC_TIER1_LIMIT) else ELECTRIC_TIER1_RATE
    return base + ELECTRIC_PPAC_EST


# ─────────────────────────────────────────────────────────────────────────────
# COST / BREAKEVEN HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def interpolate_cop(outdoor_f: float) -> float:
    """Return heat-pump COP for a given outdoor temperature (°F)."""
    curve = COP_CURVE
    if outdoor_f <= curve[0][0]:
        return curve[0][1]
    if outdoor_f >= curve[-1][0]:
        return curve[-1][1]
    for i in range(len(curve) - 1):
        t0, c0 = curve[i]
        t1, c1 = curve[i + 1]
        if t0 <= outdoor_f <= t1:
            frac = (outdoor_f - t0) / (t1 - t0)
            return c0 + frac * (c1 - c0)
    return curve[-1][1]


def cost_per_kwh_heat_electric(outdoor_f: float, rate: float | None = None) -> float:
    """
    Effective cost per kWh of *delivered heat* from the heat pump.
    cost = rate / COP(outdoor_f)
    Uses the provided rate, or ELECTRIC_RETAIL_RATE_PER_KWH if None.
    """
    rate = rate if rate is not None else ELECTRIC_RETAIL_RATE_PER_KWH
    return rate / interpolate_cop(outdoor_f)


def cost_per_kwh_heat_gas() -> float:
    """
    Effective cost per kWh of *delivered heat* from the gas boiler.
    1 therm = 29.31 kWh heat (at 100% efficiency).
    Divide by AFUE for actual delivered heat.
    cost = gas_price_per_therm / (kwh_per_therm * AFUE)
    """
    return GAS_PRICE_PER_THERM / (KWH_PER_THERM * BOILER_AFUE)


def breakeven_temp(rate: float | None = None) -> float:
    """
    Outdoor temperature (°F) at which electric and gas heat costs are equal.
    Below this temperature, gas is cheaper; above it, electric is cheaper.
    Uses the provided rate, or ELECTRIC_RETAIL_RATE_PER_KWH if None.
    """
    rate       = rate if rate is not None else ELECTRIC_RETAIL_RATE_PER_KWH
    gas_cost   = cost_per_kwh_heat_gas()
    target_cop = rate / gas_cost

    curve = COP_CURVE
    if target_cop <= curve[0][1]:
        return curve[0][0]
    if target_cop >= curve[-1][1]:
        return curve[-1][0]

    for i in range(len(curve) - 1):
        t0, c0 = curve[i]
        t1, c1 = curve[i + 1]
        if c0 <= target_cop <= c1:
            frac = (target_cop - c0) / (c1 - c0)
            return t0 + frac * (t1 - t0)

    return curve[-1][0]


def recommend(outdoor_f: float,
              lmp: float | None = None,
              rate: float | None = None) -> dict:
    """
    Return a recommendation dict for a single temperature/LMP/rate datapoint.
    Elec¢/kWh-h and Gas¢/kWh-h are cents per kWh of *delivered heat* —
    not electricity consumed — so the two columns are directly comparable.
    """
    rate      = rate if rate is not None else ELECTRIC_RETAIL_RATE_PER_KWH
    elec_cost = cost_per_kwh_heat_electric(outdoor_f, rate=rate)
    gas_cost  = cost_per_kwh_heat_gas()
    cop       = interpolate_cop(outdoor_f)
    savings   = elec_cost - gas_cost   # positive → gas cheaper

    if savings > 0.005:
        fuel, icon = "GAS", "🔴"
    elif savings < -0.005:
        fuel, icon = "ELECTRIC", "🟢"
    else:
        fuel, icon = "TOSS-UP", "🟡"

    lmp_flag = ""
    if lmp is not None:
        if lmp > LMP_HIGH_THRESHOLD:
            lmp_flag = " [⚡ grid stress]"
        elif lmp > LMP_MODERATE_THRESHOLD:
            lmp_flag = " [⚠ moderate grid]"

    return {
        "fuel":          fuel,
        "icon":          icon,
        "cop":           cop,
        "elec_cost_kwh": elec_cost,
        "gas_cost_kwh":  gas_cost,
        "savings":       savings,
        "rate":          rate,
        "lmp":           lmp,
        "lmp_flag":      lmp_flag,
    }


# ─────────────────────────────────────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────────────────────────────────────

def fetch_weather_forecast(days: int = 2) -> pd.DataFrame:
    """
    Fetch hourly temperature forecast from Open-Meteo (free, no API key).
    Returns a DataFrame with columns: [time, temp_f].
    """
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={LATITUDE}&longitude={LONGITUDE}"
        f"&hourly=temperature_2m"
        f"&temperature_unit=fahrenheit"
        f"&timezone=America/New_York"
        f"&forecast_days={days}"
    )
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return pd.DataFrame({
        "time":   pd.to_datetime(data["hourly"]["time"]),
        "temp_f": data["hourly"]["temperature_2m"],
    })


def fetch_day_ahead_lmp(nyiso_client, target_date: str) -> pd.DataFrame:
    """
    Fetch NYISO Day-Ahead hourly LMP for CENTRL on target_date.
    Day-ahead prices for tomorrow are posted by NYISO around 11 AM the prior day.
    """
    try:
        lmp    = nyiso_client.get_lmp(date=target_date, market="DAY_AHEAD_HOURLY")
        centrl = lmp[lmp["Location"] == "CENTRL"].copy()
        centrl = centrl.rename(columns={"LMP": "lmp_da", "Time": "time"})
        return centrl[["time", "lmp_da"]]
    except Exception as exc:
        print(f"  ⚠  Day-ahead LMP fetch failed: {exc}")
        return pd.DataFrame(columns=["time", "lmp_da"])


def fetch_historical_lmp(nyiso_client, start: str, end: str) -> pd.DataFrame:
    """Fetch NYISO Real-Time 5-min LMP for CENTRL over a date range."""
    lmp    = nyiso_client.get_lmp(date=start, end=end, market="REAL_TIME_5_MIN")
    centrl = lmp[lmp["Location"] == "CENTRL"].copy()
    centrl["Date"] = centrl["Time"].dt.date
    return centrl


# ─────────────────────────────────────────────────────────────────────────────
# DISPLAY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def print_header(title: str):
    print("\n" + "=" * 80)
    print(f"  {title}")
    print("=" * 80)


def print_config_summary(sense_data: dict | None = None):
    monthly_kwh   = sense_data["monthly_kwh"] if sense_data else None
    current_month = date.today().month
    live_rate     = marginal_electric_rate(monthly_kwh, current_month)
    be_temp       = breakeven_temp(rate=live_rate)
    gas_cost      = cost_per_kwh_heat_gas()

    in_winter = current_month in WINTER_MONTHS
    tier_num  = 2 if (monthly_kwh is not None and in_winter and
                      monthly_kwh >= ELECTRIC_TIER1_LIMIT) else 1

    print_header("CONFIGURATION SUMMARY")

    if sense_data:
        print(f"  Sense Energy Monitor  : connected ✅")
        print(f"  Month-to-date usage   : {monthly_kwh:.0f} kWh")
        print(f"  Heat pump running now : {'yes ✅' if sense_data['heat_pump_on'] else 'no'}")
        if sense_data["active_devices"]:
            print(f"  Active devices        : {', '.join(sense_data['active_devices'])}")
        print(f"  Billing tier active   : Tier {tier_num}  (${live_rate:.4f}/kWh effective)")
    else:
        status = "disabled — add credentials above to enable" if not SENSE_EMAIL else "connection failed"
        print(f"  Sense Energy Monitor  : {status}")
        print(f"  Using flat fallback rate: ${ELECTRIC_RETAIL_RATE_PER_KWH:.4f}/kWh")

    print()
    print(f"  Tier 1 base  (≤ {ELECTRIC_TIER1_LIMIT} kWh/mo, all months)  : ${ELECTRIC_TIER1_RATE:.4f}/kWh")
    print(f"  Tier 2 base  (> {ELECTRIC_TIER1_LIMIT} kWh/mo, Dec–Mar only): ${ELECTRIC_TIER2_RATE:.4f}/kWh")
    print(f"  PPAC estimate (added to both tiers)         : ${ELECTRIC_PPAC_EST:.4f}/kWh")
    print(f"  Effective marginal rate now                 : ${live_rate:.4f}/kWh")
    print()
    print(f"  Natural gas     : ${GAS_PRICE_PER_THERM:.3f}/therm")
    print(f"  Boiler AFUE     : {BOILER_AFUE*100:.0f}%")
    print(f"  Gas heat cost   : ${gas_cost:.4f}/kWh-heat  (fixed regardless of temperature)")
    print()
    print(f"  *** BREAKEVEN TEMPERATURE (at current rate ${live_rate:.4f}/kWh): {be_temp:.1f}°F ***")
    print(f"      Below {be_temp:.0f}°F → gas cheaper  |  Above {be_temp:.0f}°F → heat pump cheaper")
    print()
    print("  Heat pump COP at key temperatures (LG LGRED):")
    for temp in [0, 10, 20, 30, 40, 50]:
        cop    = interpolate_cop(temp)
        e_cost = cost_per_kwh_heat_electric(temp, rate=live_rate)
        winner = "→ USE GAS" if e_cost > gas_cost else "→ USE ELECTRIC"
        print(f"    {temp:3d}°F  COP={cop:.2f}  elec=${e_cost:.4f}/kWh-heat  "
              f"gas=${gas_cost:.4f}/kWh-heat  {winner:20s}")


# ─────────────────────────────────────────────────────────────────────────────
# MODE 1: RETROSPECTIVE ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def run_retrospective(nyiso_client):
    print_header(f"RETROSPECTIVE ANALYSIS  {HIST_START} → {HIST_END}  (NYISO Zone C - CENTRL)")
    print("  Note: Uses Real-Time 5-min LMP as a grid stress indicator.")
    print("  Cost recommendation is based on YOUR retail rate, not LMP.")
    print("  Tiered rate is not applied retrospectively — monthly kWh history")
    print("  is unavailable for past dates, so ELECTRIC_RETAIL_RATE_PER_KWH is used.\n")

    print("  Fetching Real-Time 5-min LMP…", end="", flush=True)
    lmp_df = fetch_historical_lmp(nyiso_client, HIST_START, HIST_END)
    print(f" {len(lmp_df):,} intervals loaded.")

    if lmp_df.empty:
        print("  No data — exiting.")
        return

    # ── Weather history ───────────────────────────────────────────────────────
    print("  Fetching historical temperature from Open-Meteo…", end="", flush=True)
    try:
        url = (
            f"https://archive-api.open-meteo.com/v1/archive"
            f"?latitude={LATITUDE}&longitude={LONGITUDE}"
            f"&start_date={HIST_START}&end_date={HIST_END}"
            f"&hourly=temperature_2m"
            f"&temperature_unit=fahrenheit"
            f"&timezone=America/New_York"
        )
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        wx   = pd.DataFrame({
            "time":   pd.to_datetime(data["hourly"]["time"]),
            "temp_f": data["hourly"]["temperature_2m"],
        })
        wx["Date"]   = wx["time"].dt.date
        daily_temp   = wx.groupby("Date")["temp_f"].mean().reset_index()
        daily_temp.columns = ["Date", "avg_temp_f"]
        has_weather  = True
        print(f" {len(wx):,} hourly records loaded.")
    except Exception as exc:
        print(f"\n  ⚠  Weather fetch failed ({exc}), using fixed 32°F fallback.")
        has_weather = False

    # ── Daily LMP stats ───────────────────────────────────────────────────────
    daily_lmp = lmp_df.groupby("Date")["LMP"].agg(
        avg_lmp="mean", max_lmp="max",
        hours_moderate=lambda s: (s > LMP_MODERATE_THRESHOLD).sum() / 12,
        hours_high=lambda s: (s > LMP_HIGH_THRESHOLD).sum() / 12,
    ).reset_index()

    if has_weather:
        daily = daily_lmp.merge(daily_temp, on="Date", how="left")
        daily["avg_temp_f"] = daily["avg_temp_f"].fillna(32.0)
    else:
        daily = daily_lmp.copy()
        daily["avg_temp_f"] = 32.0

    # ── Print table ───────────────────────────────────────────────────────────
    # Elec¢/kWh-h and Gas¢/kWh-h are both in cents per kWh of *delivered heat*
    # (not electricity consumed).  Electric = retail_rate / COP.
    # Gas = gas_price / (29.31 kWh/therm * AFUE).  Both columns use the same
    # unit so they can be compared directly — lower number wins.
    print()
    hdr = (
        f"{'Date':<12} {'AvgTemp':>8} {'COP':>5} "
        f"{'Elec¢/kWh-h':>12} {'Gas¢/kWh-h':>11} "
        f"{'AvgLMP':>8} {'MaxLMP':>8}  {'Recommendation'}"
    )
    print(hdr)
    print("-" * len(hdr))

    gas_cost_c = cost_per_kwh_heat_gas() * 100
    rows = []

    for _, row in daily.iterrows():
        temp   = row["avg_temp_f"]
        cop    = interpolate_cop(temp)
        e_cost = cost_per_kwh_heat_electric(temp) * 100   # flat fallback rate
        rec    = recommend(temp, lmp=row["avg_lmp"])

        print(
            f"{str(row['Date']):<12} {temp:8.1f} {cop:5.2f} "
            f"{e_cost:12.2f} {gas_cost_c:11.2f} "
            f"{row['avg_lmp']:8.1f} {row['max_lmp']:8.1f}  "
            f"{rec['icon']} {rec['fuel']}{rec['lmp_flag']}"
        )
        rows.append({
            "Date":            row["Date"],
            "Avg_Temp_F":      round(temp, 1),
            "COP":             round(cop, 2),
            "Elec_c_kwh_heat": round(e_cost, 3),
            "Gas_c_kwh_heat":  round(gas_cost_c, 3),
            "Avg_LMP":         round(row["avg_lmp"], 1),
            "Max_LMP":         round(row["max_lmp"], 1),
            "Rec":             rec["fuel"],
        })

    df_out   = pd.DataFrame(rows)
    n_gas    = (df_out["Rec"] == "GAS").sum()
    n_elec   = (df_out["Rec"] == "ELECTRIC").sum()
    n_tossup = (df_out["Rec"] == "TOSS-UP").sum()
    total    = len(df_out)

    print()
    print(f"  SUMMARY over {total} days:")
    print(f"    🔴 Use Gas     : {n_gas:4d} days ({100*n_gas/total:.0f}%)")
    print(f"    🟢 Use Electric: {n_elec:4d} days ({100*n_elec/total:.0f}%)")
    print(f"    🟡 Toss-up     : {n_tossup:4d} days ({100*n_tossup/total:.0f}%)")

    # ts    = datetime.now().strftime("%Y%m%d_%H%M")
    # fname = f"heating_retrospective_{ts}.csv"
    # df_out.to_csv(fname, index=False)
    # print(f"\n  Results saved to: {fname}")


# ─────────────────────────────────────────────────────────────────────────────
# MODE 2: TOMORROW FORECAST
# ─────────────────────────────────────────────────────────────────────────────

def run_forecast(nyiso_client, sense_data: dict | None = None):
    tomorrow      = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    current_month = date.today().month
    monthly_kwh   = sense_data["monthly_kwh"] if sense_data else None

    # Determine the marginal rate for tomorrow's recommendations.
    # If Sense is connected we know exactly where in the billing tier we sit.
    # Edge case: if today's usage is very close to 1,000 kWh the tier could
    # flip overnight, but this is accurate for the vast majority of the month.
    rate     = marginal_electric_rate(monthly_kwh, current_month)
    tier_num = (2 if (monthly_kwh is not None and
                      current_month in WINTER_MONTHS and
                      monthly_kwh >= ELECTRIC_TIER1_LIMIT) else 1)
    rate_src = (f"Sense — Tier {tier_num}, {monthly_kwh:.0f} kWh used this month"
                if monthly_kwh is not None else "flat fallback (no Sense)")

    print_header(f"TOMORROW'S FORECAST  {tomorrow}  (Fairport, NY)")
    print(f"  Uses NYISO Day-Ahead hourly LMP + Open-Meteo hourly temperature.")
    print(f"  Electric rate: ${rate:.4f}/kWh  [{rate_src}]\n")

    # ── Weather ───────────────────────────────────────────────────────────────
    print("  Fetching weather forecast…", end="", flush=True)
    try:
        wx          = fetch_weather_forecast(days=2)
        wx_tomorrow = wx[wx["time"].dt.date == date.today() + timedelta(days=1)].copy()
        print(f" {len(wx_tomorrow)} hourly points.")
    except Exception as exc:
        print(f"\n  ⚠  Weather fetch failed ({exc}). Using 30°F fallback.")
        hours       = pd.date_range(
            start=f"{tomorrow} 00:00", periods=24, freq="h", tz="America/New_York"
        )
        wx_tomorrow = pd.DataFrame({"time": hours, "temp_f": [30.0] * 24})

    # ── Day-Ahead LMP ─────────────────────────────────────────────────────────
    print("  Fetching NYISO Day-Ahead LMP…", end="", flush=True)
    da_lmp = fetch_day_ahead_lmp(nyiso_client, tomorrow)
    if not da_lmp.empty:
        print(f" {len(da_lmp)} hours loaded.")
    else:
        print(" not available yet (posted ~11 AM day-prior).")

    # ── Merge ─────────────────────────────────────────────────────────────────
    wx_tomorrow         = wx_tomorrow.copy()
    wx_tomorrow["hour"] = wx_tomorrow["time"].dt.hour

    if not da_lmp.empty:
        da_lmp = da_lmp.copy()
        if da_lmp["time"].dt.tz is not None:
            da_lmp["time"] = da_lmp["time"].dt.tz_convert("America/New_York")
        else:
            da_lmp["time"] = da_lmp["time"].dt.tz_localize(
                "America/New_York", ambiguous="NaT", nonexistent="shift_forward"
            )
        da_lmp["hour"] = da_lmp["time"].dt.hour
        merged = wx_tomorrow.merge(da_lmp[["hour", "lmp_da"]], on="hour", how="left")
    else:
        merged           = wx_tomorrow.copy()
        merged["lmp_da"] = None

    # ── Hour-by-hour table ────────────────────────────────────────────────────
    # Elec¢/kWh-h and Gas¢/kWh-h are both in cents per kWh of *delivered heat*
    # (not electricity consumed).  Electric = retail_rate / COP.
    # Gas = gas_price / (29.31 kWh/therm * AFUE).  Both columns use the same
    # unit so they can be compared directly — lower number wins.
    print()
    gas_cost_c = cost_per_kwh_heat_gas() * 100

    hdr = (
        f"{'Hour':<6} {'Temp°F':>7} {'COP':>5} "
        f"{'Elec¢/kWh-h':>12} {'Gas¢/kWh-h':>11} "
        f"{'DA-LMP':>8}  {'Recommendation'}"
    )
    print(hdr)
    print("-" * len(hdr))

    period_recs = []
    for _, row in merged.iterrows():
        temp   = row["temp_f"]
        lmp    = row["lmp_da"] if pd.notna(row.get("lmp_da")) else None
        rec    = recommend(temp, lmp=lmp, rate=rate)
        e_cost = rec["elec_cost_kwh"] * 100
        lmp_s  = f"{lmp:8.1f}" if lmp is not None else "     N/A"

        print(
            f"{row['hour']:02d}:00  {temp:7.1f} {rec['cop']:5.2f} "
            f"{e_cost:12.2f} {gas_cost_c:11.2f} "
            f"{lmp_s}  {rec['icon']} {rec['fuel']}{rec['lmp_flag']}"
        )
        period_recs.append(rec["fuel"])

    # ── Daily summary ─────────────────────────────────────────────────────────
    n_gas  = period_recs.count("GAS")
    n_elec = period_recs.count("ELECTRIC")
    n_tie  = period_recs.count("TOSS-UP")

    print()
    print(f"  Tomorrow at a glance:")
    print(f"    🔴 Gas cheaper for  : {n_gas:2d} hours")
    print(f"    🟢 Electric cheaper : {n_elec:2d} hours")
    print(f"    🟡 Toss-up          : {n_tie:2d} hours")
    print()

    if n_gas > n_elec:
        print("  ➤  RECOMMENDATION: RUN THE GAS BOILER tomorrow.")
        print(f"     Gas is cheaper for {n_gas} of 24 hours at the current billing rate.")
    elif n_elec >= n_gas:
        print("  ➤  RECOMMENDATION: USE THE HEAT PUMP tomorrow.")
        print(f"     Electric heat is cheaper for {n_elec} of 24 hours.")
    else:
        print("  ➤  MIXED DAY — consider switching at the breakeven temperature.")

    be = breakeven_temp(rate=rate)
    print(f"\n  Breakeven at current rate (${rate:.4f}/kWh): {be:.1f}°F")
    print(f"  Set thermostat scheduling around forecast temperatures.")


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    # Attempt Sense connection first so the config summary can show live data
    sense_data = fetch_sense_data()

    print_config_summary(sense_data=sense_data)

    nyiso = gridstatus.NYISO()

    print("\nSelect mode:")
    print("  1) Retrospective analysis (Jan–Apr 2026)")
    print("  2) Tomorrow's forecast + recommendation")
    print("  3) Both")

    choice = input("\nEnter 1, 2, or 3 [default=3]: ").strip() or "3"

    if choice in ("1", "3"):
        run_retrospective(nyiso)

    if choice in ("2", "3"):
        run_forecast(nyiso, sense_data=sense_data)

    print("\nDone.")


if __name__ == "__main__":
    main()
