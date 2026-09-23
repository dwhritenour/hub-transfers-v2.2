"""
pick_dashboard.py - Anvil Server Module
=======================================
Hub Transfers v2.0 - Pick stage server logic (Phase 3).

Serves both new forms:
  - pick_editor  (Hub Manager / Lead): pool query, pick CRUD, release,
    cancel, reopen, worker assignment
  - pick_worker  (shared kiosk tablet): worker list, my-picks, per-line
    tap recording (Picked / Short / Over), undo, complete

Design rules (locked 2026-07-30):
  - Option 1: rec status vocabulary unchanged. Pick creation is the
    Approved -> Applied trigger (replaces apply_transfers, B3).
  - Trip eligibility Option A: only Completed picks feed the Trip
    Editor (enforced in Phase 6 optimizer changes, not here).
  - C2: a Short line reduces the rec's approved qty at pick completion
    (rec closes short; the 15-min detector re-recommends if the need
    persists). No shortfall pool-return.
  - C4: over-pick allowed up to global_parameters.pick_overpick_cap_pct
    (% above planned; 0 disables).
  - C6: approved-qty edits below assigned are blocked in the REC editor
    (transfer_dashboard change, Phase 6); here the allocation guard is
    the mirror image: SUM(qty_planned) never exceeds approved.
  - C7: reopen Completed until any line is on a Finalized/Delivered
    trip assignment. C8: reassignment keeps per-line picked_by.
  - E2: pick completion with shorts/overs sends ONE batched SMS to Hub
    Managers (notification type PICK_EXCEPTIONS, falls back to
    NEW_RECOMMENDATIONS contacts), gated by SMSConfig.is_sms_enabled().

Every UPDATE is state-guarded (WHERE status = expected) with a rowcount
check - the app's own hard-won idempotency pattern. All pick actions
append to dbo.transfer_pick_events (append-only; never UPDATE/DELETE).
Rec-level milestones also go to dbo.ai_agent_log so existing tooling
sees the new stage.

Database objects: migrations 016-018 (hub_workers, transfer_picks,
transfer_pick_lines, transfer_pick_events, lookups, tta.pick_line_id).

Author: Claude (Dave Whritenour review)
Project: Hub Transfers v2.0 - Pick stage
Created: 2026-08-01
"""

import anvil.server
from datetime import datetime, date
from decimal import Decimal

from . import db_config
from .sms_notifications import (
TwilioSMSClient,
SMSConfig,
get_hub_manager_phones,
log_sms_notification,
)

__version__       = "1.4.0"
__version_date__  = "2026-09-10"
__version_notes__ = (
  "1.4.0 (2026-09-10, Hub policy): location 100 = ON HOLD, never picked "
  "for transfers. Every location lookup excludes global_parameters_text."
  "hub_hold_location (default '100') and is deterministic (largest bin "
  "first). Pool rows, pick lines and the kiosk carry on_hold (Hub "
  "inventory_current) + hold_location so the paper says '25 ON HOLD in "
  "100 - do not pick'. Lines stamped with the hold location before this "
  "version are re-resolved to a released bin when read (BS55I0600 / pick "
  "372: loc 100 held 25 and was the TOP 1 primary location). "
  "1.3.1 (2026-09-08): T-SQL string literals in the zero-pick UPDATE and the ZERO_PICK_REC_CANCELLED event INSERT were split across two Python source lines inside the SQL text (adjacent literals are NOT concatenated in T-SQL) - complete_pick failed with 'Incorrect syntax near' on pick 345, the first zero-pick under 1.3.0. Joined into single literals. "
  "1.3.0 (Transfer Logic Review 2026-09-04, findings C7 / H8 / M4): "
  "complete_pick now applies the standing zero-pick rule in code - a rec "
  "whose physical pick total is 0 (no qty>0 line anywhere, no trip "
  "assignment) is Cancelled with a note + ZERO_PICK_REC_CANCELLED event, "
  "instead of freezing at Applied/qty 0 with no UI exit (5 live cases, 63 "
  "hand-cleaned on 8/31). record_pick_line and add_lines_to_pick re-assert "
  "the pick's status inside the UPDATE/INSERT (two workers on one pick "
  "could put a line onto an already-Completed pick). Draft picks now age "
  "on created_date so a forgotten draft (pick 297: 3 days) shows amber. "
  "1.2.0 (Trip Editor v3 wave): get_pick_detail lines now carry "
  "time/inventory/base urgency + event_type (urgency visible to all "
  "Hub employees in #pick, per Hub request). download_pick_slip_for_pick "
  "refuses once the pick is Completed - the truck manifest (post-"
  "finalize, dock-owned) is the only paper for staged goods. "
  "1.1.0 (Phase 5): get_discrepancy_codes callable for the kiosk's "
  "reason buttons; get_my_picks now returns urgency_rank (1=CRITICAL.."
  "4=LOW, worst line wins) for pick-card priority colors. "
  "1.0.1: hub_product_locations lookups now filter primary_location=1 "
  "with TOP 1 (a multi-location SKU duplicated pool rows / broke the "
  "line INSERT's scalar subquery in 1.0.0 - never deployed to users). "
  "get_open_picks gains include_recent_completed for the Reopen flow. "
  "New download_pick_slip_for_pick: per-pick slip via the v5.27 "
  "pick_list_pdf_generator qty-override path. "
  "1.0.0: initial pick-stage module - pool query, pick CRUD, allocation "
  "guard with over-pick cap, kiosk callables, append-only event trail, "
  "C2 close-short on completion, batched PICK_EXCEPTIONS SMS."
)

# Rec statuses that may contribute lines to a pick. 'Applied' = already
# in picking (partially allocated recs stay in the pool while remaining>0).
_POOL_REC_STATUSES = ('Approved', 'Auto-Approved', 'Applied')


# =============================================================================
# HUB HOLD LOCATION (Hub policy 2026-09-10)
# =============================================================================
# Location 100 is the Hub's ON HOLD shelf (quality holds, possible return to
# supplier). Nothing there is ever picked for a transfer. e21 already keeps
# held units out of `available`, so the detector never recommends them; this
# keeps the PAPER honest too - no lookup below can ever hand a picker
# location 100, and picks/slips/kiosk cards say how much of the SKU is held.
# global_parameters_text.hub_hold_location overrides the code (default '100').
# =============================================================================
_HOLD_LOCATION_DEFAULT = '100'


def _hold_location():
  try:
    from . import system_settings
    v = system_settings.get_text_parameter('hub_hold_location', _HOLD_LOCATION_DEFAULT)
    return (str(v).strip() if v else _HOLD_LOCATION_DEFAULT) or _HOLD_LOCATION_DEFAULT
  except Exception:
    return _HOLD_LOCATION_DEFAULT


def _loc_subquery(sku_expr):
  """TOP 1 released (non-hold) primary location for sku_expr; needs ONE
  %s bound to the hold location. Largest bin first, then by code, so the
  same SKU always gets the same answer."""
  return ("(SELECT TOP 1 hl.location_code FROM dbo.hub_product_locations hl "
          "WHERE hl.sku = " + sku_expr + " AND hl.primary_location = 1 "
          "AND hl.location_code <> %s "
          "ORDER BY hl.location_qty DESC, hl.location_code)")


# Line statuses that consume allocation (everything but Cancelled).
_ACTIVE_LINE_STATUSES = ('Assigned', 'Picked', 'Short')

# NOTE: the rec override column really is named with a trailing space:
# [manager_override_qty ] - copied from transfer_ai_review.py usage.
_EFF_QTY_SQL = "COALESCE(r.[manager_override_qty ], r.ai_recommended_qty, r.calculated_transfer_qty)"


# =============================================================================
# SERIALIZATION HELPERS (same pattern as transfer_dashboard.py)
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
# SMALL SHARED HELPERS
# =============================================================================

def _get_param(conn, name, default):
  """Read a numeric global parameter; fall back to default."""
  cursor = conn.cursor()
  cursor.execute(
    "SELECT parameter_value FROM dbo.global_parameters WHERE parameter_name = %s",
    (name,)
  )
  row = cursor.fetchone()
  return float(row['parameter_value']) if row else default


def _log_event(conn, pick_id, event_type, pick_line_id=None,
               performed_by=None, qty=None, note=None):
  """Append one row to the (append-only) pick event trail."""
  cursor = conn.cursor()
  cursor.execute(
    """
      INSERT INTO dbo.transfer_pick_events
          (pick_id, pick_line_id, event_type, performed_by, qty, note)
      VALUES (%s, %s, %s, %s, %s, %s)
    """,
    (pick_id, pick_line_id, event_type, performed_by, qty, note)
  )


def _log_agent_action(conn, action_type, sku, old_value, new_value, reason):
  """Mirror rec-level milestones into ai_agent_log (existing tooling reads it)."""
  cursor = conn.cursor()
  cursor.execute(
    """
      INSERT INTO dbo.ai_agent_log (
          action_type, sku, old_value, new_value, reason, action_timestamp, executed_by
      )
      VALUES (%s, %s, %s, %s, %s, GETDATE(), %s)
    """,
    (action_type, sku, old_value, new_value, reason, 'pick_dashboard v' + __version__)
  )


def _pick_header(conn, pick_id):
  cursor = conn.cursor()
  cursor.execute(
    """
      SELECT p.pick_id, p.status, p.assigned_worker_code, w.display_name AS worker_name,
             p.notes, p.created_by, p.created_date, p.released_date,
             p.completed_date, p.cancelled_date
      FROM dbo.transfer_picks p
      LEFT JOIN dbo.hub_workers w ON w.worker_code = p.assigned_worker_code
      WHERE p.pick_id = %s
    """,
    (pick_id,)
  )
  return cursor.fetchone()


def _guarded_pick_status_update(conn, pick_id, from_statuses, to_status,
                                extra_set_sql=""):
  """State-guarded pick status flip; returns True if exactly one row moved."""
  cursor = conn.cursor()
  placeholders = ', '.join(['%s'] * len(from_statuses))
  cursor.execute(
    "UPDATE dbo.transfer_picks SET status = %s" + extra_set_sql +
    " WHERE pick_id = %s AND status IN (" + placeholders + ")",
    tuple([to_status, pick_id] + list(from_statuses))
  )
  return cursor.rowcount == 1


def _allocation_check(conn, recommendation_id, extra_qty,
                      exclude_line_id=None):
  """
    Allocation guard: existing active allocation + extra_qty must not
    exceed the rec's effective approved qty. Returns (ok, remaining,
    eff_qty, allocated). remaining is BEFORE adding extra_qty.
    """
  cursor = conn.cursor()
  exclude_sql = " AND pl.pick_line_id <> %s" if exclude_line_id else ""
  # Placeholder order follows the SQL text: the optional exclusion sits
  # inside the correlated subquery, ahead of the outer rec id.
  params = ((exclude_line_id, recommendation_id) if exclude_line_id
            else (recommendation_id,))
  cursor.execute(
    """
      SELECT CAST(""" + _EFF_QTY_SQL + """ AS DECIMAL(18,4)) AS eff_qty,
             (SELECT COALESCE(SUM(pl.qty_planned), 0)
              FROM dbo.transfer_pick_lines pl
              WHERE pl.recommendation_id = r.recommendation_id
                AND pl.status IN ('Assigned', 'Picked', 'Short')""" + exclude_sql + """
             ) AS allocated
      FROM dbo.ai_transfer_recommendations r
      WHERE r.recommendation_id = %s
    """,
    params
  )
  row = cursor.fetchone()
  if not row:
    return (False, 0.0, 0.0, 0.0)
  eff_qty = float(row['eff_qty'] or 0)
  allocated = float(row['allocated'] or 0)
  remaining = eff_qty - allocated
  return (extra_qty <= remaining + 1e-9, remaining, eff_qty, allocated)


# =============================================================================
# MANAGER CALLABLES - POOL AND PICK LIST
# =============================================================================

@anvil.server.callable
def get_pick_pool():
  """
    Left pane of the Pick Editor: approved rec lines with remaining
    allocation. remaining = effective approved qty - SUM(qty_planned)
    over active (non-cancelled) pick lines.
    """
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      """
        SELECT r.recommendation_id, r.sku, r.status, r.base_urgency,
               r.reviewed_date, r.recommendation_date,
               CAST(r.calculated_transfer_qty AS DECIMAL(18,4)) AS calculated_qty,
               CAST(""" + _EFF_QTY_SQL + """ AS DECIMAL(18,4)) AS approved_qty,
               COALESCE(al.allocated, 0) AS assigned_qty,
               CAST(""" + _EFF_QTY_SQL + """ AS DECIMAL(18,4))
                   - COALESCE(al.allocated, 0) AS remaining_qty,
               COALESCE(loc.location_code, '--') AS location_code,
               ps.part_desc AS part_description,
               CAST(COALESCE(inv.on_hold, 0) AS DECIMAL(18,4)) AS on_hold
        FROM dbo.ai_transfer_recommendations r
        LEFT JOIN (SELECT pl.recommendation_id,
                          SUM(pl.qty_planned) AS allocated
                   FROM dbo.transfer_pick_lines pl
                   WHERE pl.status IN ('Assigned', 'Picked', 'Short')
                   GROUP BY pl.recommendation_id) al
               ON al.recommendation_id = r.recommendation_id
        OUTER APPLY (SELECT """ + _loc_subquery('r.sku') + """ AS location_code) loc
        LEFT JOIN dbo.part_specs ps ON ps.sku = r.sku
        LEFT JOIN dbo.inventory_current inv
               ON inv.sku = r.sku AND inv.cost_center = 'Hub'
        WHERE r.status IN ('Approved', 'Auto-Approved', 'Applied')
          AND CAST(""" + _EFF_QTY_SQL + """ AS DECIMAL(18,4))
              - COALESCE(al.allocated, 0) > 0
        ORDER BY r.reviewed_date, r.recommendation_id
      """,
      (_hold_location(),)
    )
    return [serialize_row(row) for row in cursor.fetchall()]
  finally:
    conn.close()


@anvil.server.callable
def get_open_picks(include_recent_completed=False):
  """
    Pick list panel: every non-terminal pick with progress counts and
    an aging flag (Released/In_Progress older than pick_aging_alert_hours).
    include_recent_completed=True also returns picks Completed in the
    last 7 days, so the Pick Editor's Reopen flow (C7) can find them.
    """
  conn = db_config.get_nexus_connection()
  try:
    aging_hours = _get_param(conn, 'pick_aging_alert_hours', 4)
    status_sql = (
      "(p.status IN ('Draft', 'Released', 'In_Progress') "
      " OR (p.status = 'Completed' AND p.completed_date > DATEADD(day, -7, GETDATE())))"
      if include_recent_completed else
      "p.status IN ('Draft', 'Released', 'In_Progress')"
    )
    cursor = conn.cursor()
    cursor.execute(
      """
        SELECT p.pick_id, p.status, p.assigned_worker_code,
               w.display_name AS worker_name, p.notes,
               p.created_by, p.created_date, p.released_date,
               COUNT(pl.pick_line_id) AS line_count,
               SUM(CASE WHEN pl.status IN ('Picked', 'Short') THEN 1 ELSE 0 END) AS resolved_count,
               SUM(CASE WHEN pl.status = 'Short' THEN 1 ELSE 0 END) AS short_count,
               CASE WHEN p.status IN ('Released', 'In_Progress')
                         AND p.released_date IS NOT NULL
                         AND p.released_date < DATEADD(hour, -%s, GETDATE())
                    THEN 1
                    -- v1.3.0 (M4): a Draft holds its recs out of the pool and
                    -- off every tablet; age it on created_date.
                    WHEN p.status = 'Draft'
                         AND p.created_date < DATEADD(hour, -%s, GETDATE())
                    THEN 1 ELSE 0 END AS is_aging
        FROM dbo.transfer_picks p
        LEFT JOIN dbo.hub_workers w ON w.worker_code = p.assigned_worker_code
        LEFT JOIN dbo.transfer_pick_lines pl
               ON pl.pick_id = p.pick_id AND pl.status <> 'Cancelled'
        WHERE """ + status_sql + """
        GROUP BY p.pick_id, p.status, p.assigned_worker_code, w.display_name,
                 p.notes, p.created_by, p.created_date, p.released_date
        ORDER BY p.created_date
      """,
      (int(aging_hours), int(aging_hours))
    )
    return [serialize_row(row) for row in cursor.fetchall()]
  finally:
    conn.close()


@anvil.server.callable
def get_pick_detail(pick_id):
  """Header + lines (lines sorted by location for the guided path)."""
  conn = db_config.get_nexus_connection()
  try:
    header = _pick_header(conn, pick_id)
    if not header:
      raise Exception("Pick %s not found" % pick_id)
    hold_loc = _hold_location()
    cursor = conn.cursor()
    cursor.execute(
      """
        SELECT pl.pick_line_id, pl.pick_id, pl.recommendation_id, pl.sku,
               -- 1.4.0: a line stamped with the HOLD location (pre-1.4.0)
               -- is re-resolved to a released bin when read
               CASE WHEN pl.location_code = %s
                    THEN """ + _loc_subquery('pl.sku') + """
                    ELSE pl.location_code END AS location_code,
               pl.line_seq, pl.status,
               CAST(pl.qty_planned AS DECIMAL(18,4)) AS qty_planned,
               CAST(pl.qty_picked AS DECIMAL(18,4)) AS qty_picked,
               pl.discrepancy_code, pl.discrepancy_note,
               pl.picked_by, pw.display_name AS picked_by_name, pl.picked_date,
               ps.part_desc AS part_description,
               r.time_urgency, r.inventory_urgency, r.base_urgency, r.event_type,
               CAST(COALESCE(inv.on_hold, 0) AS DECIMAL(18,4)) AS on_hold,
               %s AS hold_location
        FROM dbo.transfer_pick_lines pl
        LEFT JOIN dbo.hub_workers pw ON pw.worker_code = pl.picked_by
        LEFT JOIN dbo.part_specs ps ON ps.sku = pl.sku
        LEFT JOIN dbo.ai_transfer_recommendations r
               ON r.recommendation_id = pl.recommendation_id
        LEFT JOIN dbo.inventory_current inv
               ON inv.sku = pl.sku AND inv.cost_center = 'Hub'
        WHERE pl.pick_id = %s AND pl.status <> 'Cancelled'
        ORDER BY COALESCE(pl.location_code, 'zzz'), pl.sku
      """,
      (hold_loc, hold_loc, hold_loc, pick_id)
    )
    return {
      'header': serialize_row(header),
      'lines': [serialize_row(row) for row in cursor.fetchall()],
    }
  finally:
    conn.close()


# =============================================================================
# MANAGER CALLABLES - PICK CRUD
# =============================================================================

@anvil.server.callable
def create_pick(created_by='Hub Manager', notes=None):
  """New Draft pick. Returns the new pick_id."""
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      """
        INSERT INTO dbo.transfer_picks (status, notes, created_by)
        OUTPUT INSERTED.pick_id
        VALUES ('Draft', %s, %s)
      """,
      (notes, created_by)
    )
    pick_id = cursor.fetchone()['pick_id']
    _log_event(conn, pick_id, 'PICK_CREATED', performed_by=created_by)
    conn.commit()
    return pick_id
  finally:
    conn.close()


@anvil.server.callable
def add_lines_to_pick(pick_id, lines, performed_by='Hub Manager'):
  """
    Add allocation slices to a Draft/Released pick.
    lines = [{'recommendation_id': int, 'qty': float}, ...]

    Per rec, inside one transaction:
      1. Allocation guard (active lines + new qty <= approved qty).
      2. INSERT line (denormalized sku + location).
      3. Approved/Auto-Approved -> Applied (state-guarded; B3 trigger).
    Returns {'added': n, 'errors': [msg, ...]}.
    """
  conn = db_config.get_nexus_connection()
  try:
    header = _pick_header(conn, pick_id)
    if not header or header['status'] not in ('Draft', 'Released'):
      raise Exception("Pick %s is not editable (status: %s)"
                      % (pick_id, header and header['status']))

    added, errors = 0, []
    cursor = conn.cursor()
    for line in lines:
      rec_id = int(line['recommendation_id'])
      qty = float(line['qty'])
      if qty <= 0:
        errors.append("Rec %s: qty must be > 0" % rec_id)
        continue

      ok, remaining, eff_qty, allocated = _allocation_check(conn, rec_id, qty)
      if not ok:
        errors.append("Rec %s: qty %.2f exceeds remaining %.2f (approved %.2f, already assigned %.2f)"
                      % (rec_id, qty, remaining, eff_qty, allocated))
        continue

      # UQ_transfer_pick_lines_pick_rec: one line per rec per pick.
      cursor.execute(
        "SELECT 1 AS x FROM dbo.transfer_pick_lines WHERE pick_id = %s AND recommendation_id = %s",
        (pick_id, rec_id)
      )
      if cursor.fetchone():
        errors.append("Rec %s: already on this pick (edit the existing line)" % rec_id)
        continue

      cursor.execute(
        """
          INSERT INTO dbo.transfer_pick_lines
              (pick_id, recommendation_id, sku, location_code, qty_planned)
          OUTPUT INSERTED.pick_line_id, INSERTED.sku
          SELECT %s, r.recommendation_id, r.sku,
                 """ + _loc_subquery('r.sku') + """,
                 %s
          FROM dbo.ai_transfer_recommendations r
          WHERE r.recommendation_id = %s
            AND r.status IN ('Approved', 'Auto-Approved', 'Applied')
            -- v1.3.0 (H8): the pick must still be editable at insert time
            AND EXISTS (SELECT 1 FROM dbo.transfer_picks p
                        WHERE p.pick_id = %s AND p.status IN ('Draft', 'Released'))
        """,
        (pick_id, _hold_location(), qty, rec_id, pick_id)
      )
      inserted = cursor.fetchone()
      if not inserted:
        errors.append("Rec %s: not in a pickable status (or the pick is no "
                      "longer editable)" % rec_id)
        continue

      # B3: pick creation applies the rec. Guarded; no-op if already Applied.
      cursor.execute(
        """
          UPDATE dbo.ai_transfer_recommendations
          SET status = 'Applied'
          WHERE recommendation_id = %s
            AND status IN ('Approved', 'Auto-Approved')
        """,
        (rec_id,)
      )
      if cursor.rowcount == 1:
        _log_agent_action(conn, 'PICK_CREATED', inserted['sku'],
                          'Approved', 'Applied',
                          "Allocated %.2f to pick %s" % (qty, pick_id))

      _log_event(conn, pick_id, 'LINE_ADDED',
                 pick_line_id=inserted['pick_line_id'],
                 performed_by=performed_by, qty=qty)
      added += 1

    conn.commit()
    return {'added': added, 'errors': errors}
  finally:
    conn.close()


@anvil.server.callable
def update_line_qty(pick_line_id, new_qty, performed_by='Hub Manager'):
  """Change qty_planned on an Assigned line of a Draft/Released pick."""
  new_qty = float(new_qty)
  if new_qty <= 0:
    raise Exception("Qty must be > 0 (remove the line instead)")
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      """
        SELECT pl.pick_id, pl.recommendation_id, pl.status AS line_status,
               CAST(pl.qty_planned AS DECIMAL(18,4)) AS qty_planned,
               p.status AS pick_status
        FROM dbo.transfer_pick_lines pl
        JOIN dbo.transfer_picks p ON p.pick_id = pl.pick_id
        WHERE pl.pick_line_id = %s
      """,
      (pick_line_id,)
    )
    row = cursor.fetchone()
    if not row:
      raise Exception("Line %s not found" % pick_line_id)
    if row['line_status'] != 'Assigned' or row['pick_status'] not in ('Draft', 'Released'):
      raise Exception("Line not editable (line: %s, pick: %s)"
                      % (row['line_status'], row['pick_status']))

    ok, remaining, eff_qty, allocated = _allocation_check(
      conn, row['recommendation_id'], new_qty, exclude_line_id=pick_line_id)
    if not ok:
      raise Exception("Qty %.2f exceeds remaining %.2f for rec %s"
                      % (new_qty, remaining, row['recommendation_id']))

    cursor.execute(
      """
        UPDATE dbo.transfer_pick_lines
        SET qty_planned = %s
        WHERE pick_line_id = %s AND status = 'Assigned'
      """,
      (new_qty, pick_line_id)
    )
    if cursor.rowcount != 1:
      conn.rollback()
      raise Exception("Line changed underneath you - refresh and retry")

    _log_event(conn, row['pick_id'], 'QTY_CHANGED', pick_line_id=pick_line_id,
               performed_by=performed_by, qty=new_qty,
               note="was %.2f" % float(row['qty_planned']))
    conn.commit()
    return True
  finally:
    conn.close()


@anvil.server.callable
def remove_line(pick_line_id, performed_by='Hub Manager'):
  """
    Cancel an Assigned line (row kept for audit). If the rec then has no
    active lines anywhere and no trip assignments, it reverts
    Applied -> Approved so it returns to the pool cleanly instead of
    sitting 'Applied' with zero allocation (the stranded state).
    """
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      """
        SELECT pl.pick_id, pl.recommendation_id, pl.sku,
               p.status AS pick_status
        FROM dbo.transfer_pick_lines pl
        JOIN dbo.transfer_picks p ON p.pick_id = pl.pick_id
        WHERE pl.pick_line_id = %s
      """,
      (pick_line_id,)
    )
    row = cursor.fetchone()
    if not row:
      raise Exception("Line %s not found" % pick_line_id)
    if row['pick_status'] not in ('Draft', 'Released', 'In_Progress'):
      raise Exception("Pick %s is %s - lines can no longer be removed"
                      % (row['pick_id'], row['pick_status']))

    cursor.execute(
      """
        UPDATE dbo.transfer_pick_lines
        SET status = 'Cancelled'
        WHERE pick_line_id = %s AND status = 'Assigned'
      """,
      (pick_line_id,)
    )
    if cursor.rowcount != 1:
      conn.rollback()
      raise Exception("Only Assigned lines can be removed (already picked?)")

    _log_event(conn, row['pick_id'], 'LINE_REMOVED', pick_line_id=pick_line_id,
               performed_by=performed_by)

    # Revert the rec to the pool if nothing holds it any more.
    cursor.execute(
      """
        UPDATE dbo.ai_transfer_recommendations
        SET status = 'Approved'
        WHERE recommendation_id = %s
          AND status = 'Applied'
          AND NOT EXISTS (SELECT 1 FROM dbo.transfer_pick_lines pl
                          WHERE pl.recommendation_id = %s
                            AND pl.status IN ('Assigned', 'Picked', 'Short'))
          AND NOT EXISTS (SELECT 1 FROM dbo.transfer_trip_assignments tta
                          WHERE tta.recommendation_id = %s)
      """,
      (row['recommendation_id'], row['recommendation_id'], row['recommendation_id'])
    )
    if cursor.rowcount == 1:
      _log_agent_action(conn, 'PICK_LINE_REMOVED', row['sku'],
                        'Applied', 'Approved',
                        "Last active pick line removed (pick %s)" % row['pick_id'])
    conn.commit()
    return True
  finally:
    conn.close()


@anvil.server.callable
def assign_worker(pick_id, worker_code, performed_by='Hub Manager'):
  """Assign/reassign (C8) while Draft/Released/In_Progress; per-line picked_by is untouched."""
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      "SELECT 1 AS x FROM dbo.hub_workers WHERE worker_code = %s AND is_active = 1",
      (worker_code,)
    )
    if not cursor.fetchone():
      raise Exception("Worker %s not found or inactive" % worker_code)

    cursor.execute(
      """
        UPDATE dbo.transfer_picks
        SET assigned_worker_code = %s
        WHERE pick_id = %s AND status IN ('Draft', 'Released', 'In_Progress')
      """,
      (worker_code, pick_id)
    )
    if cursor.rowcount != 1:
      conn.rollback()
      raise Exception("Pick %s is not assignable" % pick_id)

    _log_event(conn, pick_id, 'WORKER_ASSIGNED', performed_by=performed_by,
               note=worker_code)
    conn.commit()
    return True
  finally:
    conn.close()


@anvil.server.callable
def release_pick(pick_id, performed_by='Hub Manager'):
  """Draft -> Released. Requires an assigned worker and >= 1 active line."""
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      """
        SELECT p.assigned_worker_code,
               (SELECT COUNT(*) FROM dbo.transfer_pick_lines pl
                WHERE pl.pick_id = p.pick_id AND pl.status <> 'Cancelled') AS line_count
        FROM dbo.transfer_picks p WHERE p.pick_id = %s
      """,
      (pick_id,)
    )
    row = cursor.fetchone()
    if not row:
      raise Exception("Pick %s not found" % pick_id)
    if not row['assigned_worker_code']:
      raise Exception("Assign a worker before releasing")
    if row['line_count'] == 0:
      raise Exception("Pick has no lines")

    if not _guarded_pick_status_update(conn, pick_id, ('Draft',), 'Released',
                                       extra_set_sql=", released_date = SYSDATETIME()"):
      conn.rollback()
      raise Exception("Pick %s is not Draft - refresh" % pick_id)

    _log_event(conn, pick_id, 'RELEASED', performed_by=performed_by)
    conn.commit()
    return True
  finally:
    conn.close()


@anvil.server.callable
def cancel_pick(pick_id, performed_by='Hub Manager'):
  """
    Cancel a Draft/Released/In_Progress pick with NO picked/short lines
    (resolve or undo those first - C5 keeps cancel semantics explicit).
    Assigned lines are cancelled; recs with no remaining active lines
    revert Applied -> Approved (back to pool).
    """
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      """
        SELECT COUNT(*) AS touched
        FROM dbo.transfer_pick_lines
        WHERE pick_id = %s AND status IN ('Picked', 'Short')
      """,
      (pick_id,)
    )
    if cursor.fetchone()['touched'] > 0:
      raise Exception("Pick has recorded lines - undo them first or complete the pick")

    if not _guarded_pick_status_update(conn, pick_id,
                                       ('Draft', 'Released', 'In_Progress'),
                                       'Cancelled',
                                       extra_set_sql=", cancelled_date = SYSDATETIME()"):
      conn.rollback()
      raise Exception("Pick %s is not cancellable" % pick_id)

    # Collect affected recs BEFORE cancelling their lines.
    cursor.execute(
      """
        SELECT DISTINCT recommendation_id, sku FROM dbo.transfer_pick_lines
        WHERE pick_id = %s AND status = 'Assigned'
      """,
      (pick_id,)
    )
    affected = list(cursor.fetchall())

    cursor.execute(
      "UPDATE dbo.transfer_pick_lines SET status = 'Cancelled' "
      "WHERE pick_id = %s AND status = 'Assigned'",
      (pick_id,)
    )

    for rec in affected:
      cursor.execute(
        """
          UPDATE dbo.ai_transfer_recommendations
          SET status = 'Approved'
          WHERE recommendation_id = %s
            AND status = 'Applied'
            AND NOT EXISTS (SELECT 1 FROM dbo.transfer_pick_lines pl
                            WHERE pl.recommendation_id = %s
                              AND pl.status IN ('Assigned', 'Picked', 'Short'))
            AND NOT EXISTS (SELECT 1 FROM dbo.transfer_trip_assignments tta
                            WHERE tta.recommendation_id = %s)
        """,
        (rec['recommendation_id'], rec['recommendation_id'], rec['recommendation_id'])
      )
      if cursor.rowcount == 1:
        _log_agent_action(conn, 'PICK_CANCELLED', rec['sku'],
                          'Applied', 'Approved',
                          "Pick %s cancelled" % pick_id)

    _log_event(conn, pick_id, 'CANCELLED', performed_by=performed_by)
    conn.commit()
    return True
  finally:
    conn.close()


@anvil.server.callable
def reopen_pick(pick_id, performed_by='Hub Manager'):
  """
    C7: Completed -> In_Progress (manager/lead only, enforced by the
    form), blocked once ANY line rides a Finalized/Delivered trip
    assignment.
    """
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      """
        SELECT COUNT(*) AS shipped
        FROM dbo.transfer_pick_lines pl
        JOIN dbo.transfer_trip_assignments tta ON tta.pick_line_id = pl.pick_line_id
        WHERE pl.pick_id = %s AND tta.status IN ('Finalized', 'Delivered')
      """,
      (pick_id,)
    )
    if cursor.fetchone()['shipped'] > 0:
      raise Exception("A line of this pick is on a Finalized trip - reopening is blocked (C7)")

    if not _guarded_pick_status_update(conn, pick_id, ('Completed',), 'In_Progress',
                                       extra_set_sql=", completed_date = NULL"):
      conn.rollback()
      raise Exception("Pick %s is not Completed" % pick_id)

    _log_event(conn, pick_id, 'REOPENED', performed_by=performed_by)
    conn.commit()
    return True
  finally:
    conn.close()


# =============================================================================
# WORKER (KIOSK) CALLABLES
# =============================================================================

@anvil.server.callable
def remove_from_pool(recommendation_id, performed_by='Hub Manager', reason=None):
  """Remove an erroneously-approved rec from the unassigned pool.

  Reverts Approved / Auto-Approved / Applied back to 'Pending', so the SKU
  returns to the Transfer Form for a fresh decision. (To make it stay gone,
  Reject it there -- the rejection-cooldown and snooze machinery live on
  that form, not here.)

  Guards:
    - rec must currently be in a pool status;
    - rec must have NO active pick lines (remove lines from picks first --
      remove_line already reverts allocation cleanly);
    - status predicate re-asserted in the UPDATE (no race clobber).
  Audited: note appended on the rec + ai_agent_log 'POOL_REMOVED' row.
  """
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      """
        SELECT r.recommendation_id, r.sku, r.status,
               CAST(""" + _EFF_QTY_SQL + """ AS DECIMAL(18,4)) AS eff_qty,
               (SELECT COUNT(*) FROM dbo.transfer_pick_lines pl
                WHERE pl.recommendation_id = r.recommendation_id
                  AND pl.status IN ('Assigned', 'Picked', 'Short')) AS active_lines
        FROM dbo.ai_transfer_recommendations r
        WHERE r.recommendation_id = %s
      """,
      (recommendation_id,)
    )
    rec = cursor.fetchone()
    if not rec:
      return {'status': 'error', 'message': 'Rec %s not found.' % recommendation_id}
    if rec['status'] not in ('Approved', 'Auto-Approved', 'Applied'):
      return {'status': 'error',
              'message': 'Rec %s is %s - not in the pool.'
              % (recommendation_id, rec['status'])}
    if rec['active_lines'] > 0:
      return {'status': 'error',
              'message': ('%s has %d active pick line(s). Remove the line(s) '
                          'from the pick first, then remove from pool.'
                          % (rec['sku'], rec['active_lines']))}

    stamp = datetime.now().strftime('%Y-%m-%d %H:%M')
    note = ('[' + stamp + '] POOL REMOVE by ' + str(performed_by) +
            ': approval undone, returned to Pending for re-review.' +
            ((' Reason: ' + reason) if reason else '') +
            ' Prev status: ' + rec['status'])
    cursor.execute(
      """
        UPDATE dbo.ai_transfer_recommendations
        SET status = 'Pending',
            notes = ISNULL(NULLIF(CAST(notes AS nvarchar(max)), '') + CHAR(10), '') + %s
        WHERE recommendation_id = %s
          AND status IN ('Approved', 'Auto-Approved', 'Applied')
      """,
      (note, recommendation_id)
    )
    if cursor.rowcount == 0:
      conn.rollback()
      return {'status': 'error',
              'message': 'Rec %s changed state mid-action; refresh and retry.'
              % recommendation_id}
    cursor.execute(
      """
        INSERT INTO dbo.ai_agent_log
          (action_type, sku, old_value, new_value, reason, action_timestamp)
        VALUES ('POOL_REMOVED', %s, %s, 'Pending', %s, GETDATE())
      """,
      (rec['sku'], rec['status'],
       'pool remove by ' + str(performed_by) + ' (rec ' +
       str(recommendation_id) + ')' + ((': ' + reason) if reason else ''))
    )
    conn.commit()
    return {'status': 'success', 'sku': rec['sku'],
            'message': rec['sku'] + ' removed from pool and returned to Pending.'}
  except Exception as e:
    try:
      conn.rollback()
    except Exception:
      pass
    return {'status': 'error', 'message': str(e)}
  finally:
    conn.close()


@anvil.server.callable
def get_workers():
  """Active workers for the tap-your-name screen."""
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      """
        SELECT worker_code, display_name, is_lead
        FROM dbo.hub_workers
        WHERE is_active = 1
        ORDER BY COALESCE(sort_order, 999), display_name
      """
    )
    return [serialize_row(row) for row in cursor.fetchall()]
  finally:
    conn.close()


@anvil.server.callable
def get_discrepancy_codes():
  """Active short-pick reason codes for the kiosk's reason buttons."""
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      """
        SELECT discrepancy_code, description
        FROM dbo.transfer_pick_discrepancy_codes
        WHERE is_active = 1
        ORDER BY sort_order
      """
    )
    return [serialize_row(row) for row in cursor.fetchall()]
  finally:
    conn.close()


@anvil.server.callable
def get_my_picks(worker_code):
  """Released/In_Progress picks for this worker; a lead sees all (D1)."""
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      "SELECT is_lead FROM dbo.hub_workers WHERE worker_code = %s AND is_active = 1",
      (worker_code,)
    )
    row = cursor.fetchone()
    if not row:
      raise Exception("Worker %s not found or inactive" % worker_code)
    is_lead = bool(row['is_lead'])

    worker_filter = "" if is_lead else "AND p.assigned_worker_code = %s"
    params = () if is_lead else (worker_code,)
    cursor.execute(
      """
        SELECT p.pick_id, p.status, p.assigned_worker_code,
               w.display_name AS worker_name, p.released_date,
               COUNT(pl.pick_line_id) AS line_count,
               SUM(CASE WHEN pl.status IN ('Picked', 'Short') THEN 1 ELSE 0 END) AS resolved_count,
               (SELECT MIN(CASE r.base_urgency WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2
                                               WHEN 'MEDIUM' THEN 3 ELSE 4 END)
                FROM dbo.transfer_pick_lines pl2
                JOIN dbo.ai_transfer_recommendations r
                  ON r.recommendation_id = pl2.recommendation_id
                WHERE pl2.pick_id = p.pick_id
                  AND pl2.status <> 'Cancelled') AS urgency_rank
        FROM dbo.transfer_picks p
        LEFT JOIN dbo.hub_workers w ON w.worker_code = p.assigned_worker_code
        LEFT JOIN dbo.transfer_pick_lines pl
               ON pl.pick_id = p.pick_id AND pl.status <> 'Cancelled'
        WHERE p.status IN ('Released', 'In_Progress') """ + worker_filter + """
        GROUP BY p.pick_id, p.status, p.assigned_worker_code, w.display_name,
                 p.released_date
        ORDER BY p.released_date
      """,
      params
    )
    return [serialize_row(row) for row in cursor.fetchall()]
  finally:
    conn.close()


@anvil.server.callable
def record_pick_line(pick_line_id, worker_code, qty_picked,
                     discrepancy_code=None, discrepancy_note=None):
  """
    The tap. Saves one line immediately (walk-away safe, D3).
      qty == planned            -> Picked
      qty <  planned            -> Short (discrepancy_code required)
      planned < qty <= cap      -> Picked + LINE_OVER event (C4)
      qty > cap                 -> rejected
    State-guarded on status='Assigned' - double-tap safe. First touch
    flips the pick Released -> In_Progress.
    """
  qty_picked = float(qty_picked)
  if qty_picked < 0:
    raise Exception("Qty cannot be negative")
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      """
        SELECT pl.pick_id, pl.sku,
               CAST(pl.qty_planned AS DECIMAL(18,4)) AS qty_planned,
               p.status AS pick_status
        FROM dbo.transfer_pick_lines pl
        JOIN dbo.transfer_picks p ON p.pick_id = pl.pick_id
        WHERE pl.pick_line_id = %s
      """,
      (pick_line_id,)
    )
    row = cursor.fetchone()
    if not row:
      raise Exception("Line %s not found" % pick_line_id)
    if row['pick_status'] not in ('Released', 'In_Progress'):
      raise Exception("Pick is %s - lines cannot be recorded" % row['pick_status'])

    planned = float(row['qty_planned'])
    over_pct = _get_param(conn, 'pick_overpick_cap_pct', 5)
    cap = planned * (1.0 + over_pct / 100.0)

    is_over = qty_picked > planned + 1e-9
    if is_over and qty_picked > cap + 1e-9:
      raise Exception("Over-pick cap is %.0f%% (max %.2f for this line)"
                      % (over_pct, cap))

    if qty_picked < planned - 1e-9:
      new_status = 'Short'
      if not discrepancy_code:
        raise Exception("Short pick needs a reason (discrepancy code)")
    else:
      new_status = 'Picked'
      discrepancy_code = discrepancy_code if is_over else None

    # v1.3.0 (H8): re-assert the PICK status inside the write, same shape
    # as undo_line - a line can no longer be recorded onto a pick another
    # worker completed a moment ago.
    cursor.execute(
      """
        UPDATE pl
        SET pl.qty_picked = %s, pl.status = %s, pl.discrepancy_code = %s,
            pl.discrepancy_note = %s, pl.picked_by = %s, pl.picked_date = SYSDATETIME()
        FROM dbo.transfer_pick_lines pl
        JOIN dbo.transfer_picks p ON p.pick_id = pl.pick_id
        WHERE pl.pick_line_id = %s AND pl.status = 'Assigned'
          AND p.status IN ('Released', 'In_Progress')
      """,
      (qty_picked, new_status, discrepancy_code, discrepancy_note,
       worker_code, pick_line_id)
    )
    if cursor.rowcount != 1:
      conn.rollback()
      raise Exception("Line already recorded, or the pick was just completed "
                      "- refresh")

    # First touch: Released -> In_Progress (no-op after the first line).
    _guarded_pick_status_update(conn, row['pick_id'], ('Released',), 'In_Progress')

    event = 'LINE_SHORT' if new_status == 'Short' else \
    ('LINE_OVER' if is_over else 'LINE_PICKED')
    _log_event(conn, row['pick_id'], event, pick_line_id=pick_line_id,
               performed_by=worker_code, qty=qty_picked,
               note=(discrepancy_code or None))
    conn.commit()
    return {'status': new_status, 'is_over': is_over}
  finally:
    conn.close()


@anvil.server.callable
def undo_line(pick_line_id, worker_code):
  """Undo a recorded line while the pick is not Completed."""
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      """
        UPDATE pl
        SET pl.qty_picked = NULL, pl.status = 'Assigned',
            pl.discrepancy_code = NULL, pl.discrepancy_note = NULL,
            pl.picked_by = NULL, pl.picked_date = NULL
        FROM dbo.transfer_pick_lines pl
        JOIN dbo.transfer_picks p ON p.pick_id = pl.pick_id
        WHERE pl.pick_line_id = %s
          AND pl.status IN ('Picked', 'Short')
          AND p.status IN ('Released', 'In_Progress')
      """,
      (pick_line_id,)
    )
    if cursor.rowcount != 1:
      conn.rollback()
      raise Exception("Line cannot be undone (pick completed, or line not recorded)")

    cursor.execute(
      "SELECT pick_id FROM dbo.transfer_pick_lines WHERE pick_line_id = %s",
      (pick_line_id,)
    )
    pick_id = cursor.fetchone()['pick_id']
    _log_event(conn, pick_id, 'LINE_UNDO', pick_line_id=pick_line_id,
               performed_by=worker_code)
    conn.commit()
    return True
  finally:
    conn.close()


@anvil.server.callable
def complete_pick(pick_id, worker_code):
  """
    Completes a pick once every line is resolved. C2: for each rec with a
    short, the approved qty is reduced so the rec closes short - override
    is set to (picked total + qty still Assigned on OTHER picks), never
    below what other picks already hold. E2: one batched SMS to Hub
    Managers when the pick has shorts or over-picks.
    """
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      """
        SELECT SUM(CASE WHEN status = 'Assigned' THEN 1 ELSE 0 END) AS unresolved,
               SUM(CASE WHEN status = 'Short' THEN 1 ELSE 0 END) AS shorts
        FROM dbo.transfer_pick_lines
        WHERE pick_id = %s AND status <> 'Cancelled'
      """,
      (pick_id,)
    )
    counts = cursor.fetchone()
    if (counts['unresolved'] or 0) > 0:
      raise Exception("%s line(s) still unresolved" % counts['unresolved'])

    if not _guarded_pick_status_update(conn, pick_id,
                                       ('Released', 'In_Progress'), 'Completed',
                                       extra_set_sql=", completed_date = SYSDATETIME()"):
      conn.rollback()
      raise Exception("Pick %s is not open - refresh" % pick_id)

    # ---- C2: close-short per affected rec ----
    cursor.execute(
      """
        SELECT DISTINCT pl.recommendation_id, pl.sku
        FROM dbo.transfer_pick_lines pl
        WHERE pl.pick_id = %s AND pl.status = 'Short'
      """,
      (pick_id,)
    )
    short_recs = list(cursor.fetchall())

    for rec in short_recs:
      rec_id = rec['recommendation_id']
      # New approved qty = everything physically picked (any pick) +
      # everything still promised to open picks. Guarded to only ever
      # reduce, and mirrored into notes + ai_agent_log.
      cursor.execute(
        """
          UPDATE r
          SET r.[manager_override_qty ] = x.new_qty,
              r.notes = CONCAT(COALESCE(r.notes, ''),
                               CHAR(10), '[', CONVERT(VARCHAR(16), GETDATE(), 120),
                               '] CLOSED SHORT by pick ', %s,
                               ': approved qty reduced to ', CAST(x.new_qty AS VARCHAR(20)))
          FROM dbo.ai_transfer_recommendations r
          CROSS APPLY (SELECT COALESCE(SUM(CASE WHEN pl.status IN ('Picked', 'Short')
                                                THEN pl.qty_picked ELSE 0 END), 0)
                            + COALESCE(SUM(CASE WHEN pl.status = 'Assigned'
                                                THEN pl.qty_planned ELSE 0 END), 0) AS new_qty
                       FROM dbo.transfer_pick_lines pl
                       WHERE pl.recommendation_id = r.recommendation_id
                         AND pl.status IN ('Assigned', 'Picked', 'Short')) x
          WHERE r.recommendation_id = %s
            AND x.new_qty < CAST(""" + _EFF_QTY_SQL + """ AS DECIMAL(18,4))
        """,
        (pick_id, rec_id)
      )
      if cursor.rowcount == 1:
        _log_agent_action(conn, 'PICK_SHORT', rec['sku'],
                          'Applied', 'Applied',
                          "Rec %s closed short by pick %s (C2)" % (rec_id, pick_id))

    # ---- v1.3.0 (C7): the zero-pick rule, in code ----
    # A rec whose physical pick total is 0 across ALL its lines (nothing
    # pulled anywhere, no open Assigned line, no trip assignment) used to
    # freeze at Applied / qty 0 where no screen could touch it and the
    # detector re-raised the same SKU within the hour. Standing rule
    # (Dave 2026-08-31, re-confirmed 2026-09-05): Cancelled with a note;
    # the detector re-recommends if Starlite still needs it.
    zero_pick_cancelled = []
    for rec in short_recs:
      rec_id = rec['recommendation_id']
      cursor.execute(
        """
          UPDATE r
          SET r.status = 'Cancelled',
              r.reviewed_date = GETDATE(),
              r.notes = CONCAT(COALESCE(r.notes, ''),
                               CHAR(10), '[', CONVERT(VARCHAR(16), GETDATE(), 120),
                               '] ZERO PICK: pick ', %s, ' completed with qty 0 (',
                               COALESCE(x.code, 'no code'), '). Cancelled per zero-pick rule; detector re-recommends if still needed.')
          FROM dbo.ai_transfer_recommendations r
          CROSS APPLY (SELECT TOP 1 pl.discrepancy_code AS code
                       FROM dbo.transfer_pick_lines pl
                       WHERE pl.recommendation_id = r.recommendation_id
                         AND pl.pick_id = %s
                       ORDER BY pl.pick_line_id DESC) x
          WHERE r.recommendation_id = %s
            AND r.status = 'Applied'
            AND NOT EXISTS (SELECT 1 FROM dbo.transfer_pick_lines pl2
                            WHERE pl2.recommendation_id = r.recommendation_id
                              AND pl2.status IN ('Assigned', 'Picked', 'Short')
                              AND (pl2.status = 'Assigned'
                                   OR COALESCE(pl2.qty_picked, 0) > 0))
            AND NOT EXISTS (SELECT 1 FROM dbo.transfer_trip_assignments t
                            WHERE t.recommendation_id = r.recommendation_id)
        """,
        (pick_id, pick_id, rec_id)
      )
      if cursor.rowcount == 1:
        zero_pick_cancelled.append(rec_id)
        cursor.execute(
          """
            INSERT INTO dbo.transfer_pick_events
                (pick_id, pick_line_id, event_type, performed_by, qty, note)
            SELECT pl.pick_id, pl.pick_line_id, 'ZERO_PICK_REC_CANCELLED', %s, 0,
                   CONCAT('Zero pick: rec ', pl.recommendation_id,
                          ' cancelled (line completed with qty 0; rule: zero-picked + rec unresolved -> Cancelled). Detector will re-recommend if still needed.')
            FROM dbo.transfer_pick_lines pl
            WHERE pl.pick_id = %s AND pl.recommendation_id = %s
          """,
          (worker_code, pick_id, rec_id)
        )
        _log_agent_action(conn, 'ZERO_PICK_REC_CANCELLED', rec['sku'],
                          'Applied (qty 0 after pick %s)' % pick_id, 'Cancelled',
                          "Rec %s: zero-pick rule applied at pick completion (v1.3.0)"
                          % rec_id)

    _log_event(conn, pick_id, 'COMPLETED', performed_by=worker_code)
    conn.commit()

    # ---- E2: batched exception SMS (post-commit; failure never blocks) ----
    sms_result = {'sent': 0}
    try:
      sms_result = _notify_pick_exceptions(conn, pick_id)
    except Exception as e:
      print("[pick_dashboard] exception SMS failed: %s" % e)

    # ---- E3 (v2.1): short picks -> cycle-count SMS to Inventory Control ----
    # Short picks mean the system said stock was there and the shelf said
    # otherwise -- an inventory-accuracy alarm, not just a pick problem.
    # Post-commit like E2; an SMS failure never blocks completion.
    try:
      _notify_short_pick_cycle_count(conn, pick_id)
    except Exception as e:
      print("[pick_dashboard] cycle-count SMS failed: %s" % e)

    return {'completed': True, 'shorts': len(short_recs),
            'zero_pick_cancelled': len(zero_pick_cancelled),
            'sms_sent': sms_result.get('sent', 0)}
  finally:
    conn.close()


@anvil.server.callable
def download_pick_slip_for_pick(pick_id):
  """
    Per-pick Picking Slip (E1: keep printing during transition, same
    layout the floor knows). Reuses pick_list_pdf_generator with the
    v5.27 qty-override path so line quantities are the PICK's
    qty_planned slices, not the recs' full approved quantities, and the
    banner/filename carry PICK-<id> instead of a review batch id.
    Returns the same structured dict as download_pick_list_pdf.
    """
  from .pick_list_pdf_generator import generate_pick_list_pdf
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    # v1.2.0 slip guard: a Completed pick's goods are STAGED for the
    # dock - their only legitimate paper is the truck manifest, which
    # exists only after the dock finalizes a truck. Printing a slip
    # here is how goods used to leave without a trip (Aug 11-24
    # orphan incident). Open picks still print.
    cursor.execute(
      "SELECT status FROM dbo.transfer_picks WHERE pick_id = %s",
      (pick_id,)
    )
    _p = cursor.fetchone()
    if not _p:
      return {'status': 'error', 'message': 'Pick %s not found.' % pick_id}
    if _p['status'] == 'Completed':
      return {'status': 'error',
              'message': ('Pick %s is completed - its goods are staged for '
                          'the dock. No slip prints after completion; the '
                          'truck manifest (printed at the dock when the '
                          'truck is finalized) is the shipping paper.'
                          % pick_id)}
    cursor.execute(
      """
        SELECT pl.recommendation_id,
               CAST(pl.qty_planned AS DECIMAL(18,4)) AS qty_planned
        FROM dbo.transfer_pick_lines pl
        WHERE pl.pick_id = %s AND pl.status IN ('Assigned', 'Picked', 'Short')
      """,
      (pick_id,)
    )
    lines = list(cursor.fetchall())
  finally:
    conn.close()

  if not lines:
    return {'status': 'error', 'message': 'Pick %s has no active lines.' % pick_id}

  rec_ids = [l['recommendation_id'] for l in lines]
  overrides = {l['recommendation_id']: float(l['qty_planned']) for l in lines}
  try:
    pdf_media = generate_pick_list_pdf(rec_ids, qty_overrides=overrides,
                                       batch_label='PICK-%s' % pick_id)
  except TypeError:
    return {'status': 'error',
            'message': 'pick_list_pdf_generator predates v5.27 - run the '
            'Phase 4 splice (phase4_patch_pdf.py) first.'}
  if pdf_media is None:
    return {'status': 'error', 'message': 'No items to include on the slip.'}
  return {'status': 'success', 'pdf_media': pdf_media, 'filename': pdf_media.name}


def _notify_pick_exceptions(conn, pick_id):
  """One summary SMS to Hub Managers when a completed pick has shorts/overs."""
  cursor = conn.cursor()
  cursor.execute(
    """
      SELECT pl.sku, pl.status, pl.discrepancy_code,
             CAST(pl.qty_planned AS DECIMAL(18,4)) AS qty_planned,
             CAST(pl.qty_picked AS DECIMAL(18,4)) AS qty_picked
      FROM dbo.transfer_pick_lines pl
      WHERE pl.pick_id = %s
        AND (pl.status = 'Short' OR (pl.status = 'Picked' AND pl.qty_picked > pl.qty_planned))
    """,
    (pick_id,)
  )
  exceptions = list(cursor.fetchall())
  if not exceptions or not SMSConfig.is_sms_enabled():
    return {'sent': 0}

  managers = get_hub_manager_phones(conn, 'PICK_EXCEPTIONS') \
  or get_hub_manager_phones(conn, 'NEW_RECOMMENDATIONS')
  if not managers:
    return {'sent': 0}

  lines = []
  for e in exceptions[:6]:
    tag = 'SHORT' if e['status'] == 'Short' else 'OVER'
    lines.append("  %s %s: %g/%g%s" % (
      tag, e['sku'], float(e['qty_picked'] or 0), float(e['qty_planned']),
      (' (' + e['discrepancy_code'] + ')') if e['discrepancy_code'] else ''))
  if len(exceptions) > 6:
    lines.append("  + %d more" % (len(exceptions) - 6))
  message = ("Pick %s completed with %d exception(s):\n" % (pick_id, len(exceptions))
             + "\n".join(lines))

  sent = 0
  client = TwilioSMSClient()
  for m in managers:
    try:
      result = client.send_sms(m['phone'], message)
      log_sms_notification(conn, result, 'PICK_EXCEPTIONS',
                           related_skus=[e['sku'] for e in exceptions])
      if result.get('success'):
        sent += 1
    except Exception as e:
      print("[pick_dashboard] SMS to %s failed: %s" % (m.get('name'), e))
  return {'sent': sent}


def _notify_short_pick_cycle_count(conn, pick_id):
  """E3 (v2.1): when a completed pick has SHORT lines, SMS Inventory Control
  that a cycle count is required immediately for the shorted SKUs.

  Recipients: sms_notification_contacts rows whose notification_types
  contains SHORT_PICK_CYCLE_COUNT (role-agnostic lookup). If no contact is
  configured yet, this quietly does nothing -- add an Inventory_Control row
  to enable it. Separate from the E2 Hub-Manager exception summary on
  purpose: different audience, different action (count the shelf NOW).
  """
  cursor = conn.cursor()
  cursor.execute(
    """
      SELECT pl.sku, pl.discrepancy_code,
             CAST(pl.qty_planned AS DECIMAL(18,4)) AS qty_planned,
             CAST(pl.qty_picked AS DECIMAL(18,4)) AS qty_picked
      FROM dbo.transfer_pick_lines pl
      WHERE pl.pick_id = %s AND pl.status = 'Short'
      ORDER BY pl.sku
    """,
    (pick_id,)
  )
  shorts = list(cursor.fetchall())
  if not shorts or not SMSConfig.is_sms_enabled():
    return {'sent': 0}

  contacts = get_hub_manager_phones(conn, 'SHORT_PICK_CYCLE_COUNT')
  if not contacts:
    print("[pick_dashboard] short picks on pick %s but no "
          "SHORT_PICK_CYCLE_COUNT contact configured" % pick_id)
    return {'sent': 0}

  lines = []
  for s in shorts[:6]:
    lines.append("  %s: picked %g of %g%s" % (
      s['sku'], float(s['qty_picked'] or 0), float(s['qty_planned']),
      (' (' + s['discrepancy_code'] + ')') if s['discrepancy_code'] else ''))
  if len(shorts) > 6:
    lines.append("  + %d more" % (len(shorts) - 6))

  message = ("SHORT PICKS - CYCLE COUNT NEEDED\n"
             "Pick %s closed with %d short line(s). Please cycle count "
             "these Hub SKUs immediately:\n%s"
             % (pick_id, len(shorts), "\n".join(lines)))

  sent = 0
  client = TwilioSMSClient()
  for c in contacts:
    try:
      result = client.send_sms(c['phone'], message)
      log_sms_notification(conn, result, 'SHORT_PICK_CYCLE_COUNT',
                           related_skus=[s['sku'] for s in shorts])
      if result.get('success'):
        sent += 1
    except Exception as e:
      print("[pick_dashboard] cycle-count SMS to %s failed: %s"
            % (c.get('name'), e))
  return {'sent': sent}