"""
Anvil Server Module - Transfer Dashboard
=========================================
Server-side code for the Titan Flow Control Transfer Dashboard Anvil app.

This module provides database connectivity and business logic for:
    - Fetching pending transfer recommendations
    - Updating decisions (Approve/Reject) and quantities
    - Applying approved transfers and generating CSV
    - Logging actions to audit trail

Database: SQL Server (nexus) via pymssql
Uses centralized db_config for credentials (Anvil Secrets)

Author: Dave Whritenour / Claude AI Assistant
Project: Titan Flow Control - Year of Automation
Created: January 2026

Changelog
  2026-09-05 (Transfer Logic Review findings H1 / H3 / M7):
    - Every decision UPDATE re-asserts status IN ('Pending','Paused').
      A grid loaded 20 minutes ago can no longer resurrect a rec the
      stale sweep already obsoleted (or that a pick already consumed).
    - bulk_update_decisions writes manager_override_qty ONLY when the
      manager sent a quantity; an approval with the box untouched keeps
      the existing override instead of nulling it back to the AI qty.
    - reject_pending accepts the ids the manager is looking at (client
      may pass them), appends to notes instead of overwriting, and
      refuses recs that have pick lines - same guard as bulk Rejected.
"""

import anvil.email
import anvil.server
import anvil.media
from datetime import datetime, date
from decimal import Decimal

# Import centralized configuration
from . import db_config
from .db_config import TRANSFER_CONSTANTS  # noqa: F401  (kept for future use)

# Pick List PDF generator (temporary replacement for the CSV export;
# will be superseded once the e21 tie-in is in place)
# v2.0 Phase 7: pick-list import removed with apply_transfers (per-pick
# slips live in pick_dashboard.download_pick_slip_for_pick).


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


def _to_int(v):
  """Coerce a numeric value to int; 0 on failure."""
  try:
    return int(round(float(v)))
  except (TypeError, ValueError):
    return 0


def _fmt_int(v):
  """Render a numeric demand/qty value as a clean integer string."""
  return str(_to_int(v))


def _attach_demand_detail(conn, rows):
  """v5.21: Attach demand-bucket + open sales-order strings to each pending row.

  Additive and DEFENSIVE: any failure leaves rows untouched so the grid still
  loads. Two read-only fields are added to every row dict:
    - demand_buckets : weekly 0-7 / 8-14 / 15-21 / 22-28 day increments,
                       read straight from dbo.vw_sku_demand_buckets.
    - sales_orders   : open order list 'number-rel:qty', soonest ship first,
                       within the 28-day window, from dbo.sales_orders_due.
  """
  if not rows:
    return
  skus = sorted({r['sku'] for r in rows if r.get('sku')})
  if not skus:
    return
  try:
    placeholders = ','.join(['%s'] * len(skus))
    cur = conn.cursor()

    # Weekly demand buckets (one row per SKU) from the view
    cur.execute(
      "SELECT sku, week_1_incremental, week_2_incremental, "
      "week_3_incremental, week_4_incremental "
      "FROM dbo.vw_sku_demand_buckets WHERE sku IN (" + placeholders + ")",
      tuple(skus))
    buckets = {}
    for b in cur.fetchall():
      w1 = _to_int(b['week_1_incremental'])
      w2 = _to_int(b['week_2_incremental'])
      w3 = _to_int(b['week_3_incremental'])
      w4 = _to_int(b['week_4_incremental'])
      buckets[b['sku']] = (
        "0-7d: " + str(w1) +
        " | 8-14d: " + str(w2) +
        " | 15-21d: " + str(w3) +
        " | 22-28d: " + str(w4) +
        " | Total: " + str(w1 + w2 + w3 + w4))

    # Open sales orders (balance due) within 28 days, soonest ship first
    cur.execute(
      "SELECT nx_sku, nx_order_numb, nx_rel_numb, nx_qty_balance_due "
      "FROM dbo.sales_orders_due "
      "WHERE nx_qty_balance_due > 0 "
      "AND DATEDIFF(day, CAST(GETDATE() AS DATE), nx_dship_by_date) <= 28 "
      "AND nx_sku IN (" + placeholders + ") "
      "ORDER BY nx_sku, nx_dship_by_date ASC",
      tuple(skus))
    orders = {}
    for o in cur.fetchall():
      label = str(o['nx_order_numb'])
      if o.get('nx_rel_numb'):
        label = label + "-" + str(o['nx_rel_numb'])
      orders.setdefault(o['nx_sku'], []).append(
        label + ":" + _fmt_int(o['nx_qty_balance_due']))

    for r in rows:
      sku = r.get('sku')
      r['demand_buckets'] = buckets.get(sku, 'No demand in 28-day horizon')
      ord_list = orders.get(sku, [])
      r['sales_orders'] = ', '.join(ord_list) if ord_list else 'No open sales orders'
  except Exception as e:
    # Non-fatal: demand detail is a display nicety, never block the grid
    print("demand-detail enrichment skipped: " + str(e))



# =============================================================================
# DATA RETRIEVAL FUNCTIONS
# =============================================================================

@anvil.server.callable
def get_pending_transfers():
  """
    Retrieve all pending transfer recommendations for the DataGrid.
    
    Returns:
        dict: {
            'status': 'success' or 'error',
            'rows': list of transfer recommendation dicts,
            'count': number of rows,
            'message': error message if applicable
        }
    """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    query = """
        SELECT 
            recommendation_id,
            sku,
            event_type,
            inventory_urgency,
            time_urgency,
            starlite_available,
            starlite_on_hand,
            starlite_effective_on_hand,   -- v5.6: snapshot of virtual Starlite inventory at rec time
            hub_available,
            hub_on_hand,
            hub_effective_on_hand,        -- v5.6: snapshot of virtual Hub inventory at rec time
            min_stock,
            max_stock,
            total_demand,
            order_count,
            earliest_ship_date,
            latest_ship_date,
            calculated_transfer_qty,
            ai_recommended_qty,
            manager_override_qty,
            confidence_level,
            reasoning,
            risk_factors,
            seasonal_consideration,
            status,
            batch_id,
            recommendation_date,
            notes,
            paused_date,
            DATEDIFF(day, paused_date, GETDATE()) AS days_paused,
            ISNULL(has_duplicate_warning, 0) AS has_duplicate_warning
        FROM dbo.ai_transfer_recommendations
        WHERE status IN ('Pending', 'Paused')
        ORDER BY
            CASE WHEN ISNULL(has_duplicate_warning, 0) = 1 THEN 1 ELSE 2 END,  -- v5.5: duplicate warnings first
            CASE status WHEN 'Paused' THEN 1 ELSE 2 END,                       -- then Paused items
            CASE inventory_urgency
                WHEN 'CRITICAL' THEN 1
                WHEN 'HIGH' THEN 2
                WHEN 'MEDIUM' THEN 3
                WHEN 'LOW' THEN 4
                ELSE 5
            END,
            event_type DESC,
            sku
    """

    cursor.execute(query)
    rows = [serialize_row(row) for row in cursor.fetchall()]

    _attach_demand_detail(conn, rows)

    conn.close()

    return {
      'status': 'success',
      'rows': rows,
      'count': len(rows),
      'message': f'Retrieved {len(rows)} pending transfers'
    }

  except Exception as e:
    return {
      'status': 'error',
      'rows': [],
      'count': 0,
      'message': str(e)
    }


@anvil.server.callable
def get_transfer_stats():
  """
    Get summary statistics for the dashboard header.
    
    Returns:
        dict: Counts by status and urgency
    """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    # Status counts
    cursor.execute("""
            SELECT status, COUNT(*) as count
            FROM dbo.ai_transfer_recommendations
            WHERE recommendation_date >= DATEADD(day, -7, GETDATE())
            GROUP BY status
        """)
    status_counts = {row['status']: row['count'] for row in cursor.fetchall()}

    # Urgency counts for pending only
    cursor.execute("""
            SELECT inventory_urgency, COUNT(*) as count
            FROM dbo.ai_transfer_recommendations
            WHERE status = 'Pending'
            GROUP BY inventory_urgency
        """)
    urgency_counts = {row['inventory_urgency']: row['count'] for row in cursor.fetchall()}

    conn.close()

    return {
      'status': 'success',
      'by_status': status_counts,
      'by_urgency': urgency_counts,
      'pending': status_counts.get('Pending', 0),
      'approved': status_counts.get('Approved', 0),
      'rejected': status_counts.get('Rejected', 0),
      'applied': status_counts.get('Applied', 0)
    }

  except Exception as e:
    return {
      'status': 'error',
      'message': str(e)
    }


# =============================================================================
# UPDATE FUNCTIONS
# =============================================================================

@anvil.server.callable
def update_decision(recommendation_id, decision, transfer_qty=None, notes=None, pause_reason=None, reviewed_by='Hub Manager'):
  """
    Update the decision for a single transfer recommendation.
    
    Args:
        recommendation_id: The ID of the recommendation to update
        decision: 'Approved', 'Rejected', 'Paused', or 'Pending'
        transfer_qty: Optional override quantity (saved to manager_override_qty)
        notes: Optional notes from reviewer
        pause_reason: Reason for pausing (required if decision='Paused')
        reviewed_by: Who made the decision
    
    Returns:
        dict: {status, message}
    """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    # v2.0 C6 guard: a rec with active pick lines is being physically
    # picked. Block any decision away from Approved, and block qty
    # drops below what the picks already hold.
    cursor.execute("""
        SELECT COALESCE(SUM(qty_planned), 0) AS allocated
        FROM dbo.transfer_pick_lines
        WHERE recommendation_id = %s
          AND status IN ('Assigned', 'Picked', 'Short')
    """, (recommendation_id,))
    _alloc = float(cursor.fetchone()['allocated'] or 0)
    if _alloc > 0:
      if decision != 'Approved':
        conn.close()
        return {'status': 'error',
                'message': (f"Rec {recommendation_id} has {_alloc:g} unit(s) on "
                            f"active pick lines - resolve or cancel the picks "
                            f"in the Pick Editor first (rule C6).")}
      if transfer_qty is not None and float(transfer_qty) < _alloc:
        conn.close()
        return {'status': 'error',
                'message': (f"Qty {transfer_qty} is below the {_alloc:g} unit(s) "
                            f"already assigned to picks - remove pick lines "
                            f"first (rule C6).")}

    # v2.0.1 zero-qty approval guard (2026-08-06 YS55EB0050 incident):
    # the Pick Editor pool and allocation guard resolve effective qty as
    # COALESCE(override, ai, calculated). Approving a rec whose AI qty
    # is 0 with no override makes it INVISIBLE to picking - it strands
    # in Approved and the detector raises duplicates. Force an explicit
    # quantity instead.
    if decision == 'Approved':
      cursor.execute("""
          SELECT sku,
                 CAST(COALESCE(%s, [manager_override_qty ], ai_recommended_qty,
                               calculated_transfer_qty) AS DECIMAL(18,4)) AS eff_qty,
                 CAST(calculated_transfer_qty AS DECIMAL(18,4)) AS calc_qty
          FROM dbo.ai_transfer_recommendations
          WHERE recommendation_id = %s
      """, (transfer_qty, recommendation_id))
      _eff_row = cursor.fetchone()
      if _eff_row and float(_eff_row['eff_qty'] or 0) <= 0:
        conn.close()
        return {'status': 'error',
                'message': (f"Rec {recommendation_id} ({_eff_row['sku']}): the AI "
                            f"recommended 0, so approving without a quantity would "
                            f"hide it from the Pick Editor. Enter a transfer qty "
                            f"(calculated need is {float(_eff_row['calc_qty'] or 0):g}) "
                            f"or Reject it.")}

    # Handle Paused status separately (needs paused_date and pause_reason)
    if decision == 'Paused':
      query = """
                UPDATE dbo.ai_transfer_recommendations
                SET status = 'Paused',
                    paused_date = GETDATE(),
                    pause_reason = %s,
                    notes = %s,
                    reviewed_by = %s,
                    reviewed_date = GETDATE()
                WHERE recommendation_id = %s
                  AND status IN ('Pending', 'Paused')
            """
      cursor.execute(query, (pause_reason, notes, reviewed_by, recommendation_id))

    # Handle Approved/Rejected with optional quantity override
    elif transfer_qty is not None:
      query = """
                UPDATE dbo.ai_transfer_recommendations
                SET status = %s,
                    manager_override_qty = %s,
                    notes = %s,
                    reviewed_by = %s,
                    reviewed_date = GETDATE()
                WHERE recommendation_id = %s
                  AND status IN ('Pending', 'Paused')
            """
      cursor.execute(query, (decision, transfer_qty, notes, reviewed_by, recommendation_id))

    else:
      query = """
                UPDATE dbo.ai_transfer_recommendations
                SET status = %s,
                    notes = %s,
                    reviewed_by = %s,
                    reviewed_date = GETDATE()
                WHERE recommendation_id = %s
                  AND status IN ('Pending', 'Paused')
            """
      cursor.execute(query, (decision, notes, reviewed_by, recommendation_id))

    affected = cursor.rowcount
    conn.commit()
    conn.close()

    if affected > 0:
      return {
        'status': 'success',
        'message': f'Updated recommendation {recommendation_id} to {decision}'
      }
    else:
      return {
        'status': 'error',
        'message': (f'Recommendation {recommendation_id} was not updated - it is '
                    f'no longer Pending/Paused (the review may have obsoleted it '
                    f'or a pick consumed it). Refresh the list.')
      }

  except Exception as e:
    return {
      'status': 'error',
      'message': str(e)
    }


@anvil.server.callable
def bulk_update_decisions(updates):
  """
    Bulk update recommendation decisions.
    Handles Approved, Rejected, and Paused statuses.
    """
  if not updates:
    return {'status': 'error', 'message': 'No updates provided'}

  conn = get_connection()
  updated_count = 0
  blocked_zero_qty = []  # v2.0.1: recs refused because effective qty <= 0

  try:
    cursor = conn.cursor()

    for update in updates:
      decision = update['decision']
      rec_id = update['recommendation_id']

      if decision == 'Approved':
        # v2.0.1 zero-qty approval guard - same rule as update_decision:
        # an approval whose effective qty resolves to 0 can never be
        # picked, so refuse it and report the SKU back to the form.
        cursor.execute("""
            SELECT sku,
                   CAST(COALESCE(%s, [manager_override_qty ], ai_recommended_qty,
                                 calculated_transfer_qty) AS DECIMAL(18,4)) AS eff_qty
            FROM dbo.ai_transfer_recommendations
            WHERE recommendation_id = %s
        """, (update.get('transfer_qty'), rec_id))
        _eff_row = cursor.fetchone()
        if _eff_row and float(_eff_row['eff_qty'] or 0) <= 0:
          blocked_zero_qty.append({'recommendation_id': rec_id,
                                   'sku': _eff_row['sku']})
          continue
        cursor.execute("""
                    UPDATE dbo.ai_transfer_recommendations
                    SET status = 'Approved',
                        reviewed_by = 'Hub_Manager',
                        reviewed_date = GETDATE(),
                        -- 2026-09-05 (H3): keep the existing override when the
                        -- manager did not type a quantity.
                        manager_override_qty = CASE WHEN %s IS NULL
                                                    THEN manager_override_qty
                                                    ELSE %s END
                    WHERE recommendation_id = %s
                      AND status IN ('Pending', 'Paused')
                      AND (%s IS NULL OR %s >=
                           (SELECT COALESCE(SUM(pl.qty_planned), 0)
                            FROM dbo.transfer_pick_lines pl
                            WHERE pl.recommendation_id = dbo.ai_transfer_recommendations.recommendation_id
                              AND pl.status IN ('Assigned', 'Picked', 'Short')))
        """, (update.get('transfer_qty'), update.get('transfer_qty'), rec_id,
              update.get('transfer_qty'), update.get('transfer_qty')))

      elif decision == 'Rejected':
        cursor.execute("""
                    UPDATE dbo.ai_transfer_recommendations
                    SET status = 'Rejected',
                        reviewed_by = 'Hub_Manager',
                        reviewed_date = GETDATE(),
                        snooze_until = %s
                    WHERE recommendation_id = %s
                      AND status IN ('Pending', 'Paused')
                      AND NOT EXISTS (SELECT 1 FROM dbo.transfer_pick_lines pl
                                      WHERE pl.recommendation_id = dbo.ai_transfer_recommendations.recommendation_id
                                        AND pl.status IN ('Assigned', 'Picked', 'Short'))
                """, (update.get('snooze_until'), rec_id))

      elif decision == 'Paused':
        cursor.execute("""
                    UPDATE dbo.ai_transfer_recommendations
                    SET status = 'Paused',
                        paused_date = GETDATE(),
                        pause_reason = %s,
                        reviewed_by = 'Hub_Manager',
                        reviewed_date = GETDATE()
                    WHERE recommendation_id = %s
                      AND status IN ('Pending', 'Paused')
                      AND NOT EXISTS (SELECT 1 FROM dbo.transfer_pick_lines pl
                                      WHERE pl.recommendation_id = dbo.ai_transfer_recommendations.recommendation_id
                                        AND pl.status IN ('Assigned', 'Picked', 'Short'))
                """, (update.get('pause_reason'), rec_id))

      updated_count += cursor.rowcount

    conn.commit()
    _blocked_msg = ''
    if blocked_zero_qty:
      _blocked_msg = ('%d rec(s) NOT approved - the AI recommended 0, so with no '
                      'override qty they would be invisible to the Pick Editor. '
                      'Set a transfer qty first (or Reject): %s'
                      % (len(blocked_zero_qty),
                         ', '.join(b['sku'] for b in blocked_zero_qty)))
    return {'status': 'success', 'updated_count': updated_count,
            'skipped_guarded': max(0, len(updates) - updated_count),
            'blocked_zero_qty': blocked_zero_qty,
            'message': _blocked_msg}

  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': str(e)}

  finally:
    conn.close()


# =============================================================================
# APPLY TRANSFERS — REMOVED (v2.0 Phase 7)
# =============================================================================
# apply_transfers() is gone. Pick creation is the Approved -> Applied
# trigger (decision B3, Phase 3+), the client stopped calling this in
# Phase 6, and per-pick slips come from pick_dashboard. See
# BUILD_Delivery_20260802.md.
# =============================================================================


@anvil.server.callable
def reject_pending(recommendation_ids=None):
  """
    Mark all remaining Pending items as Rejected.
    Useful for clearing the queue after review.
    
    Returns:
        dict: {status, rejected_count, message}
    """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    # 2026-09-05 (M7): if the client passes the ids it is displaying, only
    # those are rejected (a rec created since the grid loaded is left for
    # the manager to see). Notes are appended, not overwritten, and recs
    # with pick lines are refused - same guard as bulk Rejected.
    id_filter = ''
    params = []
    if recommendation_ids:
      ids = [int(i) for i in recommendation_ids]
      id_filter = ' AND recommendation_id IN (' + ','.join(['%s'] * len(ids)) + ')'
      params = ids

    cursor.execute("""
            UPDATE dbo.ai_transfer_recommendations
            SET status = 'Rejected',
                reviewed_by = 'Hub Manager',
                reviewed_date = GETDATE(),
                notes = CONCAT(ISNULL(CAST(notes AS nvarchar(max)), ''),
                               ' | [', CONVERT(varchar(16), GETDATE(), 120),
                               '] Bulk rejected via dashboard')
            WHERE status = 'Pending'
              AND NOT EXISTS (SELECT 1 FROM dbo.transfer_pick_lines pl
                              WHERE pl.recommendation_id = dbo.ai_transfer_recommendations.recommendation_id
                                AND pl.status IN ('Assigned', 'Picked', 'Short'))
        """ + id_filter, tuple(params))

    affected = cursor.rowcount
    conn.commit()
    conn.close()

    return {
      'status': 'success',
      'rejected_count': affected,
      'message': f'Rejected {affected} pending transfers'
    }

  except Exception as e:
    return {
      'status': 'error',
      'rejected_count': 0,
      'message': str(e)
    }


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

@anvil.server.callable
def test_connection():
  """
    Test database connectivity.
    Use this to verify the connection is working.
    """
  try:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT TOP 5 * FROM ai_transfer_recommendations WHERE status = 'Pending'")
    rows = [serialize_row(row) for row in cursor.fetchall()]
    conn.close()
    return {"status": "success", "rows": rows, "count": len(rows)}
  except Exception as e:
    return {"status": "error", "message": str(e)}


@anvil.server.callable
def get_recommendation_detail(recommendation_id):
  """
    Get full details for a single recommendation.
    Useful for a detail/popup view.
    """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
            SELECT *
            FROM dbo.ai_transfer_recommendations
            WHERE recommendation_id = %s
        """, (recommendation_id,))

    row = cursor.fetchone()
    conn.close()

    if row:
      return {
        'status': 'success',
        'recommendation': serialize_row(row)
      }
    else:
      return {
        'status': 'error',
        'message': f'Recommendation {recommendation_id} not found'
      }

  except Exception as e:
    return {
      'status': 'error',
      'message': str(e)
    }