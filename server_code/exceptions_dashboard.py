"""
exceptions_dashboard.py - Anvil Server Module
=============================================
Hub Transfers v2.1 - the Hub Manager's exception BOARD (#exceptions).

v2.0.0 (2026-09-05, Dave's design after the Transfer Logic Review):
every stage of the pipeline where something can sit, on one screen,
with how long it has sat and how long until the system acts.

  1  APPROVED, NOT PICKED   paper only. After pipeline_exception_days
                            the rec is auto-cancelled (pipeline_monitor)
                            - the detector re-raises if Starlite still
                            needs it. Manager can cancel early.
  2  PICKED, NOT LOADED     goods are physically on the dock. NEVER
                            auto-terminated (that is how goods get
                            stranded - review C7/M12). At the threshold
                            it goes red and the Hub Managers are texted;
                            the row offers SHIPPED-CLOSE (left without a
                            trip, close as Delivered) or RETURN TO STOCK
                            (rec cancelled, Inventory Control cycle-
                            counts). A human decides, but it cannot sit
                            quietly.
  3  LOADED, NOT DELIVERED  the truck departed, the driver never
                            confirmed. Red + text at the threshold;
                            CONFIRM DELIVERED lets a manager run the
                            same cascade the driver's submit runs.
  4  OPEN e21 TRANSFER      read-only. SO 16 = shipped from the Hub, not
                            received (Starlite's to fix); pre-ship = the
                            Hub's to ship or cancel. Shows how long until
                            the row stops counting as Starlite supply
                            (e21_intransit_max_age_days, view v5.16).

Thresholds: global_parameters pipeline_exception_days (default 2) for
1-3; e21_intransit_max_age_days (2) for 4. Dave 2026-09-05: "two days
to match the limit on transfers".

Writes (all guarded, all audited, all re-verify state in the call):
  close_exception   - v1 close-out: rec -> Delivered (shipped off books)
  return_to_stock   - rec -> Cancelled, line -> Cancelled, cycle-count SMS
  confirm_delivered - manager delivery: tta/trip/recs -> Delivered
  cancel_unpicked   - rec -> Cancelled (+ untouched Assigned lines)
The auto-cancel of stage 1 lives in pipeline_monitor.py and calls
_cancel_unpicked_rec() here so the rule has exactly one implementation.

Author: Claude (Dave Whritenour review)
Project: Hub Transfers v2.1
Created: 2026-09-04 (v1), 2026-09-05 (v2)
"""

import anvil.server
from datetime import datetime, date, timedelta
from decimal import Decimal

from . import db_config

__version__       = "2.0.0"
__version_date__  = "2026-09-05"
__version_notes__ = (
  "2.0.0: exception BOARD - four pipeline stages (approved-not-picked, "
  "picked-not-loaded, loaded-not-delivered, open e21 orders) with age and "
  "days-until-the-system-acts; new actions return_to_stock, "
  "confirm_delivered, cancel_unpicked; shared _cancel_unpicked_rec used by "
  "pipeline_monitor's auto-cancel. v1 close_exception unchanged. "
  "1.0.0: stranded picked lines (aged >= 2 days or tripwire-alerted) with "
  "10-day e21 evidence, guarded close_exception."
)

_E21_WINDOW_DAYS = 10   # e21 evidence window shown on stage-2 rows
_DEFAULT_PIPELINE_DAYS = 2
_DEFAULT_E21_AGE_DAYS = 2

STAGE_APPROVED  = 'APPROVED_NOT_PICKED'
STAGE_PICKED    = 'PICKED_NOT_LOADED'
STAGE_LOADED    = 'LOADED_NOT_DELIVERED'
STAGE_E21       = 'E21_OPEN'


# =============================================================================
# HELPERS
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


def _param_int(conn, name, default):
  cursor = conn.cursor()
  cursor.execute("SELECT parameter_value FROM dbo.global_parameters WHERE parameter_name = %s",
                 (name,))
  row = cursor.fetchone()
  try:
    return int(float(row['parameter_value'])) if row else default
  except (TypeError, ValueError):
    return default


def _log_agent(conn, action_type, sku, old_value, new_value, reason, executed_by=None):
  cursor = conn.cursor()
  cursor.execute(
    """
      INSERT INTO dbo.ai_agent_log
        (action_type, sku, old_value, new_value, reason, action_timestamp, executed_by)
      VALUES (%s, %s, %s, %s, %s, GETDATE(), %s)
    """,
    (action_type, sku, old_value, new_value, reason,
     executed_by or ('exceptions_dashboard v' + __version__))
  )


def _log_event(conn, pick_id, pick_line_id, event_type, performed_by, qty, note):
  cursor = conn.cursor()
  cursor.execute(
    """
      INSERT INTO dbo.transfer_pick_events
        (pick_id, pick_line_id, event_type, performed_by, qty, note, event_date)
      VALUES (%s, %s, %s, %s, %s, %s, GETDATE())
    """,
    (pick_id, pick_line_id, event_type, performed_by, qty, note)
  )


def _check_initials(confirmed_by):
  confirmed_by = (confirmed_by or '').strip()
  if not confirmed_by or len(confirmed_by) > 20:
    raise Exception("Initials are required (max 20 characters).")
  return confirmed_by


def _check_date(s, label):
  try:
    d = datetime.strptime((s or '').strip(), '%Y-%m-%d').date()
  except ValueError:
    raise Exception("%s must be YYYY-MM-DD." % label)
  if d > date.today():
    raise Exception("%s cannot be in the future." % label)
  if d < date.today() - timedelta(days=120):
    raise Exception("%s is more than 120 days back - check it." % label)
  return d


# =============================================================================
# READ: THE BOARD
# =============================================================================

@anvil.server.callable
def get_exceptions_board():
  """
  One list, every stage. Each row carries:
    stage, stage_label, sku, qty, since (datetime), age_days, days_left
    (negative = past the threshold), overdue (bool), has_alert, ref
    (pick / trip / e21 order), detail (stage-specific text), owner,
    and the ids the actions need (recommendation_id, pick_id,
    pick_line_id, trip_id, order_num).
  Plus a summary dict and the two thresholds.
  """
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    pipe_days = _param_int(conn, 'pipeline_exception_days', _DEFAULT_PIPELINE_DAYS)
    e21_days = _param_int(conn, 'e21_intransit_max_age_days', _DEFAULT_E21_AGE_DAYS)
    rows = []

    # ---- 1. APPROVED, NOT PICKED ---------------------------------------
    cursor.execute(
      """
        SELECT r.recommendation_id, r.sku,
               CAST(COALESCE(r.[manager_override_qty ], r.ai_recommended_qty,
                             r.calculated_transfer_qty) AS DECIMAL(18,4)) AS qty,
               r.status AS rec_status, r.reviewed_date,
               DATEDIFF(day, r.reviewed_date, GETDATE()) AS age_days,
               pk.pick_id, pk.pick_status, pk.assigned_worker_code,
               ps.part_desc
        FROM dbo.ai_transfer_recommendations r
        LEFT JOIN dbo.part_specs ps ON ps.sku = r.sku
        OUTER APPLY (SELECT TOP 1 p.pick_id, p.status AS pick_status, p.assigned_worker_code
                     FROM dbo.transfer_pick_lines pl
                     JOIN dbo.transfer_picks p ON p.pick_id = pl.pick_id
                     WHERE pl.recommendation_id = r.recommendation_id
                       AND pl.status = 'Assigned'
                       AND p.status IN ('Draft', 'Released', 'In_Progress')
                     ORDER BY p.pick_id DESC) pk
        WHERE r.status IN ('Approved', 'Auto-Approved', 'Applied')
          AND r.reviewed_date IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM dbo.transfer_pick_lines x
                          WHERE x.recommendation_id = r.recommendation_id
                            AND x.status IN ('Picked', 'Short')
                            AND x.qty_picked > 0)
          AND NOT EXISTS (SELECT 1 FROM dbo.transfer_trip_assignments t
                          WHERE t.recommendation_id = r.recommendation_id)
        ORDER BY r.reviewed_date
      """
    )
    for x in cursor.fetchall():
      age = int(x['age_days'] or 0)
      if x['pick_status'] == 'In_Progress':
        detail = 'on pick P%s - being picked now' % x['pick_id']
      elif x['pick_id']:
        detail = 'on pick P%s (%s%s)' % (x['pick_id'], x['pick_status'],
                                         (', ' + x['assigned_worker_code']) if x['assigned_worker_code'] else '')
      else:
        detail = 'approved, no pick built'
      rows.append({
        'stage': STAGE_APPROVED, 'stage_label': '1 Approved, not picked',
        'sku': x['sku'], 'qty': make_serializable(x['qty']),
        'since': make_serializable(x['reviewed_date']), 'age_days': age,
        'days_left': pipe_days - age, 'overdue': age >= pipe_days,
        'has_alert': False,
        'ref': ('P%s' % x['pick_id']) if x['pick_id'] else ('R%s' % x['recommendation_id']),
        'detail': detail, 'owner': 'Hub',
        'recommendation_id': x['recommendation_id'], 'pick_id': x['pick_id'],
        'pick_line_id': None, 'trip_id': None, 'order_num': None,
        'auto_action': ('none - being picked' if x['pick_status'] == 'In_Progress'
                        else 'auto-cancel'),
        'part_description': x['part_desc'] or '',
      })

    # ---- 2. PICKED, NOT LOADED (the v1 list, all ages) -----------------
    cursor.execute(
      """
        SELECT pl.pick_line_id, pl.pick_id, pl.recommendation_id, pl.sku,
               CAST(pl.qty_picked AS DECIMAL(18,4)) AS qty,
               pl.picked_date, pl.picked_by,
               DATEDIFF(day, pl.picked_date, GETDATE()) AS age_days,
               ps.part_desc, e.e21_recv, e.e21_open,
               CASE WHEN EXISTS (SELECT 1 FROM dbo.transfer_pick_events ev
                                 WHERE ev.pick_line_id = pl.pick_line_id
                                   AND ev.event_type = 'TRIPWIRE_E21_ALERT')
                    THEN 1 ELSE 0 END AS has_alert,
               CASE WHEN EXISTS (SELECT 1 FROM dbo.transfer_trip_assignments t
                                 JOIN dbo.transfer_trips tr ON tr.trip_id = t.trip_id
                                 WHERE t.pick_line_id = pl.pick_line_id
                                   AND tr.status = 'Finalized' AND tr.shipped_date IS NULL)
                    THEN 1 ELSE 0 END AS on_open_truck
        FROM dbo.transfer_pick_lines pl
        JOIN dbo.transfer_picks p ON p.pick_id = pl.pick_id
        JOIN dbo.ai_transfer_recommendations r ON r.recommendation_id = pl.recommendation_id
        LEFT JOIN dbo.part_specs ps ON ps.sku = pl.sku
        CROSS APPLY (
          SELECT (SELECT COALESCE(SUM(h.transfer_qty), 0) FROM dbo.hub_transfer_orders_history h
                  WHERE h.sku = pl.sku AND h.received_date > DATEADD(day, -%s, GETDATE())) AS e21_recv,
                 (SELECT COALESCE(SUM(o.transfer_qty), 0) FROM dbo.hub_transfer_orders o
                  WHERE o.sku = pl.sku AND o.date_entered > DATEADD(day, -%s, GETDATE())) AS e21_open
        ) e
        WHERE p.status = 'Completed'
          AND pl.qty_picked > 0
          AND r.status IN ('Applied', 'Trip_Assigned')
          AND NOT EXISTS (SELECT 1 FROM dbo.transfer_trip_assignments t
                          JOIN dbo.transfer_trips tr ON tr.trip_id = t.trip_id
                          WHERE t.pick_line_id = pl.pick_line_id
                            AND tr.status <> 'Proposed'
                            AND NOT (tr.status = 'Finalized' AND tr.shipped_date IS NULL))
        ORDER BY has_alert DESC, pl.picked_date
      """,
      (_E21_WINDOW_DAYS, _E21_WINDOW_DAYS)
    )
    for x in cursor.fetchall():
      age = int(x['age_days'] or 0)
      recv = float(x['e21_recv'] or 0); opn = float(x['e21_open'] or 0)
      if x['on_open_truck']:
        detail = 'on a truck being loaded now'
      elif recv > 0:
        detail = 'e21: %g received at Starlite in %sd' % (recv, _E21_WINDOW_DAYS)
      elif opn > 0:
        detail = 'e21: %g on open transfer order in %sd' % (opn, _E21_WINDOW_DAYS)
      else:
        detail = 'no e21 activity - goods should be on the dock'
      rows.append({
        'stage': STAGE_PICKED, 'stage_label': '2 Picked, not loaded',
        'sku': x['sku'], 'qty': make_serializable(x['qty']),
        'since': make_serializable(x['picked_date']), 'age_days': age,
        'days_left': pipe_days - age, 'overdue': age >= pipe_days,
        'has_alert': bool(x['has_alert']),
        'ref': 'P%s' % x['pick_id'], 'detail': detail, 'owner': 'Hub',
        'recommendation_id': x['recommendation_id'], 'pick_id': x['pick_id'],
        'pick_line_id': x['pick_line_id'], 'trip_id': None, 'order_num': None,
        'auto_action': 'escalate (never auto-closed)',
        'part_description': x['part_desc'] or '',
      })

    # ---- 3. LOADED (DEPARTED), NOT DELIVERED --------------------------
    cursor.execute(
      """
        SELECT t.trip_id, t.batch_id, t.shipped_date, t.finalized_by,
               DATEDIFF(day, t.shipped_date, GETDATE()) AS age_days,
               (SELECT COUNT(*) FROM dbo.transfer_trip_assignments a WHERE a.trip_id = t.trip_id) AS lines,
               (SELECT SUM(COALESCE(a.loaded_qty, a.adjusted_qty, a.planned_qty, a.qty))
                FROM dbo.transfer_trip_assignments a WHERE a.trip_id = t.trip_id) AS units,
               (SELECT MIN(a.sku) FROM dbo.transfer_trip_assignments a WHERE a.trip_id = t.trip_id) AS first_sku
        FROM dbo.transfer_trips t
        WHERE t.status IN ('Finalized', 'Departed')
          AND t.shipped_date IS NOT NULL
          AND t.received_date IS NULL
          AND EXISTS (SELECT 1 FROM dbo.transfer_trip_assignments a WHERE a.trip_id = t.trip_id)
        ORDER BY t.shipped_date
      """
    )
    for x in cursor.fetchall():
      age = int(x['age_days'] or 0)
      n = int(x['lines'] or 0)
      rows.append({
        'stage': STAGE_LOADED, 'stage_label': '3 Departed, not delivered',
        'sku': ('%s +%d more' % (x['first_sku'], n - 1)) if n > 1 else (x['first_sku'] or ''),
        'qty': make_serializable(x['units']),
        'since': make_serializable(x['shipped_date']), 'age_days': age,
        'days_left': pipe_days - age, 'overdue': age >= pipe_days,
        'has_alert': False,
        'ref': 'T%s' % x['trip_id'],
        'detail': '%d line(s); departed by %s; driver has not confirmed' % (n, x['finalized_by'] or '?'),
        'owner': 'Driver',
        'recommendation_id': None, 'pick_id': None, 'pick_line_id': None,
        'trip_id': x['trip_id'], 'order_num': None,
        'auto_action': 'escalate (never auto-delivered)',
        'part_description': '',
      })

    # ---- 4. OPEN e21 TRANSFER ORDERS ----------------------------------
    cursor.execute(
      """
        SELECT o.sku, o.order_num, o.rel_num, o.transfer_qty, o.date_entered,
               o.ord_item_status, o.po_item_status,
               DATEDIFF(day, o.date_entered, GETDATE()) AS age_days,
               ps.part_desc,
               (SELECT MAX(a.delivered_date) FROM dbo.transfer_trip_assignments a
                WHERE a.sku = o.sku AND a.status = 'Delivered'
                  AND a.delivered_date BETWEEN DATEADD(day, -3, o.date_entered)
                                           AND DATEADD(day, 10, o.date_entered)) AS trip_delivered
        FROM dbo.hub_transfer_orders o
        LEFT JOIN dbo.part_specs ps ON ps.sku = o.sku
        ORDER BY o.date_entered, o.sku
      """
    )
    so_names = {'00': 'Hold', '05': 'Reg. hold', '10': 'Entered', '12': 'Committed',
                '14': 'Allocated', '15': "Mat'l recvd", '16': 'Shipped'}
    for x in cursor.fetchall():
      age = int(x['age_days'] or 0)
      shipped = (x['ord_item_status'] or '') == '16'
      hold = (x['ord_item_status'] or '') == '00'
      if shipped:
        owner = 'Starlite'
        detail = 'shipped from Hub, NOT received (PO %s)' % (x['po_item_status'] or '?')
      else:
        owner = 'Hub'
        detail = 'e21 %s - never shipped in e21' % so_names.get(x['ord_item_status'], x['ord_item_status'])
      if x['trip_delivered']:
        detail += '; a truck delivered this SKU %s' % x['trip_delivered'].strftime('%m/%d')
      # supply credit: v5.16 counts the row while age <= e21_days and not Hold
      left = (-999 if hold else e21_days - age)
      rows.append({
        'stage': STAGE_E21, 'stage_label': '4 Open e21 transfer',
        'sku': x['sku'], 'qty': make_serializable(x['transfer_qty']),
        'since': make_serializable(x['date_entered']), 'age_days': age,
        'days_left': left, 'overdue': (hold or age > e21_days),
        'has_alert': False,
        'ref': '%s-%s' % (x['order_num'], x['rel_num']),
        'detail': detail, 'owner': owner,
        'recommendation_id': None, 'pick_id': None, 'pick_line_id': None,
        'trip_id': None, 'order_num': x['order_num'],
        'auto_action': ('not supply (Hold)' if hold else
                        'stops counting as Starlite supply' if age <= e21_days
                        else 'NOT counted - SKU is being re-recommended'),
        'part_description': x['part_desc'] or '',
      })

    summary = {}
    for st in (STAGE_APPROVED, STAGE_PICKED, STAGE_LOADED, STAGE_E21):
      items = [r for r in rows if r['stage'] == st]
      summary[st] = {'count': len(items),
                     'overdue': sum(1 for r in items if r['overdue'])}
    return {'rows': rows, 'summary': summary,
            'pipeline_days': pipe_days, 'e21_days': e21_days}
  finally:
    conn.close()


# Back-compat for anything still calling the v1 name.
@anvil.server.callable
def get_exceptions():
  board = get_exceptions_board()
  return [r for r in board['rows'] if r['stage'] == STAGE_PICKED and (r['overdue'] or r['has_alert'])]


# =============================================================================
# WRITE 1 (v1, unchanged): SHIPPED-CLOSE - left without a trip
# =============================================================================

@anvil.server.callable
def close_exception(pick_line_id, ship_date, confirmed_by, note=None):
  """
  Manager-confirmed: this line physically shipped without a trip.
  Re-verifies the line is still stranded, then closes the rec as
  Delivered with a full audit trail.
  """
  confirmed_by = _check_initials(confirmed_by)
  ship_dt = _check_date(ship_date, 'Ship date')
  note = (note or '').strip()

  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    line = _stranded_line(cursor, pick_line_id)
    if not line:
      raise Exception("This line is no longer an open exception - it was "
                      "closed, cancelled, or put on a trip. Refresh the list.")

    audit = ("Manager reconciliation via #exceptions: confirmed shipped "
             + ship_dt.isoformat() + " without a finalized trip (pick "
             + str(line['pick_id']) + ") by " + confirmed_by
             + ((". Note: " + note) if note else "")
             + ". Closed as Delivered.")
    cursor.execute(
      """
        UPDATE dbo.ai_transfer_recommendations SET
          status = 'Delivered',
          notes = CASE WHEN notes IS NULL OR LTRIM(RTRIM(notes)) = ''
                       THEN %s ELSE CONCAT(notes, ' | ', %s) END,
          reviewed_date = GETDATE()
        WHERE recommendation_id = %s
          AND status IN ('Applied', 'Trip_Assigned')
      """,
      (audit, audit, line['recommendation_id'])
    )
    if cursor.rowcount != 1:
      conn.rollback()
      raise Exception("The recommendation changed state mid-action - "
                      "nothing was written. Refresh and retry.")
    _log_event(conn, line['pick_id'], line['pick_line_id'], 'RECONCILE_DELIVERED',
               confirmed_by, line['qty'], audit)
    _log_agent(conn, 'TRANSFER_RECONCILIATION', line['sku'],
               'Rec %s %s (pick %s, stranded)' % (line['recommendation_id'], line['rec_status'], line['pick_id']),
               'Rec %s Delivered (manager-confirmed %s)' % (line['recommendation_id'], ship_dt.isoformat()),
               audit)
    conn.commit()
    return {'closed': True, 'sku': line['sku'], 'qty': make_serializable(line['qty']),
            'recommendation_id': line['recommendation_id']}
  finally:
    conn.close()


def _stranded_line(cursor, pick_line_id):
  cursor.execute(
    """
      SELECT pl.pick_line_id, pl.pick_id, pl.sku,
             CAST(pl.qty_picked AS DECIMAL(18,4)) AS qty,
             pl.recommendation_id, r.status AS rec_status
      FROM dbo.transfer_pick_lines pl
      JOIN dbo.transfer_picks p ON p.pick_id = pl.pick_id
      JOIN dbo.ai_transfer_recommendations r ON r.recommendation_id = pl.recommendation_id
      WHERE pl.pick_line_id = %s
        AND p.status = 'Completed'
        AND pl.qty_picked > 0
        AND pl.status IN ('Picked', 'Short')
        AND r.status IN ('Applied', 'Trip_Assigned')
        AND NOT EXISTS (SELECT 1 FROM dbo.transfer_trip_assignments t
                        JOIN dbo.transfer_trips tr ON tr.trip_id = t.trip_id
                        WHERE t.pick_line_id = pl.pick_line_id
                          AND tr.status <> 'Proposed')
    """,
    (pick_line_id,)
  )
  return cursor.fetchone()


# =============================================================================
# WRITE 2 (v2): RETURN TO STOCK - goods go back on the shelf
# =============================================================================

@anvil.server.callable
def return_to_stock(pick_line_id, confirmed_by, note=None):
  """
  Manager-confirmed: the picked goods are going back to the Hub shelf.
  The pick line is Cancelled (it leaves STAGED), the rec is Cancelled
  when nothing else keeps it alive (detector re-raises if still needed),
  and Inventory Control gets a cycle-count text for the SKU.
  """
  confirmed_by = _check_initials(confirmed_by)
  note = (note or '').strip()
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    line = _stranded_line(cursor, pick_line_id)
    if not line:
      raise Exception("This line is no longer an open exception. Refresh the list.")

    audit = ("RETURNED TO STOCK via #exceptions by " + confirmed_by + ": pick "
             + str(line['pick_id']) + " line " + str(line['pick_line_id'])
             + " (" + '%g' % float(line['qty']) + " picked) put back on the shelf"
             + ((". Note: " + note) if note else "")
             + ". Cycle count requested; detector re-raises if still needed.")
    cursor.execute(
      """
        UPDATE dbo.transfer_pick_lines
        SET status = 'Cancelled',
            discrepancy_note = CONCAT(ISNULL(discrepancy_note, ''), ' | ', %s)
        WHERE pick_line_id = %s AND status IN ('Picked', 'Short')
      """,
      (audit, line['pick_line_id'])
    )
    if cursor.rowcount != 1:
      conn.rollback()
      raise Exception("The pick line changed state mid-action - nothing was written.")

    cursor.execute(
      """
        UPDATE dbo.ai_transfer_recommendations
        SET status = 'Cancelled', reviewed_date = GETDATE(),
            notes = CONCAT(ISNULL(CAST(notes AS nvarchar(max)), ''), ' | [',
                           CONVERT(varchar(16), GETDATE(), 120), '] ', %s)
        WHERE recommendation_id = %s
          AND status IN ('Applied', 'Trip_Assigned')
          AND NOT EXISTS (SELECT 1 FROM dbo.transfer_pick_lines x
                          WHERE x.recommendation_id = %s
                            AND x.pick_line_id <> %s
                            AND x.status IN ('Assigned', 'Picked', 'Short')
                            AND (x.status = 'Assigned' OR x.qty_picked > 0))
          AND NOT EXISTS (SELECT 1 FROM dbo.transfer_trip_assignments t
                          JOIN dbo.transfer_trips tr ON tr.trip_id = t.trip_id
                          WHERE t.recommendation_id = %s
                            AND tr.status <> 'Proposed' AND tr.received_date IS NULL)
      """,
      (audit, line['recommendation_id'], line['recommendation_id'],
       line['pick_line_id'], line['recommendation_id'])
    )
    rec_cancelled = cursor.rowcount == 1
    _log_event(conn, line['pick_id'], line['pick_line_id'], 'RETURNED_TO_STOCK',
               confirmed_by, line['qty'], audit + (' Rec cancelled.' if rec_cancelled else ' Rec left open (other lines/trips).'))
    _log_agent(conn, 'TRANSFER_RECONCILIATION', line['sku'],
               'Rec %s %s (pick %s, stranded)' % (line['recommendation_id'], line['rec_status'], line['pick_id']),
               'line Cancelled' + (', rec Cancelled' if rec_cancelled else ''), audit)
    conn.commit()

    sms = 0
    try:
      sms = _cycle_count_sms(conn, line['sku'], float(line['qty']), line['pick_id'], confirmed_by)
    except Exception as e:
      print('[exceptions] cycle-count SMS failed: %s' % e)
    return {'returned': True, 'sku': line['sku'], 'qty': make_serializable(line['qty']),
            'rec_cancelled': rec_cancelled, 'sms_sent': sms}
  finally:
    conn.close()


def _cycle_count_sms(conn, sku, qty, pick_id, by):
  from . import sms_notifications as sn
  if not sn.SMSConfig.is_sms_enabled():
    return 0
  contacts = sn.get_hub_manager_phones(conn, 'SHORT_PICK_CYCLE_COUNT')
  if not contacts:
    return 0
  msg = ("RETURNED TO STOCK - CYCLE COUNT NEEDED\n%s: %g units from pick %s put "
         "back on the Hub shelf by %s. Please count this SKU now." % (sku, qty, pick_id, by))
  client = sn.TwilioSMSClient()
  sent = 0
  for c in contacts:
    try:
      r = client.send_sms(c['phone'], msg)
      sn.log_sms_notification(conn, r, 'SHORT_PICK_CYCLE_COUNT', related_skus=[sku])
      if r.get('success'):
        sent += 1
    except Exception as e:
      print('[exceptions] SMS to %s failed: %s' % (c.get('name'), e))
  return sent


# =============================================================================
# WRITE 3 (v2): CONFIRM DELIVERED - manager stands in for the driver
# =============================================================================

@anvil.server.callable
def confirm_delivered(trip_id, delivered_date, confirmed_by, note=None):
  """
  The truck departed and the driver never confirmed. The manager confirms
  delivery: every assignment -> Delivered at its loaded/shipped qty, the
  trip -> Delivered with received_date, every rec on it -> Delivered.
  Same cascade as driver_delivery.submit_delivery_confirmation.
  """
  confirmed_by = _check_initials(confirmed_by)
  d = _check_date(delivered_date, 'Delivered date')
  note = (note or '').strip()
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      """
        UPDATE dbo.transfer_trips
        SET status = 'Delivered',
            received_date = CASE WHEN CAST(%s AS date) = CAST(GETDATE() AS date) THEN GETDATE()
                                 ELSE DATEADD(hour, 12, CAST(CAST(%s AS date) AS datetime)) END,
            notes = CONCAT(ISNULL(notes, ''), ' | Delivery confirmed by manager ', %s,
                           ' via #exceptions (driver did not confirm)', %s)
        WHERE trip_id = %s
          AND status IN ('Finalized', 'Departed')
          AND shipped_date IS NOT NULL
          AND received_date IS NULL
      """,
      (d.isoformat(), d.isoformat(), confirmed_by, (': ' + note) if note else '', trip_id)
    )
    if cursor.rowcount != 1:
      conn.rollback()
      raise Exception("Trip %s is not a departed, undelivered truck any more. Refresh." % trip_id)

    who = ('MGR:' + confirmed_by)[:50]
    cursor.execute(
      """
        UPDATE dbo.transfer_trip_assignments
        SET status = 'Delivered',
            delivered_qty = COALESCE(loaded_qty, adjusted_qty, planned_qty, qty),
            delivered_by = %s,
            delivered_date = (SELECT received_date FROM dbo.transfer_trips WHERE trip_id = %s),
            delivery_note = CONCAT(ISNULL(delivery_note, ''), 'manager-confirmed via #exceptions')
        WHERE trip_id = %s AND status = 'Finalized'
      """,
      (who, trip_id, trip_id)
    )
    lines = cursor.rowcount
    cursor.execute(
      """
        UPDATE r SET r.status = 'Delivered'
        FROM dbo.ai_transfer_recommendations r
        JOIN dbo.transfer_trip_assignments tta ON tta.recommendation_id = r.recommendation_id
        WHERE tta.trip_id = %s AND tta.status = 'Delivered'
          AND r.status IN ('Applied', 'Finalized', 'Trip_Assigned')
      """,
      (trip_id,)
    )
    recs = cursor.rowcount
    _log_agent(conn, 'TRIP_DELIVERED', None, 'Departed', 'Delivered',
               'trip_id=%s; assignments_delivered=%s; recs_cascaded=%s; manager-confirmed %s by %s%s'
               % (trip_id, lines, recs, d.isoformat(), confirmed_by, ('; ' + note) if note else ''),
               executed_by=who)
    conn.commit()
    return {'delivered': True, 'trip_id': trip_id, 'lines': lines, 'recs': recs}
  finally:
    conn.close()


# =============================================================================
# WRITE 4 (v2): CANCEL UNPICKED - manual and automatic (pipeline_monitor)
# =============================================================================

def _cancel_unpicked_rec(conn, recommendation_id, performed_by, reason):
  """
  The single implementation of the stage-1 rule. Cancels the rec only if
  NOTHING physical has happened: no line with qty_picked > 0, no trip
  assignment, no In_Progress pick. Untouched Assigned lines on
  Draft/Released picks are Cancelled with it; a pick left with no live
  lines is Cancelled too. Returns True if the rec was cancelled.
  """
  cursor = conn.cursor()
  cursor.execute(
    """
      SELECT r.recommendation_id, r.sku, r.status
      FROM dbo.ai_transfer_recommendations r
      WHERE r.recommendation_id = %s
        AND r.status IN ('Approved', 'Auto-Approved', 'Applied')
        AND NOT EXISTS (SELECT 1 FROM dbo.transfer_pick_lines x
                        WHERE x.recommendation_id = r.recommendation_id
                          AND x.qty_picked > 0)
        AND NOT EXISTS (SELECT 1 FROM dbo.transfer_pick_lines x
                        JOIN dbo.transfer_picks p ON p.pick_id = x.pick_id
                        WHERE x.recommendation_id = r.recommendation_id
                          AND x.status = 'Assigned' AND p.status = 'In_Progress')
        AND NOT EXISTS (SELECT 1 FROM dbo.transfer_trip_assignments t
                        WHERE t.recommendation_id = r.recommendation_id)
    """,
    (recommendation_id,)
  )
  rec = cursor.fetchone()
  if not rec:
    return False

  note = ('CANCELLED (approved, never picked) by ' + performed_by + ': ' + reason
          + '. Detector re-recommends if still needed.')
  cursor.execute(
    """
      UPDATE dbo.ai_transfer_recommendations
      SET status = 'Cancelled', reviewed_date = GETDATE(),
          notes = CONCAT(ISNULL(CAST(notes AS nvarchar(max)), ''), ' | [',
                         CONVERT(varchar(16), GETDATE(), 120), '] ', %s)
      WHERE recommendation_id = %s AND status IN ('Approved', 'Auto-Approved', 'Applied')
    """,
    (note, recommendation_id)
  )
  if cursor.rowcount != 1:
    return False

  # Untouched Assigned lines on Draft/Released picks go with the rec.
  cursor.execute(
    """
      SELECT x.pick_line_id, x.pick_id FROM dbo.transfer_pick_lines x
      JOIN dbo.transfer_picks p ON p.pick_id = x.pick_id
      WHERE x.recommendation_id = %s AND x.status = 'Assigned'
        AND p.status IN ('Draft', 'Released')
    """,
    (recommendation_id,)
  )
  lines = list(cursor.fetchall())
  for ln in lines:
    cursor.execute(
      "UPDATE dbo.transfer_pick_lines SET status = 'Cancelled', "
      "discrepancy_note = CONCAT(ISNULL(discrepancy_note, ''), ' | ', %s) "
      "WHERE pick_line_id = %s AND status = 'Assigned'",
      (note, ln['pick_line_id'])
    )
    _log_event(conn, ln['pick_id'], ln['pick_line_id'], 'LINE_REMOVED', performed_by, 0, note)
    # A pick with nothing live left is cancelled too.
    cursor.execute(
      """
        UPDATE dbo.transfer_picks SET status = 'Cancelled', cancelled_date = SYSDATETIME(),
               notes = CONCAT(ISNULL(notes, ''), ' | all lines cancelled by the pipeline rule')
        WHERE pick_id = %s AND status IN ('Draft', 'Released')
          AND NOT EXISTS (SELECT 1 FROM dbo.transfer_pick_lines y
                          WHERE y.pick_id = %s AND y.status <> 'Cancelled')
      """,
      (ln['pick_id'], ln['pick_id'])
    )
    if cursor.rowcount == 1:
      _log_event(conn, ln['pick_id'], None, 'CANCELLED', performed_by, None,
                 'pick emptied by the approved-not-picked rule')
  _log_agent(conn, 'APPROVED_NOT_PICKED_CANCELLED', rec['sku'],
             'Rec %s %s' % (recommendation_id, rec['status']),
             'Cancelled (+%d assigned line(s))' % len(lines), reason, executed_by=performed_by)
  return True


@anvil.server.callable
def cancel_unpicked(recommendation_id, confirmed_by, note=None):
  confirmed_by = _check_initials(confirmed_by)
  note = (note or '').strip()
  conn = db_config.get_nexus_connection()
  try:
    ok = _cancel_unpicked_rec(conn, recommendation_id, confirmed_by,
                              'manager cancel via #exceptions' + ((': ' + note) if note else ''))
    if not ok:
      conn.rollback()
      raise Exception("This rec is no longer cancellable from here - it was picked, "
                      "put on a trip, is being picked now, or already closed. Refresh.")
    conn.commit()
    return {'cancelled': True, 'recommendation_id': recommendation_id}
  finally:
    conn.close()