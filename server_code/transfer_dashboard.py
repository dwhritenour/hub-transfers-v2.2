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
  2026-10-25 (PAUSE alongside SNOOZE):
    - One-click PAUSE returns alongside Snooze using pause_kind ('pause' vs
      'snooze', migration 115). _snooze_rec stamps pause_kind = 'snooze' on
      both whole and split paths; wake/reject paths clear pause_kind = NULL.
      get_snoozed_transfers filters to ISNULL(pause_kind, 'snooze') = 'snooze',
      while get_pending_transfers returns Pending + (Paused AND pause_kind =
      'pause') with paused rows sorted below Pending. Added _pause_rec /
      _resume_rec, pause_transfer / resume_transfer callables, and Pause /
      Resume branches in update_decision and bulk_update_decisions.
  2026-09-23 (v2.2 SNOOZE + ORDER HOLD, Hub Manager request on SO 610353):
    - ORDER HOLD: every pending row carries order_hold_qty / order_hold_text
      ("ORDER HOLD: 30 on SO 610353-001, 6 on SO 609449-001 (36 of 37 on
      open orders)") built from sales_orders_due status 00 lines, and the
      sales_orders string tags each held line [HOLD]. Held orders are still
      demand - the app says so instead of hiding them.
    - SNOOZE replaces Pause. _snooze_rec(): amount + until-date + reason.
      Whatever is NOT snoozed is approved now (partial snooze = the original
      rec approved for the released part, a NEW Paused rec for the parked
      part, linked in notes). Paused rows no longer appear on the Transfer
      Form (get_pending_transfers = Pending only); they live on #snoozed via
      get_snoozed_transfers with live numbers from the view.
    - wake_expired_snoozes(): Paused + snooze_until <= today -> Pending
      (reviewed_date stamped so the AI gate re-reviews). Called here on
      every load AND by transfer_ai_review 5.36 each batch, so v2.2 works
      while v2.1 still owns the scheduled task.
    - wake_snoozed / resnooze / reject_snoozed for the #snoozed row actions.
      Every action writes ai_agent_log (REC_SNOOZED, REC_SNOOZE_SPLIT,
      REC_SNOOZE_EXPIRED, REC_WOKEN, REC_RESNOOZED, REC_REJECTED_FROM_SNOOZE).
    - Notes stamps use DB time (GETDATE, Eastern) via _db_stamp, matching
      paused_date / reviewed_date on the same row (first live test 2026-09-23
      showed 16:08 in notes vs 12:08 in the columns).
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


def _param_int(conn, name, default):
  """Integer global parameter, default when the row is missing or bad."""
  try:
    cur = conn.cursor()
    cur.execute("SELECT parameter_value FROM dbo.global_parameters "
                "WHERE parameter_name = %s", (name,))
    row = cur.fetchone()
    return int(float(row['parameter_value'])) if row and row['parameter_value'] else default
  except Exception:
    return default


def _horizon_days(conn, default=28):
  """global_parameters.transfer_planning_horizon_days (the view's window)."""
  return _param_int(conn, 'transfer_planning_horizon_days', default)


def _order_hold_text(h):
  """'ORDER HOLD: 30 on SO 610353-001, 6 on SO 609449-001 (36 of 37 on open
  orders)' or '' when nothing is held. One line, per order, so the Hub
  Manager knows whom to call."""
  if not h or not h.get('held'):
    return ''
  return ("ORDER HOLD: " + ', '.join(h['lines']) +
          " (%d of %d on open orders). Held orders may never release - confirm "
          "with Sales, then ship the released part or Snooze the held part."
          % (h['held'], h['total']))


def _attach_demand_detail(conn, rows):
  """v5.21: Attach demand-bucket + open sales-order strings to each pending row.

  Additive and DEFENSIVE: any failure leaves rows untouched so the grid still
  loads. Read-only fields added to every row dict:
    - demand_buckets  : weekly 0-7 / 8-14 / 15-21 / 22-28 day increments,
                        read straight from dbo.vw_sku_demand_buckets.
    - sales_orders    : open order list 'number-rel:qty', soonest ship first,
                        within the planning window, from dbo.sales_orders_due;
                        2026-09-23: a Hold-status line (e21 item or order
                        status 00) is tagged ' [HOLD]'.
    - order_hold_qty  : units of that demand on Hold-status lines (0 = none)
    - order_hold_text : the ORDER HOLD line (see _order_hold_text), or ''
  """
  if not rows:
    return
  skus = sorted({r['sku'] for r in rows if r.get('sku')})
  if not skus:
    return
  try:
    placeholders = ','.join(['%s'] * len(skus))
    horizon = _horizon_days(conn)
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

    # Open sales orders (balance due) within the planning window, soonest
    # ship first. Hold-status lines are flagged (e21 status 00 on the item
    # OR the order).
    cur.execute(
      "SELECT nx_sku, nx_order_numb, nx_rel_numb, nx_qty_balance_due, "
      "       CASE WHEN ISNULL(nx_item_status, '') = '00' "
      "              OR ISNULL(nx_order_status, '') = '00' THEN 1 ELSE 0 END AS on_hold "
      "FROM dbo.sales_orders_due "
      "WHERE nx_qty_balance_due > 0 "
      "AND DATEDIFF(day, CAST(GETDATE() AS DATE), nx_dship_by_date) <= %s "
      "AND nx_sku IN (" + placeholders + ") "
      "ORDER BY nx_sku, nx_dship_by_date ASC",
      (horizon,) + tuple(skus))
    orders = {}
    holds = {}
    for o in cur.fetchall():
      sku = o['nx_sku']
      qty = _to_int(o['nx_qty_balance_due'])
      label = str(o['nx_order_numb'])
      if o.get('nx_rel_numb'):
        label = label + "-" + str(o['nx_rel_numb'])
      held = bool(o.get('on_hold'))
      orders.setdefault(sku, []).append(
        label + ":" + str(qty) + (" [HOLD]" if held else ""))
      h = holds.setdefault(sku, {'held': 0, 'total': 0, 'lines': []})
      h['total'] += qty
      if held:
        h['held'] += qty
        h['lines'].append("%d on SO %s" % (qty, label))

    for r in rows:
      sku = r.get('sku')
      r['demand_buckets'] = buckets.get(sku, 'No demand in %d-day horizon' % horizon)
      ord_list = orders.get(sku, [])
      r['sales_orders'] = ', '.join(ord_list) if ord_list else 'No open sales orders'
      h = holds.get(sku)
      r['order_hold_qty'] = int(h['held']) if h else 0
      r['order_hold_text'] = _order_hold_text(h)
  except Exception as e:
    # Non-fatal: demand detail is a display nicety, never block the grid
    print("demand-detail enrichment skipped: " + str(e))
    for r in rows:
      r.setdefault('order_hold_qty', 0)
      r.setdefault('order_hold_text', '')


# =============================================================================
# SNOOZE (v2.2, 2026-09-23) - replaces Pause
# =============================================================================
# A snoozed rec is status 'Paused' with paused_date / pause_reason and an
# optional snooze_until (NULL = until the manager wakes it). It is OFF the
# Transfer Form and ON #snoozed. transfer_ai_review keeps treating Paused
# exactly as before: never sent to the AI (ai_skip_paused), snapshot
# touched each batch, obsoleted when the SKU no longer needs a transfer.
# =============================================================================

_HUB_MANAGER = 'Hub Manager'


def _log_agent(conn, action_type, sku, old_value, new_value, reason,
               executed_by='transfer_dashboard'):
  cur = conn.cursor()
  cur.execute(
    "INSERT INTO dbo.ai_agent_log "
    "(action_type, sku, old_value, new_value, reason, action_timestamp, executed_by) "
    "VALUES (%s, %s, %s, %s, %s, GETDATE(), %s)",
    (action_type, sku, (old_value or '')[:500], (new_value or '')[:500],
     (reason or '')[:1000], executed_by))


def _db_today(conn):
  cur = conn.cursor()
  cur.execute("SELECT CAST(GETDATE() AS DATE) AS today")
  return cur.fetchone()['today']


def _db_stamp(conn):
  """'YYYY-MM-DD HH:MM' in DATABASE time (Eastern), for the notes trail.
  Anvil's datetime.now() is UTC and was stamping notes 4 h ahead of the
  paused_date / reviewed_date / ai_agent_log columns on the same row."""
  cur = conn.cursor()
  cur.execute("SELECT CONVERT(varchar(16), GETDATE(), 120) AS stamp")
  return cur.fetchone()['stamp']


def _parse_snooze_date(value, today):
  """'YYYY-MM-DD' / date / datetime / None -> date or None (None = no date).
  Must be today or later (DB clock, Eastern)."""
  if value in (None, ''):
    return None
  if isinstance(value, datetime):
    d = value.date()
  elif isinstance(value, date):
    d = value
  else:
    try:
      d = datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
    except ValueError:
      raise Exception("Snooze date must be YYYY-MM-DD (got %r)." % (value,))
  if d < today:
    raise Exception("Snooze date %s is in the past (today is %s)." % (d, today))
  return d


def _fmt_date(d):
  return d.strftime('%Y-%m-%d') if d else 'no date'


def _snooze_rec(conn, recommendation_id, snooze_qty, snooze_until, reason,
                reviewed_by=_HUB_MANAGER, total_qty=None):
  """
  The single implementation of Snooze. Validates everything BEFORE the
  first write so a refused row leaves nothing half-done.

    snooze_qty   None / >= effective qty -> the whole rec is snoozed
                 1 .. eff-1              -> SPLIT: original rec Approved for
                                            (eff - snooze_qty) = ships now;
                                            NEW Paused rec for snooze_qty
    snooze_until date / 'YYYY-MM-DD' / None (None = until woken by hand)
    reason       free text (pre-filled by the client with the ORDER HOLD line)
    total_qty    the request size the manager was looking at (the row's qty
                 box); when given it replaces the DB effective qty so the
                 split matches what the dialog said would ship

  Returns {'mode': 'all'|'split', 'sku', 'ship_qty', 'snooze_qty',
           'snoozed_rec_id', 'until'}. Raises Exception with a message the
  form can show. Does NOT commit - the caller owns the transaction.
  """
  cur = conn.cursor()
  today = _db_today(conn)
  until = _parse_snooze_date(snooze_until, today)
  reason = (reason or '').strip() or 'Snoozed'
  reviewed_by = (reviewed_by or _HUB_MANAGER)[:50]

  cur.execute("""
      SELECT r.recommendation_id, r.sku, r.status,
             CAST(COALESCE(r.[manager_override_qty ], r.ai_recommended_qty,
                           r.calculated_transfer_qty) AS DECIMAL(18,4)) AS eff_qty,
             (SELECT COALESCE(SUM(pl.qty_planned), 0)
              FROM dbo.transfer_pick_lines pl
              WHERE pl.recommendation_id = r.recommendation_id
                AND pl.status IN ('Assigned', 'Picked', 'Short')) AS allocated
      FROM dbo.ai_transfer_recommendations r
      WHERE r.recommendation_id = %s
        AND r.status IN ('Pending', 'Paused')
  """, (recommendation_id,))
  rec = cur.fetchone()
  if not rec:
    raise Exception("Rec %s is no longer Pending/Snoozed (the review may have "
                    "obsoleted it or a pick consumed it). Refresh the list."
                    % recommendation_id)
  if float(rec['allocated'] or 0) > 0:
    raise Exception("Rec %s (%s) has %g unit(s) on active pick lines - resolve "
                    "or cancel the picks in the Pick Editor first (rule C6)."
                    % (recommendation_id, rec['sku'], float(rec['allocated'])))

  sku = rec['sku']
  eff = float(rec['eff_qty'] or 0)
  if total_qty not in (None, ''):
    try:
      if float(total_qty) > 0:
        eff = float(total_qty)
    except (TypeError, ValueError):
      pass
  if snooze_qty in (None, ''):
    snooze_qty = eff
  try:
    snooze_qty = float(snooze_qty)
  except (TypeError, ValueError):
    raise Exception("Snooze quantity must be a number (got %r)." % (snooze_qty,))
  if snooze_qty <= 0:
    raise Exception("Snooze quantity must be at least 1.")

  stamp = _db_stamp(conn)
  until_txt = _fmt_date(until)

  # ---------------------------------------------------------- whole rec
  if eff <= 0 or snooze_qty >= eff:
    parked = int(eff) if eff > 0 else int(snooze_qty)
    note = ' | [%s] Snoozed %d until %s: %s' % (stamp, parked, until_txt, reason)
    # A rec whose DB qty is 0 (AI said 0 on a hold) records the parked
    # amount as the override so the Snoozed form shows a number; a rec
    # that already has a qty keeps it untouched.
    db_eff = float(rec['eff_qty'] or 0)
    cur.execute("""
        UPDATE dbo.ai_transfer_recommendations
        SET status = 'Paused',
            pause_kind = 'snooze',
            paused_date = GETDATE(),
            pause_reason = %s,
            snooze_until = %s,
            [manager_override_qty ] = CASE WHEN %s <= 0 THEN %s ELSE [manager_override_qty ] END,
            reviewed_by = %s,
            reviewed_date = GETDATE(),
            notes = CONCAT(ISNULL(CAST(notes AS nvarchar(max)), ''), %s)
        WHERE recommendation_id = %s
          AND status IN ('Pending', 'Paused')
    """, (reason[:1000], until, db_eff, parked, reviewed_by, note, recommendation_id))
    if cur.rowcount != 1:
      raise Exception("Rec %s moved while you were deciding - refresh the list."
                      % recommendation_id)
    _log_agent(conn, 'REC_SNOOZED', sku,
               'rec %s %s qty %d' % (recommendation_id, rec['status'], parked),
               'Paused until %s' % until_txt, reason, executed_by=reviewed_by)
    return {'mode': 'all', 'sku': sku, 'ship_qty': 0, 'snooze_qty': parked,
            'snoozed_rec_id': recommendation_id, 'until': until_txt}

  # ---------------------------------------------------------- split
  ship_qty = int(round(eff - snooze_qty))
  parked = int(round(snooze_qty))

  # 1. The released part ships now: the original rec is Approved for it.
  cur.execute("""
      UPDATE dbo.ai_transfer_recommendations
      SET status = 'Approved',
          [manager_override_qty ] = %s,
          reviewed_by = %s,
          reviewed_date = GETDATE(),
          notes = CONCAT(ISNULL(CAST(notes AS nvarchar(max)), ''), %s)
      WHERE recommendation_id = %s
        AND status IN ('Pending', 'Paused')
  """, (ship_qty, reviewed_by,
        ' | [%s] Approved %d now; %d snoozed until %s: %s'
        % (stamp, ship_qty, parked, until_txt, reason),
        recommendation_id))
  if cur.rowcount != 1:
    raise Exception("Rec %s moved while you were deciding - refresh the list."
                    % recommendation_id)

  # 2. The parked part becomes its own Paused rec: same snapshot, qty =
  #    parked (calc, AI and override all set so a batch 'touch', which
  #    rewrites calculated_transfer_qty, cannot change what was parked).
  cur.execute("""
      INSERT INTO dbo.ai_transfer_recommendations (
          sku, event_type, base_urgency,
          starlite_available, starlite_on_hand, hub_available, hub_on_hand,
          e21_in_transit_qty, fresh_approved_qty, effective_on_hand,
          starlite_effective_on_hand, hub_effective_on_hand,
          min_stock, max_stock, avg_daily_usage,
          total_demand, order_count, earliest_ship_date, latest_ship_date,
          demand_overdue, demand_1_2_days, remaining_after_demand,
          time_urgency, inventory_urgency, hub_status,
          calculated_transfer_qty, ai_recommended_qty, [manager_override_qty ],
          confidence_level, reasoning, seasonal_consideration, risk_factors,
          recommendation_date, status, batch_id, created_by, has_duplicate_warning,
          paused_date, pause_reason, snooze_until, reviewed_by, reviewed_date, notes, pause_kind
      )
      OUTPUT INSERTED.recommendation_id
      SELECT sku, event_type, base_urgency,
             starlite_available, starlite_on_hand, hub_available, hub_on_hand,
             e21_in_transit_qty, fresh_approved_qty, effective_on_hand,
             starlite_effective_on_hand, hub_effective_on_hand,
             min_stock, max_stock, avg_daily_usage,
             total_demand, order_count, earliest_ship_date, latest_ship_date,
             demand_overdue, demand_1_2_days, remaining_after_demand,
             time_urgency, inventory_urgency, hub_status,
             %s, %s, %s,
             confidence_level,
             CONCAT(%s, CHAR(10), CHAR(10), ISNULL(CAST(reasoning AS nvarchar(max)), '')),
             seasonal_consideration, risk_factors,
             recommendation_date, 'Paused', batch_id, 'snooze_split', has_duplicate_warning,
             GETDATE(), %s, %s, %s, GETDATE(), %s, 'snooze'
      FROM dbo.ai_transfer_recommendations
      WHERE recommendation_id = %s
  """, (parked, parked, parked,
        'SNOOZED %d of %d from rec #%s until %s (%s). The other %d were approved and ship now.'
        % (parked, int(eff), recommendation_id, until_txt, reason, ship_qty),
        reason[:1000], until, reviewed_by,
        '[%s] Split from rec #%s: %d snoozed until %s, %d approved on the original'
        % (stamp, recommendation_id, parked, until_txt, ship_qty),
        recommendation_id))
  new_row = cur.fetchone()
  new_id = int(new_row['recommendation_id']) if new_row else None

  # 3. Back-link the original to the parked rec.
  if new_id:
    cur.execute("""
        UPDATE dbo.ai_transfer_recommendations
        SET notes = CONCAT(ISNULL(CAST(notes AS nvarchar(max)), ''), %s)
        WHERE recommendation_id = %s
    """, (' (snoozed part = rec #%d)' % new_id, recommendation_id))

  _log_agent(conn, 'REC_SNOOZE_SPLIT', sku,
             'rec %s %s eff qty %d' % (recommendation_id, rec['status'], int(eff)),
             'Approved %d now; rec #%s Paused %d until %s'
             % (ship_qty, new_id, parked, until_txt),
             reason, executed_by=reviewed_by)
  return {'mode': 'split', 'sku': sku, 'ship_qty': ship_qty, 'snooze_qty': parked,
          'snoozed_rec_id': new_id, 'until': until_txt}


def _pause_rec(conn, recommendation_id, reviewed_by=_HUB_MANAGER):
  """One-click PAUSE: whole rec, no date, no reason. Pending -> Paused/'pause'.
  Stays on the Transfer Form; nothing wakes it but Resume."""
  cur = conn.cursor()
  cur.execute("""
      SELECT sku, status FROM dbo.ai_transfer_recommendations
      WHERE recommendation_id = %s
  """, (recommendation_id,))
  rec = cur.fetchone()
  if not rec:
    raise Exception("Rec %s not found." % recommendation_id)
  if rec['status'] != 'Pending':
    raise Exception("Rec %s is %s, not Pending - refresh the list." % (recommendation_id, rec['status']))
  stamp = _db_stamp(conn)
  cur.execute("""
      UPDATE dbo.ai_transfer_recommendations
      SET status = 'Paused',
          pause_kind = 'pause',
          paused_date = GETDATE(),
          pause_reason = NULL,
          snooze_until = NULL,
          reviewed_by = %s,
          reviewed_date = GETDATE(),
          notes = CONCAT(ISNULL(CAST(notes AS nvarchar(max)), ''), %s)
      WHERE recommendation_id = %s AND status = 'Pending'
  """, (reviewed_by, ' | [%s] Paused' % stamp, recommendation_id))
  if cur.rowcount != 1:
    raise Exception("Rec %s moved while you were deciding - refresh the list." % recommendation_id)
  _log_agent(conn, 'REC_PAUSED', rec['sku'], 'rec %s Pending' % recommendation_id,
             'Paused (pause)', 'one-click pause', executed_by=reviewed_by)
  return {'recommendation_id': recommendation_id, 'sku': rec['sku']}


def _resume_rec(conn, recommendation_id, reviewed_by=_HUB_MANAGER):
  """RESUME a paused rec: Paused/'pause' -> Pending. reviewed_date is stamped
  so the detector's gate re-reviews it with fresh numbers on the next batch."""
  cur = conn.cursor()
  cur.execute("""
      SELECT sku, status, pause_kind, paused_date FROM dbo.ai_transfer_recommendations
      WHERE recommendation_id = %s
  """, (recommendation_id,))
  rec = cur.fetchone()
  if not rec:
    raise Exception("Rec %s not found." % recommendation_id)
  if rec['status'] != 'Paused' or rec['pause_kind'] != 'pause':
    raise Exception("Rec %s is not paused (it is %s%s) - refresh the list."
                    % (recommendation_id, rec['status'],
                       '/' + rec['pause_kind'] if rec['pause_kind'] else ''))
  stamp = _db_stamp(conn)
  cur.execute("""
      UPDATE dbo.ai_transfer_recommendations
      SET status = 'Pending',
          pause_kind = NULL,
          snooze_until = NULL,
          reviewed_by = %s,
          reviewed_date = GETDATE(),
          notes = CONCAT(ISNULL(CAST(notes AS nvarchar(max)), ''), %s)
      WHERE recommendation_id = %s AND status = 'Paused' AND pause_kind = 'pause'
  """, (reviewed_by, ' | [%s] Resumed - back to Pending' % stamp, recommendation_id))
  if cur.rowcount != 1:
    raise Exception("Rec %s moved while you were deciding - refresh the list." % recommendation_id)
  _log_agent(conn, 'REC_RESUMED', rec['sku'], 'rec %s Paused (pause)' % recommendation_id,
             'Pending', 'resumed by hand', executed_by=reviewed_by)
  return {'recommendation_id': recommendation_id, 'sku': rec['sku']}


def wake_expired_snoozes(conn, performed_by='snooze_expired'):
  """Paused recs whose snooze_until has arrived go back to Pending.
  reviewed_date is stamped (> recommendation_date) so the AI re-review
  gate treats it as 'human touched' and re-reviews with fresh numbers on
  the next batch. Commits. Returns the number woken."""
  cur = conn.cursor()
  cur.execute("""
      SELECT recommendation_id, sku, snooze_until
      FROM dbo.ai_transfer_recommendations
      WHERE status = 'Paused'
        AND snooze_until IS NOT NULL
        AND snooze_until <= CAST(GETDATE() AS DATE)
  """)
  due = list(cur.fetchall())
  if not due:
    return 0
  stamp = _db_stamp(conn)
  woken = 0
  for r in due:
    cur.execute("""
        UPDATE dbo.ai_transfer_recommendations
        SET status = 'Pending',
            pause_kind = NULL,
            snooze_until = NULL,
            reviewed_by = %s,
            reviewed_date = GETDATE(),
            notes = CONCAT(ISNULL(CAST(notes AS nvarchar(max)), ''), %s)
        WHERE recommendation_id = %s AND status = 'Paused'
    """, (performed_by, ' | [%s] Snooze expired (was until %s) - back to Pending'
          % (stamp, _fmt_date(r['snooze_until'])), r['recommendation_id']))
    if cur.rowcount == 1:
      woken += 1
      _log_agent(conn, 'REC_SNOOZE_EXPIRED', r['sku'],
                 'rec %s Paused until %s' % (r['recommendation_id'], _fmt_date(r['snooze_until'])),
                 'Pending', 'snooze date reached', executed_by=performed_by)
  conn.commit()
  return woken


@anvil.server.callable
def snooze_transfer(recommendation_id, snooze_qty=None, snooze_until=None,
                    reason=None, reviewed_by=_HUB_MANAGER, total_qty=None):
  """Snooze one rec (whole or part). See _snooze_rec."""
  conn = get_connection()
  try:
    result = _snooze_rec(conn, recommendation_id, snooze_qty, snooze_until, reason,
                         reviewed_by, total_qty=total_qty)
    conn.commit()
    result['status'] = 'success'
    return result
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': str(e)}
  finally:
    conn.close()


@anvil.server.callable
def pause_transfer(recommendation_id, reviewed_by=_HUB_MANAGER):
  conn = get_connection()
  try:
    r = _pause_rec(conn, recommendation_id, reviewed_by)
    conn.commit()
    r['status'] = 'success'
    return r
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': str(e)}
  finally:
    conn.close()


@anvil.server.callable
def resume_transfer(recommendation_id, reviewed_by=_HUB_MANAGER):
  conn = get_connection()
  try:
    r = _resume_rec(conn, recommendation_id, reviewed_by)
    conn.commit()
    r['status'] = 'success'
    return r
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': str(e)}
  finally:
    conn.close()


@anvil.server.callable
def get_snoozed_transfers():
  """Every snoozed (Paused) rec with the numbers it was parked on AND the
  live numbers from the view, for the #snoozed form."""
  try:
    conn = get_connection()
    woken = wake_expired_snoozes(conn)
    cur = conn.cursor()
    cur.execute("""
        SELECT r.recommendation_id, r.sku, ps.part_desc AS part_description,
               r.status, r.event_type,
               CAST(COALESCE(r.[manager_override_qty ], r.ai_recommended_qty,
                             r.calculated_transfer_qty) AS DECIMAL(18,4)) AS snoozed_qty,
               r.pause_reason, r.reviewed_by, r.paused_date, r.snooze_until,
               DATEDIFF(day, CAST(GETDATE() AS DATE), r.snooze_until) AS days_left,
               DATEDIFF(day, r.paused_date, GETDATE()) AS days_snoozed,
               r.time_urgency AS rec_time_urgency,
               r.inventory_urgency AS rec_inventory_urgency,
               r.starlite_available AS rec_starlite_available,
               r.hub_available AS rec_hub_available,
               r.reasoning, r.notes,
               ISNULL(r.has_duplicate_warning, 0) AS has_duplicate_warning,
               v.starlite_available AS live_starlite_available,
               v.hub_available AS live_hub_available,
               v.demand_in_horizon AS live_demand,
               v.time_urgency AS live_time_urgency,
               v.inventory_urgency AS live_inventory_urgency,
               v.suggested_transfer_qty AS live_calc_qty,
               CASE WHEN v.sku IS NULL THEN 0 ELSE 1 END AS still_needed
        FROM dbo.ai_transfer_recommendations r
        LEFT JOIN dbo.part_specs ps ON ps.sku = r.sku
        LEFT JOIN dbo.vw_unified_transfer_detection v
               ON v.sku = r.sku AND v.transfer_needed = 'Yes'
        WHERE r.status = 'Paused' AND ISNULL(r.pause_kind, 'snooze') = 'snooze'
        ORDER BY CASE WHEN r.snooze_until IS NULL THEN 1 ELSE 0 END,
                 r.snooze_until, r.paused_date, r.sku
    """)
    rows = [serialize_row(row) for row in cur.fetchall()]
    _attach_demand_detail(conn, rows)
    conn.close()
    return {'status': 'success', 'rows': rows, 'count': len(rows), 'woken': woken}
  except Exception as e:
    return {'status': 'error', 'rows': [], 'count': 0, 'message': str(e)}


@anvil.server.callable
def wake_snoozed(recommendation_id, note=None, reviewed_by=_HUB_MANAGER):
  """#snoozed WAKE NOW: back to Pending; re-reviewed on the next batch."""
  conn = get_connection()
  try:
    cur = conn.cursor()
    note = (note or '').strip()
    stamp = _db_stamp(conn)
    cur.execute("""
        UPDATE dbo.ai_transfer_recommendations
        SET status = 'Pending',
            pause_kind = NULL,
            snooze_until = NULL,
            reviewed_by = %s,
            reviewed_date = GETDATE(),
            notes = CONCAT(ISNULL(CAST(notes AS nvarchar(max)), ''), %s)
        OUTPUT INSERTED.sku
        WHERE recommendation_id = %s AND status = 'Paused'
    """, ((reviewed_by or _HUB_MANAGER)[:50],
          ' | [%s] Woken by hand%s' % (stamp, (': ' + note) if note else ''),
          recommendation_id))
    row = cur.fetchone()
    if not row:
      conn.rollback()
      return {'status': 'error',
              'message': 'Rec %s is no longer snoozed - refresh.' % recommendation_id}
    _log_agent(conn, 'REC_WOKEN', row['sku'], 'rec %s Paused' % recommendation_id,
               'Pending', note or 'manager wake via #snoozed', executed_by=reviewed_by)
    conn.commit()
    return {'status': 'success', 'sku': row['sku'], 'recommendation_id': recommendation_id}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': str(e)}
  finally:
    conn.close()


@anvil.server.callable
def resnooze(recommendation_id, snooze_until=None, note=None, reviewed_by=_HUB_MANAGER):
  """#snoozed CHANGE DATE: new until-date (None = no date) on a snoozed rec."""
  conn = get_connection()
  try:
    cur = conn.cursor()
    until = _parse_snooze_date(snooze_until, _db_today(conn))
    note = (note or '').strip()
    stamp = _db_stamp(conn)
    cur.execute("""
        UPDATE dbo.ai_transfer_recommendations
        SET snooze_until = %s,
            reviewed_by = %s,
            reviewed_date = GETDATE(),
            notes = CONCAT(ISNULL(CAST(notes AS nvarchar(max)), ''), %s)
        OUTPUT INSERTED.sku, DELETED.snooze_until AS old_until
        WHERE recommendation_id = %s AND status = 'Paused'
    """, (until, (reviewed_by or _HUB_MANAGER)[:50],
          ' | [%s] Snooze date changed to %s%s' % (stamp, _fmt_date(until), (': ' + note) if note else ''),
          recommendation_id))
    row = cur.fetchone()
    if not row:
      conn.rollback()
      return {'status': 'error',
              'message': 'Rec %s is no longer snoozed - refresh.' % recommendation_id}
    _log_agent(conn, 'REC_RESNOOZED', row['sku'],
               'until %s' % _fmt_date(row['old_until']), 'until %s' % _fmt_date(until),
               note or 'manager change via #snoozed', executed_by=reviewed_by)
    conn.commit()
    return {'status': 'success', 'sku': row['sku'], 'until': _fmt_date(until)}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': str(e)}
  finally:
    conn.close()


@anvil.server.callable
def reject_snoozed(recommendation_id, note=None, reviewed_by=_HUB_MANAGER):
  """#snoozed REJECT: the parked rec is rejected (never re-raised while the
  need is unchanged - the v5.22 cooldown applies)."""
  conn = get_connection()
  try:
    cur = conn.cursor()
    note = (note or '').strip()
    stamp = _db_stamp(conn)
    cur.execute("""
        UPDATE dbo.ai_transfer_recommendations
        SET status = 'Rejected',
            pause_kind = NULL,
            snooze_until = NULL,
            reviewed_by = %s,
            reviewed_date = GETDATE(),
            notes = CONCAT(ISNULL(CAST(notes AS nvarchar(max)), ''), %s)
        OUTPUT INSERTED.sku
        WHERE recommendation_id = %s AND status = 'Paused'
          AND NOT EXISTS (SELECT 1 FROM dbo.transfer_pick_lines pl
                          WHERE pl.recommendation_id = dbo.ai_transfer_recommendations.recommendation_id
                            AND pl.status IN ('Assigned', 'Picked', 'Short'))
    """, ((reviewed_by or _HUB_MANAGER)[:50],
          ' | [%s] Rejected from #snoozed%s' % (stamp, (': ' + note) if note else ''),
          recommendation_id))
    row = cur.fetchone()
    if not row:
      conn.rollback()
      return {'status': 'error',
              'message': 'Rec %s is no longer snoozed (or has pick lines) - refresh.' % recommendation_id}
    _log_agent(conn, 'REC_REJECTED_FROM_SNOOZE', row['sku'], 'rec %s Paused' % recommendation_id,
               'Rejected', note or 'manager reject via #snoozed', executed_by=reviewed_by)
    conn.commit()
    return {'status': 'success', 'sku': row['sku']}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': str(e)}
  finally:
    conn.close()



# =============================================================================
# DATA RETRIEVAL FUNCTIONS
# =============================================================================

@anvil.server.callable
def get_pending_transfers():
  """
    Retrieve all pending + paused transfer recommendations for the DataGrid.

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
    # v2.2: snoozes whose date has arrived come back before the grid loads
    # (also done each batch by transfer_ai_review 5.36).
    wake_expired_snoozes(conn)
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
            pause_kind,
            DATEDIFF(day, paused_date, GETDATE()) AS days_paused,
            ISNULL(has_duplicate_warning, 0) AS has_duplicate_warning
        FROM dbo.ai_transfer_recommendations
        WHERE status = 'Pending'
           OR (status = 'Paused' AND pause_kind = 'pause')   -- 2026-10-25: paused stays on the form
        ORDER BY
            CASE WHEN status = 'Paused' THEN 2 ELSE 1 END,          -- paused sink below pending
            CASE WHEN ISNULL(has_duplicate_warning, 0) = 1 THEN 1 ELSE 2 END,  -- v5.5: duplicate warnings first
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

    # v2.2: the Snooze dialog's default until-date (today + N days). Row
    # in dbo.global_parameters 'snooze_default_days'; 7 if absent.
    _snooze_days = _param_int(conn, 'snooze_default_days', 7)
    for r in rows:
      r['snooze_default_days'] = _snooze_days

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
        decision: 'Approved', 'Rejected', 'Paused' (=Snooze), 'Pause', 'Resume'
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

    # v2.2: Paused = Snooze (whole rec, no date) - one implementation.
    if decision == 'Paused':
      r = _snooze_rec(conn, recommendation_id, None, None, pause_reason or notes, reviewed_by)
      conn.commit()
      conn.close()
      return {'status': 'success',
              'message': 'Snoozed recommendation %s (%s)' % (recommendation_id, r['sku'])}
    elif decision == 'Pause':
      r = _pause_rec(conn, recommendation_id, reviewed_by)
      conn.commit(); conn.close()
      return {'status': 'success', 'message': 'Paused recommendation %s (%s)' % (recommendation_id, r['sku'])}
    elif decision == 'Resume':
      r = _resume_rec(conn, recommendation_id, reviewed_by)
      conn.commit(); conn.close()
      return {'status': 'success', 'message': 'Resumed recommendation %s (%s)' % (recommendation_id, r['sku'])}

    # Handle Approved/Rejected with optional quantity override
    elif transfer_qty is not None:
      query = """
                UPDATE dbo.ai_transfer_recommendations
                SET status = %s,
                    pause_kind = NULL,
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
                    pause_kind = NULL,
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
  snoozed = []           # v2.2: {'sku', 'mode', 'ship_qty', 'snooze_qty', 'until'}
  paused = []
  resumed = []
  snooze_errors = []     # v2.2: {'recommendation_id', 'message'}

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
                        pause_kind = NULL,
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
                        pause_kind = NULL,
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
        # v2.2 SNOOZE: amount + until-date + reason from the row's dialog.
        # _snooze_rec validates before it writes, so a refused row costs
        # nothing; a DB failure mid-way still rolls the whole batch back.
        try:
          r = _snooze_rec(conn, rec_id, update.get('snooze_qty'),
                          update.get('snooze_until'), update.get('pause_reason'),
                          'Hub_Manager', total_qty=update.get('snooze_total'))
          snoozed.append(r)
          updated_count += 1
        except Exception as se:
          if 'moved while you were deciding' in str(se):
            raise
          snooze_errors.append({'recommendation_id': rec_id, 'message': str(se)})
        continue
      elif decision in ('Pause', 'Resume'):
        try:
          if decision == 'Pause':
            r = _pause_rec(conn, rec_id, 'Hub_Manager'); paused.append(r)
          else:
            r = _resume_rec(conn, rec_id, 'Hub_Manager'); resumed.append(r)
          updated_count += 1
        except Exception as pe:
          if 'moved while you were deciding' in str(pe):
            raise
          snooze_errors.append({'recommendation_id': rec_id, 'message': str(pe)})
        continue

      updated_count += cursor.rowcount

    conn.commit()
    _blocked_msg = ''
    if blocked_zero_qty:
      _blocked_msg = ('%d rec(s) NOT approved - the AI recommended 0, so with no '
                      'override qty they would be invisible to the Pick Editor. '
                      'Set a transfer qty first (or Reject): %s'
                      % (len(blocked_zero_qty),
                         ', '.join(b['sku'] for b in blocked_zero_qty)))
    if snooze_errors:
      _blocked_msg = (_blocked_msg + ('\n' if _blocked_msg else '') +
                      '%d snooze(s) refused: ' % len(snooze_errors) +
                      '; '.join('#%s %s' % (e['recommendation_id'], e['message'])
                                for e in snooze_errors))
    return {'status': 'success', 'updated_count': updated_count,
            'skipped_guarded': max(0, len(updates) - updated_count),
            'blocked_zero_qty': blocked_zero_qty,
            'snoozed': snoozed,
            'paused': paused,
            'resumed': resumed,
            'snooze_errors': snooze_errors,
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