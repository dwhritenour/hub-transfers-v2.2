"""
load_dashboard.py - Anvil Server Module
=======================================
Hub Transfers v2.1 - Truck Loading stage (design locked 2026-08-30).

Serves the kiosk's new Truck tab. The flow is FINALIZE -> LOAD -> DEPART
("finalize before reality", decision D-7):

  1. FINALIZE  A worker selects staged lines (completed-pick lines not on
               any live trip) and finalizes the truck. The trip is created
               with status 'Finalized' BEFORE loading begins, and the
               manifest (the only shipping paper) exists from this moment.
  2. LOAD      Lines are tapped as physically loaded (loaded_by /
               loaded_date / loaded_qty, migration 039). Short-loads need
               a reason code and text the Hub Managers (D-3). A line that
               will not go can be removed back to STAGED before departure.
  3. DEPART    Blocked until every line is resolved. Reconciles short /
               zero loads (v1.3.0: loaded qty becomes THE qty; zero-loaded
               lines leave the truck and their rec is cancelled; Inventory
               Control gets a cycle-count SMS), stamps
               transfer_trips.shipped_date + status 'Departed', then
               requires an explicit "still at the Hub" acknowledgment for
               every line remaining in STAGED (the gate).

Design rules (locked 2026-08-30):
  - D-1: staged pool is freely selectable; the truck optimizer's current
    Proposed batch is surfaced as a pre-checked suggestion only.
  - D-2: nothing reaches a truck without a completed pick (eligibility is
    completed-pick lines exclusively; same C1/Option A rule as v2.0).
  - D-3: short-load => reason code + immediate SMS to Hub Managers.
  - D-4: this module is the ONLY finalize path in v2.1. The Trip Editor
    keeps oversight but its finalize actions are retired at cutover.
  - D-5: NO role gating. Any active hub_workers row may finalize, load,
    depart and acknowledge. Accountability is the worker code recorded on
    every event - not a permission wall.
  - D-6: one open (finalized, not departed) truck per worker; parallel
    trucks by different workers are fine.

Every UPDATE is state-guarded (WHERE expected-state) with a rowcount
check. All actions append to dbo.transfer_pick_events (append-only).
New event types (no CHECK constraint on event_type - verified
2026-08-30): LINE_LOADED, LINE_SHORT_LOAD, LINE_UNLOADED, LINE_REMOVED,
TRUCK_FINALIZED, TRUCK_DEPARTED, STILL_AT_HUB_ACK.
Trip-level milestones also go to dbo.ai_agent_log.

Database objects: migration 039 (tta loaded_qty / loaded_by /
loaded_date / load_discrepancy_code). transfer_trips.shipped_date and
all other objects pre-exist.

Author: Claude (Dave Whritenour review)
Project: Hub Transfers v2.1 - Truck Loading stage
Created: 2026-08-30
"""

import anvil.server
from datetime import datetime, date
from decimal import Decimal

from . import db_config
from .pick_dashboard import _hold_location   # 1.4.0: ON HOLD location (Hub policy)
from .sms_notifications import (
TwilioSMSClient,
SMSConfig,
get_hub_manager_phones,
log_sms_notification,
)

__version__       = "1.4.1"
__version_date__  = "2026-10-29"
__version_notes__ = (
  "1.4.1 (2026-10-29): a truck whose LAST line is removed at load time cancels "
  "itself (transfer_trips.status 'Cancelled', notes, ai_agent_log "
  "TRUCK_CANCELLED_EMPTY) in the same transaction, and remove_line_from_truck "
  "returns {'truck_cancelled': True, ...} so the dock returns to the staged list. "
  "get_open_truck ignores a Finalized truck with no assignments. Trip 1828 "
  "(2026-10-29 15:31): both lines removed to add parts, truck stranded on every "
  "dock screen with '0 of 0 loaded' and DEPART greyed, cancelled by hand. "
  "1.4.0 (2026-09-10, Hub policy): dock location lookups exclude the ON "
  "HOLD location (global_parameters_text.hub_hold_location, default "
  "'100') and are deterministic - see pick_dashboard 1.4.0. "
  "1.3.1 (2026-09-08): same split-literal T-SQL bug as pick_dashboard 1.3.1 in three notes - finalize REMAINDER_NOT_CARRIED and both _reconcile_short_loads UPDATEs (zero and short). Would have failed FINALIZE with a partial allocation and DEPART with a short load. Joined into single literals. "
  "1.3.0 (Transfer Logic Review 2026-09-04, findings C5/C6/H9/M3/M8/Q6): "
  "DEPART now (a) sets transfer_trips.status='Departed' (was never "
  "advanced), (b) reconciles short loads - tta.qty/adjusted_qty become "
  "the LOADED qty so the manifest, driver, view and e21 all agree, the "
  "rec gets a note, and Inventory Control gets one cycle-count SMS "
  "(SHORT_PICK_CYCLE_COUNT) for every short/zero line (Dave 9/5: "
  "shortfalls go to cycle count, NOT back to STAGED), (c) drops any "
  "zero-loaded line off the truck and cancels its rec with a note when "
  "nothing else holds it (the zero-pick rule applied at the dock). "
  "get_open_truck no longer requires the caller to be the finalizer "
  "(own truck preferred, else any open kiosk truck) so a truck is never "
  "unreachable when its finalizer goes home. Every load-stage write "
  "re-asserts 'truck still open' inside the UPDATE/DELETE (no more "
  "check-then-write window). remove_line decrements trip volume/weight "
  "and scopes its rec-revert guard to LIVE trips. finalize notes a rec's "
  "un-picked remainder ('detector will re-raise') so the manager is told "
  "why a partly-shipped rec comes back as a new one. "
  "1.2.0: get_dock_capacity callable (truck_cap_ft3 / truck_cap_lb from "
  "global_parameters, optimizer defaults) so the dock grid can show live "
  "utilization against the same limits the optimizer packs to. "
  "1.1.0 (Trip Editor v3 / dock PC): get_staged_lines gains per-line "
  "unit + total volume/weight (part_specs) so the dock grid's capacity "
  "summary live-updates BEFORE finalize; get_truck lines gain "
  "time/inventory/base urgency + volume/weight (Hub request: urgency "
  "values visible to all Hub employees in #pick and #dock). "
  "1.0.0: initial Truck Loading module - staged pool query with "
  "optimizer-suggestion flag, finalize-before-loading truck creation, "
  "per-line load / short-load (+ manager SMS, D-3) / undo / remove, "
  "departure with resolution check and still-at-Hub acknowledgment "
  "gate, per-truck manifest via trip_pdf_generator."
)

# Trips this module creates are recognizable by created_by.
_KIOSK_CREATED_BY = 'kiosk_finalize'

# Rec statuses whose completed-pick lines may board a truck.
_BOARDABLE_REC_STATUSES = ('Applied', 'Trip_Assigned')


# =============================================================================
# SERIALIZATION HELPERS (same pattern as pick_dashboard.py)
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
  cursor = conn.cursor()
  cursor.execute(
    """
      INSERT INTO dbo.ai_agent_log (
          action_type, sku, old_value, new_value, reason, action_timestamp, executed_by
      )
      VALUES (%s, %s, %s, %s, %s, GETDATE(), %s)
    """,
    (action_type, sku, old_value, new_value, reason,
     'load_dashboard v' + __version__)
  )


def _require_active_worker(conn, worker_code):
  cursor = conn.cursor()
  cursor.execute(
    "SELECT worker_code, display_name FROM dbo.hub_workers "
    "WHERE worker_code = %s AND is_active = 1",
    (worker_code,)
  )
  row = cursor.fetchone()
  if not row:
    raise Exception("Worker %s not found or inactive" % worker_code)
  return row


def _priority_for(event_type, time_urgency, inventory_urgency):
  """
    Priority code/sort for kiosk-created tta rows. Prefer the truck
    optimizer's own map so PDFs color identically; fall back to a
    compatible local ranking if the import ever fails.
    """
  try:
    from .transfer_truck_optimizer import assign_priority
    sort, code = assign_priority({
      'event_type': event_type,
      'time_urgency': time_urgency,
      'inventory_urgency': inventory_urgency,
    })
    return sort, code
  except Exception:
    urgency = time_urgency if event_type == 2 else inventory_urgency
    rank = {'CRITICAL': 1, 'HIGH': 2, 'MEDIUM': 3, 'WARNING': 3,
            'LOW': 4}.get((urgency or '').upper(), 7)
    return rank, "%d-%s" % (rank, (urgency or 'ROUTINE').upper())


# One SQL fragment, used verbatim by both the staged query and the
# finalize-time revalidation, so the two can never drift apart:
# a STAGED line is a picked (or short-with-qty) line of a Completed pick,
# on a boardable rec, with no assignment on any live (non-Proposed) trip.
_STAGED_WHERE = """
        p.status = 'Completed'
        AND pl.status IN ('Picked', 'Short')
        AND pl.qty_picked > 0
        AND r.status IN ('Applied', 'Trip_Assigned')
        AND NOT EXISTS (SELECT 1
                        FROM dbo.transfer_trip_assignments t2
                        JOIN dbo.transfer_trips tr2 ON tr2.trip_id = t2.trip_id
                        WHERE t2.pick_line_id = pl.pick_line_id
                          AND tr2.status <> 'Proposed')
"""


# =============================================================================
# STAGED POOL
# =============================================================================

@anvil.server.callable
def get_staged_lines():
  """
    The kiosk STAGED list: every completed-pick line not on a live trip.
    is_suggested = 1 when the truck optimizer's current Proposed batch
    includes the line (D-1 pre-check). age_hours drives the >24h badge.
    """
  conn = db_config.get_nexus_connection()
  try:
    hold_loc = _hold_location()
    cursor = conn.cursor()
    cursor.execute(
      """
        SELECT pl.pick_line_id, pl.pick_id, pl.recommendation_id, pl.sku,
               CAST(pl.qty_picked AS DECIMAL(18,4)) AS qty,
               p.completed_date,
               DATEDIFF(HOUR, p.completed_date, GETDATE()) AS age_hours,
               r.event_type, r.base_urgency, r.time_urgency, r.inventory_urgency,
               CASE WHEN pl.location_code IS NULL OR pl.location_code = %s
                    THEN COALESCE(loc.location_code, '--')
                    ELSE pl.location_code END AS location_code,
               ps.part_desc AS part_description,
               CAST(COALESCE(ps.act_volume_ft3, 0) * COALESCE(ps.volume_factor, 1)
                    AS DECIMAL(18,4)) AS unit_volume_ft3,
               CAST(COALESCE(ps.act_weight_lb, 0) * COALESCE(ps.weight_factor, 1)
                    AS DECIMAL(18,4)) AS unit_weight_lb,
               CAST(pl.qty_picked * COALESCE(ps.act_volume_ft3, 0)
                    * COALESCE(ps.volume_factor, 1) AS DECIMAL(18,4)) AS total_volume_ft3,
               CAST(pl.qty_picked * COALESCE(ps.act_weight_lb, 0)
                    * COALESCE(ps.weight_factor, 1) AS DECIMAL(18,4)) AS total_weight_lb,
               CASE WHEN EXISTS (SELECT 1
                                 FROM dbo.transfer_trip_assignments t3
                                 JOIN dbo.transfer_trips tr3 ON tr3.trip_id = t3.trip_id
                                 WHERE t3.pick_line_id = pl.pick_line_id
                                   AND tr3.status = 'Proposed')
                    THEN 1 ELSE 0 END AS is_suggested
        FROM dbo.transfer_pick_lines pl
        JOIN dbo.transfer_picks p ON p.pick_id = pl.pick_id
        JOIN dbo.ai_transfer_recommendations r
          ON r.recommendation_id = pl.recommendation_id
        OUTER APPLY (SELECT (SELECT TOP 1 hl.location_code FROM dbo.hub_product_locations hl
                             WHERE hl.sku = pl.sku AND hl.primary_location = 1
                               AND hl.location_code <> %s
                             ORDER BY hl.location_qty DESC, hl.location_code) AS location_code) loc
        LEFT JOIN dbo.part_specs ps ON ps.sku = pl.sku
        WHERE """ + _STAGED_WHERE + """
        ORDER BY p.completed_date, pl.pick_line_id
      """,
      (hold_loc, hold_loc)
    )
    return [serialize_row(row) for row in cursor.fetchall()]
  finally:
    conn.close()


# =============================================================================
# FINALIZE (step 1 - creates the trip BEFORE loading)
# =============================================================================

@anvil.server.callable
def get_open_truck(worker_code):
  """
    An open (finalized, not departed) kiosk truck, or None. The caller's
    own truck is preferred; otherwise any open kiosk truck is returned so
    a truck is never stranded when its finalizer is unavailable (v1.3.0,
    review H9). D-6 (one open truck per finalizer) is enforced in
    finalize_truck, not here.
    1.4.1: a Finalized truck with no assignments is not 'open' - it is
    either mid-cancel or a leftover; the dock must not resume it.
    """
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      """
        SELECT TOP 1 trip_id FROM dbo.transfer_trips
        WHERE created_by = %s
          AND status = 'Finalized' AND shipped_date IS NULL
          AND EXISTS (SELECT 1 FROM dbo.transfer_trip_assignments a
                      WHERE a.trip_id = transfer_trips.trip_id)
        ORDER BY CASE WHEN finalized_by = %s THEN 0 ELSE 1 END, trip_id DESC
      """,
      (_KIOSK_CREATED_BY, worker_code)
    )
    row = cursor.fetchone()
    return get_truck(row['trip_id']) if row else None
  finally:
    conn.close()


@anvil.server.callable
def finalize_truck(worker_code, pick_line_ids, planned_date=None):
  """
    Step 1: commit the trip BEFORE loading (D-7).

    Validates the selection against the STAGED rule, removes any
    optimizer-Proposed assignments the lines are riding (the suggestion
    is consumed), creates the trip as 'Finalized' with one assignment per
    line (qty = qty_picked), flips Applied recs to Trip_Assigned, and
    logs everything. Returns {'trip_id', 'batch_id', 'line_count'}.
    """
  conn = db_config.get_nexus_connection()
  try:
    worker = _require_active_worker(conn, worker_code)
    if not pick_line_ids:
      raise Exception("Select at least one staged line")
    pick_line_ids = [int(i) for i in pick_line_ids]

    # D-6: one open truck per worker.
    cursor = conn.cursor()
    cursor.execute(
      """
        SELECT TOP 1 trip_id FROM dbo.transfer_trips
        WHERE created_by = %s AND finalized_by = %s
          AND status = 'Finalized' AND shipped_date IS NULL
          AND EXISTS (SELECT 1 FROM dbo.transfer_trip_assignments a
                      WHERE a.trip_id = transfer_trips.trip_id)
      """,
      (_KIOSK_CREATED_BY, worker_code)
    )
    open_row = cursor.fetchone()
    if open_row:
      raise Exception("You already have truck %s open - depart it first"
                      % open_row['trip_id'])

    # Re-validate every selected line against the STAGED rule right now.
    placeholders = ', '.join(['%s'] * len(pick_line_ids))
    cursor.execute(
      """
        SELECT pl.pick_line_id, pl.pick_id, pl.recommendation_id, pl.sku,
               CAST(pl.qty_picked AS DECIMAL(18,4)) AS qty,
               r.status AS rec_status, r.event_type,
               r.time_urgency, r.inventory_urgency,
               CASE WHEN pl.location_code IS NULL OR pl.location_code = %s
                    THEN loc.location_code ELSE pl.location_code END AS location_code
        FROM dbo.transfer_pick_lines pl
        JOIN dbo.transfer_picks p ON p.pick_id = pl.pick_id
        JOIN dbo.ai_transfer_recommendations r
          ON r.recommendation_id = pl.recommendation_id
        OUTER APPLY (SELECT (SELECT TOP 1 hl.location_code FROM dbo.hub_product_locations hl
                             WHERE hl.sku = pl.sku AND hl.primary_location = 1
                               AND hl.location_code <> %s
                             ORDER BY hl.location_qty DESC, hl.location_code) AS location_code) loc
        WHERE pl.pick_line_id IN (""" + placeholders + """)
          AND """ + _STAGED_WHERE,
      (_hold_location(), _hold_location()) + tuple(pick_line_ids)
    )
    lines = list(cursor.fetchall())
    good_ids = {l['pick_line_id'] for l in lines}
    bad = [i for i in pick_line_ids if i not in good_ids]
    if bad:
      raise Exception("Line(s) no longer available for a truck "
                      "(picked elsewhere or changed underneath you): %s "
                      "- refresh the staged list" % bad)

    # Consume any optimizer suggestion rows these lines are riding.
    cursor.execute(
      "DELETE FROM dbo.transfer_trip_assignments "
      "WHERE pick_line_id IN (" + placeholders + ") "
      "  AND trip_id IN (SELECT trip_id FROM dbo.transfer_trips "
      "                  WHERE status = 'Proposed')",
      tuple(pick_line_ids)
    )

    # Volume / weight totals from part_specs (optimizer defaults on miss).
    skus = list({l['sku'] for l in lines})
    sku_ph = ', '.join(['%s'] * len(skus))
    cursor.execute(
      """
        SELECT sku,
               COALESCE(act_volume_ft3, 0) * COALESCE(volume_factor, 1) AS unit_vol,
               COALESCE(act_weight_lb, 0) * COALESCE(weight_factor, 1) AS unit_wt
        FROM dbo.part_specs WHERE sku IN (""" + sku_ph + ")",
      tuple(skus)
    )
    specs = {r['sku']: (float(r['unit_vol'] or 0), float(r['unit_wt'] or 0))
             for r in cursor.fetchall()}
    total_vol = sum(float(l['qty']) * specs.get(l['sku'], (0.1, 0.5))[0] for l in lines)
    total_wt = sum(float(l['qty']) * specs.get(l['sku'], (0.1, 0.5))[1] for l in lines)

    # Anvil server datetime.now() is UTC (house rule: GETDATE() is ET) -
    # both the batch id and the default planned date come from the DB
    # clock so evening trucks don't jump to tomorrow's date.
    cursor.execute(
      "SELECT FORMAT(GETDATE(), 'yyyyMMdd_HHmmss') AS stamp, "
      "       CAST(GETDATE() AS DATE) AS today"
    )
    now_row = cursor.fetchone()
    batch_id = 'K' + now_row['stamp']
    planned = planned_date or now_row['today']

    cursor.execute(
      """
        INSERT INTO dbo.transfer_trips
            (batch_id, trip_number, planned_date, planned_time, status,
             total_volume_ft3, total_weight_lb, sku_count,
             created_date, created_by, finalized_date, finalized_by)
        OUTPUT INSERTED.trip_id
        VALUES (%s, 1, %s, NULL, 'Finalized', %s, %s, %s,
                GETDATE(), %s, GETDATE(), %s)
      """,
      (batch_id, planned, round(total_vol), round(total_wt), len(lines),
       _KIOSK_CREATED_BY, worker_code)
    )
    trip_id = cursor.fetchone()['trip_id']

    for l in lines:
      p_sort, p_code = _priority_for(l['event_type'], l['time_urgency'],
                                     l['inventory_urgency'])
      qty = float(l['qty'])
      unit_vol, unit_wt = specs.get(l['sku'], (0.1, 0.5))
      cursor.execute(
        """
          INSERT INTO dbo.transfer_trip_assignments
              (batch_id, trip_number, trip_id, recommendation_id, sku,
               qty, planned_qty, priority_code, priority_sort, event_type,
               urgency, volume_ft3, weight_lb, location_code,
               pick_line_id, status, created_date, created_by)
          VALUES (%s, 1, %s, %s, %s,
                  %s, %s, %s, %s, %s,
                  %s, %s, %s, %s,
                  %s, 'Finalized', GETDATE(), %s)
        """,
        (batch_id, trip_id, l['recommendation_id'], l['sku'],
         qty, qty, p_code, p_sort, l['event_type'],
         (l['time_urgency'] if l['event_type'] == 2 else l['inventory_urgency']),
         qty * unit_vol, qty * unit_wt, l['location_code'],
         l['pick_line_id'], _KIOSK_CREATED_BY)
      )

      # B3-style status walk: rec joins the trip.
      cursor.execute(
        """
          UPDATE dbo.ai_transfer_recommendations
          SET status = 'Trip_Assigned'
          WHERE recommendation_id = %s AND status = 'Applied'
        """,
        (l['recommendation_id'],)
      )

    for pick_id in sorted({l['pick_id'] for l in lines}):
      n = sum(1 for l in lines if l['pick_id'] == pick_id)
      _log_event(conn, pick_id, 'TRUCK_FINALIZED', performed_by=worker_code,
                 qty=n, note='trip %s (%s)' % (trip_id, batch_id))

    # v1.3.0 (Q6, Dave 9/5): a rec whose approved qty was only partly
    # picked leaves the pool the moment it is Trip_Assigned. The
    # remainder is NOT re-staged - the view credits only what is on the
    # truck, so the detector re-raises the SKU if Starlite still needs
    # it. Tell the manager on the rec so "I already approved this" has
    # an answer.
    for rec_id in sorted({l['recommendation_id'] for l in lines}):
      cursor.execute(
        """
          SELECT r.sku,
                 CAST(COALESCE(r.[manager_override_qty ], r.ai_recommended_qty,
                               r.calculated_transfer_qty) AS DECIMAL(18,4)) AS approved,
                 CAST(COALESCE((SELECT SUM(pl.qty_planned)
                                FROM dbo.transfer_pick_lines pl
                                WHERE pl.recommendation_id = r.recommendation_id
                                  AND pl.status IN ('Assigned', 'Picked', 'Short')), 0)
                      AS DECIMAL(18,4)) AS allocated,
                 (SELECT COUNT(*) FROM dbo.transfer_pick_lines pl
                  WHERE pl.recommendation_id = r.recommendation_id
                    AND pl.status = 'Assigned') AS open_lines
          FROM dbo.ai_transfer_recommendations r
          WHERE r.recommendation_id = %s
        """,
        (rec_id,)
      )
      rr = cursor.fetchone()
      if not rr:
        continue
      remainder = float(rr['approved'] or 0) - float(rr['allocated'] or 0)
      if remainder > 0.0001 and int(rr['open_lines'] or 0) == 0:
        cursor.execute(
          """
            UPDATE dbo.ai_transfer_recommendations
            SET notes = CONCAT(ISNULL(CAST(notes AS nvarchar(max)), ''),
                               ' | [', CONVERT(varchar(16), GETDATE(), 120),
                               '] Trip ', %s, ': approved ', %s, ', on truck ',
                               %s, '; remainder ', %s, ' was never picked and is NOT carried forward - the detector will re-raise this SKU if Starlite still needs it.')
            WHERE recommendation_id = %s
          """,
          (trip_id, '%g' % float(rr['approved'] or 0),
           '%g' % float(rr['allocated'] or 0), '%g' % remainder, rec_id)
        )
        _log_agent_action(conn, 'REMAINDER_NOT_CARRIED', rr['sku'],
                          '%g approved' % float(rr['approved'] or 0),
                          '%g on trip %s' % (float(rr['allocated'] or 0), trip_id),
                          "Rec %s: %g un-picked remainder dropped at finalize; "
                          "detector re-raises if still needed (Q6 decision 2026-09-05)"
                          % (rec_id, remainder))

    _log_agent_action(conn, 'TRUCK_FINALIZED', None,
                      'Staged', 'Finalized',
                      "Trip %s (%s): %d line(s) finalized BEFORE loading by %s (%s)"
                      % (trip_id, batch_id, len(lines), worker['display_name'],
                         worker_code))
    conn.commit()
    return {'trip_id': trip_id, 'batch_id': batch_id, 'line_count': len(lines)}
  finally:
    conn.close()


@anvil.server.callable
def get_truck(trip_id):
  """Header + load lines for one kiosk truck (also used to resume)."""
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      """
        SELECT t.trip_id, t.batch_id, t.status, t.planned_date,
               t.finalized_date, t.finalized_by,
               w.display_name AS finalized_by_name,
               t.shipped_date, t.sku_count
        FROM dbo.transfer_trips t
        LEFT JOIN dbo.hub_workers w ON w.worker_code = t.finalized_by
        WHERE t.trip_id = %s
      """,
      (trip_id,)
    )
    header = cursor.fetchone()
    if not header:
      raise Exception("Trip %s not found" % trip_id)

    cursor.execute(
      """
        SELECT a.assignment_id, a.pick_line_id, a.recommendation_id, a.sku,
               CAST(COALESCE(a.adjusted_qty, a.planned_qty, a.qty) AS DECIMAL(18,4)) AS target_qty,
               CAST(a.loaded_qty AS DECIMAL(18,4)) AS loaded_qty,
               a.loaded_by, lw.display_name AS loaded_by_name, a.loaded_date,
               a.load_discrepancy_code, a.location_code,
               pl.pick_id, ps.part_desc AS part_description,
               r.time_urgency, r.inventory_urgency, r.base_urgency, r.event_type,
               CAST(COALESCE(a.adjusted_qty, a.planned_qty, a.qty)
                    * COALESCE(ps.act_volume_ft3, 0) * COALESCE(ps.volume_factor, 1)
                    AS DECIMAL(18,4)) AS total_volume_ft3,
               CAST(COALESCE(a.adjusted_qty, a.planned_qty, a.qty)
                    * COALESCE(ps.act_weight_lb, 0) * COALESCE(ps.weight_factor, 1)
                    AS DECIMAL(18,4)) AS total_weight_lb
        FROM dbo.transfer_trip_assignments a
        LEFT JOIN dbo.transfer_pick_lines pl ON pl.pick_line_id = a.pick_line_id
        LEFT JOIN dbo.hub_workers lw ON lw.worker_code = a.loaded_by
        LEFT JOIN dbo.part_specs ps ON ps.sku = a.sku
        LEFT JOIN dbo.ai_transfer_recommendations r
               ON r.recommendation_id = a.recommendation_id
        WHERE a.trip_id = %s
        ORDER BY COALESCE(a.location_code, 'zzz'), a.sku
      """,
      (trip_id,)
    )
    return {
      'header': serialize_row(header),
      'lines': [serialize_row(row) for row in cursor.fetchall()],
    }
  finally:
    conn.close()


# =============================================================================
# LOAD (step 2 - taps against the finalized list)
# =============================================================================

def _loadable_assignment(conn, assignment_id):
  """Fetch one assignment on an open kiosk truck, or raise."""
  cursor = conn.cursor()
  cursor.execute(
    """
      SELECT a.assignment_id, a.trip_id, a.pick_line_id, a.sku,
             a.recommendation_id, pl.pick_id,
             CAST(COALESCE(a.adjusted_qty, a.planned_qty, a.qty) AS DECIMAL(18,4)) AS target_qty,
             a.loaded_date, t.status AS trip_status, t.shipped_date
      FROM dbo.transfer_trip_assignments a
      JOIN dbo.transfer_trips t ON t.trip_id = a.trip_id
      LEFT JOIN dbo.transfer_pick_lines pl ON pl.pick_line_id = a.pick_line_id
      WHERE a.assignment_id = %s
    """,
    (assignment_id,)
  )
  row = cursor.fetchone()
  if not row:
    raise Exception("Assignment %s not found" % assignment_id)
  if row['trip_status'] != 'Finalized' or row['shipped_date'] is not None:
    raise Exception("Truck %s is not open for loading (status %s%s)"
                    % (row['trip_id'], row['trip_status'],
                       ', departed' if row['shipped_date'] else ''))
  return row


@anvil.server.callable
def record_line_loaded(assignment_id, worker_code):
  """The tap: full target quantity went on the truck."""
  conn = db_config.get_nexus_connection()
  try:
    _require_active_worker(conn, worker_code)
    row = _loadable_assignment(conn, assignment_id)
    target = float(row['target_qty'] or 0)

    cursor = conn.cursor()
    cursor.execute(
      """
        UPDATE a
        SET a.loaded_qty = %s, a.loaded_by = %s, a.loaded_date = SYSDATETIME(),
            a.load_discrepancy_code = NULL
        FROM dbo.transfer_trip_assignments a
        JOIN dbo.transfer_trips t ON t.trip_id = a.trip_id
        WHERE a.assignment_id = %s AND a.loaded_date IS NULL
          AND t.status = 'Finalized' AND t.shipped_date IS NULL
      """,
      (target, worker_code, assignment_id)
    )
    if cursor.rowcount != 1:
      conn.rollback()
      raise Exception("Line already recorded (double tap?) - refresh")

    _log_event(conn, row['pick_id'], 'LINE_LOADED',
               pick_line_id=row['pick_line_id'], performed_by=worker_code,
               qty=target, note='trip %s' % row['trip_id'])
    conn.commit()
    return True
  finally:
    conn.close()


@anvil.server.callable
def record_short_load(assignment_id, worker_code, qty_loaded,
                      discrepancy_code, note=None):
  """
    Less than the target went on. Requires a reason code (same vocabulary
    as pick shorts) and texts the Hub Managers immediately (D-3).
    """
  qty_loaded = float(qty_loaded)
  if qty_loaded < 0:
    raise Exception("Qty cannot be negative")
  if not discrepancy_code:
    raise Exception("Short load needs a reason (discrepancy code)")
  conn = db_config.get_nexus_connection()
  try:
    worker = _require_active_worker(conn, worker_code)
    row = _loadable_assignment(conn, assignment_id)
    target = float(row['target_qty'] or 0)
    if qty_loaded >= target:
      raise Exception("Qty %.2f is not short of the target %.2f - use the "
                      "normal load tap" % (qty_loaded, target))

    cursor = conn.cursor()
    cursor.execute(
      """
        UPDATE a
        SET a.loaded_qty = %s, a.loaded_by = %s, a.loaded_date = SYSDATETIME(),
            a.load_discrepancy_code = %s
        FROM dbo.transfer_trip_assignments a
        JOIN dbo.transfer_trips t ON t.trip_id = a.trip_id
        WHERE a.assignment_id = %s AND a.loaded_date IS NULL
          AND t.status = 'Finalized' AND t.shipped_date IS NULL
      """,
      (qty_loaded, worker_code, discrepancy_code, assignment_id)
    )
    if cursor.rowcount != 1:
      conn.rollback()
      raise Exception("Line already recorded (double tap?) - refresh")

    _log_event(conn, row['pick_id'], 'LINE_SHORT_LOAD',
               pick_line_id=row['pick_line_id'], performed_by=worker_code,
               qty=qty_loaded,
               note='%s; target %.2f; trip %s%s'
               % (discrepancy_code, target, row['trip_id'],
                  ('; ' + note) if note else ''))
    _log_agent_action(conn, 'LINE_SHORT_LOAD', row['sku'],
                      '%.2f' % target, '%.2f' % qty_loaded,
                      "Trip %s short-load by %s (%s): %s"
                      % (row['trip_id'], worker['display_name'], worker_code,
                         discrepancy_code))
    conn.commit()

    # D-3: text the Hub Managers (post-commit; failure never blocks).
    sms_sent = 0
    try:
      sms_sent = _notify_short_load(conn, row, worker, target, qty_loaded,
                                    discrepancy_code)
    except Exception as e:
      print("[load_dashboard] short-load SMS failed: %s" % e)
    return {'recorded': True, 'sms_sent': sms_sent}
  finally:
    conn.close()


@anvil.server.callable
def undo_line_load(assignment_id, worker_code):
  """Undo a load tap while the truck has not departed."""
  conn = db_config.get_nexus_connection()
  try:
    _require_active_worker(conn, worker_code)
    row = _loadable_assignment(conn, assignment_id)

    cursor = conn.cursor()
    cursor.execute(
      """
        UPDATE a
        SET a.loaded_qty = NULL, a.loaded_by = NULL, a.loaded_date = NULL,
            a.load_discrepancy_code = NULL
        FROM dbo.transfer_trip_assignments a
        JOIN dbo.transfer_trips t ON t.trip_id = a.trip_id
        WHERE a.assignment_id = %s AND a.loaded_date IS NOT NULL
          AND t.status = 'Finalized' AND t.shipped_date IS NULL
      """,
      (assignment_id,)
    )
    if cursor.rowcount != 1:
      conn.rollback()
      raise Exception("Line is not recorded as loaded")

    _log_event(conn, row['pick_id'], 'LINE_UNLOADED',
               pick_line_id=row['pick_line_id'], performed_by=worker_code,
               note='trip %s' % row['trip_id'])
    conn.commit()
    return True
  finally:
    conn.close()


@anvil.server.callable
def remove_line_from_truck(assignment_id, worker_code, reason):
  """
    Take a line off the truck before departure. The assignment row is
    deleted, the line returns to STAGED, and the rec reverts
    Trip_Assigned -> Applied when nothing else holds it on a trip.
    1.4.1: when the removed line was the last one the truck is cancelled
    and the result says so.
    """
  if not reason:
    raise Exception("Removing a line needs a reason")
  conn = db_config.get_nexus_connection()
  try:
    _require_active_worker(conn, worker_code)
    row = _loadable_assignment(conn, assignment_id)

    cursor = conn.cursor()
    cursor.execute(
      "SELECT COALESCE(volume_ft3, 0) AS v, COALESCE(weight_lb, 0) AS w "
      "FROM dbo.transfer_trip_assignments WHERE assignment_id = %s",
      (assignment_id,)
    )
    vw = cursor.fetchone() or {'v': 0, 'w': 0}

    # v1.3.0 (M3): the DELETE itself re-asserts that the truck is still
    # open, so a Remove confirmed after another worker departed the
    # truck is refused instead of deleting a line off a moving truck.
    cursor.execute(
      """
        DELETE a
        FROM dbo.transfer_trip_assignments a
        JOIN dbo.transfer_trips t ON t.trip_id = a.trip_id
        WHERE a.assignment_id = %s
          AND t.status = 'Finalized' AND t.shipped_date IS NULL
      """,
      (assignment_id,)
    )
    if cursor.rowcount != 1:
      conn.rollback()
      raise Exception("Line changed underneath you (truck departed?) - refresh")

    cursor.execute(
      """
        UPDATE dbo.transfer_trips
        SET sku_count = CASE WHEN sku_count > 0 THEN sku_count - 1 ELSE 0 END,
            total_volume_ft3 = CASE WHEN COALESCE(total_volume_ft3, 0) - %s > 0
                                    THEN total_volume_ft3 - %s ELSE 0 END,
            total_weight_lb  = CASE WHEN COALESCE(total_weight_lb, 0) - %s > 0
                                    THEN total_weight_lb - %s ELSE 0 END
        WHERE trip_id = %s
      """,
      (vw['v'], vw['v'], vw['w'], vw['w'], row['trip_id'])
    )

    # Rec reverts to Applied only when no LIVE trip still holds it
    # (v1.3.0: scoped to open/departed-undelivered trips - a delivered
    # assignment from months ago no longer pins the rec at Trip_Assigned).
    cursor.execute(
      """
        UPDATE dbo.ai_transfer_recommendations
        SET status = 'Applied'
        WHERE recommendation_id = %s
          AND status = 'Trip_Assigned'
          AND NOT EXISTS (SELECT 1
                          FROM dbo.transfer_trip_assignments t2
                          JOIN dbo.transfer_trips tr2 ON tr2.trip_id = t2.trip_id
                          WHERE t2.recommendation_id = %s
                            AND tr2.status <> 'Proposed'
                            AND tr2.received_date IS NULL)
      """,
      (row['recommendation_id'], row['recommendation_id'])
    )

    # 1.4.1: last line off -> the truck is empty. There is no depart or
    # delete path for an empty truck (depart refuses it), so it cancels
    # itself here and the dock goes back to the staged list.
    cursor.execute(
      "SELECT COUNT(*) AS n FROM dbo.transfer_trip_assignments WHERE trip_id = %s",
      (row['trip_id'],)
    )
    remaining = int((cursor.fetchone() or {'n': 0})['n'] or 0)
    truck_cancelled = False
    if remaining == 0:
      cursor.execute(
        """
          UPDATE dbo.transfer_trips
          SET status = 'Cancelled',
              sku_count = 0, total_volume_ft3 = 0, total_weight_lb = 0,
              notes = CONCAT(ISNULL(CAST(notes AS nvarchar(max)), ''),
                             ' | [', CONVERT(varchar(16), GETDATE(), 120),
                             '] Cancelled: last line removed at load time by ', %s, ' (', %s, ')')
          WHERE trip_id = %s AND status = 'Finalized' AND shipped_date IS NULL
        """,
        (worker_code, reason, row['trip_id'])
      )
      truck_cancelled = cursor.rowcount == 1
      if truck_cancelled:
        _log_agent_action(conn, 'TRUCK_CANCELLED_EMPTY', None,
                          'Trip %s Finalized' % row['trip_id'], 'Cancelled',
                          "Last line (%s) removed at load time by %s: %s - truck "
                          "cancelled, lines are back in the staged list"
                          % (row['sku'], worker_code, reason))

    _log_event(conn, row['pick_id'], 'LINE_REMOVED',
               pick_line_id=row['pick_line_id'], performed_by=worker_code,
               note='off trip %s: %s' % (row['trip_id'], reason))
    _log_agent_action(conn, 'LINE_REMOVED_FROM_TRUCK', row['sku'],
                      'Trip %s' % row['trip_id'], 'Staged',
                      "Removed at load time by %s: %s" % (worker_code, reason))
    conn.commit()
    return {'ok': True, 'trip_id': row['trip_id'], 'truck_cancelled': truck_cancelled}
  finally:
    conn.close()


# =============================================================================
# DEPART (step 3 - the gate)
# =============================================================================

@anvil.server.callable
def get_departure_checklist(trip_id):
  """
    What stands between this truck and departure: unresolved load lines,
    and the staged lines that will need a still-at-Hub acknowledgment.
    """
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      """
        SELECT a.assignment_id, a.sku,
               CAST(COALESCE(a.adjusted_qty, a.planned_qty, a.qty) AS DECIMAL(18,4)) AS target_qty
        FROM dbo.transfer_trip_assignments a
        WHERE a.trip_id = %s AND a.loaded_date IS NULL
      """,
      (trip_id,)
    )
    unresolved = [serialize_row(r) for r in cursor.fetchall()]
    return {'unresolved': unresolved, 'staged': get_staged_lines()}
  finally:
    conn.close()


@anvil.server.callable
def depart_truck(trip_id, worker_code, acknowledged_pick_line_ids=None):
  """
    Step 3. Refuses while any line is unresolved. Recomputes the STAGED
    set server-side; every staged line must appear in
    acknowledged_pick_line_ids or departure is refused with the missing
    list (the client shows the acknowledgment dialog and retries).
    Stamps shipped_date (state-guarded) and logs everything.
    """
  conn = db_config.get_nexus_connection()
  try:
    worker = _require_active_worker(conn, worker_code)
    acked = {int(i) for i in (acknowledged_pick_line_ids or [])}

    cursor = conn.cursor()
    cursor.execute(
      """
        SELECT a.assignment_id, a.sku, a.pick_line_id, pl.pick_id,
               CAST(a.loaded_qty AS DECIMAL(18,4)) AS loaded_qty,
               a.load_discrepancy_code
        FROM dbo.transfer_trip_assignments a
        LEFT JOIN dbo.transfer_pick_lines pl ON pl.pick_line_id = a.pick_line_id
        WHERE a.trip_id = %s
      """,
      (trip_id,)
    )
    trip_lines = list(cursor.fetchall())
    if not trip_lines:
      raise Exception("Trip %s has no lines - remove the truck instead" % trip_id)

    cursor.execute(
      "SELECT COUNT(*) AS n FROM dbo.transfer_trip_assignments "
      "WHERE trip_id = %s AND loaded_date IS NULL",
      (trip_id,)
    )
    unresolved = cursor.fetchone()['n']
    if unresolved > 0:
      raise Exception("%s line(s) not yet loaded (or shorted/removed) - "
                      "resolve them before departing" % unresolved)

    # The gate: every currently-staged line needs an explicit ack.
    staged = get_staged_lines()
    missing = [s for s in staged if int(s['pick_line_id']) not in acked]
    if missing:
      return {'departed': False, 'needs_ack': missing}

    if not _guarded_depart(conn, trip_id):
      conn.rollback()
      raise Exception("Truck %s is not open (already departed?) - refresh"
                      % trip_id)

    # v1.3.0 (C5, Dave 9/5): reconcile short / zero loads NOW, inside the
    # departure transaction, so every downstream reader (manifest reprint,
    # driver ladder, detection view credit, e21 comparison) sees what
    # physically left. The shortfall is NOT re-staged - Inventory Control
    # cycle-counts it.
    shortfalls = _reconcile_short_loads(conn, trip_id, trip_lines, worker_code)
    trip_lines = [l for l in trip_lines
                  if float(l['loaded_qty'] or 0) > 0]
    if not trip_lines:
      conn.rollback()
      raise Exception("Every line on truck %s was loaded at 0 - nothing to "
                      "depart. Remove the lines instead." % trip_id)

    for pick_id in sorted({l['pick_id'] for l in trip_lines if l['pick_id']}):
      _log_event(conn, pick_id, 'TRUCK_DEPARTED', performed_by=worker_code,
                 note='trip %s' % trip_id)

    staged_by_line = {int(s['pick_line_id']): s for s in staged}
    for line_id in sorted(staged_by_line):
      s = staged_by_line[line_id]
      _log_event(conn, s['pick_id'], 'STILL_AT_HUB_ACK',
                 pick_line_id=line_id, performed_by=worker_code,
                 qty=s['qty'],
                 note='acknowledged at departure of trip %s' % trip_id)

    shorts = sum(1 for l in trip_lines if l['load_discrepancy_code'])
    _log_agent_action(conn, 'TRUCK_DEPARTED', None,
                      'Finalized', 'Departed',
                      "Trip %s departed by %s (%s): %d line(s), %d short, "
                      "%d zero-loaded line(s) dropped, "
                      "%d staged line(s) acknowledged still at Hub"
                      % (trip_id, worker['display_name'], worker_code,
                         len(trip_lines), shorts,
                         sum(1 for x in shortfalls if x['loaded'] <= 0),
                         len(staged_by_line)))
    conn.commit()

    # Post-commit: one cycle-count SMS to Inventory Control for every
    # short / zero line (failure never blocks the departure).
    cc_sent = 0
    if shortfalls:
      try:
        cc_sent = _notify_dock_shortfall_cycle_count(conn, trip_id, shortfalls,
                                                     worker)
      except Exception as e:
        print("[load_dashboard] cycle-count SMS failed: %s" % e)
    return {'departed': True, 'lines': len(trip_lines), 'shorts': shorts,
            'dropped': sum(1 for x in shortfalls if x['loaded'] <= 0),
            'acknowledged': len(staged_by_line), 'cycle_count_sms': cc_sent}
  finally:
    conn.close()


def _guarded_depart(conn, trip_id):
  """v1.3.0 (C6): stamps shipped_date AND advances status to 'Departed'.
  Refuses if the driver has somehow already confirmed delivery."""
  cursor = conn.cursor()
  cursor.execute(
    """
      UPDATE dbo.transfer_trips
      SET shipped_date = GETDATE(), status = 'Departed'
      WHERE trip_id = %s AND status = 'Finalized'
        AND shipped_date IS NULL AND received_date IS NULL
    """,
    (trip_id,)
  )
  return cursor.rowcount == 1


def _reconcile_short_loads(conn, trip_id, trip_lines, worker_code):
  """
    v1.3.0 (C5). For every line whose loaded_qty is below its target:
      - loaded 0  -> the assignment is deleted (the line never left), and
                     the rec is Cancelled with a note when no other live
                     trip or picked line holds it (zero-pick rule at the
                     dock). The pick line itself is left as recorded; the
                     cycle count reconciles the shelf.
      - 0 < loaded < target -> tta.qty and tta.adjusted_qty become the
                     loaded qty (so manifest / driver / view / e21 agree);
                     the rec gets a note. The shortfall is NOT carried
                     forward - the detector re-raises if Starlite still
                     needs it.
    Returns a list of {sku, target, loaded, code, pick_id, pick_line_id}
    for the cycle-count SMS. Runs inside the caller's transaction.
    """
  cursor = conn.cursor()
  cursor.execute(
    """
      SELECT a.assignment_id, a.sku, a.recommendation_id, a.pick_line_id,
             pl.pick_id,
             CAST(COALESCE(a.adjusted_qty, a.planned_qty, a.qty) AS DECIMAL(18,4)) AS target_qty,
             CAST(a.loaded_qty AS DECIMAL(18,4)) AS loaded_qty,
             a.load_discrepancy_code
      FROM dbo.transfer_trip_assignments a
      LEFT JOIN dbo.transfer_pick_lines pl ON pl.pick_line_id = a.pick_line_id
      WHERE a.trip_id = %s
        AND a.loaded_qty IS NOT NULL
        AND a.loaded_qty < COALESCE(a.adjusted_qty, a.planned_qty, a.qty)
    """,
    (trip_id,)
  )
  shorts = list(cursor.fetchall())
  out = []
  for sh in shorts:
    target = float(sh['target_qty'] or 0)
    loaded = float(sh['loaded_qty'] or 0)
    code = sh['load_discrepancy_code'] or 'SHORT'
    out.append({'sku': sh['sku'], 'target': target, 'loaded': loaded,
                'code': code, 'pick_id': sh['pick_id'],
                'pick_line_id': sh['pick_line_id']})

    if loaded <= 0:
      cursor.execute(
        "DELETE FROM dbo.transfer_trip_assignments WHERE assignment_id = %s",
        (sh['assignment_id'],)
      )
      cursor.execute(
        "UPDATE dbo.transfer_trips SET sku_count = "
        "CASE WHEN sku_count > 0 THEN sku_count - 1 ELSE 0 END WHERE trip_id = %s",
        (trip_id,)
      )
      # Cancel the rec when nothing else keeps it alive.
      cursor.execute(
        """
          UPDATE dbo.ai_transfer_recommendations
          SET status = 'Cancelled', reviewed_date = GETDATE(),
              notes = CONCAT(ISNULL(CAST(notes AS nvarchar(max)), ''),
                             ' | [', CONVERT(varchar(16), GETDATE(), 120),
                             '] ZERO LOAD at dock on trip ', %s, ' (', %s,
                             '): 0 of ', %s, ' loaded by ', %s,
                             '. Cancelled per zero-pick rule; Inventory Control to cycle count; detector re-raises if still needed.')
          WHERE recommendation_id = %s
            AND status IN ('Trip_Assigned', 'Applied')
            AND NOT EXISTS (SELECT 1
                            FROM dbo.transfer_trip_assignments t2
                            JOIN dbo.transfer_trips tr2 ON tr2.trip_id = t2.trip_id
                            WHERE t2.recommendation_id = %s
                              AND tr2.status <> 'Proposed'
                              AND tr2.received_date IS NULL)
            AND NOT EXISTS (SELECT 1 FROM dbo.transfer_pick_lines pl2
                            WHERE pl2.recommendation_id = %s
                              AND pl2.pick_line_id <> %s
                              AND pl2.status IN ('Assigned', 'Picked', 'Short')
                              AND (pl2.status = 'Assigned' OR pl2.qty_picked > 0))
        """,
        (trip_id, code, '%g' % target, worker_code,
         sh['recommendation_id'], sh['recommendation_id'],
         sh['recommendation_id'], sh['pick_line_id'] or -1)
      )
      rec_cancelled = cursor.rowcount == 1
      if sh['pick_id']:
        _log_event(conn, sh['pick_id'], 'LINE_NOT_LOADED',
                   pick_line_id=sh['pick_line_id'], performed_by=worker_code,
                   qty=0,
                   note='trip %s departed without this line (%s, target %g)%s'
                   % (trip_id, code, target,
                      '; rec cancelled' if rec_cancelled else ''))
      _log_agent_action(conn, 'DOCK_ZERO_LOAD', sh['sku'],
                        '%g on trip %s' % (target, trip_id),
                        'dropped' + (', rec Cancelled' if rec_cancelled else ''),
                        "Rec %s: loaded 0 (%s) by %s; cycle count requested"
                        % (sh['recommendation_id'], code, worker_code))
    else:
      cursor.execute(
        """
          UPDATE dbo.transfer_trip_assignments
          SET qty = %s, adjusted_qty = %s,
              adjusted_by = %s, adjusted_date = GETDATE()
          WHERE assignment_id = %s
        """,
        (loaded, loaded, 'dock_short:' + str(worker_code)[:30],
         sh['assignment_id'])
      )
      cursor.execute(
        """
          UPDATE dbo.ai_transfer_recommendations
          SET notes = CONCAT(ISNULL(CAST(notes AS nvarchar(max)), ''),
                             ' | [', CONVERT(varchar(16), GETDATE(), 120),
                             '] SHORT LOAD on trip ', %s, ' (', %s, '): ', %s,
                             ' of ', %s, ' loaded by ', %s,
                             '. Shipped qty reduced to match; shortfall to Inventory Control cycle count; detector re-raises if still needed.')
          WHERE recommendation_id = %s
        """,
        (trip_id, code, '%g' % loaded, '%g' % target, worker_code,
         sh['recommendation_id'])
      )
      if sh['pick_id']:
        _log_event(conn, sh['pick_id'], 'DOCK_SHORTFALL',
                   pick_line_id=sh['pick_line_id'], performed_by=worker_code,
                   qty=target - loaded,
                   note='trip %s: %g of %g loaded (%s); shipped qty set to %g'
                   % (trip_id, loaded, target, code, loaded))
      _log_agent_action(conn, 'DOCK_SHORTFALL', sh['sku'],
                        '%g' % target, '%g' % loaded,
                        "Rec %s: trip %s shipped %g of %g (%s); cycle count requested"
                        % (sh['recommendation_id'], trip_id, loaded, target, code))
  return out


def _notify_dock_shortfall_cycle_count(conn, trip_id, shortfalls, worker):
  """
    v1.3.0: one SMS to the SHORT_PICK_CYCLE_COUNT contacts (Inventory
    Control) listing every short / zero-loaded line on a departed truck.
    Same channel and shape as the pick-stage E3 text.
    """
  if not shortfalls or not SMSConfig.is_sms_enabled():
    return 0
  contacts = get_hub_manager_phones(conn, 'SHORT_PICK_CYCLE_COUNT')
  if not contacts:
    print("[load_dashboard] dock shortfalls on trip %s but no "
          "SHORT_PICK_CYCLE_COUNT contact configured" % trip_id)
    return 0

  lines = []
  for x in shortfalls[:6]:
    lines.append("  %s: loaded %g of %g (%s)"
                 % (x['sku'], x['loaded'], x['target'], x['code']))
  if len(shortfalls) > 6:
    lines.append("  + %d more" % (len(shortfalls) - 6))
  message = ("DOCK SHORT LOAD - CYCLE COUNT NEEDED\n"
             "Trip %s departed with %d short/zero line(s) (by %s). Please "
             "cycle count these Hub SKUs now:\n%s"
             % (trip_id, len(shortfalls), worker['display_name'],
                "\n".join(lines)))
  sent = 0
  client = TwilioSMSClient()
  for c in contacts:
    try:
      result = client.send_sms(c['phone'], message)
      log_sms_notification(conn, result, 'SHORT_PICK_CYCLE_COUNT',
                           related_skus=[x['sku'] for x in shortfalls])
      if result.get('success'):
        sent += 1
    except Exception as e:
      print("[load_dashboard] cycle-count SMS to %s failed: %s"
            % (c.get('name'), e))
  return sent


# =============================================================================
# MANIFEST + LOOKUPS
# =============================================================================

@anvil.server.callable
def download_manifest(trip_id):
  """The truck's manifest PDF - exists only because FINALIZE happened."""
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      "SELECT batch_id FROM dbo.transfer_trips WHERE trip_id = %s",
      (trip_id,)
    )
    row = cursor.fetchone()
    if not row:
      raise Exception("Trip %s not found" % trip_id)
    batch_id = row['batch_id']
  finally:
    conn.close()

  from .trip_pdf_generator import generate_trip_pdf
  return generate_trip_pdf(batch_id)


@anvil.server.callable
def get_dock_capacity():
  """
    Truck capacity limits for the dock grid's live utilization display -
    the SAME parameters the optimizer packs to (global_parameters
    truck_cap_ft3 / truck_cap_lb, optimizer defaults on miss).
    """
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      """
        SELECT parameter_name, parameter_value
        FROM dbo.global_parameters
        WHERE parameter_name IN ('truck_cap_ft3', 'truck_cap_lb')
      """
    )
    params = {r['parameter_name']: float(r['parameter_value'])
              for r in cursor.fetchall()}
    return {
      'volume_ft3': float(params.get('truck_cap_ft3', 1456.0)),
      'weight_lb': float(params.get('truck_cap_lb', 13000.0)),
    }
  finally:
    conn.close()


@anvil.server.callable
def get_load_discrepancy_codes():
  """Same reason-code vocabulary the kiosk pick flow uses."""
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


# =============================================================================
# SMS (D-3)
# =============================================================================

def _notify_short_load(conn, assignment_row, worker, target, qty_loaded,
                       discrepancy_code):
  """One immediate SMS to Hub Managers for a short-load (D-3)."""
  if not SMSConfig.is_sms_enabled():
    return 0
  managers = get_hub_manager_phones(conn, 'LOAD_EXCEPTIONS') \
  or get_hub_manager_phones(conn, 'PICK_EXCEPTIONS') \
  or get_hub_manager_phones(conn, 'NEW_RECOMMENDATIONS')
  if not managers:
    return 0

  message = ("SHORT LOAD on trip %s: %s loaded %g of %g (%s) by %s"
             % (assignment_row['trip_id'], assignment_row['sku'],
                qty_loaded, target, discrepancy_code,
                worker['display_name']))
  sent = 0
  client = TwilioSMSClient()
  for m in managers:
    try:
      result = client.send_sms(m['phone'], message)
      log_sms_notification(conn, result, 'LOAD_EXCEPTIONS',
                           related_skus=[assignment_row['sku']])
      if result.get('success'):
        sent += 1
    except Exception as e:
      print("[load_dashboard] SMS to %s failed: %s" % (m.get('name'), e))
  return sent