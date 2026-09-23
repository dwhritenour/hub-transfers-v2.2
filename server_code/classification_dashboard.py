# =============================================================================
# classification_dashboard.py
# -----------------------------------------------------------------------------
# Version:       5.12.4
# Last updated:  2026-05-12
# Changelog (this module only):
#   5.12.4 (2026-05-12) BUG FIX: 'Months as A-Item' was still mostly 0
#                        after 5.12.2/5.12.3. The earlier fix moved tenure
#                        sourcing from aim.classification_date to
#                        ai_agent_log promotion events — correct in spirit,
#                        but only caught ~20 of ~194 stocked A-items. The
#                        other ~174 are continuously-A SKUs that were
#                        classified at system inception (2025-12-06) and
#                        never had a B->A or C->A transition. The
#                        promotion CTE returned NULL for them, and the
#                        ISNULL fallback to aim.classification_date hit
#                        the same recently-rewritten field the original
#                        bug was about — DATEDIFF returned 0.
#                        Two changes:
#                          1. Promotion CTE now also catches old-format
#                             C->A (was only B->A).
#                          2. New 'first_a' CTE finds the EARLIEST log row
#                             showing the SKU as A — PROMOTION: row,
#                             UPDATE: A row, or old literal A. Used as the
#                             tenure anchor when no promotion event exists.
#                             For inception-A SKUs this lands near
#                             2025-12-06, giving ~5 months at time of fix.
#                        Resolution chain: promotion -> first-A evidence
#                        -> classification_date. classification_date
#                        remains as a last-resort fallback for SKUs with
#                        no log rows at all (rare).
#   5.12.0 (2026-04-29) FIRST VERSIONED RELEASE — brought into the v5.8+
#                        versioning scheme.
#                        Added: get_a_items_export() — generates an Excel
#                        workbook (one sheet per series) with the current
#                        stocked-A-items population and their min/max,
#                        usage, tenure, demand pattern, and on-hand
#                        counts. Called from both abc_classification and
#                        minmax_review forms via a shared Export button.
#   5.12.1 (2026-04-29) Workbook layout: switched from one-sheet-per-series
#                        to a single 'A-Items' sheet with each series
#                        rendered as a banded section (banner row +
#                        SKU rows + blank separator). Per-tab navigation
#                        was overwhelming with many series; the single
#                        sheet keeps everything in one scroll.
#   5.12.2 (2026-04-29) BUG FIX: 'Months as A-Item' was reading mostly 0.
#                        Cause: usp_Apply_A_Item_Classification resets
#                        aim.classification_date to GETDATE() on every
#                        apply run, including UPDATE-type (monthly
#                        metric refresh) recommendations. That made the
#                        column reflect "row last touched", not "A-item
#                        since".
#                        Fix: tenure now sourced from ai_agent_log —
#                        DATEDIFF(month, MAX(promotion_timestamp), now)
#                        per SKU. Falls back to classification_date when
#                        no promotion log exists (rare; seeded-from-
#                        inception SKUs).
#   5.12.3 (2026-04-29) BUG FIX: 5.12.2's tenure subquery raised
#                        "data types text and varchar are incompatible
#                        in the equal to operator." ai_agent_log
#                        old_value/new_value are SQL Server `text`
#                        type, which forbids '='. LIKE on text is OK,
#                        but the older-format equality branch
#                        (new_value = 'A' AND old_value = 'B') needs
#                        CAST AS varchar(50) before comparing.
# =============================================================================

"""
Anvil Server Module - ABC Classification Dashboard
====================================================
Server-side code for the Titan Flow Control ABC Classification Review system.

This module provides database connectivity and business logic for:
    - Fetching classification recommendations with filtering and search
    - Updating decisions (Approve/Reject)
    - Applying approved classification changes to production table
    - SKU browser for ad-hoc classification visibility
    - Summary counts for dashboard cards
    - Last review date from ai_agent_log
    - A-items Excel export (shared by abc_classification and minmax_review forms)

Database: SQL Server (nexus) via pymssql
Uses centralized db_config for credentials (Anvil Secrets)

Author: Dave Whritenour / Claude AI Assistant
Project: Titan Flow Control - Year of Automation
Created: March 2026
"""

__version__       = "5.12.4"
__version_date__  = "2026-05-12"
__version_notes__ = (
  "A-items export Months as A-Item fix: tenure now resolves through "
  "promotion -> earliest A-evidence in ai_agent_log -> classification_date "
  "fallback, instead of stopping at classification_date when no promotion "
  "event exists. Continuously-A SKUs now show months back to system "
  "inception (~5 months) instead of 0."
)

import anvil.server
import anvil.media
import io
from datetime import datetime, date
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

# Import centralized configuration
from . import db_config


# =============================================================================
# SERIALIZATION HELPERS
# =============================================================================

def make_serializable(value):
  """
    Convert non-serializable types to serializable ones.
    Anvil can't serialize datetime, Decimal, bytes directly.
  """
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


def serialize_row(row):
  """Convert a row dict to all serializable values."""
  return {key: make_serializable(val) for key, val in row.items()}


# =============================================================================
# DATABASE CONNECTION
# =============================================================================

def get_connection():
  """
    Create and return a database connection.
    Uses centralized db_config for credentials.
  """
  return db_config.get_nexus_connection()


# =============================================================================
# GET CLASSIFICATION RECOMMENDATIONS  (dedup + live Current values)
# =============================================================================

@anvil.server.callable
def get_classification_recommendations(status_filter=None, search_text=None):
  """
    Fetch classification recommendations for the review DataGrid.

    Returns one row per SKU (the most recent recommendation_date) with
    LIVE current values from a_items_master, not the historical snapshot
    stored on the recommendation row. Snapshots are preserved as
    snapshot_* keys for audit context.

    Args:
        status_filter: 'Pending', 'Pending_Review', 'Approved', 'Rejected',
                       'Applied', or None for all
        search_text:   Search by SKU

    Returns:
        dict with:
            - 'status': 'success' or 'error'
            - 'recommendations': list of serialized row dicts (1 per SKU)
            - 'counts': dict of status counts (1 per SKU)
            - 'last_review_date': timestamp of most recent review run
  """
  try:
    conn = get_connection()
    cursor = conn.cursor(as_dict=True)

    # -----------------------------------------------------------------
    # Latest recommendation per SKU, joined to a_items_master for live
    # current state.
    # -----------------------------------------------------------------
    query = """
        WITH latest_per_sku AS (
            SELECT
                r.recommendation_id,
                r.sku,
                r.change_type,
                r.current_classification AS snapshot_classification,
                r.current_total_sold     AS snapshot_total_sold,
                r.current_running_pct    AS snapshot_running_pct,
                r.current_is_stocked     AS snapshot_is_stocked,
                r.new_classification,
                r.new_total_sold,
                r.new_running_pct,
                r.new_is_stocked,
                r.months_as_a_item,
                r.classification_changes_12mo,
                r.confidence_level,
                r.reasoning,
                r.recommendation_date,
                r.status,
                r.reviewed_by,
                r.reviewed_date,
                r.notes,
                ROW_NUMBER() OVER (
                    PARTITION BY r.sku
                    ORDER BY r.recommendation_date DESC, r.recommendation_id DESC
                ) AS rn
            FROM dbo.ai_classification_recommendations r
        )
        SELECT
            l.recommendation_id,
            l.sku,
            l.change_type,
            -- LIVE current values from a_items_master (fall back to
            -- snapshot if the SKU isn't in a_items_master)
            COALESCE(a.abc_classification,       l.snapshot_classification) AS current_classification,
            COALESCE(a.total_sold_yearly,        l.snapshot_total_sold)     AS current_total_sold,
            COALESCE(a.running_pct_of_all,       l.snapshot_running_pct)    AS current_running_pct,
            COALESCE(a.is_stocked_at_starlite,   l.snapshot_is_stocked)     AS current_is_stocked,
            -- Preserve the snapshot for the detail popup / audit
            l.snapshot_classification,
            l.snapshot_total_sold,
            l.snapshot_running_pct,
            l.snapshot_is_stocked,
            l.new_classification,
            l.new_total_sold,
            l.new_running_pct,
            l.new_is_stocked,
            l.months_as_a_item,
            l.classification_changes_12mo,
            l.confidence_level,
            l.reasoning,
            l.recommendation_date,
            l.status,
            l.reviewed_by,
            l.reviewed_date,
            l.notes,
            -- Demand pattern join (unchanged)
            m.demand_pattern,
            m.avg_daily_usage,
            m.coefficient_of_variation,
            m.days_with_sales,
            m.days_of_data,
            m.avg_order_size
        FROM latest_per_sku l
        LEFT JOIN dbo.a_items_master    a ON l.sku = a.sku
        LEFT JOIN dbo.min_max_calculated m ON l.sku = m.sku
        WHERE l.rn = 1
    """
    params = []

    if status_filter:
      query += " AND l.status = %s"
      params.append(status_filter)

    if search_text:
      query += " AND l.sku LIKE %s"
      search_param = '%' + search_text + '%'
      params.append(search_param)

    # Sort: Pending first (DEMOTION before PROMOTION), then Approved, Rejected, Applied
    query += """
        ORDER BY
            CASE UPPER(l.status)
                WHEN 'PENDING' THEN 1
                WHEN 'PENDING_REVIEW' THEN 1
                WHEN 'APPROVED' THEN 2
                WHEN 'REJECTED' THEN 3
                WHEN 'APPLIED' THEN 4
                ELSE 5
            END,
            CASE WHEN UPPER(l.change_type) = 'DEMOTION' THEN 0 ELSE 1 END,
            l.confidence_level DESC,
            l.sku
    """

    cursor.execute(query, params)
    rows = cursor.fetchall()

    # Serialize and format
    recommendations = []
    for row in rows:
      serialized = serialize_row(row)

      # Format dates for clean display
      if serialized.get('recommendation_date'):
        try:
          rec_date = datetime.fromisoformat(serialized['recommendation_date'])
          serialized['recommendation_date_display'] = rec_date.strftime('%m/%d/%Y %I:%M %p')
        except (ValueError, TypeError):
          serialized['recommendation_date_display'] = serialized['recommendation_date']

      if serialized.get('reviewed_date'):
        try:
          rev_date = datetime.fromisoformat(serialized['reviewed_date'])
          serialized['reviewed_date_display'] = rev_date.strftime('%m/%d/%Y %I:%M %p')
        except (ValueError, TypeError):
          serialized['reviewed_date_display'] = serialized['reviewed_date']

      recommendations.append(serialized)

    # -----------------------------------------------------------------
    # Summary counts: dedup to latest per SKU (unchanged from dedup patch)
    # -----------------------------------------------------------------
    cursor.execute("""
        WITH latest_per_sku AS (
            SELECT status,
                   ROW_NUMBER() OVER (
                       PARTITION BY sku
                       ORDER BY recommendation_date DESC, recommendation_id DESC
                   ) AS rn
            FROM dbo.ai_classification_recommendations
        )
        SELECT status, COUNT(*) AS cnt
        FROM latest_per_sku
        WHERE rn = 1
        GROUP BY status
    """)
    raw_counts = {row['status']: row['cnt'] for row in cursor.fetchall()}

    counts = {}
    for key, val in raw_counts.items():
      normalized = key.title() if key else key
      counts[normalized] = counts.get(normalized, 0) + val

    # -----------------------------------------------------------------
    # Last review date from ai_agent_log
    # -----------------------------------------------------------------
    cursor.execute("""
        SELECT TOP 1 action_timestamp
        FROM dbo.ai_agent_log
        WHERE action_type = 'Classification_Review_Completed'
        ORDER BY action_timestamp DESC
    """)
    last_review_row = cursor.fetchone()
    last_review_date = None
    if last_review_row and last_review_row['action_timestamp']:
      last_review_date = make_serializable(last_review_row['action_timestamp'])

    conn.close()

    return {
      'status': 'success',
      'recommendations': recommendations,
      'counts': counts,
      'last_review_date': last_review_date
    }

  except Exception as e:
    return {
      'status': 'error',
      'message': str(e),
      'recommendations': [],
      'counts': {},
      'last_review_date': None
    }


# =============================================================================
# GET CLASSIFICATION REVIEW STATS  (replaces lines ~286-355)
# =============================================================================

@anvil.server.callable
def get_classification_review_stats():
  """
    Lightweight counts query for the dashboard header cards. Same
    per-SKU dedup as the main grid: counts SKUs by their LATEST
    recommendation status.

    Returns:
        dict: {
            'status': 'success' or 'error',
            'counts': {status: count},
            'pending': int,
            'approved': int,
            'rejected': int,
            'applied': int,
            'last_review_date': timestamp or None
        }
  """
  try:
    conn = get_connection()
    cursor = conn.cursor(as_dict=True)

    # Status counts - one row per SKU (latest only)
    cursor.execute("""
        WITH latest_per_sku AS (
            SELECT status,
                   ROW_NUMBER() OVER (
                       PARTITION BY sku
                       ORDER BY recommendation_date DESC, recommendation_id DESC
                   ) AS rn
            FROM dbo.ai_classification_recommendations
        )
        SELECT status, COUNT(*) AS cnt
        FROM latest_per_sku
        WHERE rn = 1
        GROUP BY status
    """)
    raw_counts = {row['status']: row['cnt'] for row in cursor.fetchall()}

    # Normalize status keys to title case with underscores preserved
    counts = {}
    for key, val in raw_counts.items():
      normalized = key.title() if key else key
      counts[normalized] = counts.get(normalized, 0) + val

    # Last review date from ai_agent_log
    cursor.execute("""
        SELECT TOP 1 action_timestamp
        FROM dbo.ai_agent_log
        WHERE action_type = 'Classification_Review_Completed'
        ORDER BY action_timestamp DESC
    """)
    last_review_row = cursor.fetchone()
    last_review_date = None
    if last_review_row and last_review_row['action_timestamp']:
      last_review_date = make_serializable(last_review_row['action_timestamp'])

    conn.close()

    return {
      'status': 'success',
      'counts': counts,
      'pending': counts.get('Pending', 0) + counts.get('Pending_Review', 0),
      'approved': counts.get('Approved', 0),
      'rejected': counts.get('Rejected', 0),
      'applied': counts.get('Applied', 0),
      'last_review_date': last_review_date
    }

  except Exception as e:
    return {
      'status': 'error',
      'message': str(e),
      'counts': {},
      'pending': 0,
      'approved': 0,
      'rejected': 0,
      'applied': 0,
      'last_review_date': None
    }


# =============================================================================
# UPDATE DECISION (single recommendation)
# =============================================================================

@anvil.server.callable
def update_classification_decision(recommendation_id, decision, notes=None, reviewed_by='Production Manager'):
  """
    Update the decision for a single classification recommendation.

    Args:
        recommendation_id: The ID of the recommendation to update
        decision: 'Approved' or 'Rejected'
        notes: Optional reviewer notes
        reviewed_by: Who made the decision

    Returns:
        dict: {status, message}
  """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE dbo.ai_classification_recommendations
        SET status = %s,
            notes = %s,
            reviewed_by = %s,
            reviewed_date = GETDATE()
        WHERE recommendation_id = %s
    """, (decision, notes, reviewed_by, recommendation_id))

    affected = cursor.rowcount
    conn.commit()
    conn.close()

    if affected > 0:
      return {
        'status': 'success',
        'message': 'Updated recommendation ' + str(recommendation_id) + ' to ' + decision
      }
    else:
      return {
        'status': 'error',
        'message': 'Recommendation ' + str(recommendation_id) + ' not found'
      }

  except Exception as e:
    return {
      'status': 'error',
      'message': str(e)
    }


# =============================================================================
# BULK UPDATE DECISIONS
# =============================================================================

@anvil.server.callable
def bulk_update_classification_decisions(updates, reviewed_by='Production Manager'):
  """
    Bulk update recommendation decisions from the review grid.

    Args:
        updates: list of dicts, each with:
            - 'recommendation_id': int
            - 'decision': 'Approved' or 'Rejected'
            - 'notes': optional string
        reviewed_by: Who made the decisions

    Returns:
        dict: {status, updated_count, message}
  """
  if not updates:
    return {'status': 'error', 'message': 'No updates provided'}

  conn = get_connection()
  updated_count = 0

  try:
    cursor = conn.cursor()

    for update in updates:
      decision = update['decision']
      rec_id = update['recommendation_id']
      notes = update.get('notes')

      cursor.execute("""
          UPDATE dbo.ai_classification_recommendations
          SET status = %s,
              notes = %s,
              reviewed_by = %s,
              reviewed_date = GETDATE()
          WHERE recommendation_id = %s
            AND (status = 'Pending' OR status = 'Pending_Review')
      """, (decision, notes, reviewed_by, rec_id))

      updated_count += cursor.rowcount

    conn.commit()
    return {
      'status': 'success',
      'updated_count': updated_count,
      'message': 'Updated ' + str(updated_count) + ' recommendations'
    }

  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': str(e)}

  finally:
    conn.close()


# =============================================================================
# APPLY APPROVED CLASSIFICATIONS
# =============================================================================

@anvil.server.callable
def apply_approved_classifications(reviewed_by='Production Manager'):
  """
    Apply all approved classification recommendations to production via stored procedure.
    Uses usp_Apply_A_Item_Classification to handle all updates and logging
    in a single database transaction (avoids Anvil server timeout).

    Returns:
        dict: {
            'status': 'success' or 'error',
            'applied_count': number of SKUs updated,
            'message': summary message
        }
  """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("EXEC dbo.usp_Apply_A_Item_Classification @reviewed_by = %s", (reviewed_by,))

    # Skip past any intermediate result sets to reach the final SELECT
    row = cursor.fetchone()
    while row is None and cursor.nextset():
      row = cursor.fetchone()

    conn.commit()
    conn.close()

    if row:
      # Handle both dict-style and index-style column access
      try:
        applied_count = int(row['applied_count'] or 0)
        summary = row['summary'] or ''
      except (KeyError, TypeError):
        applied_count = int(row[0] or 0)
        summary = str(row[1] or '') if len(row) > 1 else ''

      # Check if the SP returned an error
      if summary.startswith('Error:'):
        return {
          'status': 'error',
          'applied_count': 0,
          'message': summary
        }

      return {
        'status': 'success',
        'applied_count': applied_count,
        'message': summary
      }
    else:
      return {
        'status': 'success',
        'applied_count': 0,
        'message': 'No approved recommendations to apply'
      }

  except Exception as e:
    return {
      'status': 'error',
      'applied_count': 0,
      'message': str(e)
    }


# =============================================================================
# SKU SEARCH (for autocomplete / browser)
# =============================================================================

@anvil.server.callable
def search_classification_skus(search_text):
  """
    Search for SKUs in a_items_master for the SKU browser.
    Returns a lightweight list for autocomplete/selection.

    Args:
        search_text: partial SKU to search

    Returns:
        dict with:
            - 'status': 'success' or 'error'
            - 'skus': list of dicts with sku, classification, total_sold, running_pct, is_stocked
  """
  if not search_text or len(search_text) < 2:
    return {'status': 'success', 'skus': []}

  try:
    conn = get_connection()
    cursor = conn.cursor(as_dict=True)

    cursor.execute("""
        SELECT TOP 20
            sku,
            abc_classification AS classification,
            total_sold_yearly AS total_sold,
            running_pct_of_all AS running_pct,
            is_stocked_at_starlite AS is_stocked,
            classification_date AS last_updated
        FROM dbo.a_items_master
        WHERE sku LIKE %s
        ORDER BY sku
    """, ('%' + search_text + '%',))

    skus = [serialize_row(row) for row in cursor.fetchall()]
    conn.close()

    return {'status': 'success', 'skus': skus}

  except Exception as e:
    return {'status': 'error', 'message': str(e), 'skus': []}


# =============================================================================
# SKU BROWSER - Detail View
# =============================================================================

@anvil.server.callable
def get_classification_sku_detail(sku):
  """
    Get comprehensive classification detail for a single SKU.
    Used by the SKU browser panel.

    Returns:
        dict with:
            - 'status': 'success' or 'error'
            - 'sku_data': current a_items_master values
            - 'history': past recommendations for this SKU
            - 'demand_pattern': from min_max_calculated
            - 'sales_summary': monthly usage data
  """
  try:
    conn = get_connection()
    cursor = conn.cursor(as_dict=True)

    # -----------------------------------------------------------------
    # Current classification data from a_items_master
    # -----------------------------------------------------------------
    cursor.execute("""
        SELECT
            sku,
            abc_classification AS classification,
            total_sold_yearly AS total_sold,
            running_pct_of_all AS running_pct,
            is_stocked_at_starlite AS is_stocked,
            classification_date AS last_updated
        FROM dbo.a_items_master
        WHERE sku = %s
    """, (sku,))
    sku_row = cursor.fetchone()

    if not sku_row:
      conn.close()
      return {
        'status': 'error',
        'message': 'SKU ' + sku + ' not found in a_items_master'
      }

    sku_data = serialize_row(sku_row)

    # Format last updated date
    if sku_data.get('last_updated'):
      try:
        upd_date = datetime.fromisoformat(sku_data['last_updated'])
        sku_data['last_updated_display'] = upd_date.strftime('%m/%d/%Y %I:%M %p')
        sku_data['last_updated_short'] = upd_date.strftime('%m/%d/%Y')
      except (ValueError, TypeError):
        sku_data['last_updated_display'] = sku_data['last_updated']
        sku_data['last_updated_short'] = sku_data['last_updated']

    # -----------------------------------------------------------------
    # Months as A Item from most recent recommendation
    # -----------------------------------------------------------------
    cursor.execute("""
        SELECT TOP 1 months_as_a_item
        FROM dbo.ai_classification_recommendations
        WHERE sku = %s
        ORDER BY recommendation_date DESC
    """, (sku,))
    months_row = cursor.fetchone()
    if months_row:
      sku_data['months_as_a'] = months_row['months_as_a_item']
    else:
      sku_data['months_as_a'] = None

    # -----------------------------------------------------------------
    # Demand pattern and usage stats from min_max_calculated
    # -----------------------------------------------------------------
    cursor.execute("""
        SELECT
            demand_pattern, avg_daily_usage, coefficient_of_variation,
            days_with_sales, days_of_data, avg_order_size
        FROM dbo.min_max_calculated
        WHERE sku = %s
    """, (sku,))
    demand_row = cursor.fetchone()
    demand_data = serialize_row(demand_row) if demand_row else {}

    # -----------------------------------------------------------------
    # Recent monthly sales from usage_data (last 12 months)
    # -----------------------------------------------------------------
    cursor.execute("""
        SELECT
            DATEPART(YEAR, usage_date) AS year,
            DATEPART(MONTH, usage_date) AS month,
            SUM(qty_sold) AS total_qty,
            SUM(order_count) AS total_orders
        FROM dbo.usage_data
        WHERE sku = %s
          AND usage_date >= DATEADD(MONTH, -12, GETDATE())
        GROUP BY DATEPART(YEAR, usage_date), DATEPART(MONTH, usage_date)
        ORDER BY year DESC, month DESC
    """, (sku,))
    usage_rows = cursor.fetchall()

    sales_summary = []
    for u_row in usage_rows:
      sales_summary.append(serialize_row(u_row))

    # -----------------------------------------------------------------
    # Recommendation history (most recent first)
    # -----------------------------------------------------------------
    cursor.execute("""
        SELECT
            recommendation_id,
            change_type,
            current_classification,
            new_classification,
            confidence_level,
            reasoning,
            recommendation_date,
            status,
            reviewed_by,
            reviewed_date
        FROM dbo.ai_classification_recommendations
        WHERE sku = %s
        ORDER BY recommendation_date DESC
    """, (sku,))
    history_rows = cursor.fetchall()

    history = []
    for h_row in history_rows:
      h_serialized = serialize_row(h_row)
      # Format dates
      if h_serialized.get('recommendation_date'):
        try:
          h_date = datetime.fromisoformat(h_serialized['recommendation_date'])
          h_serialized['recommendation_date_display'] = h_date.strftime('%m/%d/%Y')
        except (ValueError, TypeError):
          h_serialized['recommendation_date_display'] = h_serialized['recommendation_date']
      history.append(h_serialized)

    conn.close()

    return {
      'status': 'success',
      'sku_data': sku_data,
      'history': history,
      'demand_data': demand_data,
      'sales_summary': sales_summary
    }

  except Exception as e:
    return {
      'status': 'error',
      'message': str(e)
    }


# =============================================================================
# A-ITEMS EXCEL EXPORT — shared by abc_classification + minmax_review forms
# =============================================================================

# Workbook styling (kept consistent with the Purchasing export in
# forecast_display.py so the two exports look like part of the same suite).
_EXP_HEADER_FILL = PatternFill(start_color='1F4E79', end_color='1F4E79', fill_type='solid')
_EXP_HEADER_FONT = Font(bold=True, color='FFFFFF', size=11)


def _exp_style_header_row(ws, row_idx, num_cols):
  for col in range(1, num_cols + 1):
    cell = ws.cell(row=row_idx, column=col)
    cell.fill = _EXP_HEADER_FILL
    cell.font = _EXP_HEADER_FONT
    cell.alignment = Alignment(horizontal='center', vertical='center')


def _exp_auto_width(ws, max_width=40):
  for column in ws.columns:
    max_len = 0
    col_letter = get_column_letter(column[0].column)
    for cell in column:
      try:
        if cell.value is not None:
          max_len = max(max_len, len(str(cell.value)))
      except Exception:
        pass
    ws.column_dimensions[col_letter].width = min(max_len + 2, max_width)


@anvil.server.callable
def get_a_items_export():
  """
  Build an Excel workbook of currently-stocked A-item SKUs, with one
  sheet per series. Returns anvil.BlobMedia (xlsx) for client-side
  download. Used by the Export button on both the abc_classification
  and minmax_review forms.

  Population: a_items_master rows where abc_classification = 'A' AND
              is_stocked_at_starlite = 1 (the source of truth, set by
              the ABC review workflow).

  Columns per row:
      SKU
      Classification
      Min                   (min_max_calculated.min_stock)
      Max                   (min_max_calculated.max_stock)
      Avg Daily Usage       (min_max_calculated.avg_daily_usage)
      Total Sold            (a_items_master.total_sold_yearly)
      Months as A-Item      (DATEDIFF(month, classification_date, GETDATE()))
      Pattern               (min_max_calculated.demand_pattern)
      Starlite OH           (inventory_current where cost_center='Starlite')
      Hub OH                (inventory_current where cost_center='Hub')
      Days of Supply        (Starlite OH / avg_daily_usage when usage > 0)

  Returns:
      anvil.BlobMedia (xlsx).
      Or {'status': 'error', 'error': '...'} if no A-items exist.
  """
  conn = get_connection()
  try:
    cursor = conn.cursor()
    # "Months as A-Item" tenure source:
    #
    # We CANNOT use aim.classification_date — usp_Apply_A_Item_Classification
    # resets that to GETDATE() on every apply run, including UPDATE-type
    # recommendations (monthly metric refreshes that don't change A/B
    # status). That makes classification_date reflect "row last touched",
    # not "A-item since".
    #
    # Source of truth is ai_agent_log: each B->A transition is logged
    # as a CLASSIFICATION_CHANGE row. We find the most recent promotion
    # per SKU and DATEDIFF from there. Two log formats are in the wild:
    #   * Older single-letter:  new_value = 'A' (old_value = 'B')
    #   * Newer proc-formatted: new_value LIKE 'PROMOTION:%'
    # The OR catches both. Fallback to classification_date only if no
    # promotion log exists at all (rare, but covers seeded-from-inception
    # SKUs).
    # "Months as A-Item" tenure resolution chain (v5.12.4):
    #   1. Most-recent promotion-to-A from ai_agent_log (covers SKUs that
    #      were demoted then re-promoted — current tenure starts at the
    #      re-promotion).
    #   2. Earliest log row showing the SKU as A (PROMOTION row, UPDATE: A
    #      row, or old literal A row). Covers SKUs that have been A since
    #      system inception and never had a transition event.
    #   3. aim.classification_date as last-resort fallback (rare; SKU with
    #      no CLASSIFICATION_CHANGE log rows at all).
    #
    # v5.12.4 was needed because step (1) on its own caught only ~20 of
    # ~194 stocked A-items — most have been A continuously and have no
    # promotion event. The previous fallback to classification_date was
    # wrong because usp_Apply_A_Item_Classification rewrites that field
    # on every monthly refresh, so it doesn't reflect tenure.
    cursor.execute("""
      SELECT
          RTRIM(aim.sku)                AS sku,
          aim.abc_classification        AS classification,
          aim.total_sold_yearly         AS total_sold,
          DATEDIFF(
              month,
              COALESCE(
                  prom.last_promotion_ts,
                  first_a.first_a_ts,
                  aim.classification_date
              ),
              GETDATE()
          )                             AS months_as_a_item,
          ISNULL(RTRIM(ps.series), '')  AS series,
          mmc.min_stock                 AS min_stock,
          mmc.max_stock                 AS max_stock,
          mmc.avg_daily_usage           AS avg_daily_usage,
          mmc.demand_pattern            AS demand_pattern,
          star.on_hand                  AS starlite_on_hand,
          hub.on_hand                   AS hub_on_hand,
          CASE
              WHEN mmc.avg_daily_usage > 0 AND star.on_hand IS NOT NULL
              THEN CAST(star.on_hand AS DECIMAL(18,4)) / mmc.avg_daily_usage
              ELSE NULL
          END                           AS days_of_supply
      FROM dbo.a_items_master aim
      LEFT JOIN dbo.min_max_calculated mmc
          ON RTRIM(aim.sku) = RTRIM(mmc.sku)
      LEFT JOIN dbo.part_specs ps
          ON RTRIM(aim.sku) = RTRIM(ps.sku)
      LEFT JOIN dbo.inventory_current star
          ON RTRIM(star.sku) = RTRIM(aim.sku)
         AND star.cost_center = 'Starlite'
      LEFT JOIN dbo.inventory_current hub
          ON RTRIM(hub.sku) = RTRIM(aim.sku)
         AND hub.cost_center = 'Hub'

      -- (1) Most-recent promotion event TO 'A'. Covers both formats:
      --     new:  new_value LIKE 'PROMOTION:%'
      --     old:  new_value='A' AND old_value IN ('B','C')
      -- v5.12.4 added 'C' to the old-format IN list; v5.12.3 only
      -- caught B->A.
      -- old_value/new_value are SQL Server `text` type, which does not
      -- support '=' or '<>'. CAST to varchar(50) before equality.
      LEFT JOIN (
          SELECT RTRIM(sku) AS sku,
                 MAX(action_timestamp) AS last_promotion_ts
          FROM dbo.ai_agent_log
          WHERE action_type = 'CLASSIFICATION_CHANGE'
            AND (
                  new_value LIKE 'PROMOTION:%'
                  OR (
                        CAST(new_value AS varchar(50)) = 'A'
                    AND CAST(old_value AS varchar(50)) IN ('B', 'C')
                  )
                )
          GROUP BY RTRIM(sku)
      ) prom ON prom.sku = RTRIM(aim.sku)

      -- (2) v5.12.4: Earliest log evidence that the SKU was A. Used as
      -- the tenure anchor for continuously-A SKUs that have no
      -- promotion row in the log. Matches any of:
      --   - PROMOTION: row (new format, any source class)
      --   - UPDATE: A row (monthly refresh of an already-A SKU)
      --   - literal 'A' (old format)
      LEFT JOIN (
          SELECT RTRIM(sku) AS sku,
                 MIN(action_timestamp) AS first_a_ts
          FROM dbo.ai_agent_log
          WHERE action_type = 'CLASSIFICATION_CHANGE'
            AND (
                  new_value LIKE 'PROMOTION:%'
                  OR new_value LIKE 'UPDATE: A%'
                  OR CAST(new_value AS varchar(50)) = 'A'
                )
          GROUP BY RTRIM(sku)
      ) first_a ON first_a.sku = RTRIM(aim.sku)

      WHERE aim.abc_classification = 'A'
        AND aim.is_stocked_at_starlite = 1
      ORDER BY series, RTRIM(aim.sku)
    """)
    rows = cursor.fetchall()
  finally:
    conn.close()

  if not rows:
    return {'status': 'error',
            'error': 'No stocked A-item SKUs found in a_items_master.'}

  # Group rows by series
  by_series = {}
  for r in rows:
    series = r['series'] or '(no series)'
    by_series.setdefault(series, []).append(r)

  # Build workbook — single "A-Items" sheet with each series visually
  # banded as a section, separated by a blank row.
  wb = Workbook()
  ws = wb.active
  ws.title = 'A-Items'

  headers = [
    'SKU', 'Classification', 'Min', 'Max', 'Avg Daily Usage',
    'Total Sold', 'Months as A-Item', 'Pattern',
    'Starlite OH', 'Hub OH', 'Days of Supply',
  ]
  num_cols = len(headers)
  last_col_letter = get_column_letter(num_cols)

  # Series banner styling: lighter blue band, bold dark blue text
  series_banner_fill = PatternFill(start_color='D9E2F3', end_color='D9E2F3',
                                   fill_type='solid')
  series_banner_font = Font(bold=True, size=12, color='1F4E79')

  # ---- Title rows ----
  ws['A1'] = 'A-Item SKUs Export'
  ws['A1'].font = Font(bold=True, size=14, color='1F4E79')
  ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=num_cols)

  ws['A2'] = (f'Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}    '
              f'Series: {len(by_series)}    '
              f'SKUs: {sum(len(v) for v in by_series.values())}')
  ws['A2'].font = Font(italic=True, size=10, color='666666')
  ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=num_cols)

  # ---- Column header row (single, at top) ----
  header_row_idx = 4
  for c_idx, name in enumerate(headers, start=1):
    ws.cell(row=header_row_idx, column=c_idx, value=name)
  _exp_style_header_row(ws, header_row_idx, num_cols)

  # ---- Series sections ----
  current_row = header_row_idx + 1
  for series in sorted(by_series.keys()):
    members = by_series[series]

    # Series banner row — merged across all columns
    banner_row = current_row
    ws.cell(row=banner_row, column=1,
            value=f'{series}    ({len(members)} SKUs)')
    ws.cell(row=banner_row, column=1).font = series_banner_font
    ws.cell(row=banner_row, column=1).alignment = Alignment(horizontal='left',
                                                            vertical='center')
    # Apply fill to every cell in the banner row so the band color extends
    # across the merged region in older Excel viewers
    for c in range(1, num_cols + 1):
      ws.cell(row=banner_row, column=c).fill = series_banner_fill
    ws.merge_cells(start_row=banner_row, start_column=1,
                   end_row=banner_row,   end_column=num_cols)
    current_row += 1

    # Data rows for this series
    for row in members:
      ws.cell(row=current_row, column=1, value=row['sku'])
      ws.cell(row=current_row, column=2, value=row['classification'])
      ws.cell(row=current_row, column=3,
              value=float(row['min_stock']) if row['min_stock'] is not None else None)
      ws.cell(row=current_row, column=4,
              value=float(row['max_stock']) if row['max_stock'] is not None else None)
      ws.cell(row=current_row, column=5,
              value=float(row['avg_daily_usage']) if row['avg_daily_usage'] is not None else None)
      ws.cell(row=current_row, column=6,
              value=float(row['total_sold']) if row['total_sold'] is not None else None)
      ws.cell(row=current_row, column=7,
              value=int(row['months_as_a_item']) if row['months_as_a_item'] is not None else None)
      ws.cell(row=current_row, column=8, value=row['demand_pattern'])
      ws.cell(row=current_row, column=9,
              value=float(row['starlite_on_hand']) if row['starlite_on_hand'] is not None else None)
      ws.cell(row=current_row, column=10,
              value=float(row['hub_on_hand']) if row['hub_on_hand'] is not None else None)
      ws.cell(row=current_row, column=11,
              value=float(row['days_of_supply']) if row['days_of_supply'] is not None else None)

      # Number formats
      for col in (3, 4, 6, 9, 10):  # integer-style counts
        ws.cell(row=current_row, column=col).number_format = '#,##0'
      for col in (5, 11):           # decimals
        ws.cell(row=current_row, column=col).number_format = '#,##0.00'
      ws.cell(row=current_row, column=7).number_format = '0'  # months

      current_row += 1

    # Blank separator row between series (skip after the last series)
    current_row += 1

  # Freeze the column headers (title + meta + header row stay visible)
  ws.freeze_panes = ws.cell(row=header_row_idx + 1, column=1)
  _exp_auto_width(ws, max_width=22)

  # Save to BytesIO and wrap in BlobMedia
  buf = io.BytesIO()
  wb.save(buf)
  buf.seek(0)

  timestamp = datetime.now().strftime('%Y%m%d_%H%M')
  filename = f'A_Items_Export_{timestamp}.xlsx'
  return anvil.BlobMedia(
    content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    content=buf.read(),
    name=filename,
  )


# =============================================================================
# GET RECOMMENDATION DETAIL (expandable row)
# =============================================================================

@anvil.server.callable
def get_classification_recommendation_detail(recommendation_id):
  """
    Get full detail for a single classification recommendation.
    Used when expanding a row in the review grid.

    Returns:
        dict with full recommendation data including reasoning and demand pattern
  """
  try:
    conn = get_connection()
    cursor = conn.cursor(as_dict=True)

    cursor.execute("""
        SELECT
            r.recommendation_id,
            r.sku,
            r.change_type,
            r.current_classification,
            r.current_total_sold,
            r.current_running_pct,
            r.current_is_stocked,
            r.new_classification,
            r.new_total_sold,
            r.new_running_pct,
            r.new_is_stocked,
            r.months_as_a_item,
            r.classification_changes_12mo,
            r.confidence_level,
            r.reasoning,
            r.recommendation_date,
            r.status,
            r.reviewed_by,
            r.reviewed_date,
            r.notes,
            m.demand_pattern,
            m.avg_daily_usage,
            m.std_dev,
            m.coefficient_of_variation,
            m.days_with_sales,
            m.days_of_data,
            m.avg_order_size,
            m.total_orders,
            m.lead_time,
            m.safety_days
        FROM dbo.ai_classification_recommendations r
        LEFT JOIN dbo.min_max_calculated m ON r.sku = m.sku
        WHERE r.recommendation_id = %s
    """, (recommendation_id,))

    row = cursor.fetchone()
    conn.close()

    if not row:
      return {'status': 'error', 'message': 'Recommendation not found'}

    detail = serialize_row(row)

    return {'status': 'success', 'detail': detail}

  except Exception as e:
    return {'status': 'error', 'message': str(e)}


# =============================================================================
# GET REVIEW TASK STATUS (for background task polling)
# =============================================================================

@anvil.server.callable
def get_classification_review_task_status(task_id):
  """
    Poll the status of a running classification review background task.
    Used by the UI Timer to show progress.

    Args:
        task_id: The background task ID

    Returns:
        dict with task state information
  """
  try:
    task = anvil.server.get_background_task(task_id)
    state = task.get_state()

    return {
      'status': state.get('status', 'Unknown'),
      'current_sku': state.get('current_sku', ''),
      'change_type': state.get('change_type', ''),
      'progress': state.get('progress', 0),
      'total_candidates': state.get('total_recommendations', 0),
      'summary': state.get('summary', ''),
      'is_running': task.is_running(),
      'is_completed': task.is_completed()
    }

  except Exception as e:
    return {
      'status': 'Error: ' + str(e),
      'is_running': False,
      'is_completed': True
    }