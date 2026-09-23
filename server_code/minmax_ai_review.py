# =============================================================================
# minmax_ai_review.py
# -----------------------------------------------------------------------------
# Version:       5.13.1
# Last updated:  2026-05-11
# Changelog (this module only):
#   5.13.1 (2026-05-11) TWO BUG FIXES:
#                       (1) get_divergence_threshold() used a truthiness
#                           test (`if row and row['parameter_value']:`)
#                           which silently fell through to the 15.0 default
#                           whenever minmax_divergence_threshold_pct was set
#                           to 0 in the DB. Now uses `is not None` so a
#                           legitimate 0 is honored. Matches the pattern
#                           already used by get_review_cooldown_days().
#                       (2) analyze_sku_with_ai() had no client-side timeout
#                           on the Anthropic API call. A hung response would
#                           stall the entire batch on a single SKU (observed
#                           today on YS12I0150 with 82 candidates). Added
#                           timeout=60.0, expanded transient-error detection
#                           to include the exception class name and Timeout/
#                           APIConnectionError markers.
#   5.13.0 (2026-05-11) OUTLIER-AWARE TREND ANALYSIS
#                       Addresses systemic over-recommendation on sporadic-
#                       demand SKUs caused by single-day customer orders
#                       dominating the 30-day window. Three coordinated
#                       changes:
#                       (1) get_usage_trends() now computes additional
#                           signals: top-day units, top-day % of window,
#                           avg-excluding-top-day, and an outlier_dominated
#                           flag (top day > 40% of window total).
#                       (2) get_divergent_skus() filters out SKUs that have
#                           been reviewed within the last N days
#                           (REVIEW_COOLDOWN_DAYS, default 7) to prevent the
#                           same SKU from being re-recommended hours after a
#                           prior batch ran.
#                       (3) build_minmax_prompt() includes a new RECENT
#                           WINDOW OUTLIER ANALYSIS section, plus explicit
#                           reasoning guardrails telling the AI to anchor to
#                           the formula baseline (not the inflated 30d
#                           average) when the recent window is outlier-
#                           dominated, and to NEVER cite percentage demand
#                           shifts that are calculated against an outlier-
#                           dominated window.
#   5.12.0 (2026-04-29) FIRST VERSIONED RELEASE.
# =============================================================================

"""
MinMax AI Review - Anvil Background Task
=========================================
Ported from minmax_ai_review.py command-line script for Anvil execution.
Uses centralized db_config for database and API connections.

RECOMMENDATION STATUS LIFECYCLE
===============================
Status Flow:
  Pending -> Approved/Rejected/Modified -> Applied (terminal)

DIVERGENCE DETECTION
====================
Only SKUs where the statistical baseline (vw_min_max_calculated) has diverged
from the current production values (min_max_calculated) by more than the
configured threshold percentage are included in each review run.

NEW IN v5.13 - COOLDOWN
=======================
A SKU that already has a recommendation younger than REVIEW_COOLDOWN_DAYS
(default 7 days) is excluded from the current run regardless of status.
This prevents a second batch run from re-recommending the same SKU within
hours of the first, which was producing oscillating Min/Max values.

NEW IN v5.13 - OUTLIER-AWARE TRENDS
===================================
For sporadic-demand SKUs (CV > ~2, demand_pattern = 'Sporadic'), a single
large customer order can dominate the 30-day usage window and produce a
misleading "demand shift" signal. v5.13 computes the largest single day,
its percentage of the window total, the average with that day removed, and
flags any window where the top day is > OUTLIER_TOP_DAY_PCT (40%) of the
window total. The AI is given these signals explicitly and instructed how
to weight them.

BASELINE vs CURRENT COMPARISON
==============================
The AI receives both values to prevent oscillation:
  - Baseline: Formula-calculated from vw_min_max_calculated
  - Current:  Production values from min_max_calculated
  - Percentage difference

MINIMUM THRESHOLD RULE
======================
  MIN_THRESHOLD = MAX(2, CEILING(avg_order_size))
  Max Stock floor = 2x threshold.
"""

__version__       = "5.13.1"
__version_date__  = "2026-05-11"
__version_notes__ = "Threshold=0 honored; API timeout added to prevent batch stalls"

import anvil.server
from datetime import datetime, date
from decimal import Decimal
import json
import uuid
import time

# Import centralized configuration
from . import db_config


# =============================================================================
# TUNABLES
# =============================================================================
# Cooldown: skip SKUs with a recommendation younger than this many days.
# Prevents same-day re-runs from oscillating Min/Max values.
REVIEW_COOLDOWN_DAYS = 7

# Outlier threshold: if a single day's units >= this fraction of the
# 30-day window total, the window is flagged as "outlier_dominated" and
# the AI is instructed to discount the raw 30d trend.
OUTLIER_TOP_DAY_PCT = 40.0   # percent (e.g. 40.0 = 40%)

# Sporadic-CV trigger: above this CV, big single-day orders are the SKU's
# normal pattern, not a demand shift.
SPORADIC_CV_THRESHOLD = 2.0

# Per-call timeout (seconds) for Claude API requests. Without this, a hung
# response stalls the whole batch on a single SKU. APITimeoutError is then
# treated as transient by the retry loop in analyze_sku_with_ai.
AI_CALL_TIMEOUT_SECONDS = 60.0


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_connection():
  """Get database connection from centralized config."""
  return db_config.get_nexus_connection()


def make_serializable(value):
  if value is None:
    return None
  elif isinstance(value, (datetime, date)):
    return value.isoformat()
  elif isinstance(value, Decimal):
    return float(value)
  elif isinstance(value, bytes):
    return value.decode('utf-8', errors='replace')
  else:
    return value


# =============================================================================
# DATABASE FUNCTIONS - Data Retrieval
# =============================================================================

def get_divergence_threshold(conn):
  """
  Get the MinMax divergence threshold percentage from global_parameters.
  Default: 15% if not configured.

  v5.13.1: Uses `is not None` (was a truthiness test). A legitimate value
  of 0 in the DB now correctly returns 0 instead of falling through to the
  default 15.0.
  """
  query = """
    SELECT parameter_value
    FROM dbo.global_parameters
    WHERE parameter_name = 'minmax_divergence_threshold_pct'
  """
  cursor = conn.cursor()
  cursor.execute(query)
  row = cursor.fetchone()

  if row and row['parameter_value'] is not None:
    try:
      return float(row['parameter_value'])
    except (TypeError, ValueError):
      pass
  return 15.0


def get_review_cooldown_days(conn):
  """
  Get the cooldown window (in days) from global_parameters. If not
  configured, falls back to the module-level REVIEW_COOLDOWN_DAYS constant.
  """
  query = """
    SELECT parameter_value
    FROM dbo.global_parameters
    WHERE parameter_name = 'minmax_review_cooldown_days'
  """
  cursor = conn.cursor()
  cursor.execute(query)
  row = cursor.fetchone()
  if row and row['parameter_value'] is not None:
    try:
      return int(float(row['parameter_value']))
    except (TypeError, ValueError):
      pass
  return REVIEW_COOLDOWN_DAYS


def get_divergent_skus(conn, threshold_pct, cooldown_days):
  """
  Find AI-managed SKUs where the statistical baseline (view) has diverged
  from production values (table) by more than the threshold percentage.

  v5.13: Now also excludes SKUs that have a recommendation - of ANY
  status - younger than `cooldown_days`. Previously only Pending rows
  were excluded, which allowed the same SKU to be re-reviewed within
  hours of being Applied (oscillation risk).

  Returns list of dicts with both baseline and production values.
  """
  query = """
    SELECT
        t.sku,
        -- Production values
        t.min_stock       AS prod_min,
        t.max_stock       AS prod_max,
        t.avg_daily_usage AS prod_avg_daily_usage,
        t.std_dev         AS prod_std_dev,
        t.days_of_data    AS prod_days_of_data,
        t.days_with_sales AS prod_days_with_sales,
        t.coefficient_of_variation AS prod_cv,
        t.demand_pattern  AS prod_demand_pattern,
        t.total_orders    AS prod_total_orders,
        t.avg_order_size  AS prod_avg_order_size,
        t.order_qty       AS prod_order_qty,
        t.lead_time       AS prod_lead_time,
        t.safety_days     AS prod_safety_days,
        t.review_cycle    AS prod_review_cycle,
        t.service_level   AS prod_service_level,
        t.ai_managed,
        t.ai_updated_date,
        t.ai_updated_by,
        -- Baseline values
        v.min_stock       AS baseline_min,
        v.max_stock       AS baseline_max,
        v.avg_daily_usage AS baseline_avg_daily_usage,
        v.std_dev         AS baseline_std_dev,
        v.days_of_data    AS baseline_days_of_data,
        v.days_with_sales AS baseline_days_with_sales,
        v.coefficient_of_variation AS baseline_cv,
        v.demand_pattern  AS baseline_demand_pattern,
        v.total_orders    AS baseline_total_orders,
        v.avg_order_size  AS baseline_avg_order_size,
        v.order_qty       AS baseline_order_qty,
        -- Divergence calculations
        CASE
          WHEN t.min_stock > 0
          THEN ABS(v.min_stock - t.min_stock) * 100.0 / t.min_stock
          ELSE 0
        END AS min_pct_change,
        CASE
          WHEN t.max_stock > 0
          THEN ABS(v.max_stock - t.max_stock) * 100.0 / t.max_stock
          ELSE 0
        END AS max_pct_change
    FROM dbo.min_max_calculated t
    INNER JOIN dbo.vw_min_max_calculated v ON t.sku = v.sku
    WHERE t.ai_managed = 1
      AND NOT EXISTS (
          SELECT 1 FROM dbo.ai_minmax_recommendations r
          WHERE r.sku = t.sku
            AND r.recommendation_date >= DATEADD(DAY, -%s, GETDATE())
      )
      AND (
          CASE WHEN t.min_stock > 0
               THEN ABS(v.min_stock - t.min_stock) * 100.0 / t.min_stock
               ELSE 0 END > %s
          OR
          CASE WHEN t.max_stock > 0
               THEN ABS(v.max_stock - t.max_stock) * 100.0 / t.max_stock
               ELSE 0 END > %s
      )
    ORDER BY
        CASE WHEN t.min_stock > 0
             THEN ABS(v.min_stock - t.min_stock) * 100.0 / t.min_stock
             ELSE 0 END
        + CASE WHEN t.max_stock > 0
               THEN ABS(v.max_stock - t.max_stock) * 100.0 / t.max_stock
               ELSE 0 END DESC
  """
  cursor = conn.cursor()
  cursor.execute(query, (cooldown_days, threshold_pct, threshold_pct))
  results = list(cursor.fetchall())
  return results


def get_seasonal_context(conn):
  """Fetch seasonal factors for current and upcoming 3 months."""
  current_month = datetime.now().month
  months = [(current_month + i - 1) % 12 + 1 for i in range(4)]

  placeholders = ','.join(['%s'] * len(months))
  query = """
    SELECT month_number, sales_factor
    FROM dbo.seasonal_patterns
    WHERE pattern_type = 'company_wide'
      AND month_number IN (""" + placeholders + """)
    ORDER BY month_number
  """
  cursor = conn.cursor()
  cursor.execute(query, tuple(months))
  rows = list(cursor.fetchall())

  month_names = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

  seasonal = {
    'current_month': current_month,
    'current_month_name': month_names[current_month],
    'factors': {}
  }

  for row in rows:
    mn = row['month_number']
    factor = float(row['sales_factor']) if row['sales_factor'] else 1.0
    seasonal['factors'][mn] = {'month_name': month_names[mn], 'factor': factor}

  if current_month not in seasonal['factors']:
    seasonal['factors'][current_month] = {'month_name': month_names[current_month], 'factor': 1.0}

  return seasonal


def get_inventory_levels(conn, sku):
  """Get current inventory levels for a SKU from inventory_current."""
  query = """
    SELECT cost_center, on_hand, available, committed, allocated
    FROM dbo.inventory_current
    WHERE sku = %s
      AND cost_center IN ('Starlite', 'Hub')
  """
  cursor = conn.cursor()
  cursor.execute(query, (sku,))
  rows = list(cursor.fetchall())

  inventory = {
    'starlite_on_hand': 0, 'starlite_available': 0,
    'hub_on_hand': 0, 'hub_available': 0
  }

  for row in rows:
    cc = row['cost_center']
    if cc == 'Starlite':
      inventory['starlite_on_hand'] = float(row['on_hand'] or 0)
      inventory['starlite_available'] = float(row['available'] or 0)
    elif cc == 'Hub':
      inventory['hub_on_hand'] = float(row['on_hand'] or 0)
      inventory['hub_available'] = float(row['available'] or 0)

  return inventory


def get_usage_trends(conn, sku):
  """
  Get recent vs prior usage trends with OUTLIER DETECTION.

  v5.13 returns the legacy fields plus several new outlier signals:
    recent_top_day_units      - largest single qty_sold day in last 30
    recent_top_day_pct        - that day as % of 30d total
    recent_avg_ex_top_day     - avg/day with the top single day removed
    trend_pct_ex_top          - trend % computed with recent_avg_ex_top_day
    outlier_dominated         - bool, True if top_day_pct > OUTLIER_TOP_DAY_PCT
    recent_total_units        - 30d sum
    recent_days_with_sales    - 30d non-zero day count
  """
  recent_days = 30
  prior_days = 30

  # Recent window with outlier stats
  recent_query = """
    SELECT
      COUNT(*)                                                 AS days_in_window,
      ISNULL(SUM(qty_sold), 0)                                 AS total_units,
      ISNULL(MAX(qty_sold), 0)                                 AS top_day_units,
      SUM(CASE WHEN qty_sold > 0 THEN 1 ELSE 0 END)            AS days_with_sales,
      AVG(CAST(qty_sold AS DECIMAL(18,4)))                     AS avg_per_calendar_day,
      CASE
        WHEN COUNT(*) > 1
        THEN (ISNULL(SUM(qty_sold), 0) - ISNULL(MAX(qty_sold), 0))
             * 1.0 / (COUNT(*) - 1)
        ELSE 0
      END                                                      AS avg_ex_top_day
    FROM dbo.usage_data
    WHERE sku = %s
      AND usage_date >= DATEADD(DAY, %s, CAST(GETDATE() AS DATE))
      AND usage_date <  CAST(GETDATE() AS DATE)
  """
  cursor = conn.cursor()
  cursor.execute(recent_query, (sku, -recent_days))
  recent = cursor.fetchone() or {}

  # Prior window (just an avg, for trend comparison)
  prior_query = """
    SELECT AVG(CAST(qty_sold AS DECIMAL(18,4))) AS avg_per_day
    FROM dbo.usage_data
    WHERE sku = %s
      AND usage_date >= DATEADD(DAY, %s, CAST(GETDATE() AS DATE))
      AND usage_date <  DATEADD(DAY, %s, CAST(GETDATE() AS DATE))
  """
  cursor.execute(prior_query, (sku, -(recent_days + prior_days), -recent_days))
  prior = cursor.fetchone() or {}

  # Extract values
  recent_avg            = float(recent.get('avg_per_calendar_day') or 0)
  recent_total          = float(recent.get('total_units') or 0)
  recent_top_day        = float(recent.get('top_day_units') or 0)
  recent_avg_ex_top_day = float(recent.get('avg_ex_top_day') or 0)
  recent_days_w_sales   = int(recent.get('days_with_sales') or 0)
  prior_avg             = float(prior.get('avg_per_day') or 0)

  # Outlier signal
  if recent_total > 0:
    top_day_pct = round(recent_top_day * 100.0 / recent_total, 1)
  else:
    top_day_pct = 0.0
  outlier_dominated = top_day_pct >= OUTLIER_TOP_DAY_PCT

  # Trend (raw)
  if prior_avg > 0:
    trend_pct = round((recent_avg - prior_avg) / prior_avg * 100, 1)
    if trend_pct > 5:
      trend_direction = 'UP'
    elif trend_pct < -5:
      trend_direction = 'DOWN'
    else:
      trend_direction = 'STABLE'
  else:
    trend_pct = 0.0
    trend_direction = 'NEW' if recent_avg > 0 else 'NO DATA'

  # Trend with top-day removed (the "real" trend if it's a sporadic SKU)
  if prior_avg > 0:
    trend_pct_ex_top = round((recent_avg_ex_top_day - prior_avg) / prior_avg * 100, 1)
  else:
    trend_pct_ex_top = 0.0

  return {
    'recent_avg':            recent_avg,
    'recent_total_units':    recent_total,
    'recent_top_day_units':  recent_top_day,
    'recent_top_day_pct':    top_day_pct,
    'recent_avg_ex_top_day': recent_avg_ex_top_day,
    'recent_days_with_sales': recent_days_w_sales,
    'prior_avg':             prior_avg,
    'trend_pct':             trend_pct,
    'trend_direction':       trend_direction,
    'trend_pct_ex_top':      trend_pct_ex_top,
    'outlier_dominated':     outlier_dominated,
  }


# =============================================================================
# AI PROMPT BUILDER (v5.13 - outlier-aware)
# =============================================================================

def build_minmax_prompt(sku_data, seasonal, inventory, usage_trends):
  """
  Build AI analysis prompt for Min/Max recommendation.

  v5.13 adds:
    * RECENT WINDOW OUTLIER ANALYSIS section
    * Explicit reasoning guardrails for sporadic / outlier-dominated cases
    * Specific instruction to anchor to baseline formula when the 30d
      window is single-day-driven
  """
  sku = sku_data['sku']

  # Production (current) values
  prod_min = float(sku_data['prod_min'] or 0)
  prod_max = float(sku_data['prod_max'] or 0)
  prod_avg_daily = float(sku_data['prod_avg_daily_usage'] or 0)

  # Baseline (formula) values
  baseline_min = float(sku_data['baseline_min'] or 0)
  baseline_max = float(sku_data['baseline_max'] or 0)
  baseline_avg_daily = float(sku_data['baseline_avg_daily_usage'] or 0)

  # Divergence
  min_pct = float(sku_data['min_pct_change'] or 0)
  max_pct = float(sku_data['max_pct_change'] or 0)

  # Statistical profile
  cv = float(sku_data['prod_cv'] or 0)
  demand_pattern = sku_data['prod_demand_pattern'] or 'Unknown'
  days_of_data = int(sku_data['prod_days_of_data'] or 0)
  days_with_sales = int(sku_data['prod_days_with_sales'] or 0)
  avg_order_size = float(sku_data['prod_avg_order_size'] or 0)
  total_orders = int(sku_data['prod_total_orders'] or 0)

  # Parameters
  lead_time = float(sku_data['prod_lead_time'] or 2)
  safety_days = float(sku_data['prod_safety_days'] or 5)
  review_cycle = float(sku_data['prod_review_cycle'] or 5)
  service_level = float(sku_data['prod_service_level'] or 95)

  # Minimum threshold
  min_threshold = max(2, int(-(-avg_order_size // 1)))
  max_threshold = 2 * min_threshold

  # Days of supply
  if prod_avg_daily > 0:
    prod_min_dos = round(prod_min / prod_avg_daily, 1)
    prod_max_dos = round(prod_max / prod_avg_daily, 1)
    baseline_min_dos = round(baseline_min / baseline_avg_daily, 1) if baseline_avg_daily > 0 else 0
    baseline_max_dos = round(baseline_max / baseline_avg_daily, 1) if baseline_avg_daily > 0 else 0
  else:
    prod_min_dos = prod_max_dos = baseline_min_dos = baseline_max_dos = 0

  # Seasonal forecast string
  seasonal_str = ""
  for mn, data in sorted(seasonal['factors'].items()):
    seasonal_str = seasonal_str + "  - " + data['month_name'] + ": " + str(data['factor']) + "x average demand\n"

  # ----- Outlier-window flags / phrasing (NEW v5.13) -----------------
  is_sporadic = (cv >= SPORADIC_CV_THRESHOLD) or (str(demand_pattern).lower() == 'sporadic')
  outlier_flag = usage_trends.get('outlier_dominated', False)
  top_day_units = usage_trends.get('recent_top_day_units', 0)
  top_day_pct = usage_trends.get('recent_top_day_pct', 0)
  recent_avg_ex_top = usage_trends.get('recent_avg_ex_top_day', 0)
  trend_pct_ex_top = usage_trends.get('trend_pct_ex_top', 0)

  outlier_section = (
    "RECENT WINDOW OUTLIER ANALYSIS (last 30 days):\n"
    "- Total units sold:           " + str(int(usage_trends.get('recent_total_units', 0))) + "\n"
    "- Days with sales:            " + str(usage_trends.get('recent_days_with_sales', 0)) + " / 30\n"
    "- Largest single day:         " + str(int(top_day_units)) + " units (" + str(top_day_pct) + "% of 30d total)\n"
    "- Avg/day INCLUDING top day:  " + str(round(usage_trends.get('recent_avg', 0), 4)) + "\n"
    "- Avg/day EXCLUDING top day:  " + str(round(recent_avg_ex_top, 4)) + "\n"
    "- Trend % vs prior 30d (raw): " + str(usage_trends.get('trend_pct', 0)) + "%\n"
    "- Trend % EXCLUDING top day:  " + str(trend_pct_ex_top) + "%\n"
    "- Outlier-dominated window?   " + ("YES (top day >= " + str(OUTLIER_TOP_DAY_PCT) + "% of window)" if outlier_flag else "no") + "\n"
    "- Sporadic-CV SKU?            " + ("YES (CV " + str(round(cv, 2)) + " >= " + str(SPORADIC_CV_THRESHOLD) + ")" if is_sporadic else "no") + "\n"
  )

  # ----- Reasoning guardrails (NEW v5.13) ----------------------------
  guardrails = """REASONING GUARDRAILS - READ CAREFULLY:

1. If "Outlier-dominated window? YES", the raw 30-day recent average is
   inflated by a single customer order. DO NOT cite the raw trend % as
   evidence of a demand shift. Instead compare "Avg/day EXCLUDING top day"
   to the long-term avg daily usage:
     - If they are within +/- 25% of each other, the spike is the SKU's
       normal sporadic pattern, NOT a demand shift. Anchor your
       recommendation to the FORMULA BASELINE, not the recent average.
     - If even with the top day removed the recent average is still
       materially higher than long-term, then a real shift may be
       underway and a modest upward adjustment is justified.

2. If "Sporadic-CV SKU? YES" (CV >= 2.0 or demand_pattern = 'Sporadic'),
   large single-day orders are normal for this item. The formula baseline
   already accounts for variability via safety_days. Treat single-day
   spikes as expected, not as new information.

3. NEVER quote percentage changes like "demand increased 472%" in your
   reasoning if the underlying 30d window is outlier-dominated. Use the
   "Trend % EXCLUDING top day" figure instead, and state plainly that
   one customer's bulk order is being excluded from the trend signal.

4. When in doubt between the recent-window average and the formula
   baseline for a sporadic SKU, DEFER TO THE FORMULA BASELINE. The
   baseline already has safety_days baked in for exactly this kind of
   variability.

5. Do NOT recommend a Min above the formula baseline Min unless one of:
     - The recent avg EXCLUDING the top day is materially above long-term, OR
     - Recent inventory data clearly shows stockouts (negative available
       at Starlite is a strong signal), OR
     - You can point to a specific business event (not a single bulk order)
       that justifies higher coverage.
   If none of those apply, recommend AT OR BELOW the baseline.

6. NEVER recommend below the MINIMUM THRESHOLD rule (next section).
"""

  prompt = """You are an inventory analyst AI for Titan Flow Control. Your role is to evaluate
Min/Max stock levels AND teach inventory principles to personnel who are learning.

CONTEXT:
- Starlite is a FORWARD STOCKING LOCATION (lean inventory, target 5-10 days supply)
- Hub is 6 minutes away (2-day replenishment lead time)
- Frequent small transfers from Hub are preferred over large inventory holdings
- These are A-classification items (high-value, regularly stocked)

SKU: """ + str(sku) + """
Demand Pattern: """ + demand_pattern + """

STATISTICAL PROFILE:
- Avg Daily Usage: """ + str(round(prod_avg_daily, 4)) + """ units/day (over """ + str(days_of_data) + """ days)
- Standard Deviation: """ + str(round(float(sku_data['prod_std_dev'] or 0), 4)) + """
- Coefficient of Variation: """ + str(round(cv, 2)) + """
- Days with Sales: """ + str(days_with_sales) + """ of """ + str(days_of_data) + """ (""" + str(round(days_with_sales / max(days_of_data, 1) * 100, 1)) + """%)
- Total Orders: """ + str(total_orders) + """
- Avg Order Size: """ + str(round(avg_order_size, 2)) + """ units

KEY PARAMETERS:
- Lead Time: """ + str(int(lead_time)) + """ days
- Safety Days: """ + str(int(safety_days)) + """ days
- Review Cycle: """ + str(int(review_cycle)) + """ days
- Service Level: """ + str(int(service_level)) + """%

CURRENT PRODUCTION VALUES (includes prior AI adjustments):
- Min Stock: """ + str(int(prod_min)) + """ (""" + str(prod_min_dos) + """ days supply at long-term avg)
- Max Stock: """ + str(int(prod_max)) + """ (""" + str(prod_max_dos) + """ days supply at long-term avg)

FORMULA BASELINE (recalculated from current usage data):
- Min Stock: """ + str(int(baseline_min)) + """ (""" + str(baseline_min_dos) + """ days supply)
- Max Stock: """ + str(int(baseline_max)) + """ (""" + str(baseline_max_dos) + """ days supply)

DIVERGENCE:
- Min divergence: """ + str(round(min_pct, 1)) + """% (baseline """ + ("higher" if baseline_min > prod_min else "lower") + """ than production)
- Max divergence: """ + str(round(max_pct, 1)) + """% (baseline """ + ("higher" if baseline_max > prod_max else "lower") + """ than production)

CURRENT INVENTORY:
- Starlite On Hand: """ + str(int(inventory['starlite_on_hand'])) + """  Available: """ + str(int(inventory['starlite_available'])) + """
- Hub On Hand:      """ + str(int(inventory['hub_on_hand'])) + """  Available: """ + str(int(inventory['hub_available'])) + """

USAGE TRENDS (raw 30-day comparison):
- Recent Avg Daily: """ + str(round(usage_trends['recent_avg'], 4)) + """
- Prior Avg Daily:  """ + str(round(usage_trends['prior_avg'], 4)) + """
- Trend: """ + usage_trends['trend_direction'] + """ (""" + str(round(usage_trends['trend_pct'], 1)) + """%)

""" + outlier_section + """
SEASONAL CONTEXT (upcoming months):
""" + seasonal_str + """
MINIMUM THRESHOLD RULE:
- MIN_THRESHOLD = MAX(2, CEILING(avg_order_size)) = """ + str(min_threshold) + """
- Your recommended Min MUST be >= """ + str(min_threshold) + """ (enough for at least one typical order)
- Your recommended Max MUST be >= """ + str(max_threshold) + """ (2x minimum threshold)

""" + guardrails + """
YOUR TASK:
1. Analyze whether the current production Min/Max values should be adjusted
   given the formula baseline, the trend signal AFTER applying the guardrails
   above, and the inventory situation.
2. If the formula baseline is the right answer (especially for sporadic SKUs
   with outlier-dominated windows), recommend values at or very close to the
   baseline rather than reacting to the inflated recent average.
3. If current values are still appropriate, recommend keeping them and explain
   why (cite the outlier signal, the trend-ex-top-day, or seasonal factors).
4. NEVER recommend below the minimum thresholds (Min >= """ + str(min_threshold) + """, Max >= """ + str(max_threshold) + """).
5. In your reasoning, explicitly state:
     - Whether the recent 30d window is outlier-dominated, and if so, what
       fraction of the window came from one day
     - Whether you anchored to the formula baseline or to the recent average,
       and why
     - The trend % EXCLUDING the top day, NOT the raw trend %, when the window
       is outlier-dominated
6. Include one key learning point about Min/Max inventory management.

Respond in JSON format only:
{
    "recommended_min": <integer - MUST be >= """ + str(min_threshold) + """>,
    "recommended_max": <integer - MUST be >= """ + str(max_threshold) + """ and > recommended_min>,
    "confidence_level": "High/Medium/Low",
    "anchor_used": "baseline_formula" | "recent_average" | "current_values",
    "outlier_acknowledged": <true/false - did you explicitly account for the outlier signal>,
    "reasoning": "<detailed explanation suitable for training, 2-3 paragraphs>",
    "seasonal_consideration": "<how seasonality affects this recommendation>",
    "risk_factors": "<concerns or risks to watch>",
    "learning_point": "<one key inventory principle this situation illustrates>"
}
"""
  return prompt


# =============================================================================
# AI ANALYSIS
# =============================================================================

def analyze_sku_with_ai(client, prompt, max_retries=3):
  """
  Send prompt to Claude API and parse JSON response with retry.

  v5.13.1: Adds a per-call timeout (AI_CALL_TIMEOUT_SECONDS) so a hung
  API request can't stall the whole batch on a single SKU.
  APITimeoutError / APIConnectionError are treated as transient and
  retried with exponential backoff. After max_retries the SKU is
  recorded as an error and the batch moves on.
  """
  last_error = None

  for attempt in range(max_retries):
    try:
      response = client.messages.create(
        model=db_config.AI_MODEL,
        max_tokens=db_config.AI_MAX_TOKENS,
        timeout=AI_CALL_TIMEOUT_SECONDS,
        messages=[{"role": "user", "content": prompt}]
      )

      response_text = response.content[0].text.strip()

      if '```json' in response_text:
        response_text = response_text.split('```json')[1].split('```')[0].strip()
      elif '```' in response_text:
        response_text = response_text.split('```')[1].split('```')[0].strip()

      result = json.loads(response_text)

      required = ['recommended_min', 'recommended_max', 'confidence_level', 'reasoning']
      for field in required:
        if field not in result:
          return {'error': 'Missing field: ' + field, 'raw_response': response_text}

      result['recommended_min'] = int(result['recommended_min'])
      result['recommended_max'] = int(result['recommended_max'])

      return result

    except json.JSONDecodeError as e:
      return {'error': 'JSON parse error: ' + str(e), 'raw_response': response_text}
    except Exception as e:
      # Include the exception class name so transient-error detection can
      # match APITimeoutError / APIConnectionError even when str(e) is empty.
      last_error = type(e).__name__ + ': ' + (str(e) or repr(e))

      transient_markers = [
        '529', '429', '500', '502', '503',
        'timeout', 'Timeout',
        'ConnectionError', 'APIConnectionError',
      ]
      is_transient = any(marker in last_error for marker in transient_markers)

      if is_transient and attempt < max_retries - 1:
        wait_seconds = 2 ** (attempt + 1)
        print('Retrying SKU after ' + str(wait_seconds) + 's (attempt '
              + str(attempt + 1) + '/' + str(max_retries) + '): ' + last_error)
        time.sleep(wait_seconds)
        continue
      else:
        return {'error': 'AI API error: ' + last_error}

  return {'error': 'AI API error after ' + str(max_retries) + ' retries: ' + last_error}


# =============================================================================
# DATABASE FUNCTIONS - Save Results
# =============================================================================

def save_recommendation(conn, sku_data, ai_result, batch_id):
  """Insert a new Pending recommendation into ai_minmax_recommendations."""
  avg_order_size = float(sku_data['prod_avg_order_size'] or 0)
  min_threshold = max(2, int(-(-avg_order_size // 1)))
  max_threshold = 2 * min_threshold

  rec_min = max(ai_result['recommended_min'], min_threshold)
  rec_max = max(ai_result['recommended_max'], max_threshold)

  if rec_max <= rec_min:
    rec_max = rec_min + max(min_threshold, 1)

  full_reasoning = ai_result.get('reasoning', '')
  seasonal = ai_result.get('seasonal_consideration', '')
  risk = ai_result.get('risk_factors', '')
  learning = ai_result.get('learning_point', '')
  anchor = ai_result.get('anchor_used', '')
  outlier_ack = ai_result.get('outlier_acknowledged', None)

  # Surface the new audit fields at the top of reasoning so they're visible
  # in the review grid without a schema change.
  meta_lines = []
  if anchor:
    meta_lines.append('ANCHOR: ' + str(anchor))
  if outlier_ack is not None:
    meta_lines.append('OUTLIER ACKNOWLEDGED: ' + str(outlier_ack))
  if meta_lines:
    full_reasoning = '[' + ' | '.join(meta_lines) + ']\n\n' + full_reasoning

  if seasonal:
    full_reasoning = full_reasoning + '\n\nSEASONAL: ' + seasonal
  if risk:
    full_reasoning = full_reasoning + '\n\nRISKS: ' + risk
  if learning:
    full_reasoning = full_reasoning + '\n\nLEARNING POINT: ' + learning

  query = """
    INSERT INTO dbo.ai_minmax_recommendations (
        sku, current_min, current_max,
        recommended_min, recommended_max,
        confidence_level, reasoning,
        recommendation_date, status, batch_id,
        reviewed_by, reviewed_date, notes
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, GETDATE(), 'Pending', %s,
            NULL, NULL, NULL)
  """
  cursor = conn.cursor()
  cursor.execute(query, (
    sku_data['sku'],
    sku_data['prod_min'],
    sku_data['prod_max'],
    rec_min,
    rec_max,
    ai_result.get('confidence_level', 'Medium'),
    full_reasoning,
    batch_id
  ))
  conn.commit()

  return {'recommended_min': rec_min, 'recommended_max': rec_max}


def log_action(conn, action_type, sku, old_value, new_value, reason, executed_by='minmax_ai_review.py'):
  """Log an action to ai_agent_log."""
  query = """
    INSERT INTO dbo.ai_agent_log (
        action_type, sku, old_value, new_value, reason,
        action_timestamp, executed_by
    )
    VALUES (%s, %s, %s, %s, %s, GETDATE(), %s)
  """
  cursor = conn.cursor()
  cursor.execute(query, (action_type, sku, old_value, new_value, reason, executed_by))
  conn.commit()


# =============================================================================
# MAIN BACKGROUND TASK
# =============================================================================

@anvil.server.callable
def trigger_minmax_review():
  """Start the MinMax AI review as a background task."""
  try:
    task = anvil.server.launch_background_task('run_minmax_review')
    return {'status': 'launched', 'task_id': task.get_id()}
  except Exception as e:
    return {'status': 'error', 'message': str(e)}


@anvil.server.background_task
def run_minmax_review():
  """
  Main orchestrator for the MinMax AI review process.

  v5.13: now applies REVIEW_COOLDOWN_DAYS (configurable via
  global_parameters.minmax_review_cooldown_days, default 7) so SKUs
  recently recommended are skipped, preventing oscillation.
  """
  batch_id = 'MM-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + str(uuid.uuid4())[:8]

  results = {
    'batch_id': batch_id,
    'skus_reviewed': 0,
    'recommendations_created': 0,
    'skipped_no_change': 0,
    'errors': 0,
    'error_skus': []
  }

  conn = None

  try:
    conn = get_connection()
    client = db_config.get_anthropic_client()

    anvil.server.task_state['status'] = 'Loading configuration'
    threshold_pct = get_divergence_threshold(conn)
    cooldown_days = get_review_cooldown_days(conn)
    anvil.server.task_state['threshold'] = threshold_pct
    anvil.server.task_state['cooldown_days'] = cooldown_days

    anvil.server.task_state['status'] = 'Loading seasonal data'
    seasonal = get_seasonal_context(conn)

    anvil.server.task_state['status'] = 'Identifying divergent SKUs'
    candidates = get_divergent_skus(conn, threshold_pct, cooldown_days)
    total_candidates = len(candidates)
    anvil.server.task_state['total_candidates'] = total_candidates

    if total_candidates == 0:
      anvil.server.task_state['status'] = 'Complete - no divergent SKUs found'
      log_action(
        conn, 'MinMax_Review_Completed', None,
        batch_id,
        '0 SKUs reviewed (threshold: ' + str(threshold_pct) + '%, cooldown: ' + str(cooldown_days) + 'd)',
        'No divergent SKUs found',
        'minmax_ai_review.py'
      )
      results['status'] = 'complete'
      return results

    for i, sku_data in enumerate(candidates):
      sku = sku_data['sku']
      progress = str(i + 1) + '/' + str(total_candidates)
      anvil.server.task_state['status'] = 'Reviewing ' + sku + ' (' + progress + ')'
      anvil.server.task_state['current_sku'] = sku
      anvil.server.task_state['progress'] = i + 1

      try:
        inventory = get_inventory_levels(conn, sku)
        usage_trends = get_usage_trends(conn, sku)

        prompt = build_minmax_prompt(sku_data, seasonal, inventory, usage_trends)
        ai_result = analyze_sku_with_ai(client, prompt)

        if 'error' in ai_result:
          results['errors'] += 1
          results['error_skus'].append(sku + ': ' + ai_result['error'])
          log_action(
            conn, 'MinMax_Review_Error', sku,
            None, ai_result['error'],
            'Batch:' + batch_id,
            'minmax_ai_review.py'
          )
          continue

        rec_min = int(ai_result['recommended_min'])
        rec_max = int(ai_result['recommended_max'])
        prod_min = int(float(sku_data['prod_min'] or 0))
        prod_max = int(float(sku_data['prod_max'] or 0))

        if rec_min == prod_min and rec_max == prod_max:
          results['skipped_no_change'] += 1
          log_action(
            conn, 'MinMax_Review_No_Change', sku,
            'Min:' + str(prod_min) + ' Max:' + str(prod_max),
            'AI agrees with current values',
            'Batch:' + batch_id,
            'minmax_ai_review.py'
          )
          continue

        final_values = save_recommendation(conn, sku_data, ai_result, batch_id)
        results['recommendations_created'] += 1
        results['skus_reviewed'] += 1

        log_action(
          conn, 'MinMax_Review_Recommendation', sku,
          'Min:' + str(prod_min) + ' Max:' + str(prod_max),
          'Min:' + str(final_values['recommended_min']) + ' Max:' + str(final_values['recommended_max']),
          'Batch:' + batch_id + ' Confidence:' + ai_result.get('confidence_level', 'N/A')
          + ' Anchor:' + ai_result.get('anchor_used', 'n/a')
          + ' OutlierAck:' + str(ai_result.get('outlier_acknowledged', 'n/a')),
          'minmax_ai_review.py'
        )

        time.sleep(0.5)

      except Exception as e:
        results['errors'] += 1
        results['error_skus'].append(sku + ': ' + str(e))
        print('Error processing SKU ' + sku + ': ' + str(e))

    summary = (str(total_candidates) + ' SKUs analyzed, ' +
               str(results['recommendations_created']) + ' recommendations generated, ' +
               str(results['skipped_no_change']) + ' no change, ' +
               str(results['errors']) + ' errors '
               + '(cooldown ' + str(cooldown_days) + 'd, threshold ' + str(threshold_pct) + '%)')

    log_action(
      conn, 'MinMax_Review_Completed', None,
      batch_id,
      summary,
      'Monthly AI review' if not anvil.server.task_state.get('manual_trigger') else 'Manual trigger',
      'minmax_ai_review.py'
    )

    anvil.server.task_state['status'] = 'Complete'
    anvil.server.task_state['summary'] = summary
    results['status'] = 'complete'

    return results

  except Exception as e:
    error_msg = 'MinMax review failed: ' + str(e)
    anvil.server.task_state['status'] = 'Error: ' + str(e)

    try:
      if conn:
        log_action(
          conn, 'MinMax_Review_Failed', None,
          batch_id, error_msg,
          'System error',
          'minmax_ai_review.py'
        )
    except:
      pass

    raise

  finally:
    if conn:
      try:
        conn.close()
      except:
        pass