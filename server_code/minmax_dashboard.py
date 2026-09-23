"""
Anvil Server Module - MinMax Dashboard (v61.2 - 2026-05-11)
==========================================================
Server-side code for the Titan Flow Control MinMax Review system.

CHANGES IN v61:
  * NEW: submit_manual_minmax_override(sku, override_min, override_max,
    override_reason) - lets a manager stage a manual Min/Max override for
    ANY SKU, regardless of whether the AI has flagged it for review.
    If a Pending or Modified recommendation already exists for the SKU,
    the most recent one is updated in place (status -> Modified, override
    fields set). Otherwise a new ai_minmax_recommendations row is inserted
    with confidence_level='Manual' and a 'MANUAL-' batch_id. The staged
    row appears in the main grid; Apply to Production commits it via the
    existing usp_Apply_Approved_MinMax SP.

  * Rolled in v60.1 hotfix to apply_approved_minmax: tolerant of
    intermediate result sets emitted by inner SPs, surfaces clearer error
    messages.

(Carries forward all v60 functionality: override columns on
ai_minmax_recommendations, 'Modified' workflow, validated bulk updates.)

Database: SQL Server (nexus) via pymssql
Uses centralized db_config for credentials (Anvil Secrets)

Author: Dave Whritenour / Claude AI Assistant
Project: Titan Flow Control - Year of Automation
"""

import anvil.server
from datetime import datetime, date
from decimal import Decimal

# Import centralized configuration
from . import db_config


# =============================================================================
# SERIALIZATION HELPERS
# =============================================================================

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


def serialize_row(row):
  return {key: make_serializable(val) for key, val in row.items()}


# =============================================================================
# DATABASE CONNECTION
# =============================================================================

def get_connection():
  return db_config.get_nexus_connection()


# =============================================================================
# INTERNAL: validate Modified row update payload
# =============================================================================

def _validate_modified_payload(update):
  reason = update.get('override_reason')
  if not reason or not str(reason).strip():
    return False, "Override reason is required for Modified decisions"

  try:
    omin = int(update.get('override_min'))
    omax = int(update.get('override_max'))
  except (TypeError, ValueError):
    return False, "override_min and override_max must be numbers"

  if omin < 0 or omax < 0:
    return False, "override_min and override_max must be non-negative"

  if omax < omin:
    return False, "override_max must be >= override_min"

  return True, None


def _row_get(row, key, idx=None):
  """Helper: read a column from a pymssql row by name, falling back to position."""
  try:
    return row[key]
  except (KeyError, TypeError):
    if idx is not None:
      try:
        return row[idx]
      except (IndexError, TypeError):
        return None
    return None


# =============================================================================
# GET MINMAX RECOMMENDATIONS  (v61.2 - live Current values)
# =============================================================================

@anvil.server.callable
def get_minmax_recommendations(status_filter=None, search_text=None):
  """
  Return one row per SKU (latest by recommendation_date). Current Min /
  Current Max are pulled live from min_max_calculated, not from the
  snapshot stored on the recommendation row. The snapshot is preserved
  as snapshot_min / snapshot_max for audit context.
  """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    # Window-function dedup, then join to min_max_calculated for live
    # current values and demand stats.
    query = """
        WITH latest_per_sku AS (
            SELECT
                r.recommendation_id,
                r.sku,
                r.current_min   AS snapshot_min,
                r.current_max   AS snapshot_max,
                r.recommended_min,
                r.recommended_max,
                r.confidence_level,
                r.reasoning,
                r.recommendation_date,
                r.status,
                r.reviewed_by,
                r.reviewed_date,
                r.notes,
                r.batch_id,
                r.override_min,
                r.override_max,
                r.override_reason,
                r.override_by,
                r.override_date,
                ROW_NUMBER() OVER (
                    PARTITION BY r.sku
                    ORDER BY r.recommendation_date DESC, r.recommendation_id DESC
                ) AS rn
            FROM dbo.ai_minmax_recommendations r
        )
        SELECT
            l.recommendation_id,
            l.sku,
            -- LIVE current values from production table (fall back to
            -- snapshot if the SKU has somehow been removed from min_max_calculated)
            COALESCE(m.min_stock, l.snapshot_min) AS current_min,
            COALESCE(m.max_stock, l.snapshot_max) AS current_max,
            -- Preserve the snapshot for the detail popup
            l.snapshot_min,
            l.snapshot_max,
            l.recommended_min,
            l.recommended_max,
            l.confidence_level,
            l.reasoning,
            l.recommendation_date,
            l.status,
            l.reviewed_by,
            l.reviewed_date,
            l.notes,
            l.batch_id,
            l.override_min,
            l.override_max,
            l.override_reason,
            l.override_by,
            l.override_date,
            -- pct-change vs LIVE current
            CASE
              WHEN COALESCE(m.min_stock, l.snapshot_min) > 0
              THEN ROUND(
                     (l.recommended_min - COALESCE(m.min_stock, l.snapshot_min))
                     * 100.0 / COALESCE(m.min_stock, l.snapshot_min), 1)
              ELSE 0
            END AS min_pct_change,
            CASE
              WHEN COALESCE(m.max_stock, l.snapshot_max) > 0
              THEN ROUND(
                     (l.recommended_max - COALESCE(m.max_stock, l.snapshot_max))
                     * 100.0 / COALESCE(m.max_stock, l.snapshot_max), 1)
              ELSE 0
            END AS max_pct_change,
            m.demand_pattern,
            m.avg_daily_usage,
            m.coefficient_of_variation,
            m.days_with_sales,
            m.days_of_data,
            m.avg_order_size
        FROM latest_per_sku l
        LEFT JOIN dbo.min_max_calculated m ON l.sku = m.sku
        WHERE l.rn = 1
    """
    params = []

    if status_filter:
      query += " AND l.status = %s"
      params.append(status_filter)

    if search_text:
      query += " AND l.sku LIKE %s"
      params.append('%' + search_text + '%')

    query += """
        ORDER BY
            CASE UPPER(l.status)
                WHEN 'PENDING'  THEN 1
                WHEN 'MODIFIED' THEN 2
                WHEN 'APPROVED' THEN 3
                WHEN 'REJECTED' THEN 4
                WHEN 'APPLIED'  THEN 5
                ELSE 6
            END,
            ABS(CASE WHEN COALESCE(m.min_stock, l.snapshot_min) > 0
                     THEN (l.recommended_min - COALESCE(m.min_stock, l.snapshot_min))
                          * 100.0 / COALESCE(m.min_stock, l.snapshot_min)
                     ELSE 0 END)
            + ABS(CASE WHEN COALESCE(m.max_stock, l.snapshot_max) > 0
                       THEN (l.recommended_max - COALESCE(m.max_stock, l.snapshot_max))
                            * 100.0 / COALESCE(m.max_stock, l.snapshot_max)
                       ELSE 0 END) DESC,
            l.sku
    """

    cursor.execute(query, params)
    rows = cursor.fetchall()

    recommendations = []
    for row in rows:
      serialized = serialize_row(row)

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

      if serialized.get('override_date'):
        try:
          o_date = datetime.fromisoformat(serialized['override_date'])
          serialized['override_date_display'] = o_date.strftime('%m/%d/%Y %I:%M %p')
        except (ValueError, TypeError):
          serialized['override_date_display'] = serialized['override_date']

      recommendations.append(serialized)

    # Per-SKU dedup counts (unchanged from v61.1)
    cursor.execute("""
        WITH latest_per_sku AS (
            SELECT status,
                   ROW_NUMBER() OVER (
                       PARTITION BY sku
                       ORDER BY recommendation_date DESC, recommendation_id DESC
                   ) AS rn
            FROM dbo.ai_minmax_recommendations
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

    cursor.execute("""
        SELECT TOP 1 action_timestamp
        FROM dbo.ai_agent_log
        WHERE action_type = 'MinMax_Review_Completed'
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
# GET MINMAX REVIEW STATS  (replaces lines ~260-308)
# =============================================================================

@anvil.server.callable
def get_minmax_review_stats():
  """
  Lightweight counts query for the counter cards. Same per-SKU dedup as
  the main grid: counts SKUs by their LATEST recommendation status.
  """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        WITH latest_per_sku AS (
            SELECT status,
                   ROW_NUMBER() OVER (
                       PARTITION BY sku
                       ORDER BY recommendation_date DESC, recommendation_id DESC
                   ) AS rn
            FROM dbo.ai_minmax_recommendations
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

    cursor.execute("""
        SELECT TOP 1 action_timestamp
        FROM dbo.ai_agent_log
        WHERE action_type = 'MinMax_Review_Completed'
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
      'pending':  counts.get('Pending', 0),
      'approved': counts.get('Approved', 0),
      'modified': counts.get('Modified', 0),
      'rejected': counts.get('Rejected', 0),
      'applied':  counts.get('Applied', 0),
      'last_review_date': last_review_date
    }

  except Exception as e:
    return {
      'status': 'error',
      'message': str(e),
      'counts': {},
      'pending': 0, 'approved': 0, 'modified': 0, 'rejected': 0, 'applied': 0,
      'last_review_date': None
    }


# =============================================================================
# UPDATE DECISION (single recommendation)
# =============================================================================

@anvil.server.callable
def update_minmax_decision(recommendation_id, decision, notes=None,
                           reviewed_by='Production Manager',
                           override_min=None, override_max=None,
                           override_reason=None):
  try:
    if decision == 'Modified':
      ok, err = _validate_modified_payload({
        'override_min': override_min,
        'override_max': override_max,
        'override_reason': override_reason,
      })
      if not ok:
        return {'status': 'error', 'message': err}

    conn = get_connection()
    cursor = conn.cursor()

    if decision == 'Modified':
      cursor.execute("""
          UPDATE dbo.ai_minmax_recommendations
          SET status          = %s,
              notes           = %s,
              reviewed_by     = %s,
              reviewed_date   = GETDATE(),
              override_min    = %s,
              override_max    = %s,
              override_reason = %s,
              override_by     = %s,
              override_date   = GETDATE()
          WHERE recommendation_id = %s
      """, (
        decision, notes, reviewed_by,
        int(override_min), int(override_max), str(override_reason).strip(),
        reviewed_by,
        recommendation_id
      ))
    else:
      cursor.execute("""
          UPDATE dbo.ai_minmax_recommendations
          SET status        = %s,
              notes         = %s,
              reviewed_by   = %s,
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
    return {'status': 'error', 'message': str(e)}


# =============================================================================
# BULK UPDATE DECISIONS
# =============================================================================

@anvil.server.callable
def bulk_update_minmax_decisions(updates, reviewed_by='Production Manager'):
  if not updates:
    return {'status': 'error', 'message': 'No updates provided'}

  errors = []
  for u in updates:
    if u.get('decision') == 'Modified':
      ok, err = _validate_modified_payload(u)
      if not ok:
        errors.append({'recommendation_id': u.get('recommendation_id'), 'error': err})

  if errors:
    return {
      'status': 'error',
      'message': 'Validation failed for ' + str(len(errors)) + ' Modified row(s)',
      'errors': errors
    }

  conn = get_connection()
  updated_count = 0

  try:
    cursor = conn.cursor()

    for update in updates:
      decision = update['decision']
      rec_id = update['recommendation_id']
      notes = update.get('notes')

      if decision == 'Modified':
        cursor.execute("""
            UPDATE dbo.ai_minmax_recommendations
            SET status          = %s,
                notes           = %s,
                reviewed_by     = %s,
                reviewed_date   = GETDATE(),
                override_min    = %s,
                override_max    = %s,
                override_reason = %s,
                override_by     = %s,
                override_date   = GETDATE()
            WHERE recommendation_id = %s
              AND status IN ('Pending', 'Modified')
        """, (
          decision, notes, reviewed_by,
          int(update['override_min']),
          int(update['override_max']),
          str(update['override_reason']).strip(),
          reviewed_by,
          rec_id
        ))
      else:
        cursor.execute("""
            UPDATE dbo.ai_minmax_recommendations
            SET status        = %s,
                notes         = %s,
                reviewed_by   = %s,
                reviewed_date = GETDATE()
            WHERE recommendation_id = %s
              AND status IN ('Pending', 'Modified')
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
# *** NEW v61 *** SUBMIT MANUAL OVERRIDE (any SKU, not just AI-flagged)
# =============================================================================

@anvil.server.callable
def submit_manual_minmax_override(sku, override_min, override_max,
                                  override_reason,
                                  reviewed_by='Production Manager'):
  """
    Stage a manual Min/Max override for ANY SKU, regardless of AI review state.

    Behavior:
      * Validates inputs (numeric Min/Max, non-empty reason, Max >= Min >= 0).
      * Verifies SKU exists in min_max_calculated.
      * If a Pending or Modified recommendation already exists for the SKU,
        updates the MOST RECENT such row in place:
            status -> 'Modified'
            override_min/max/reason/by/date set
        (preserves recommendation_id and the AI's original recommended_min/max
        so the divergence context is retained).
      * Otherwise inserts a new ai_minmax_recommendations row with:
            status              = 'Modified'
            confidence_level    = 'Manual'
            reasoning           = '<auto-explanation>'
            recommended_min/max = current values (no AI rec exists)
            batch_id            = 'MANUAL-YYYYMMDDHHMMSS'
      * Logs a MinMax_Manual_Override_Staged entry to ai_agent_log.

    The staged row will then be visible in the main grid and will be applied
    by the next 'Apply to Production' click via usp_Apply_Approved_MinMax.

    Args:
        sku: SKU string
        override_min: int (>= 0)
        override_max: int (>= override_min)
        override_reason: non-empty justification string
        reviewed_by: user identifier for audit columns

    Returns:
        dict: {
            'status':            'success' or 'error',
            'message':           description string,
            'action':            'updated' or 'inserted' (when successful),
            'recommendation_id': int (when successful),
            'sku':               echoed
        }
  """
  # ---- Input validation -----------------------------------------------
  if not sku or not str(sku).strip():
    return {'status': 'error', 'message': 'SKU is required'}
  sku_clean = str(sku).strip()

  ok, err = _validate_modified_payload({
    'override_min': override_min,
    'override_max': override_max,
    'override_reason': override_reason,
  })
  if not ok:
    return {'status': 'error', 'message': err}

  omin = int(override_min)
  omax = int(override_max)
  reason = str(override_reason).strip()

  conn = None
  try:
    conn = get_connection()
    cursor = conn.cursor()

    # ---- Verify SKU exists and pull current values -----------------
    cursor.execute("""
        SELECT min_stock, max_stock
        FROM dbo.min_max_calculated
        WHERE sku = %s
    """, (sku_clean,))
    cur_row = cursor.fetchone()

    if not cur_row:
      conn.close()
      return {
        'status': 'error',
        'message': 'SKU not found in min_max_calculated: ' + sku_clean
      }

    current_min = int((_row_get(cur_row, 'min_stock', 0)) or 0)
    current_max = int((_row_get(cur_row, 'max_stock', 1)) or 0)

    # Optional sanity: refuse no-op overrides (same as current values)
    if omin == current_min and omax == current_max:
      conn.close()
      return {
        'status': 'error',
        'message': 'Override Min/Max match the current values for ' + sku_clean
        + ' (Min ' + str(current_min) + ', Max ' + str(current_max) + ').'
        + ' Nothing to change.'
      }

    # ---- Check for an existing Pending/Modified row ----------------
    cursor.execute("""
        SELECT TOP 1 recommendation_id
        FROM dbo.ai_minmax_recommendations
        WHERE sku = %s
          AND status IN ('Pending', 'Modified')
        ORDER BY recommendation_date DESC
    """, (sku_clean,))
    existing = cursor.fetchone()

    rec_id = None
    action = None

    if existing:
      # Update in place
      rec_id = _row_get(existing, 'recommendation_id', 0)
      cursor.execute("""
          UPDATE dbo.ai_minmax_recommendations
          SET status          = 'Modified',
              override_min    = %s,
              override_max    = %s,
              override_reason = %s,
              override_by     = %s,
              override_date   = GETDATE(),
              reviewed_by     = %s,
              reviewed_date   = GETDATE()
          WHERE recommendation_id = %s
      """, (omin, omax, reason, reviewed_by, reviewed_by, rec_id))
      action = 'updated'
    else:
      # Insert a new manual-entry row
      batch_id = 'MANUAL-' + datetime.now().strftime('%Y%m%d%H%M%S')
      reasoning = (
        'Manual override entered by ' + str(reviewed_by)
        + '. No AI recommendation; current values used as baseline.'
      )

      cursor.execute("""
          INSERT INTO dbo.ai_minmax_recommendations (
              sku,
              current_min,
              current_max,
              recommended_min,
              recommended_max,
              confidence_level,
              reasoning,
              recommendation_date,
              status,
              reviewed_by,
              reviewed_date,
              notes,
              batch_id,
              override_min,
              override_max,
              override_reason,
              override_by,
              override_date
          )
          OUTPUT INSERTED.recommendation_id
          VALUES (
              %s, %s, %s, %s, %s,
              'Manual', %s, GETDATE(), 'Modified',
              %s, GETDATE(), NULL, %s,
              %s, %s, %s, %s, GETDATE()
          )
      """, (
        sku_clean, current_min, current_max, current_min, current_max,
        reasoning,
        reviewed_by,
        batch_id,
        omin, omax, reason, reviewed_by
      ))

      new_row = cursor.fetchone()
      rec_id = _row_get(new_row, 'recommendation_id', 0) if new_row else None
      action = 'inserted'

    # ---- ai_agent_log entry ----------------------------------------
    old_val = 'Min:' + str(current_min) + ' Max:' + str(current_max)
    new_val = 'Min:' + str(omin) + ' Max:' + str(omax)
    audit_reason = 'Staged by ' + str(reviewed_by) + '. Reason: ' + reason

    cursor.execute("""
        INSERT INTO dbo.ai_agent_log (
            action_type, sku, old_value, new_value, reason,
            action_timestamp, executed_by
        )
        VALUES (
            'MinMax_Manual_Override_Staged',
            %s, %s, %s, %s,
            GETDATE(),
            'submit_manual_minmax_override'
        )
    """, (sku_clean, old_val, new_val, audit_reason))

    conn.commit()
    conn.close()

    return {
      'status': 'success',
      'action': action,
      'recommendation_id': rec_id,
      'sku': sku_clean,
      'message': 'Manual override ' + action + ' for ' + sku_clean
      + ' (Min ' + str(omin) + ' / Max ' + str(omax) + ').'
      + ' Use Apply to Production to commit.'
    }

  except Exception as e:
    try:
      if conn is not None:
        conn.rollback()
        conn.close()
    except Exception:
      pass
    raw_msg = str(e) if e is not None else ''
    if not raw_msg or raw_msg.strip() in ('0', ''):
      raw_msg = type(e).__name__ + ': ' + repr(e)
    return {'status': 'error', 'message': raw_msg}


# =============================================================================
# APPLY APPROVED + MODIFIED RECOMMENDATIONS  (v60.1 hotfix folded in)
# =============================================================================

@anvil.server.callable
def apply_approved_minmax(reviewed_by='Production Manager'):
  """
    Apply all Approved AND Modified MinMax recommendations to production via
    stored procedure usp_Apply_Approved_MinMax (v60+).

    v60.1 hotfix: tolerant of inner SP result sets and clearer error reporting.
  """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("EXEC dbo.usp_Apply_Approved_MinMax @reviewed_by = %s",
                   (reviewed_by,))

    last_row = None
    while True:
      try:
        row = cursor.fetchone()
        while row is not None:
          last_row = row
          row = cursor.fetchone()
      except Exception:
        pass

      try:
        more = cursor.nextset()
      except Exception:
        more = False

      if not more:
        break

    conn.commit()
    conn.close()

    if last_row is None:
      return {
        'status': 'success',
        'applied_count': 0,
        'message': 'No approved or modified recommendations to apply'
      }

    applied_count = 0
    summary = ''

    try:
      applied_count = int(last_row['applied_count'] or 0)
      summary = last_row['summary'] or ''
    except (KeyError, TypeError, ValueError, IndexError):
      try:
        if len(last_row) >= 2:
          applied_count = int(last_row[0] or 0)
          summary = str(last_row[1] or '')
      except (TypeError, ValueError, IndexError):
        pass

    if isinstance(summary, str) and summary.startswith('Error:'):
      return {'status': 'error', 'applied_count': 0, 'message': summary}

    return {
      'status': 'success',
      'applied_count': applied_count,
      'message': summary or ('Applied ' + str(applied_count) + ' MinMax changes')
    }

  except Exception as e:
    raw_msg = str(e) if e is not None else ''
    if not raw_msg or raw_msg.strip() in ('0', ''):
      raw_msg = type(e).__name__ + ': ' + repr(e)
    return {'status': 'error', 'applied_count': 0, 'message': raw_msg}


# =============================================================================
# SKU BROWSER - Detail View
# =============================================================================

@anvil.server.callable
def get_minmax_sku_detail(sku):
  try:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            sku, min_stock, max_stock, avg_daily_usage, std_dev,
            days_of_data, days_with_sales, coefficient_of_variation,
            demand_pattern, total_orders, avg_order_size, order_qty,
            lead_time, safety_days, review_cycle, service_level,
            ai_managed, ai_updated_date, ai_updated_by,
            avg_inv, part_vol_ft3
        FROM dbo.min_max_calculated
        WHERE sku = %s
    """, (sku,))
    sku_row = cursor.fetchone()

    if not sku_row:
      conn.close()
      return {
        'status': 'error',
        'message': 'SKU ' + sku + ' not found in min_max_calculated'
      }

    sku_data = serialize_row(sku_row)

    avg_daily = float(sku_row['avg_daily_usage'] or 0)
    if avg_daily > 0:
      sku_data['min_days_supply'] = round(float(sku_row['min_stock'] or 0) / avg_daily, 1)
      sku_data['max_days_supply'] = round(float(sku_row['max_stock'] or 0) / avg_daily, 1)
    else:
      sku_data['min_days_supply'] = 0
      sku_data['max_days_supply'] = 0

    if sku_data.get('ai_updated_date'):
      try:
        ai_date = datetime.fromisoformat(sku_data['ai_updated_date'])
        sku_data['ai_updated_date_display'] = ai_date.strftime('%m/%d/%Y %I:%M %p')
        sku_data['ai_updated_date_short'] = ai_date.strftime('%m/%d/%Y')
      except (ValueError, TypeError):
        sku_data['ai_updated_date_display'] = sku_data['ai_updated_date']
        sku_data['ai_updated_date_short'] = sku_data['ai_updated_date']

    cursor.execute("""
        SELECT min_stock, max_stock, avg_daily_usage, std_dev,
               coefficient_of_variation, demand_pattern,
               days_of_data, days_with_sales, avg_order_size
        FROM dbo.vw_min_max_calculated
        WHERE sku = %s
    """, (sku,))
    baseline_row = cursor.fetchone()
    baseline = serialize_row(baseline_row) if baseline_row else {}

    if baseline and sku_data:
      prod_min = float(sku_data.get('min_stock') or 0)
      prod_max = float(sku_data.get('max_stock') or 0)
      base_min = float(baseline.get('min_stock') or 0)
      base_max = float(baseline.get('max_stock') or 0)

      baseline['min_pct_change'] = round(
        abs(base_min - prod_min) * 100.0 / prod_min, 1
      ) if prod_min > 0 else 0
      baseline['max_pct_change'] = round(
        abs(base_max - prod_max) * 100.0 / prod_max, 1
      ) if prod_max > 0 else 0
      baseline['min_direction'] = 'higher' if base_min > prod_min else ('lower' if base_min < prod_min else 'same')
      baseline['max_direction'] = 'higher' if base_max > prod_max else ('lower' if base_max < prod_max else 'same')

    cursor.execute("""
        SELECT cost_center, on_hand, available, committed, allocated
        FROM dbo.inventory_current
        WHERE sku = %s
          AND cost_center IN ('Starlite', 'Hub')
    """, (sku,))
    inv_rows = list(cursor.fetchall())

    inventory = {
      'starlite_on_hand': 0, 'starlite_available': 0,
      'starlite_committed': 0, 'starlite_allocated': 0,
      'hub_on_hand': 0, 'hub_available': 0
    }
    for inv_row in inv_rows:
      cc = inv_row['cost_center']
      if cc == 'Starlite':
        inventory['starlite_on_hand'] = float(inv_row['on_hand'] or 0)
        inventory['starlite_available'] = float(inv_row['available'] or 0)
        inventory['starlite_committed'] = float(inv_row['committed'] or 0)
        inventory['starlite_allocated'] = float(inv_row['allocated'] or 0)
      elif cc == 'Hub':
        inventory['hub_on_hand'] = float(inv_row['on_hand'] or 0)
        inventory['hub_available'] = float(inv_row['available'] or 0)

    if avg_daily > 0:
      inventory['starlite_days_supply'] = round(inventory['starlite_on_hand'] / avg_daily, 1)
    else:
      inventory['starlite_days_supply'] = 0

    cursor.execute("""
        SELECT
            recommendation_id,
            current_min,
            current_max,
            recommended_min,
            recommended_max,
            override_min,
            override_max,
            override_reason,
            override_by,
            override_date,
            confidence_level,
            reasoning,
            recommendation_date,
            status,
            reviewed_by,
            reviewed_date,
            batch_id
        FROM dbo.ai_minmax_recommendations
        WHERE sku = %s
        ORDER BY recommendation_date DESC
    """, (sku,))
    history_rows = cursor.fetchall()

    history = []
    for h_row in history_rows:
      h_serialized = serialize_row(h_row)
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
      'baseline': baseline,
      'inventory': inventory,
      'history': history
    }

  except Exception as e:
    return {'status': 'error', 'message': str(e)}


# =============================================================================
# SKU SEARCH
# =============================================================================

@anvil.server.callable
def search_minmax_skus(search_text):
  if not search_text or len(search_text) < 2:
    return {'status': 'success', 'skus': []}

  try:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT TOP 20
            sku, min_stock, max_stock, demand_pattern,
            avg_daily_usage, ai_managed
        FROM dbo.min_max_calculated
        WHERE sku LIKE %s
        ORDER BY sku
    """, ('%' + search_text + '%',))

    skus = [serialize_row(row) for row in cursor.fetchall()]
    conn.close()

    return {'status': 'success', 'skus': skus}

  except Exception as e:
    return {'status': 'error', 'message': str(e), 'skus': []}


# =============================================================================
# GET REVIEW TASK STATUS
# =============================================================================

@anvil.server.callable
def get_minmax_review_task_status(task_id):
  try:
    task = anvil.server.get_background_task(task_id)
    state = task.get_state()

    return {
      'status': state.get('status', 'Unknown'),
      'current_sku': state.get('current_sku', ''),
      'progress': state.get('progress', 0),
      'total_candidates': state.get('total_candidates', 0),
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


# =============================================================================
# GET RECOMMENDATION DETAIL
# =============================================================================

@anvil.server.callable
def get_minmax_recommendation_detail(recommendation_id):
  try:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            r.recommendation_id,
            r.sku,
            r.current_min,
            r.current_max,
            r.recommended_min,
            r.recommended_max,
            r.override_min,
            r.override_max,
            r.override_reason,
            r.override_by,
            r.override_date,
            r.confidence_level,
            r.reasoning,
            r.recommendation_date,
            r.status,
            r.reviewed_by,
            r.reviewed_date,
            r.notes,
            r.batch_id,
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
        FROM dbo.ai_minmax_recommendations r
        LEFT JOIN dbo.min_max_calculated m ON r.sku = m.sku
        WHERE r.recommendation_id = %s
    """, (recommendation_id,))

    row = cursor.fetchone()
    conn.close()

    if not row:
      return {'status': 'error', 'message': 'Recommendation not found'}

    detail = serialize_row(row)

    avg_daily = float(row['avg_daily_usage'] or 0)
    if avg_daily > 0:
      detail['current_min_dos'] = round(float(row['current_min'] or 0) / avg_daily, 1)
      detail['current_max_dos'] = round(float(row['current_max'] or 0) / avg_daily, 1)
      detail['recommended_min_dos'] = round(float(row['recommended_min'] or 0) / avg_daily, 1)
      detail['recommended_max_dos'] = round(float(row['recommended_max'] or 0) / avg_daily, 1)
      if row['override_min'] is not None:
        detail['override_min_dos'] = round(float(row['override_min']) / avg_daily, 1)
      if row['override_max'] is not None:
        detail['override_max_dos'] = round(float(row['override_max']) / avg_daily, 1)
    else:
      detail['current_min_dos'] = 0
      detail['current_max_dos'] = 0
      detail['recommended_min_dos'] = 0
      detail['recommended_max_dos'] = 0

    return {'status': 'success', 'detail': detail}

  except Exception as e:
    return {'status': 'error', 'message': str(e)}