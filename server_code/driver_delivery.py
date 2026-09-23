"""
Driver Delivery - Anvil Server Module (v5.8)
=============================================
Option C "Driver Marks Delivered" — mobile form that lets the truck
driver confirm physical delivery of a finalized trip at Starlite.

What this module does, at a glance:

    1. Driver opens the Anvil app on mobile, taps "Confirm Delivery".
    2. list_trips_awaiting_delivery() shows every DEPARTED trip
       (shipped_date set) that has not yet been delivered
       (transfer_trips.received_date IS NULL).
    3. Driver picks a trip → get_trip_assignments_for_delivery(trip_id)
       returns the per-SKU lines with planned qty pre-populated.
    4. Driver adjusts qty / adds a note where needed → tap Submit.
    5. submit_delivery_confirmation(trip_id, lines, driver_name) runs a
       single transactional cascade:
         - transfer_trips.received_date   := GETDATE()
         - transfer_trip_assignments      := Delivered + per-SKU detail
         - ai_transfer_recommendations    := Delivered (via
           FK_transfer_trip_recommendations, scoped by trip_id)
    6. ai_agent_log gets a TRIP_DELIVERED row with the cascade counts.
    7. If any line is a discrepancy vs. planned, sms_notifications.py
       fires a DELIVERY_DISCREPANCY SMS to the Hub Manager AFTER the
       DB commit (so a Twilio outage cannot roll back a valid delivery).

State machine this closes (v5.7 established Finalized; v5.8 adds Delivered):

    Pending -> Approved/Rejected/Paused -> Applied -> Trip_Assigned
        -> Finalized (cascade on Finalize Batch, v5.7)
        -> Delivered (cascade on driver Submit,   v5.8 — THIS MODULE)

    Trip side (v5.30): Finalized -(dock DEPART)-> Departed
        -(driver Submit)-> Delivered. Only DEPARTED trips are deliverable.

"Received" is intentionally reserved for a future e21-integrated
Starlite receiving step. Do NOT use it here.

Author: Dave Whritenour / Claude
Created: 2026-04-22
"""

# =============================================================================
# Version:       5.8.4
# Last updated:  2026-04-29
# Changelog (this module only):
#   5.8.4 (2026-04-29) * BUG FIX: Hub Manager qty edits made in
#                        trip_editor (txt_qty -> update_assignment_qty
#                        writes to transfer_trip_assignments.adjusted_qty)
#                        were silently ignored by driver_deliver because
#                        get_trip_assignments_for_delivery never selected
#                        adjusted_qty and _planned_qty's COALESCE ladder
#                        only considered the recommendation-table fields.
#                        Result: driver UI displayed the original AI/
#                        calculated qty, not the manager's adjusted qty.
#                        Fix:
#                          - Added tta.adjusted_qty to the SELECT.
#                          - Inserted adjusted_qty as the highest-priority
#                            entry in _planned_qty()'s ladder.
#                        Companion fix in pick_list_pdf_generator.py v5.8.4.
#   5.8.3 (2026-04-28) * Cascade rec.status from BOTH 'Finalized' AND
#                        'Trip_Assigned' to 'Delivered'. Was previously
#                        'Finalized' only, which left pre-v5.7 recs
#                        orphaned in Trip_Assigned after delivery,
#                        padding starlite_addition in the v5.6
#                        PendingTransferQty CTE and silently suppressing
#                        fresh transfer recommendations across 113 SKUs.
#                        Paired with one-time historical cleanup in
#                        sql/cleanup_stale_trip_assigned_recs.sql.
#   5.8.2 (2026-04-23) * Two-text SMS model: DELIVERY_RECEIPT now fires
#                        on every successful delivery (new
#                        send_delivery_receipt_notification in
#                        sms_notifications.py); DELIVERY_DISCREPANCY
#                        still fires in addition when one or more lines
#                        diverge from plan.
#                      * Response envelope adds receipt_sms_sent and
#                        discrepancy_sms_sent fields; rolled-up into a
#                        back-compat sms_sent for older clients.
#   5.8.1 (2026-04-23) * Restored truncated tail of
#                        submit_delivery_confirmation (missing closing
#                        brace, except block, and finally conn.close()).
#                      * Added EXISTS filter to
#                        list_trips_awaiting_delivery so trips with zero
#                        transfer_trip_assignments rows (orphan finalized
#                        trips) cannot reach the driver UI.
#                      * Synced response envelope with client/driver_deliver:
#                        get_trip_assignments_for_delivery returns
#                        {trip, lines} envelope; submit payload uses 'note'
#                        not 'delivery_note'; client reads
#                        'assignments_delivered' on success.
#   5.8.0 (2026-04-22) Initial release of Driver Marks Delivered (Option C).
#                      Single-form UI with three internal views (home /
#                      confirm / success). Server orchestrates the
#                      transfer_trips -> tta -> recs cascade transactionally
#                      and fires DELIVERY_DISCREPANCY SMS on variance.
# =============================================================================

__version__       = "5.30.0"
__version_date__  = "2026-09-05"
__version_notes__ = (
  "5.30.0 (Transfer Logic Review 2026-09-04, findings C2/C6/D4/M9/M10): "
  "(1) rec->Delivered cascade now includes 'Applied' - a rec that reached "
  "the truck at Applied was left crediting phantom Starlite supply forever "
  "(recs 3010/3037/3319, 261 units); "
  "(2) the driver queue and both submit guards require the truck to have "
  "DEPARTED (shipped_date set, status Finalized/Departed) - previously a "
  "truck could be 'delivered' while still being loaded on the dock; "
  "(3) submit stamps transfer_trips.status='Delivered' (status was never "
  "advanced past Finalized on any of 533 trips); "
  "(4) tta.loaded_qty heads the planned-qty ladder so a dock short load is "
  "what the driver sees and what the discrepancy check compares against; "
  "(5) submit refuses (rolls back, no received_date) unless every "
  "assignment on the trip was confirmed - a dropped line can no longer "
  "leave the driver queue as Finalized with its rec Delivered; "
  "(6) TRIP_DELIVERED audit row now carries action_timestamp. "
  "Prior: 5.29.0 (Phase 7) fail-closed delivery auth; adjusted_qty/qty in "
  "the planned-qty ladder."
)


import anvil.server
import anvil.secrets
import anvil.users
from datetime import datetime, date
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .db_config import get_nexus_connection as get_db_connection

# SMS is a best-effort post-commit side effect. Import lazily inside the
# function so a syntax error in sms_notifications.py cannot prevent the
# driver from submitting a delivery.


# =============================================================================
# AUTH — Phase 1, mirrors admin_check.py conventions
# =============================================================================

def _get_allowlist(secret_name: str) -> set:
  """Parse a comma-separated Anvil Secret into a lowercase set of emails."""
  try:
    raw = anvil.secrets.get_secret(secret_name) or ''
  except Exception:
    raw = ''
  return {e.strip().lower() for e in raw.split(',') if e.strip()}


def _current_user_email() -> Optional[str]:
  """Best-effort: return the current Anvil user's email, or None."""
  try:
    user = anvil.users.get_user()
    if user and 'email' in user:
      return (user['email'] or '').strip().lower()
  except Exception:
    pass
  return None


def _current_user_display_name() -> str:
  """
  Best-effort display name for audit fields. Anvil's Users service
  typically exposes 'name' or 'email' — fall back gracefully.
  """
  try:
    user = anvil.users.get_user()
    if user:
      for key in ('name', 'full_name', 'display_name', 'email'):
        if key in user and user[key]:
          return str(user[key])
  except Exception:
    pass
  return 'driver'


def _is_valid_worker(worker_code: Optional[str]) -> Optional[str]:
  """Return the display name for an ACTIVE dbo.hub_workers row, else
  None. The kiosk-identity path added in v5.29 (Phase 7)."""
  if not worker_code:
    return None
  try:
    conn = get_db_connection()
    try:
      cursor = conn.cursor()
      cursor.execute(
        "SELECT display_name FROM dbo.hub_workers "
        "WHERE worker_code = %s AND is_active = 1",
        (worker_code,))
      row = cursor.fetchone()
      return row['display_name'] if row else None
    finally:
      conn.close()
  except Exception:
    return None


def _is_authorized_for_delivery(worker_code: Optional[str] = None) -> bool:
  """v5.29 (Phase 7): FAIL CLOSED.

  A delivery caller must present at least one real identity:
    - a logged-in Anvil Users session (email must be on an allowlist
      when any allowlist is configured), OR
    - a valid, active hub_workers worker_code (kiosk pattern - same
      identities the pick stage uses).

  The old permissive fallbacks (no allowlists -> allow; no session ->
  allow) are gone: they let anyone with the URL confirm deliveries
  anonymously, with delivered_by = 'driver'.
  """
  email = _current_user_email()
  if email is not None:
    drivers  = _get_allowlist('DRIVER_EMAILS')
    managers = _get_allowlist('HUB_MANAGER_EMAILS')
    admins   = _get_allowlist('FORECAST_ADMIN_EMAILS')
    all_allowed = drivers | managers | admins
    # Allowlists configured -> enforce membership. None configured ->
    # any authenticated session is acceptable.
    return (email in all_allowed) if all_allowed else True
  return _is_valid_worker(worker_code) is not None


def _require_delivery_auth(worker_code: Optional[str] = None):
  """
  Raise anvil.server.PermissionDenied unless the caller presents a
  valid identity (Users session or hub_workers code). Call at the top
  of every @anvil.server.callable in this module.
  """
  if not _is_authorized_for_delivery(worker_code):
    raise anvil.server.PermissionDenied(
      'Delivery needs an identity: tap your name on the delivery '
      'screen (or log in). Contact your Hub Manager if you need access.'
    )


# =============================================================================
# HELPERS
# =============================================================================

def _serialize(value):
  """Make DB row values JSON-safe for Anvil client transport."""
  if value is None:
    return None
  if isinstance(value, (datetime, date)):
    return value.isoformat()
  if isinstance(value, Decimal):
    return float(value)
  if isinstance(value, bytes):
    return value.decode('utf-8', errors='replace')
  return value


def _planned_qty(row: Dict[str, Any]) -> float:
  """
  Standard COALESCE ladder for "how much was supposed to be on this
  trip for this SKU".

  Priority:
    0. transfer_trip_assignments.loaded_qty   (v5.30: the dock's LOAD /
       SHORT entry - physical truth; NULL for pre-dock-era rows)
    1. transfer_trip_assignments.adjusted_qty
       (the most recent trip-level edit by the Hub Manager in the
       trip_editor form — this is what the Hub Manager actually
       intends to ship and overrides everything else)
    2. ai_transfer_recommendations.manager_override_qty
       (an earlier override at the recommendation level, before
       trip assignment)
    3. ai_transfer_recommendations.ai_recommended_qty
       (AI's recommended qty)
    4. ai_transfer_recommendations.calculated_transfer_qty
       (formula baseline)

  Only adjusted_qty was missing prior to v5.8.4, which caused
  Hub Manager qty edits in trip_editor to be silently ignored
  downstream by both driver_delivery and pick_list_pdf_generator.
  """
  for key in ('loaded_qty',     # v5.30: what the dock actually put on the truck
              'adjusted_qty',
              'qty',            # v5.29: pick-era tta.qty = packed slice of qty_picked
              'manager_override_qty',
              'ai_recommended_qty',
              'calculated_transfer_qty'):
    v = row.get(key)
    if v is not None:
      return float(v)
  return 0.0


# =============================================================================
# SERVER CALLABLES
# =============================================================================

@anvil.server.callable
def list_trips_awaiting_delivery(worker_code: Optional[str] = None) -> List[Dict[str, Any]]:
  """
  Return every trip that has DEPARTED the Hub dock (shipped_date set;
  status Departed, or Finalized for rows stamped before v5.30) and is
  not yet Delivered (no received_date yet). A truck that is finalized
  but still being loaded is NOT shown - v5.30, review finding C6.

  Ordered by planned_date ascending, trip_number ascending — the
  driver should see the oldest/next-to-deliver trips at the top.

  Returns a list of dicts safe to render directly in a RepeatingPanel:
      [ { 'trip_id': int, 'batch_id': str, 'trip_number': int,
          'planned_date': 'YYYY-MM-DD', 'sku_count': int,
          'total_volume_ft3': float, 'total_weight_lb': float,
          'finalized_date': 'YYYY-MM-DDTHH:MM:SS' }, ... ]
  """
  _require_delivery_auth(worker_code)

  conn = get_db_connection()
  try:
    cursor = conn.cursor()
    # Defensive EXISTS filter: only surface trips that actually have
    # assignment rows. Protects the driver's queue from the kind of
    # orphan data we cleaned up on 2026-04-22 (19 legacy Finalized
    # trips had zero rows in transfer_trip_assignments — either dev
    # test data whose assignments got pruned, or empty trips from the
    # consolidator). If that shape ever reappears, the driver won't
    # see a blank confirm screen — the trip just won't show up.
    cursor.execute("""
        SELECT t.trip_id, t.batch_id, t.trip_number, t.planned_date,
               t.sku_count, t.total_volume_ft3, t.total_weight_lb,
               t.finalized_date
        FROM dbo.transfer_trips t
        WHERE t.status IN ('Finalized', 'Departed')
          AND t.shipped_date IS NOT NULL      -- v5.30: must have DEPARTED the dock
          AND t.received_date IS NULL
          AND EXISTS (
              SELECT 1
              FROM dbo.transfer_trip_assignments tta
              WHERE tta.trip_id = t.trip_id
          )
        ORDER BY t.planned_date ASC, t.trip_number ASC
    """)
    rows = cursor.fetchall()
    return [
      {k: _serialize(row[k]) for k in row.keys()}
      for row in rows
    ]
  finally:
    conn.close()


@anvil.server.callable
def get_trip_assignments_for_delivery(trip_id: int, worker_code: Optional[str] = None) -> Dict[str, Any]:
  """
  Return the per-SKU lines for the driver's confirmation screen.

  Returns:
      {
        'trip': { trip metadata, matching list_trips_awaiting_delivery },
        'lines': [
          { 'assignment_id': int,
            'sku': str,
            'part_description': str,
            'planned_qty': float,
            'stock_location': str | None,
            'delivered_qty': None,   # always None on first open
            'delivery_note': None,
            'status': 'Finalized' }
        ]
      }

  If trip_id is not Finalized-and-undelivered, returns {'error': ...}
  so the client can show a friendly message.
  """
  _require_delivery_auth(worker_code)

  conn = get_db_connection()
  try:
    cursor = conn.cursor()

    cursor.execute("""
        SELECT trip_id, batch_id, trip_number, planned_date,
               sku_count, total_volume_ft3, total_weight_lb,
               finalized_date, status, received_date, shipped_date
        FROM dbo.transfer_trips
        WHERE trip_id = %s
    """, (trip_id,))
    trip = cursor.fetchone()

    if not trip:
      return {'error': f'Trip {trip_id} not found.'}
    if trip['status'] not in ('Finalized', 'Departed'):
      return {'error': f'Trip {trip_id} is not deliverable '
              f'(current: {trip["status"]}).'}
    if trip['shipped_date'] is None:
      # v5.30 (C6): the dock has not tapped TRUCK DEPARTED yet.
      return {'error': f'Trip {trip_id} has not departed the Hub yet - '
              f'the dock must tap TRUCK DEPARTED before it can be delivered.'}
    if trip['received_date'] is not None:
      return {'error': f'Trip {trip_id} was already delivered on '
              f'{_serialize(trip["received_date"])}.'}

    # Per-SKU lines. LEFT JOIN part_specs so a missing row does not
    # hide the assignment — SKU is always rendered, description may
    # be blank. Uses part_specs.part_desc (the actual column name;
    # there is no separate stock_location column on this table).
    cursor.execute("""
        SELECT tta.assignment_id,
               tta.sku,
               tta.recommendation_id,
               tta.status,
               tta.delivered_qty,
               tta.delivery_note,
               tta.loaded_qty,           -- v5.30: dock LOAD/SHORT qty (ladder rung 0)
               tta.adjusted_qty,         -- v5.8.4: trip-editor manager edit
               tta.qty,                  -- v5.29: pick-era packed slice
               r.manager_override_qty,
               r.ai_recommended_qty,
               r.calculated_transfer_qty,
               ps.part_desc
        FROM dbo.transfer_trip_assignments tta
        INNER JOIN dbo.ai_transfer_recommendations r
            ON r.recommendation_id = tta.recommendation_id
        LEFT JOIN dbo.part_specs ps
            ON ps.sku = tta.sku
        WHERE tta.trip_id = %s
        ORDER BY tta.sku
    """, (trip_id,))
    line_rows = cursor.fetchall()

    lines = []
    for row in line_rows:
      lines.append({
        'assignment_id':    row['assignment_id'],
        'sku':              row['sku'],
        'part_desc':        row['part_desc'] or '',
        'planned_qty':      _planned_qty(row),
        'delivered_qty':    _serialize(row['delivered_qty']),
        'delivery_note':    row['delivery_note'],
        'status':           row['status'],
      })

    return {
      'trip':  {k: _serialize(trip[k]) for k in trip.keys()},
      'lines': lines,
    }
  finally:
    conn.close()


@anvil.server.callable
def submit_delivery_confirmation(trip_id: int,
                                 lines: List[Dict[str, Any]],
                                 driver_name: Optional[str] = None,
                                 worker_code: Optional[str] = None
                                ) -> Dict[str, Any]:
  """
  Transactionally record a driver's delivery confirmation.

  Args:
      trip_id:     transfer_trips.trip_id being confirmed.
      lines:       [ { 'assignment_id': int,
                       'delivered_qty': number,
                       'note': str | None }, ... ]
                   Any assignment NOT included in `lines` is treated
                   as "not confirmed"; its row stays Finalized.
      driver_name: Optional override. If omitted, the server derives
                   a display name from the Anvil user session (audit
                   integrity — clients cannot spoof identity).

  Returns:
      { 'success': True/False,
        'trip_id': int,
        'trip_marked_delivered': bool,     # False if already delivered
        'assignments_delivered': int,      # rows updated on tta
        'recs_cascaded': int,              # rows updated on recs
        'discrepancy_count': int,
        'sms_sent': bool | None,           # rolled-up: True if every
                                           # attempted send succeeded,
                                           # False if any failed, None
                                           # if no SMS was attempted
        'receipt_sms_sent': bool | None,   # per-SMS detail
        'discrepancy_sms_sent': bool | None,
        'error': str | None }
  """
  _require_delivery_auth(worker_code)

  # Identity — server-derived, not client-supplied.
  effective_driver = (
    driver_name
    if driver_name and _current_user_email() is None  # dev mode fallback
    else _current_user_display_name()
  )
  # Hard cap — table column is varchar(50).
  effective_driver = effective_driver[:50]

  # v5.29: with no Users session, a validated kiosk identity beats the
  # free-typed name for the audit column.
  if _current_user_email() is None and worker_code:
    _wname = _is_valid_worker(worker_code)
    if _wname:
      effective_driver = _wname[:50]

  # Defensive: the client MUST send something we can iterate over.
  if not isinstance(lines, list):
    return {'success': False, 'error': 'lines must be a list.'}

  conn = get_db_connection()
  try:
    cursor = conn.cursor()

    # ------------------------------------------------------------------
    # Step 1: stamp received_date on the trip. Guarded by status and
    # NULL so this is idempotent under retries — second submit will
    # get rowcount=0 and bail cleanly.
    # ------------------------------------------------------------------
    # v5.30 (C6): the truck must have DEPARTED (shipped_date set), and
    # the trip status is advanced to 'Delivered' so transfer_trips.status
    # finally means something (it sat at 'Finalized' on all 533 trips).
    cursor.execute("""
        UPDATE dbo.transfer_trips
        SET received_date = GETDATE(),
            status        = 'Delivered'
        WHERE trip_id = %s
          AND status IN ('Finalized', 'Departed')
          AND shipped_date IS NOT NULL
          AND received_date IS NULL
    """, (trip_id,))
    trip_marked = (cursor.rowcount == 1)

    if not trip_marked:
      # Either the trip doesn't exist, is not Finalized, or was already
      # delivered. Return without touching any child rows.
      conn.rollback()
      return {
        'success': False,
        'trip_id': trip_id,
        'trip_marked_delivered': False,
        'assignments_delivered': 0,
        'recs_cascaded': 0,
        'discrepancy_count': 0,
        'sms_sent': None,
        'receipt_sms_sent': None,
        'discrepancy_sms_sent': None,
        'error': 'Trip not eligible for delivery - already delivered, '
        'not yet departed from the Hub dock, or not in a deliverable state.',
      }

    # ------------------------------------------------------------------
    # Step 2: pre-fetch planned qtys for every assignment on this trip
    # so we can (a) validate that the client's assignment_ids belong to
    # this trip and (b) detect discrepancies post-commit without a
    # second read.
    # ------------------------------------------------------------------
    cursor.execute("""
        SELECT tta.assignment_id,
               tta.sku,
               tta.loaded_qty,     -- v5.30: dock LOAD/SHORT qty (ladder rung 0)
               tta.adjusted_qty,   -- v5.29: THE fix - Trip Editor edits were
                                   -- invisible here, so every edited line
                                   -- raised a false delivery discrepancy
               tta.qty,            -- v5.29: pick-era packed slice
               r.manager_override_qty,
               r.ai_recommended_qty,
               r.calculated_transfer_qty
        FROM dbo.transfer_trip_assignments tta
        INNER JOIN dbo.ai_transfer_recommendations r
            ON r.recommendation_id = tta.recommendation_id
        WHERE tta.trip_id = %s
    """, (trip_id,))
    planned_by_assignment = {}
    for row in cursor.fetchall():
      planned_by_assignment[row['assignment_id']] = {
        'sku':     row['sku'],
        'planned': _planned_qty(row),
      }

    # ------------------------------------------------------------------
    # Step 3: update each line's assignment row. Filter by trip_id AND
    # status='Finalized' so we cannot bleed into a neighbouring trip
    # or double-apply to an already-Delivered row.
    # ------------------------------------------------------------------
    assignments_delivered = 0
    discrepancies = []

    for line in lines:
      aid = line.get('assignment_id')
      if aid is None or aid not in planned_by_assignment:
        # Silently skip unknown assignment_ids rather than erroring —
        # the trip-level stamp above already succeeded and we don't
        # want a single bad client payload to poison the whole submit.
        continue

      try:
        delivered_qty = float(line.get('delivered_qty') or 0)
      except (TypeError, ValueError):
        continue
      note = line.get('note')
      if note is not None:
        note = str(note)[:500]  # respect column width

      cursor.execute("""
          UPDATE dbo.transfer_trip_assignments
          SET status         = 'Delivered',
              delivered_qty  = %s,
              delivery_note  = %s,
              delivered_by   = %s,
              delivered_date = GETDATE()
          WHERE assignment_id = %s
            AND trip_id       = %s
            AND status        = 'Finalized'
      """, (delivered_qty, note, effective_driver, aid, trip_id))

      if cursor.rowcount == 1:
        assignments_delivered += 1

        planned = planned_by_assignment[aid]['planned']
        if abs(delivered_qty - planned) > 0.001:
          discrepancies.append({
            'assignment_id': aid,
            'sku':           planned_by_assignment[aid]['sku'],
            'planned_qty':   planned,
            'delivered_qty': delivered_qty,
            'note':          note,
          })

    # ------------------------------------------------------------------
    # Step 3.5 (v5.30, review M9): all-or-nothing. If the client payload
    # dropped or mangled a line, refuse the whole submit so the trip
    # cannot leave the driver queue with an assignment still Finalized
    # and its rec cascaded to Delivered underneath it.
    # ------------------------------------------------------------------
    expected = len(planned_by_assignment)
    if assignments_delivered != expected:
      conn.rollback()
      return {
        'success': False,
        'trip_id': trip_id,
        'trip_marked_delivered': False,
        'assignments_delivered': 0,
        'recs_cascaded': 0,
        'discrepancy_count': 0,
        'sms_sent': None,
        'receipt_sms_sent': None,
        'discrepancy_sms_sent': None,
        'error': (f'Only {assignments_delivered} of {expected} lines were '
                  f'confirmed - nothing was saved. Refresh the trip and '
                  f'submit every line.'),
      }

    # ------------------------------------------------------------------
    # Step 4: cascade rec status -> Delivered for every rec tied to
    # this trip via FK_transfer_trip_recommendations. Scoped by
    # trip_id (not batch_id) because batches can span trips with
    # different delivery dates.
    #
    # Filter on BOTH 'Finalized' (the normal v5.7 path) AND
    # 'Trip_Assigned' (the safety net). 'Trip_Assigned' covers two
    # cases that the narrower 'Finalized' filter would silently miss:
    #   - Recs that were Finalized before the v5.7 Finalize Batch
    #     cascade landed (so their state machine was never advanced
    #     past Trip_Assigned).
    #   - Recs that somehow slip past a future Finalize Batch run
    #     (bug, race, partial commit) and would otherwise become
    #     phantom inventory in the v5.6 PendingTransferQty CTE,
    #     suppressing fresh transfer recommendations.
    # See sql/cleanup_stale_trip_assigned_recs.sql for the one-time
    # historical cleanup that paired with this filter widening.
    # ------------------------------------------------------------------
    # v5.30 (review C2): 'Applied' added. The dock's finalize only
    # promotes a rec that is exactly Applied at that instant; a rec that
    # reaches the truck still Applied (second-pick remainder path,
    # optimizer era) was never cascaded and kept crediting its full qty
    # as live Starlite supply forever (recs 3010/3037/3319 - 261 units).
    # Scoped to assignments this submit just marked Delivered.
    cursor.execute("""
        UPDATE r
        SET r.status = 'Delivered'
        FROM dbo.ai_transfer_recommendations r
        INNER JOIN dbo.transfer_trip_assignments tta
            ON tta.recommendation_id = r.recommendation_id
        WHERE tta.trip_id = %s
          AND tta.status  = 'Delivered'
          AND r.status    IN ('Applied', 'Finalized', 'Trip_Assigned')
    """, (trip_id,))
    recs_cascaded = cursor.rowcount

    # ------------------------------------------------------------------
    # Step 5: ai_agent_log — single row summarizing the cascade.
    # Mirrors the BATCH_FINALIZED entry format used in v5.7.
    # ------------------------------------------------------------------
    cascade_summary = (
      f'trip_id={trip_id}; assignments_delivered={assignments_delivered}; '
      f'recs_cascaded={recs_cascaded}; discrepancies={len(discrepancies)}'
    )
    cursor.execute("""
        INSERT INTO dbo.ai_agent_log
            (action_type, sku, old_value, new_value, reason,
             action_timestamp, executed_by)
        VALUES ('TRIP_DELIVERED', NULL, 'Departed', 'Delivered',
                %s, GETDATE(), %s)
    """, (cascade_summary, effective_driver))

    conn.commit()

    # ------------------------------------------------------------------
    # Step 6 (POST-COMMIT): SMS notifications. Outside the DB txn so a
    # Twilio outage cannot roll back a valid delivery. Import lazily
    # so a bug in sms_notifications.py cannot prevent delivery submit.
    #
    # Two-text model:
    #   - DELIVERY_RECEIPT  -> sent on EVERY delivery. Short "it
    #     happened" text so the Hub Manager has a running record.
    #   - DELIVERY_DISCREPANCY -> sent ONLY when one or more lines
    #     diverge from planned. Contains the per-SKU detail.
    # On a divergent delivery, the Hub Manager gets both (short
    # receipt + long detail). On a clean delivery, just the receipt.
    # ------------------------------------------------------------------
    receipt_sent     = None   # None = not attempted
    discrepancy_sent = None   # None = not attempted (clean delivery)

    try:
      from . import sms_notifications
    except Exception as e:
      print(f'[driver_delivery] sms_notifications import failed: {e}')
      sms_notifications = None

    if sms_notifications is not None:
      # Always-on receipt
      if hasattr(sms_notifications,
                 'send_delivery_receipt_notification'):
        receipt_sent = False
        try:
          result = sms_notifications.send_delivery_receipt_notification(
            trip_id=trip_id,
            assignments_delivered=assignments_delivered,
            discrepancy_count=len(discrepancies),
            driver_name=effective_driver,
          )
          receipt_sent = bool(result and result.get('sent_count', 0) > 0)
        except Exception as e:
          print(f'[driver_delivery] receipt SMS failed: {e}')
          receipt_sent = False

      # Discrepancy detail — only when there are variances
      if discrepancies and hasattr(
        sms_notifications, 'send_delivery_discrepancy_notification'):
        discrepancy_sent = False
        try:
          result = sms_notifications.send_delivery_discrepancy_notification(
            trip_id=trip_id,
            discrepancies=discrepancies,
            driver_name=effective_driver,
          )
          discrepancy_sent = bool(result and result.get('sent_count', 0) > 0)
        except Exception as e:
          print(f'[driver_delivery] discrepancy SMS failed: {e}')
          discrepancy_sent = False

    # Backwards-compatible 'sms_sent' field for older clients: True if
    # at least one SMS went out (receipt or discrepancy), False if any
    # attempted send failed, None if nothing was attempted.
    if receipt_sent is None and discrepancy_sent is None:
      sms_sent = None
    else:
      attempted = [s for s in (receipt_sent, discrepancy_sent)
                   if s is not None]
      sms_sent = all(attempted) if attempted else None

    return {
      'success': True,
      'trip_id': trip_id,
      'trip_marked_delivered': True,
      'assignments_delivered': assignments_delivered,
      'recs_cascaded': recs_cascaded,
      'discrepancy_count': len(discrepancies),
      'sms_sent': sms_sent,
      'receipt_sms_sent': receipt_sent,
      'discrepancy_sms_sent': discrepancy_sent,
      'error': None,
    }
  except Exception as e:
    # Any error after Step 1 opens the txn but before commit — roll
    # back so we don't leave transfer_trips.received_date stamped
    # while tta / recs are still Finalized.
    try:
      conn.rollback()
    except Exception:
      pass
    print(f'[driver_delivery] submit_delivery_confirmation failed: {e}')
    return {
      'success': False,
      'trip_id': trip_id,
      'trip_marked_delivered': False,
      'assignments_delivered': 0,
      'recs_cascaded': 0,
      'discrepancy_count': 0,
      'sms_sent': None,
      'receipt_sms_sent': None,
      'discrepancy_sms_sent': None,
      'error': f'Server error: {e}',
    }
  finally:
    conn.close()