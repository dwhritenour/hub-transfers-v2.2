"""
Transfer Truck Optimizer - Anvil Server Module
===============================================
Organizes approved transfers into efficient truck trips using bin-packing algorithm.
Creates proposed trip assignments for Hub Manager review.

To Use:
    1. Add this code to your Anvil Server Module
    2. Call from a form button: anvil.server.call('run_truck_optimizer')
    3. Or with options: anvil.server.call('run_truck_optimizer', planned_date='2026-01-15', dry_run=True)

Author: Dave Whritenour / Claude AI Assistant
Project: Titan Flow Control - Year of Automation
Created: January 2026
"""

# =============================================================================
# Version:       5.20.0
# Last updated:  2026-06-12
# Changelog (this module only):
#   5.20.0 (2026-06-12) e21-fulfillment auto-cascade (order 600754
#                        incident): recs approved in the morning were
#                        shipped directly through e21 by the Hub before
#                        the optimizer packed them (Hub workflow drift).
#                        The v5.18 pack guard correctly skipped them
#                        (live Hub OH 0 = goods on the truck — WORKING
#                        AS INTENDED, prevented a double-ship), but the
#                        recs then sat 'Applied' forever, double-counted
#                        in the detection view (starlite_addition_active
#                        AND e21_in_transit_qty), and would have been
#                        packed + double-shipped once Hub on-PO stock
#                        arrived and live availability went positive.
#                        NEW _cascade_e21_fulfilled_recs pre-pack pass
#                        (runs BEFORE the v5.9 dedupe so a fulfilled rec
#                        can't be chosen as dedupe keeper): any Applied/
#                        Auto-Approved rec whose SKU has a SHIPPED
#                        (TRY_CAST(ord_item_status) >= 16) e21 transfer
#                        order with qty >= rec qty entered on/after the
#                        rec's review date is cascaded -> 'Delivered'
#                        (transfer happened; Cancelled would be false
#                        history) with note + E21_FULFILLED_CASCADE log
#                        row. Un-shipped lines (status 00/10) NEVER
#                        match — backorder-stub pattern; an entered
#                        order can still be cancelled. Surfaced in the
#                        optimizer status message and the e21_cascaded
#                        return key. One-time manual precursor for recs
#                        1514/1516/1517:
#                        sql/cascade_e21_fulfilled_recs_20260612.sql.
#   5.19.0 (2026-06-10) NEW callable cancel_zero_stock_recs — thin wrapper
#                        around the new Nexus stored procedure
#                        dbo.sp_cancel_zero_hub_stock_recs (see
#                        sql/create_sp_cancel_zero_hub_stock_recs.sql).
#                        Wired to btn_clear_zero_stock on trip_editor
#                        v5.19: the form calls dry_run=True to preview
#                        the candidate list for the confirm dialog,
#                        then dry_run=False to execute. Closes the
#                        v5.18 gap where pack-time-skipped recs (live
#                        Hub available <= 0, status Applied/
#                        Auto-Approved) were unreachable by the qty-0
#                        path because they never enter a batch. Safe by
#                        design: detection ignores zero-balance Hub
#                        SKUs, so a fresh rec is raised automatically
#                        when stock returns — cancelling never loses
#                        demand. Supersedes the one-time backfill
#                        sql/cancel_zero_stock_applied_recs_20260610.sql.
#   5.18.1 (2026-06-10) BUG FIX: first live run of v5.18.0 threw
#                        anvil.server.SerializationError — the
#                        skipped_no_stock list put transfer_qty into the
#                        run_truck_optimizer return value as
#                        decimal.Decimal (pymssql's native type for
#                        DECIMAL columns), which Anvil cannot serialize
#                        across the server/client boundary. Server-side
#                        work had already committed (batch + log rows
#                        were fine); only the response failed. Fix:
#                        int()/float() casts at dict construction.
#                        Lesson: anything destined for an
#                        @anvil.server.callable return value must be
#                        cast to plain Python types at the point it
#                        leaves the cursor — same convention the rest
#                        of this module already follows (see
#                        get_batch_details_flat's float()/int() casts).
#   5.18.0 (2026-06-10) Zero-Hub-stock handling (CV42LCSM0600 incident,
#                        2026-06-09/10):
#                        Background: a rec was approved, the pick slip
#                        printed, but the piece could not be physically
#                        located at the Hub. Cycle count zeroed e21;
#                        EasyMorph zeroed inventory_current. The rec
#                        then became IMMORTAL: get_eligible_transfers
#                        filters only on status+qty (never live stock),
#                        so every optimizer run re-packed it, and both
#                        exits available to the Hub Manager (leave
#                        unselected at partial finalize, or Cancel
#                        Batch) revert the rec to 'Applied' — straight
#                        back into the next run. No terminal
#                        "can't fulfill" path existed anywhere in the
#                        lifecycle. (The v5.12.1 dedupe even masked the
#                        symptom by auto-obsoleting newer duplicate
#                        recs 1387/1393 against immortal rec 1362.)
#                        Two changes:
#                        (1) NEW callable remove_assignment_cancel_rec:
#                            Hub Manager enters qty 0 in trip_editor →
#                            RowTemplate2 v5.18 confirms, then this
#                            deletes the tta row, recalcs (or deletes)
#                            the trip header, and moves the rec from
#                            any ACTIVE status to terminal 'Cancelled'
#                            with an appended note. Logs
#                            ASSIGNMENT_CANCELLED to ai_agent_log.
#                            'Cancelled' is excluded from the
#                            vw_unified_transfer_detection active
#                            allow-list automatically (it only credits
#                            Auto-Approved/Approved/Applied/
#                            Trip_Assigned/Finalized), so the phantom
#                            +qty drops out of effective on-hand — the
#                            unit isn't coming. Companion one-line
#                            change in pick_list_pdf_generator v5.18
#                            adds 'Cancelled' to the blocked-status
#                            deny-list.
#                        (2) Pack-time guard in run_truck_optimizer:
#                            recs whose LIVE Hub available is <= 0
#                            (LEFT JOIN inventory_current, missing row
#                            = 0 — EasyMorph drops the Hub row
#                            entirely when zeroed) are skipped before
#                            packing, stay 'Applied', and are reported
#                            in the status message + one
#                            SKIPPED_NO_HUB_STOCK ai_agent_log row per
#                            rec. They resurface automatically when
#                            stock returns, or can be cancelled via
#                            the qty-0 path / Transfer Approvals.
#   5.12.1 (2026-05-13) BUG FIX: delete_batch's cascade-revert (v5.9.0)
#                        was a silent no-op since the day it shipped.
#                        The SELECT used
#                          WHERE batch_id = %s AND status = 'Trip_Assigned'
#                        against ai_transfer_recommendations, but
#                        rec.batch_id is the AI review batch_id (e.g.
#                        '20260512-548dfc5f') while the function is
#                        called with the trip's batch_id (e.g.
#                        '20260513_141007'). Those columns hold
#                        completely different identifier spaces and
#                        never match, so the cascade returned zero
#                        rows on every Cancel Batch action and the
#                        downstream tta + trip DELETEs proceeded to
#                        orphan every Trip_Assigned rec on the batch.
#                        Orphans are invisible to the next optimizer
#                        run (which filters on status='Applied') and
#                        pad starlite_addition in the v5.6 view
#                        (LEFT JOIN to tta returns NULL, the ISNULL
#                        defensive default treats them as Proposed and
#                        counts them as virtual inventory). 11 orphans
#                        accumulated between 2026-04-23 and 2026-05-13;
#                        cleanup in
#                        sql/cleanup_orphan_trip_assigned_recs.sql +
#                        sql/cleanup_orphan_trip_assigned_recs_followup.sql.
#                        Fix: JOIN through transfer_trip_assignments to
#                        find the recs actually linked to the trip,
#                        same pattern revert_proposed_batches already
#                        uses correctly. ~12 lines changed inside
#                        delete_batch; consolidation path was already
#                        correct and is untouched.
#   5.12.0 (2026-05-04) * Dedupe filter: _dedupe_same_sku_applied_recs
#                        now excludes qty=0 ("approved no-op") recs from
#                        both the keeper-detection set AND the
#                        duplicate-detection set. Fixes the YS58D0800
#                        slow-bleed bug where ancient rec 404 (qty=0,
#                        Applied since 2026-04-02) silently obsoleted
#                        three subsequent fresh recs (666, 694, 731)
#                        for a total of 60 nullified units between
#                        2026-04-29 and 2026-05-04. SQL filter is in
#                        both the GROUP BY and the per-SKU SELECT so
#                        no caller path can bypass it.
#   5.9.0 (2026-04-23) * delete_batch cascade-revert: recs stuck at
#                        status='Trip_Assigned' when a batch is canceled
#                        are now auto-reverted to 'Applied' so they stay
#                        visible to the next optimizer run. Prior to v5.9
#                        canceling a batch left recs orphaned, invisible
#                        in both Trip Editor and Transfer Approvals —
#                        required manual SQL cleanup (see 2026-04-23
#                        YS12I0150 incident).
#                      * Optimizer dedupe-by-SKU: before packing, any SKU
#                        with two or more live Applied/Auto-Approved recs
#                        has its NEWER duplicates auto-obsoleted. Oldest
#                        rec wins (preserves review history). Prevents
#                        double-shipping the same SKU in one batch.
#                      * New action_types in ai_agent_log:
#                        BATCH_DELETED (cascade summary) and
#                        DUPLICATE_OBSOLETED (one row per obsoleted rec).
#   5.8.1 (2026-04-23) carve planned_time HH:MM truncation fix; clearer
#                      run_truck_optimizer message when Proposed batch
#                      already covers all Applied recs.
#   5.8.0 (2026-04-23) * finalize_selected_assignments callable — partial
#                        "carve and finalize" flow for the trip_editor
#                        checkbox column.
#                      * Layer 3 log stamping: BATCH_FINALIZED reason now
#                        includes (v{__version__}); new BATCH_PARTIAL_FINALIZED
#                        action_type for the carve path.
#   5.7.0 (2026-04-22) Trip_Assigned -> Finalized cascade in finalize_batch.
#   5.6.0 (2026-04-21) Effective-on-hand rollout (view-driven virtual supply).
# =============================================================================
__version__       = "5.28.0"
__version_date__  = "2026-06-12"
__version_notes__ = (
  "e21-fulfillment auto-cascade: pre-pack pass moves Applied recs whose "
  "SKU already shipped on an e21 transfer order (status >= 16, qty >= rec "
  "qty, entered on/after review date) to 'Delivered' — kills the "
  "double-ship-on-restock risk from Hub e21 direct-entry drift "
  "(order 600754 incident, 2026-06-12)."
)

import anvil.server
import math
from datetime import datetime, timedelta
import io
import csv
import anvil.media

# =============================================================================
# DATABASE CONNECTION
# =============================================================================
# Adjust this import based on your Anvil project structure.
# Option 1: If you have a db_config module in Anvil
# from . import db_config
# conn = db_config.get_nexus_connection()

# =============================================================================
# CONFIGURATION
# =============================================================================
from .db_config import get_nexus_connection as get_db_connection

DEFAULT_TRIP_TIME = "08:00"

# Priority Mappings: (event_type, urgency) -> (priority_sort, priority_code)
PRIORITY_MAP = {
  # Type 2 (Sales Order Driven) - uses time_urgency
  (2, 'CRITICAL'): (1, '1-CRITICAL'),
  (2, 'HIGH'): (2, '2-HIGH'),
  (2, 'MEDIUM'): (4, '4-MEDIUM'),
  (2, 'LOW'): (6, '6-LOW'),
  (2, 'NO DATE'): (6, '6-LOW'),
  # Type 1 (Min/Max Replenishment) - uses inventory_urgency
  (1, 'CRITICAL'): (3, '3-INV_CRIT'),
  (1, 'WARNING'): (5, '5-WARNING'),
  (1, 'LOW'): (7, '7-ROUTINE'),
  (1, 'OK'): (7, '7-ROUTINE'),
  (1, None): (7, '7-ROUTINE'),
}


# =============================================================================
# DATABASE READ FUNCTIONS
# =============================================================================

def get_truck_capacity(conn):
  """Get truck capacity limits from global_parameters."""
  cursor = conn.cursor()
  cursor.execute("""
        SELECT parameter_name, parameter_value
        FROM global_parameters
        WHERE parameter_name IN ('truck_cap_ft3', 'truck_cap_lb')
    """)

  params = {}
  for row in cursor.fetchall():
    params[row['parameter_name']] = float(row['parameter_value'])

  return {
    'volume_ft3': float(params.get('truck_cap_ft3', 1456.0)),
    'weight_lb': float(params.get('truck_cap_lb', 13000.0))
  }


# Update recommendation status so they aren't reprocessed
def update_recommendation_status(conn, recommendation_ids, new_status):
  """Update status of processed recommendations."""
  if not recommendation_ids:
    return
  cursor = conn.cursor()
  placeholders = ','.join(['%s'] * len(recommendation_ids))
  cursor.execute(f"""
        UPDATE ai_transfer_recommendations
        SET status = %s
        WHERE recommendation_id IN ({placeholders})
    """, (new_status, *recommendation_ids))
  conn.commit()

def get_eligible_transfers(conn):
  """v5.28: Get COMPLETED PICK LINES ready for trip assignment.

  One candidate per un-tripped picked line (qty = qty_picked) - the
  physically confirmed quantity, per decision C1/Option A. The old
  Applied-rec query is gone; a rec can no longer reach a truck without
  passing through a completed pick.
  """
  cursor = conn.cursor()
  # v5.28 (C1/Option A): eligibility is LINE-based. Only lines of
  # COMPLETED picks, physically picked (qty_picked > 0) and not yet on
  # any trip assignment, feed the packer. qty = qty_picked. The rec
  # supplies urgency/context; Cancelled/Obsolete recs are excluded so a
  # cancelled rec's staged lines never ship.
  query = """
        SELECT
            pl.pick_line_id,
            pl.recommendation_id,
            r.sku,
            r.event_type,
            r.base_urgency,
            r.time_urgency,
            r.inventory_urgency,
            r.hub_available,
            r.hub_on_hand,
            r.starlite_available,
            r.min_stock,
            r.max_stock,
            pl.qty_picked as transfer_qty,
            r.batch_id
        FROM transfer_pick_lines pl
        JOIN transfer_picks p ON p.pick_id = pl.pick_id
        JOIN ai_transfer_recommendations r
          ON r.recommendation_id = pl.recommendation_id
        WHERE p.status = 'Completed'
          AND pl.status IN ('Picked', 'Short')
          AND pl.qty_picked > 0
          AND r.status IN ('Applied', 'Trip_Assigned')
          AND NOT EXISTS (SELECT 1 FROM transfer_trip_assignments t
                          WHERE t.pick_line_id = pl.pick_line_id)
        ORDER BY pl.pick_line_id
    """
  cursor.execute(query)

  transfers = []
  for row in cursor.fetchall():
    transfers.append({
      'pick_line_id': row['pick_line_id'],
      'recommendation_id': row['recommendation_id'],
      'sku': row['sku'],
      'event_type': row['event_type'],
      'base_urgency': row['base_urgency'],
      'time_urgency': row['time_urgency'],
      'inventory_urgency': row['inventory_urgency'],
      'hub_available': row['hub_available'],
      'hub_on_hand': row['hub_on_hand'],
      'starlite_available': row['starlite_available'],
      'min_stock': row['min_stock'],
      'max_stock': row['max_stock'],
      'transfer_qty': row['transfer_qty'],
      'batch_id': row['batch_id']
    })
  return transfers


def get_part_specs(conn, skus):
  """Get volume and weight specs for SKUs."""
  if not skus:
    return {}
  cursor = conn.cursor()
  placeholders = ','.join(['%s' for _ in skus])
  query = f"""
        SELECT
            sku,
            COALESCE(act_volume_ft3, 0) as act_volume_ft3,
            COALESCE(volume_factor, 1) as volume_factor,
            COALESCE(act_weight_lb, 0) as act_weight_lb,
            COALESCE(weight_factor, 1) as weight_factor
        FROM part_specs
        WHERE sku IN ({placeholders})
    """
  cursor.execute(query, tuple(skus))

  specs = {}
  for row in cursor.fetchall():
    specs[row['sku']] = {
      'unit_volume_ft3': float(row['act_volume_ft3']) * float(row['volume_factor']),
      'unit_weight_lb': float(row['act_weight_lb']) * float(row['weight_factor'])
    }
  return specs


def get_hub_locations(conn, skus):
  """Get primary location codes for SKUs at Hub."""
  if not skus:
    return {}
  cursor = conn.cursor()
  placeholders = ','.join(['%s' for _ in skus])
  # 2026-09-10 (Hub policy): never the ON HOLD location; largest released bin wins.
  try:
    from .pick_dashboard import _hold_location
    hold_loc = _hold_location()
  except Exception:
    hold_loc = '100'
  query = f"""
        SELECT sku, location_code
        FROM hub_product_locations
        WHERE sku IN ({placeholders})
          AND primary_location = 1
          AND location_code <> %s
        ORDER BY sku, location_qty ASC, location_code DESC
    """
  cursor.execute(query, tuple(skus) + (hold_loc,))

  locations = {}
  for row in cursor.fetchall():
    locations[row['sku']] = row['location_code']
  return locations


def get_hub_inventory(conn, skus):
  """Get current Hub on-hand quantities for SKUs."""
  if not skus:
    return {}
  cursor = conn.cursor()
  placeholders = ','.join(['%s' for _ in skus])
  query = f"""
        SELECT sku, on_hand
        FROM inventory_current
        WHERE sku IN ({placeholders})
          AND cost_center = 'Hub'
    """
  cursor.execute(query, tuple(skus))

  inventory = {}
  for row in cursor.fetchall():
    inventory[row['sku']] = float(row['on_hand']) if row['on_hand'] else 0
  return inventory


def get_hub_availability(conn, skus):
  """v5.18: Get LIVE Hub available + on_hand for SKUs.

  Returns {sku: {'available': float, 'on_hand': float}} with an entry
  for EVERY requested SKU. SKUs with no Hub row in inventory_current
  come back as 0/0 — this matters because EasyMorph drops the Hub row
  entirely when a SKU is cycle-counted to zero (observed with
  CV42LCSM0600 on 2026-06-09), so a missing row IS the zero-stock
  signal, not an error.

  Gate on 'available' (= On Hand − Committed per the e21 formula):
  if on_hand > 0 but available <= 0, the physical stock is committed
  to Hub customer orders and shouldn't be packed onto a truck either.
  This matches the detection view's 'Hub Empty' test
  (ISNULL(hub.available, 0) = 0).
  """
  result = {sku: {'available': 0.0, 'on_hand': 0.0} for sku in skus}
  if not skus:
    return result
  cursor = conn.cursor()
  placeholders = ','.join(['%s' for _ in skus])
  cursor.execute(f"""
        SELECT sku,
               ISNULL(available, 0) AS available,
               ISNULL(on_hand, 0)  AS on_hand
        FROM inventory_current
        WHERE sku IN ({placeholders})
          AND cost_center = 'Hub'
    """, tuple(skus))
  for row in cursor.fetchall():
    result[row['sku']] = {
      'available': float(row['available'] or 0),
      'on_hand': float(row['on_hand'] or 0),
    }
  return result


def _cascade_e21_fulfilled_recs(conn):
  """v5.20: Auto-resolve Applied/Auto-Approved recs already fulfilled
  outside the app via a SHIPPED e21 transfer order.

  Incident (2026-06-12, order 600754): three transfers were approved in
  the morning; before the optimizer packed them, the Hub processed the
  transfer directly in e21 (the known Hub-does-the-full-e21-lifecycle
  workflow drift). EasyMorph then showed Hub on_hand 0, the v5.18 pack
  guard correctly skipped the recs (preventing a double-ship) — but the
  recs sat 'Applied' forever, double-counted in the detection view
  (starlite_addition_active AND e21_in_transit_qty), and would have been
  PACKED AND DOUBLE-SHIPPED the moment Hub replenishment (on-PO stock)
  arrived and live availability went positive.

  Match rule — a rec is considered e21-fulfilled when hub_transfer_orders
  has a row for the same SKU with:
    * ord_item_status >= 16 (SHIPPED — deliberately excludes 00/10
      entered-not-shipped lines; un-shipped e21 lines can linger at
      status 10 forever per the backorder-stub pattern, and an entered
      order can still be cancelled),
    * transfer_qty >= the rec's resolved qty (the Hub ships at least
      what the rec asked; e21 sometimes rounds up, e.g. 8 vs rec 4 on
      CV42SSV0200),
    * date_entered on/after the rec's review date (date-level compare —
      hub_transfer_orders.date_entered has no time component), so an
      old unrelated shipment can't resolve a fresh rec.

  Action: rec -> 'Delivered' (NOT Cancelled — the transfer happened;
  precedent: STALE_TRIP_ASSIGNED_CASCADE 2026-04-28) with an appended
  note + one E21_FULFILLED_CASCADE ai_agent_log row per rec.

  Returns: list of {'recommendation_id', 'sku', 'rec_qty', 'order_num',
  'e21_qty'} for the optimizer status message. Empty list = nothing to do.
  """
  cursor = conn.cursor()

  # One row per rec: smallest matching shipped order for the audit note.
  cursor.execute("""
      SELECT r.recommendation_id,
             r.sku,
             r.status AS old_status,
             COALESCE(r.manager_override_qty, r.ai_recommended_qty,
                      r.calculated_transfer_qty) AS rec_qty,
             (SELECT MIN(h2.order_num)
              FROM hub_transfer_orders h2
              WHERE h2.sku = r.sku
                AND TRY_CAST(h2.ord_item_status AS INT) >= 16
                AND h2.transfer_qty >= COALESCE(r.manager_override_qty,
                                                r.ai_recommended_qty,
                                                r.calculated_transfer_qty)
                AND h2.date_entered >= CAST(r.reviewed_date AS DATE)
             ) AS order_num
      FROM ai_transfer_recommendations r
      WHERE r.status IN ('Applied', 'Auto-Approved')
        AND COALESCE(r.manager_override_qty, r.ai_recommended_qty,
                     r.calculated_transfer_qty) > 0
        AND EXISTS (
            SELECT 1 FROM hub_transfer_orders hto
            WHERE hto.sku = r.sku
              AND TRY_CAST(hto.ord_item_status AS INT) >= 16
              AND hto.transfer_qty >= COALESCE(r.manager_override_qty,
                                               r.ai_recommended_qty,
                                               r.calculated_transfer_qty)
              AND hto.date_entered >= CAST(r.reviewed_date AS DATE)
        )
        AND NOT EXISTS (
            SELECT 1 FROM transfer_pick_lines pl
            WHERE pl.recommendation_id = r.recommendation_id
              AND pl.status IN ('Assigned', 'Picked', 'Short')
        )
  """)
  matches = cursor.fetchall()
  if not matches:
    return []

  cascaded = []
  for m in matches:
    rec_id = int(m['recommendation_id'])
    sku = m['sku']
    rec_qty = int(float(m['rec_qty'] or 0))
    order_num = str(m['order_num'] or '?').strip()

    note = (
      f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M')}] "
      f"E21_FULFILLED_CASCADE: transfer fulfilled outside the app via "
      f"shipped e21 transfer order {order_num} before the optimizer "
      f"packed it. Cascaded {m['old_status']} -> Delivered to prevent "
      f"double-ship when Hub restocks. (v{__version__})"
    )
    # State-guarded UPDATE (v5.9 lesson) — only flips if still active.
    cursor.execute("""
        UPDATE ai_transfer_recommendations
        SET status = 'Delivered',
            notes = CONCAT(COALESCE(notes, ''), %s)
        WHERE recommendation_id = %s
          AND status IN ('Applied', 'Auto-Approved')
          AND NOT EXISTS (SELECT 1 FROM transfer_pick_lines pl
                          WHERE pl.recommendation_id = ai_transfer_recommendations.recommendation_id
                            AND pl.status IN ('Assigned', 'Picked', 'Short'))
    """, (note, rec_id))
    if cursor.rowcount == 0:
      continue  # concurrent change — skip, don't log

    cursor.execute("""
        INSERT INTO ai_agent_log (
            action_type, sku, old_value, new_value, reason, executed_by
        )
        VALUES ('E21_FULFILLED_CASCADE', %s, %s, 'Delivered', %s,
                'truck_optimizer')
    """, (
      sku,
      m['old_status'],
      (
        f"Rec {rec_id} (qty {rec_qty}) fulfilled via shipped e21 transfer "
        f"order {order_num} (Hub direct-entry drift) before optimizer "
        f"pack. Auto-cascaded to Delivered. (v{__version__})"
      ),
    ))
    cascaded.append({
      'recommendation_id': rec_id,
      'sku': sku,
      'rec_qty': rec_qty,
      'order_num': order_num,
    })

  conn.commit()
  return cascaded


def _dedupe_same_sku_applied_recs(conn):
  """v5.9: Collapse duplicate live Applied/Auto-Approved recs.

  If two or more recs exist at status='Applied' (or 'Auto-Approved')
  for the same SKU, packing both would ship the same SKU twice in one
  batch. This happened on 2026-04-23 with YS12I0150: v5.5 auto-paused
  the newer duplicate, a reviewer un-paused it, and the optimizer
  packed both recs into the same trip.

  Rule: keep the oldest rec (lowest recommendation_id — created first,
  carries the earliest review history), obsolete the newer recs with
  an audit note and one ai_agent_log row per obsoleted rec.

  v5.12: Filter out qty=0 ("approved no-op") recs from both the keeper
  set AND the duplicate-detection set. Without this filter, an ancient
  qty=0 Applied rec — created when the Hub Manager overrode AI qty to
  0 to "approve no transfer" — sits forever and silently obsoletes
  every fresh meaningful rec for the same SKU on every optimizer run.
  This bit YS58D0800 three times between 2026-04-29 and 2026-05-04 (60
  units of nullified transfers via rec 404, qty=0 from 2026-04-02). A
  qty=0 rec is not a real transfer; it should never be a dedupe keeper
  and should never count as a duplicate of anything. The qty filter
  lives directly in the SQL so it can't be bypassed by callers.

  Called from run_truck_optimizer right before fetching the eligible
  transfer list, so the list it returns is already deduped.

  Returns a list of dedupe outcomes, one per affected SKU:
      [{'sku': 'YS12I0150', 'kept_id': 616, 'obsoleted_ids': [620]}, ...]
  Empty list when nothing needed to change.
  """
  cursor = conn.cursor()

  # Find SKUs with more than one live rec. This is a small query — the
  # set of truly duplicated SKUs on any given day is typically zero or
  # one. Do it as a GROUP BY rather than a self-join so the expensive
  # work (picking the keeper) only runs when duplicates actually exist.
  # v5.12: only count recs with effective qty > 0 (see docstring for
  # the YS58D0800 incident that motivated this filter).
  cursor.execute("""
        SELECT sku
        FROM   ai_transfer_recommendations
        WHERE  status IN ('Applied', 'Auto-Approved')
          AND  COALESCE(manager_override_qty,
                        ai_recommended_qty,
                        calculated_transfer_qty) > 0
          AND  NOT EXISTS (SELECT 1 FROM transfer_pick_lines pl
                           WHERE pl.recommendation_id = ai_transfer_recommendations.recommendation_id
                             AND pl.status IN ('Assigned', 'Picked', 'Short'))
        GROUP BY sku
        HAVING COUNT(*) > 1
    """)
  dupe_skus = [row['sku'] for row in cursor.fetchall()]

  if not dupe_skus:
    return []

  today_str = datetime.now().strftime('%Y-%m-%d')
  results = []

  for sku in dupe_skus:
    # Pull the live recs for this SKU ordered oldest first. We use
    # recommendation_id (monotonic insert order) rather than
    # created_date because two recs inserted in the same second can
    # tie on created_date but never on recommendation_id.
    # v5.12: same qty>0 filter as the GROUP BY above so a qty=0 rec
    # can't sneak in as the keeper here.
    cursor.execute("""
            SELECT recommendation_id
            FROM   ai_transfer_recommendations
            WHERE  sku = %s
              AND  status IN ('Applied', 'Auto-Approved')
              AND  COALESCE(manager_override_qty,
                            ai_recommended_qty,
                            calculated_transfer_qty) > 0
              AND  NOT EXISTS (SELECT 1 FROM transfer_pick_lines pl
                               WHERE pl.recommendation_id = ai_transfer_recommendations.recommendation_id
                                 AND pl.status IN ('Assigned', 'Picked', 'Short'))
            ORDER BY recommendation_id ASC
        """, (sku,))
    recs = cursor.fetchall()

    # Defensive: if a concurrent process resolved the duplicate
    # between our GROUP BY and this SELECT, skip.
    if len(recs) < 2:
      continue

    kept_id = recs[0]['recommendation_id']
    obsoleted_ids = [r['recommendation_id'] for r in recs[1:]]

    placeholders = ','.join(['%s'] * len(obsoleted_ids))
    note_suffix = (
      f"\n[{today_str}] Auto-obsoleted by optimizer dedupe: "
      f"duplicate of rec {kept_id} (same SKU {sku}). "
      f"Kept oldest rec; removed newer to prevent double-shipping. "
      f"(v{__version__})"
    )
    cursor.execute(
      f"""
        UPDATE ai_transfer_recommendations
        SET    status = 'Obsolete',
               notes  = ISNULL(notes, '') + %s
        WHERE  recommendation_id IN ({placeholders})
          AND  status IN ('Applied', 'Auto-Approved')
          AND  NOT EXISTS (SELECT 1 FROM transfer_pick_lines pl
                           WHERE pl.recommendation_id = ai_transfer_recommendations.recommendation_id
                             AND pl.status IN ('Assigned', 'Picked', 'Short'))
      """,
      (note_suffix, *obsoleted_ids)
    )

    # One audit-log row per obsoleted rec — makes it easy to count
    # how many duplicates have been caught over time.
    for dup_id in obsoleted_ids:
      cursor.execute("""
            INSERT INTO ai_agent_log (
                action_type, sku, old_value, new_value, reason, executed_by
            )
            VALUES ('DUPLICATE_OBSOLETED', %s, 'Applied', 'Obsolete',
                    %s, 'truck_optimizer_anvil')
        """, (
        sku,
        f"rec {dup_id} obsoleted; duplicate of kept rec {kept_id} "
        f"(v{__version__})"
      ))

    results.append({
      'sku': sku,
      'kept_id': kept_id,
      'obsoleted_ids': obsoleted_ids
    })

  conn.commit()
  return results


# ==============================================
# NEW FUNCTION: update_assignment_date_time
# ==============================================
@anvil.server.callable
def update_assignment_date_time(assignment_id, new_date, new_time):
  """Update planned date and time for a specific assignment."""
  try:
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
            UPDATE transfer_trip_assignments
            SET planned_date = %s,
                planned_time = %s
            WHERE assignment_id = %s
        """, (new_date, new_time, assignment_id))

    conn.commit()
    conn.close()

    return {'success': True}

  except Exception as e:
    import traceback
    print(f"ERROR in update_assignment_date_time: {traceback.format_exc()}")
    return {'success': False, 'error': str(e)}

# ==============================================
# NEW FUNCTION: get_batch_details_flat
# ==============================================
@anvil.server.callable
def get_batch_details_flat(batch_id):
  """
    Get all assignments for a batch as a flat list.
    Each assignment includes its planned_date/time for per-SKU editing.
    """
  try:
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
            SELECT
                a.assignment_id, a.batch_id, a.trip_id, a.trip_number,
                a.sku, a.qty, a.planned_qty, a.adjusted_qty,
                a.priority_code, a.priority_sort, a.event_type, a.urgency,
                a.volume_ft3, a.weight_lb, a.location_code,
                a.hub_on_hand_at_assign, a.split_note, a.status, a.pick_notes,
                a.planned_date, a.planned_time,
                p.part_desc
            FROM transfer_trip_assignments a
            LEFT JOIN part_specs p ON a.sku = p.sku
            WHERE a.batch_id = %s
            ORDER BY a.planned_date, a.trip_number, a.priority_sort, a.sku
        """, (batch_id,))

    assignments = []
    for row in cursor.fetchall():
      assignments.append({
        'assignment_id': int(row['assignment_id']),
        'batch_id': row['batch_id'],
        'trip_id': int(row['trip_id']) if row['trip_id'] else None,
        'trip_number': int(row['trip_number']) if row['trip_number'] else 1,
        'sku': row['sku'] or '',
        'part_desc': row['part_desc'] or '',
        'qty': float(row['qty']) if row['qty'] else 0,
        'planned_qty': float(row['planned_qty']) if row['planned_qty'] else 0,
        'adjusted_qty': float(row['adjusted_qty']) if row['adjusted_qty'] else None,
        'priority_code': row['priority_code'] or '',
        'priority_sort': int(row['priority_sort']) if row['priority_sort'] else 0,
        'event_type': int(row['event_type']) if row['event_type'] else 0,
        'urgency': row['urgency'] or '',
        'volume_ft3': float(row['volume_ft3']) if row['volume_ft3'] else 0,
        'weight_lb': float(row['weight_lb']) if row['weight_lb'] else 0,
        'location_code': row['location_code'] or '--',
        'hub_on_hand': float(row['hub_on_hand_at_assign']) if row['hub_on_hand_at_assign'] else 0,
        'split_note': row['split_note'] or '',
        'status': row['status'] or '',
        'pick_notes': row['pick_notes'] or '',
        'planned_date': str(row['planned_date']) if row['planned_date'] else None,
        'planned_time': str(row['planned_time'])[:5] if row['planned_time'] else '08:00'
      })

    conn.close()

    # Get truck capacity
    conn = get_db_connection()
    truck_capacity = get_truck_capacity(conn)
    conn.close()

    return {
      'success': True,
      'batch_id': batch_id,
      'assignments': assignments,
      'truck_capacity': truck_capacity
    }

  except Exception as e:
    import traceback
    print(f"ERROR in get_batch_details_flat: {traceback.format_exc()}")
    return {'success': False, 'error': str(e), 'assignments': []}

# =============================================================================
# PRIORITY & PACKING LOGIC
# =============================================================================

def assign_priority(transfer):
  """Assign priority code based on event type and urgency."""
  event_type = transfer['event_type']
  urgency = transfer.get('time_urgency', 'LOW') if event_type == 2 else transfer.get('inventory_urgency', 'LOW')
  key = (event_type, urgency)
  return PRIORITY_MAP.get(key, (7, '7-ROUTINE'))


def prepare_items_for_packing(transfers, part_specs, locations, hub_inventory):
  """Prepare transfer items with all data needed for bin-packing."""
  items = []
  for t in transfers:
    sku = t['sku']
    qty = float(t['transfer_qty'] or 0)
    if qty <= 0:
      continue

    specs = part_specs.get(sku, {'unit_volume_ft3': 0.1, 'unit_weight_lb': 0.5})
    priority_sort, priority_code = assign_priority(t)

    items.append({
      'pick_line_id': t.get('pick_line_id'),   # v5.28: survives splits via item.copy()
      'recommendation_id': t['recommendation_id'],
      'sku': sku,
      'qty': qty,
      'unit_volume_ft3': specs['unit_volume_ft3'],
      'unit_weight_lb': specs['unit_weight_lb'],
      'total_volume_ft3': qty * specs['unit_volume_ft3'],
      'total_weight_lb': qty * specs['unit_weight_lb'],
      'priority_sort': priority_sort,
      'priority_code': priority_code,
      'event_type': t['event_type'],
      'urgency': t.get('time_urgency') if t['event_type'] == 2 else t.get('inventory_urgency'),
      'location_code': locations.get(sku),
      'hub_on_hand': hub_inventory.get(sku, 0),
      'batch_id': t.get('batch_id'),
      'split_note': None
    })
  return items


def split_oversized_items(items, truck_capacity):
  """Split items that exceed truck capacity into multiple segments."""
  max_volume = truck_capacity['volume_ft3']
  max_weight = truck_capacity['weight_lb']
  result = []

  for item in items:
    total_vol = item['total_volume_ft3']
    total_wt = item['total_weight_lb']

    if total_vol <= max_volume and total_wt <= max_weight:
      result.append(item)
      continue

      # Need to split
    unit_vol = item['unit_volume_ft3']
    unit_wt = item['unit_weight_lb']

    max_by_vol = int(max_volume / unit_vol) if unit_vol > 0 else int(item['qty'])
    max_by_wt = int(max_weight / unit_wt) if unit_wt > 0 else int(item['qty'])
    max_per_trip = max(1, min(max_by_vol, max_by_wt))

    remaining = item['qty']
    segment_num = 1
    total_segments = math.ceil(item['qty'] / max_per_trip)

    while remaining > 0:
      segment_qty = min(remaining, max_per_trip)
      segment = item.copy()
      segment['qty'] = segment_qty
      segment['total_volume_ft3'] = segment_qty * unit_vol
      segment['total_weight_lb'] = segment_qty * unit_wt
      segment['split_note'] = f"SPLIT {segment_num} of {total_segments}"
      result.append(segment)
      remaining -= segment_qty
      segment_num += 1

  return result


def efficient_bin_pack(items, truck_capacity):
  """Bin-pack items into trips efficiently, respecting priority order."""
  max_volume = truck_capacity['volume_ft3']
  max_weight = truck_capacity['weight_lb']

  items = sorted(items, key=lambda x: (x['priority_sort'], x['sku']))
  trips = []
  unassigned = items.copy()

  while unassigned:
    trip = {
      'items': [],
      'remaining_volume': max_volume,
      'remaining_weight': max_weight,
      'used_volume': 0,
      'used_weight': 0
    }

    made_progress = True
    while made_progress and unassigned:
      made_progress = False
      for i, item in enumerate(unassigned):
        if (item['total_volume_ft3'] <= trip['remaining_volume'] and
            item['total_weight_lb'] <= trip['remaining_weight']):
          trip['items'].append(item)
          trip['remaining_volume'] -= item['total_volume_ft3']
          trip['remaining_weight'] -= item['total_weight_lb']
          trip['used_volume'] += item['total_volume_ft3']
          trip['used_weight'] += item['total_weight_lb']
          unassigned.pop(i)
          made_progress = True
          break

    if trip['items']:
      trips.append(trip)

  return trips


# =============================================================================
# DATABASE WRITE FUNCTIONS
# =============================================================================

def generate_batch_id():
  """Generate a unique batch ID for this optimizer run."""
  return datetime.now().strftime('%Y%m%d_%H%M%S')


def create_trip_headers(conn, trips, batch_id, planned_date, created_by='truck_optimizer_anvil'):
  """Create transfer_trips header records. Returns mapping of trip_number to trip_id."""
  cursor = conn.cursor()
  trip_ids = {}

  for trip_num, trip in enumerate(trips, start=1):
    cursor.execute("""
            INSERT INTO transfer_trips (
                batch_id, trip_number, planned_date, planned_time, status,
                total_volume_ft3, total_weight_lb, sku_count, created_date, created_by
            )
            VALUES (%s, %s, %s, %s, 'Proposed', %s, %s, %s, GETDATE(), %s)
        """, (
      batch_id, trip_num, planned_date, DEFAULT_TRIP_TIME,
      round(trip['used_volume'], 2), round(trip['used_weight'], 2),
      len(trip['items']), created_by
    ))

    # Get the inserted trip_id (with column alias for dict cursor)
    cursor.execute("SELECT SCOPE_IDENTITY() AS trip_id")
    row = cursor.fetchone()
    trip_ids[trip_num] = int(row['trip_id']) if row else None

  conn.commit()
  return trip_ids


def create_trip_assignments(conn, trips, trip_ids, batch_id, created_by='truck_optimizer_anvil'):
  """Create transfer_trip_assignments detail records."""
  cursor = conn.cursor()

  for trip_num, trip in enumerate(trips, start=1):
    trip_id = trip_ids[trip_num]
    for item in trip['items']:
      cursor.execute("""
                INSERT INTO transfer_trip_assignments (
                    batch_id, trip_number, trip_id, recommendation_id, sku, qty,
                    planned_qty, priority_code, priority_sort, event_type, urgency,
                    volume_ft3, weight_lb, location_code, hub_on_hand_at_assign,
                    split_note, pick_line_id, status, created_date, created_by
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Proposed', GETDATE(), %s)
            """, (
        batch_id, trip_num, trip_id, item['recommendation_id'], item['sku'],
        item['qty'], item['qty'], item['priority_code'], item['priority_sort'],
        item['event_type'], item['urgency'], round(item['total_volume_ft3'], 2),
        round(item['total_weight_lb'], 2), item['location_code'], item['hub_on_hand'],
        item['split_note'], item.get('pick_line_id'), created_by
      ))

  conn.commit()


def log_optimizer_run(conn, batch_id, trips, total_skus, created_by='truck_optimizer_anvil'):
  """Log the optimizer run to ai_agent_log."""
  cursor = conn.cursor()
  total_volume = sum(t['used_volume'] for t in trips)
  total_weight = sum(t['used_weight'] for t in trips)

  cursor.execute("""
        INSERT INTO ai_agent_log (
            action_type, sku, old_value, new_value, reason, action_timestamp, executed_by
        )
        VALUES ('TRUCK_OPTIMIZER', %s, NULL, %s, %s, GETDATE(), %s)
    """, (
    batch_id,
    f"{len(trips)} trips created",
    f"Processed {total_skus} SKUs. Total: {total_volume:.1f} ft³, {total_weight:.1f} lb. Status: Proposed",
    created_by
  ))
  conn.commit()


# =============================================================================
# BATCH CONSOLIDATION HELPERS
# =============================================================================

def get_proposed_batch_summary(conn):
  """
  Check for existing Proposed batches and return a summary.
  Returns dict with batch info if Proposed batches exist, else None.
  """
  cursor = conn.cursor()
  cursor.execute("""
    SELECT
      t.batch_id,
      COUNT(DISTINCT a.assignment_id) as item_count,
      COUNT(DISTINCT a.sku) as sku_count,
      COUNT(DISTINCT t.trip_id) as trip_count
    FROM transfer_trips t
    JOIN transfer_trip_assignments a ON t.batch_id = a.batch_id
    WHERE t.status = 'Proposed'
    GROUP BY t.batch_id
    ORDER BY MIN(t.created_date) DESC
  """)

  batches = []
  total_items = 0
  for row in cursor.fetchall():
    batches.append({
      'batch_id': row['batch_id'],
      'item_count': int(row['item_count']),
      'sku_count': int(row['sku_count']),
      'trip_count': int(row['trip_count']),
    })
    total_items += int(row['item_count'])

  if not batches:
    return None

  return {
    'batches': batches,
    'total_batch_count': len(batches),
    'total_items': total_items,
  }


def revert_proposed_batches(conn):
  """
  Revert ALL Proposed batches:
    1. Set recommendation statuses back to 'Applied' for items in Proposed batches
    2. Delete trip assignments
    3. Delete trip headers
  Returns count of reverted recommendation IDs.
  """
  cursor = conn.cursor()

  # Step 1: Get recommendation_ids tied to Proposed batch assignments
  cursor.execute("""
    SELECT DISTINCT a.recommendation_id
    FROM transfer_trip_assignments a
    JOIN transfer_trips t ON a.batch_id = t.batch_id
    WHERE t.status = 'Proposed'
      AND a.recommendation_id IS NOT NULL
  """)
  rec_ids = [row['recommendation_id'] for row in cursor.fetchall()]

  # Step 2: Revert those recommendations back to 'Applied'
  if rec_ids:
    placeholders = ','.join(['%s'] * len(rec_ids))
    cursor.execute(f"""
      UPDATE ai_transfer_recommendations
      SET status = 'Applied'
      WHERE recommendation_id IN ({placeholders})
        AND status = 'Trip_Assigned'
    """, tuple(rec_ids))

  # Step 3: Get the batch_ids being reverted (for logging)
  cursor.execute("""
    SELECT DISTINCT batch_id
    FROM transfer_trips
    WHERE status = 'Proposed'
  """)
  reverted_batch_ids = [row['batch_id'] for row in cursor.fetchall()]

  # Step 4: Delete assignments for Proposed batches
  cursor.execute("""
    DELETE a
    FROM transfer_trip_assignments a
    JOIN transfer_trips t ON a.batch_id = t.batch_id
    WHERE t.status = 'Proposed'
  """)

  # Step 5: Delete Proposed trip headers
  cursor.execute("""
    DELETE FROM transfer_trips
    WHERE status = 'Proposed'
  """)

  conn.commit()

  # Log the consolidation revert
  for bid in reverted_batch_ids:
    cursor.execute("""
      INSERT INTO ai_agent_log (
        action_type, sku, old_value, new_value, reason, action_timestamp, executed_by
      )
      VALUES ('BATCH_REVERTED', NULL, %s, 'Reverted for consolidation',
              %s, GETDATE(), 'truck_optimizer_anvil')
    """, (bid, f"Reverted {len(rec_ids)} recommendations to Applied for re-optimization"))

  conn.commit()
  return len(rec_ids)


# =============================================================================
# MAIN CALLABLE FUNCTION
# =============================================================================

@anvil.server.callable
def run_truck_optimizer(planned_date=None, dry_run=False, consolidate=False):
  """
  Main entry point - call from Anvil form button.

  Parameters:
    planned_date: Date for the planned trips (defaults to tomorrow)
    dry_run: If True, simulate without saving to database
    consolidate: If True, revert existing Proposed batches before running
                 the optimizer so all items are packed together.
                 If False (default), check for existing batches first
                 and return a prompt if consolidation is needed.
  """
  try:
    # Default planned date is tomorrow
    if planned_date is None:
      planned_date = (datetime.now() + timedelta(days=1)).date()
    elif isinstance(planned_date, str):
      planned_date = datetime.strptime(planned_date, '%Y-%m-%d').date()

      # Connect to database
    print("Connecting to database...")  # Shows in Anvil App Logs
    conn = get_db_connection()
    print("Connected successfully")

    # ── Check for existing Proposed batches ──
    pending_summary = get_proposed_batch_summary(conn)

    if pending_summary and not consolidate:
      # There are existing Proposed batches — check if there are also
      # new Applied items that would create a second batch
      new_transfers = get_eligible_transfers(conn)

      if new_transfers:
        # New items exist AND there's already a pending batch
        # → Ask the user whether to consolidate
        conn.close()
        return {
          'success': True,
          'consolidation_needed': True,
          'pending_summary': pending_summary,
          'new_item_count': len(new_transfers),
          'message': (
            f"There's already a pending batch with "
            f"{pending_summary['total_items']} items. "
            f"There are {len(new_transfers)} new items ready to process. "
            f"Consolidate and re-optimize all items together?"
          )
        }
      else:
        # No new items — an existing Proposed batch already covers
        # every Applied rec. Point the user at Trip Editor instead of
        # showing the terse "no eligible transfers" message, which
        # reads like something is broken when in fact everything is
        # already packed and waiting for review.
        conn.close()
        batch_count = pending_summary.get('total_batch_count', 1)
        total_items = pending_summary['total_items']
        batch_word = 'batch' if batch_count == 1 else 'batches'
        item_word = 'item' if total_items == 1 else 'items'
        return {
          'success': True,
          'message': (
            f"No new items to pack. "
            f"{batch_count} pending {batch_word} ({total_items} {item_word}) "
            f"already waiting in Trip Editor — open it to review and finalize."
          ),
          'pending_summary': pending_summary,
          'trips_count': 0,
          'items_count': 0,
          'trips': []
        }

    # ── If consolidating, revert existing Proposed batches first ──
    if consolidate and pending_summary:
      print("Consolidating: reverting existing Proposed batches...")
      reverted_count = revert_proposed_batches(conn)
      print(f"Reverted {reverted_count} recommendations to Applied")

    # ── v5.9: dedupe same-SKU live recs before packing ──
    # Two or more Applied recs for the same SKU would ship the same SKU
    # twice in one batch. Keep the oldest rec, obsolete newer duplicates
    # with an audit note and a DUPLICATE_OBSOLETED log row.
    # ── v5.20: e21-fulfillment cascade ──
    # Runs BEFORE dedupe so a rec already shipped via e21 can never be
    # chosen as a dedupe "keeper" (which would obsolete a fresh,
    # legitimately packable rec for the same SKU).
    print("e21-fulfillment pass: checking for recs shipped outside the app...")
    e21_cascaded = _cascade_e21_fulfilled_recs(conn)
    if e21_cascaded:
      for c in e21_cascaded:
        print(
          f"  {c['sku']}: rec {c['recommendation_id']} (qty {c['rec_qty']}) "
          f"already shipped on e21 order {c['order_num']} -> Delivered"
        )

    print("Dedupe pass: checking for duplicate live recs...")
    dedupe_results = _dedupe_same_sku_applied_recs(conn)
    if dedupe_results:
      total_obsoleted = sum(len(r['obsoleted_ids']) for r in dedupe_results)
      print(
        f"Deduped {len(dedupe_results)} SKU(s), obsoleted "
        f"{total_obsoleted} duplicate rec(s):"
      )
      for r in dedupe_results:
        print(
          f"  {r['sku']}: kept rec {r['kept_id']}, "
          f"obsoleted {r['obsoleted_ids']}"
        )
    else:
      print("Dedupe pass: no duplicates found")

    # Get truck capacity
    print("Getting truck capacity...")
    truck_capacity = get_truck_capacity(conn)
    print(f"Truck capacity: {truck_capacity}")

    # Get eligible transfers (re-fetch after potential revert)
    print("Getting eligible transfers...")
    transfers = get_eligible_transfers(conn)
    print(f"Found {len(transfers)} transfers")

    # ── v5.18: zero-Hub-stock guard ──
    # The rec's own hub_available column is a SNAPSHOT taken at
    # recommendation time (rec 1362 said 2 while the live value was 0).
    # Check LIVE availability and pull unfulfillable recs out before
    # packing. They stay 'Applied' (deliberate — they resurface on
    # their own when EasyMorph reports stock again, and the Hub Manager
    # can kill one for good via qty-0 in trip_editor or via Transfer
    # Approvals). One SKIPPED_NO_HUB_STOCK log row per skipped rec so
    # repeated skips are visible in the audit trail.
    skipped_no_stock = []
    if transfers:
      live_avail = get_hub_availability(
        conn, list(set(t['sku'] for t in transfers)))
      packable = []
      for t in transfers:
        la = live_avail.get(t['sku'], {'available': 0.0, 'on_hand': 0.0})
        if la['available'] <= 0:
          # v5.18.1: cast DB-native types to plain Python here. pymssql
          # returns DECIMAL columns as decimal.Decimal, and this dict
          # travels back to the client in the run_truck_optimizer return
          # value — anvil.server cannot serialize Decimal (SerializationError
          # observed on first v5.18.0 run, 2026-06-10). la[...] values are
          # already floats via get_hub_availability.
          skipped_no_stock.append({
            'recommendation_id': int(t['recommendation_id']),
            'sku': t['sku'],
            'transfer_qty': float(t['transfer_qty'] or 0),
            'live_available': la['available'],
            'live_on_hand': la['on_hand'],
          })
        else:
          packable.append(t)
      if skipped_no_stock:
        cursor = conn.cursor()
        for s in skipped_no_stock:
          cursor.execute("""
              INSERT INTO ai_agent_log (
                  action_type, sku, old_value, new_value, reason, executed_by
              )
              VALUES ('SKIPPED_NO_HUB_STOCK', %s, %s, %s, %s, 'truck_optimizer')
          """, (
            s['sku'],
            f"{s['transfer_qty']:g}",
            '0',
            (
              f"Rec {s['recommendation_id']} skipped at pack time: live Hub "
              f"available={s['live_available']:.0f}, "
              f"on_hand={s['live_on_hand']:.0f}. Rec stays 'Applied' until "
              f"stock returns or it is cancelled. (v{__version__})"
            ),
          ))
        conn.commit()
        skipped_skus = [s['sku'] for s in skipped_no_stock]
        print(f"v5.18 guard: skipped {len(skipped_no_stock)} rec(s) with "
              f"no live Hub stock: {', '.join(skipped_skus)}")
      transfers = packable

    if not transfers:
      conn.close()
      # v5.9: if dedupe just ran and obsoleted everything, the user
      # should see what happened — otherwise "no eligible transfers"
      # after they approved recs looks like a bug.
      if dedupe_results:
        total_obsoleted = sum(len(r['obsoleted_ids']) for r in dedupe_results)
        msg = (
          f"No eligible transfers to pack — dedupe pass obsoleted "
          f"{total_obsoleted} duplicate rec(s) and no unique recs remain. "
          f"Check the ai_agent_log DUPLICATE_OBSOLETED rows for details."
        )
      else:
        msg = 'No eligible transfers found to process.'
      # v5.18: if the zero-stock guard ate everything, say so —
      # otherwise "no eligible transfers" right after approving recs
      # looks like a bug.
      if skipped_no_stock:
        skipped_skus = ', '.join(s['sku'] for s in skipped_no_stock)
        msg += (
          f" Skipped {len(skipped_no_stock)} SKU(s) with no live Hub "
          f"stock: {skipped_skus}. They stay Applied until stock "
          f"returns, or cancel them via qty-0 in the Trip Editor."
        )
      # v5.20: same for the e21-fulfillment cascade.
      if e21_cascaded:
        e21_preview = ', '.join(
          f"{c['sku']} (e21 {c['order_num']})" for c in e21_cascaded)
        msg += (
          f" Auto-resolved {len(e21_cascaded)} rec(s) already shipped "
          f"via e21: {e21_preview}."
        )
      return {
        'success': True,
        'message': msg,
        'trips_count': 0,
        'items_count': 0,
        'trips': [],
        'dedupe_results': dedupe_results,
        'skipped_no_stock': skipped_no_stock,
        'e21_cascaded': e21_cascaded,
        'version': __version__
      }

      # Get SKU list and lookup data
    skus = list(set(t['sku'] for t in transfers))
    print(f"SKUs to process: {skus}")

    part_specs = get_part_specs(conn, skus)
    print(f"Got specs for {len(part_specs)} SKUs")

    locations = get_hub_locations(conn, skus)
    print(f"Got locations for {len(locations)} SKUs")

    hub_inventory = get_hub_inventory(conn, skus)
    print(f"Got inventory for {len(hub_inventory)} SKUs")

    # Prepare and pack items
    items = prepare_items_for_packing(transfers, part_specs, locations, hub_inventory)
    print(f"Prepared {len(items)} items")

    items = split_oversized_items(items, truck_capacity)
    print(f"After split: {len(items)} items")

    trips = efficient_bin_pack(items, truck_capacity)
    print(f"Created {len(trips)} trips")

    # ── Guard against empty result ──
    # prepare_items_for_packing() drops rows with qty <= 0, and
    # split_oversized_items()/efficient_bin_pack() can produce an empty
    # list if none of the eligible transfers ended up packable (e.g.
    # all had zero transfer_qty, missing specs, etc.). Generating a
    # batch_id in that case would create a "phantom" batch with no
    # transfer_trips rows, which the UI can't resolve and Anvil would
    # render as "<Invalid value>" in the batch dropdown.
    if not trips:
      conn.close()
      _no_pack_msg = (
        f'Found {len(transfers)} eligible transfer(s) but none were '
        f'packable (check transfer_qty, part specs, and hub inventory). '
        f'No batch was created.'
      )
      # v5.18: append the zero-stock skip report here too.
      if skipped_no_stock:
        skipped_skus = ', '.join(s['sku'] for s in skipped_no_stock)
        _no_pack_msg += (
          f" Also skipped {len(skipped_no_stock)} SKU(s) with no live "
          f"Hub stock: {skipped_skus}."
        )
      return {
        'success': True,
        'message': _no_pack_msg,
        'trips_count': 0,
        'items_count': 0,
        'trips': [],
        'skipped_no_stock': skipped_no_stock,
        'e21_cascaded': e21_cascaded
      }

    # Build trip summaries for UI display
    max_vol = truck_capacity['volume_ft3']
    max_wt = truck_capacity['weight_lb']

    trip_summaries = []
    for trip_num, trip in enumerate(trips, start=1):
      vol_pct = (trip['used_volume'] / max_vol) * 100
      wt_pct = (trip['used_weight'] / max_wt) * 100
      trip_summaries.append({
        'trip_number': trip_num,
        'sku_count': len(trip['items']),
        'volume_ft3': round(trip['used_volume'], 1),
        'volume_pct': round(vol_pct, 0),
        'weight_lb': round(trip['used_weight'], 1),
        'weight_pct': round(wt_pct, 0),
        'items': [
          {
            'sku': i['sku'],
            'qty': i['qty'],
            'priority': i['priority_code'],
            'location': i['location_code'],
            'split_note': i['split_note']
          }
          for i in trip['items']
        ]
      })

      # Save to database (unless dry run)
    batch_id = None
    if not dry_run:
      batch_id = generate_batch_id()
      print(f"Saving batch {batch_id}...")

      trip_ids = create_trip_headers(conn, trips, batch_id, planned_date)
      print(f"Created {len(trip_ids)} trip headers")

      create_trip_assignments(conn, trips, trip_ids, batch_id)
      print("Created trip assignments")

      # v5.28: rec -> Trip_Assigned only when FULLY staged: no lines
      # still being picked, no completed lines awaiting a trip, and no
      # unallocated approved remainder. Partial recs stay 'Applied' so
      # the Pick Editor pool keeps offering the rest - and since
      # eligibility is line-based, an Applied rec can't be double-packed.
      packed_rec_ids = sorted({t['recommendation_id'] for item in trips for t in item['items']})
      if packed_rec_ids:
        cursor = conn.cursor()
        ph = ','.join(['%s'] * len(packed_rec_ids))
        cursor.execute(f"""
            UPDATE r
            SET r.status = 'Trip_Assigned'
            FROM ai_transfer_recommendations r
            WHERE r.recommendation_id IN ({ph})
              AND r.status = 'Applied'
              AND NOT EXISTS (SELECT 1 FROM transfer_pick_lines pl
                              WHERE pl.recommendation_id = r.recommendation_id
                                AND pl.status = 'Assigned')
              AND NOT EXISTS (SELECT 1 FROM transfer_pick_lines pl
                              JOIN transfer_picks p ON p.pick_id = pl.pick_id
                              WHERE pl.recommendation_id = r.recommendation_id
                                AND p.status = 'Completed'
                                AND pl.status IN ('Picked', 'Short')
                                AND pl.qty_picked > 0
                                AND NOT EXISTS (SELECT 1 FROM transfer_trip_assignments t2
                                                WHERE t2.pick_line_id = pl.pick_line_id))
              AND COALESCE(r.manager_override_qty, r.ai_recommended_qty,
                           r.calculated_transfer_qty)
                  <= (SELECT COALESCE(SUM(pl.qty_planned), 0)
                      FROM transfer_pick_lines pl
                      WHERE pl.recommendation_id = r.recommendation_id
                        AND pl.status IN ('Assigned', 'Picked', 'Short'))
        """, tuple(packed_rec_ids))
        flipped = cursor.rowcount
        conn.commit()
        print(f"Marked {flipped} fully-staged rec(s) 'Trip_Assigned' "
              f"(batch touched {len(packed_rec_ids)} rec(s))")

      log_optimizer_run(conn, batch_id, trips, len(transfers))
      print("Logged to ai_agent_log")

    conn.close()

    # Build result
    total_items = sum(len(t['items']) for t in trips)
    total_vol = sum(t['used_volume'] for t in trips)
    total_wt = sum(t['used_weight'] for t in trips)

    if dry_run:
      message = f"DRY RUN: Would create {len(trips)} trips with {total_items} items ({total_vol:,.0f} ft³, {total_wt:,.0f} lb)"
    elif consolidate:
      message = f"Consolidated and created {len(trips)} trips with {total_items} items. Batch: {batch_id}. Ready for review."
    else:
      message = f"Created {len(trips)} trips with {total_items} items. Batch: {batch_id}. Ready for review."

    # v5.9: if the dedupe pass caught any duplicates, tack a short note
    # onto the user-facing message so it's obvious why the trip count
    # differs from the rec count they were expecting.
    if dedupe_results:
      total_obsoleted = sum(len(r['obsoleted_ids']) for r in dedupe_results)
      dupe_skus = [r['sku'] for r in dedupe_results]
      sku_preview = ', '.join(dupe_skus[:3])
      if len(dupe_skus) > 3:
        sku_preview += f', +{len(dupe_skus) - 3} more'
      message += (
        f" (Auto-resolved {total_obsoleted} duplicate rec(s) on "
        f"{sku_preview} before packing.)"
      )

    # v5.18: surface the zero-stock skip report on the success path so
    # the Hub Manager knows why a rec they approved isn't in the batch.
    if skipped_no_stock:
      skipped_skus = [s['sku'] for s in skipped_no_stock]
      sku_preview = ', '.join(skipped_skus[:3])
      if len(skipped_skus) > 3:
        sku_preview += f', +{len(skipped_skus) - 3} more'
      message += (
        f" Skipped {len(skipped_no_stock)} SKU(s) with no live Hub "
        f"stock: {sku_preview}."
      )

    # v5.20: surface the e21-fulfillment cascade so the Hub Manager knows
    # why an approved rec isn't in the batch — it already shipped.
    if e21_cascaded:
      e21_preview = ', '.join(
        f"{c['sku']} (e21 {c['order_num']})" for c in e21_cascaded[:3])
      if len(e21_cascaded) > 3:
        e21_preview += f", +{len(e21_cascaded) - 3} more"
      message += (
        f" Auto-resolved {len(e21_cascaded)} rec(s) already shipped via "
        f"e21: {e21_preview}."
      )

    return {
      'success': True,
      'message': message,
      'trips_count': len(trips),
      'items_count': total_items,
      'total_volume_ft3': round(total_vol, 1),
      'total_weight_lb': round(total_wt, 1),
      'batch_id': batch_id,
      'planned_date': str(planned_date),
      'trips': trip_summaries,
      'dry_run': dry_run,
      'dedupe_results': dedupe_results,
      'skipped_no_stock': skipped_no_stock,
      'e21_cascaded': e21_cascaded,
      'version': __version__
    }

  except Exception as e:
    import traceback
    error_details = traceback.format_exc()
    print(f"ERROR in run_truck_optimizer: {error_details}")  # Shows in Anvil App Logs
    return {
      'success': False,
      'message': f'Error: {type(e).__name__}: {str(e)}',
      'error': str(e),
      'error_type': type(e).__name__,
      'error_details': error_details,
      'trips_count': 0,
      'items_count': 0,
      'trips': []
    }


@anvil.server.callable
def get_pending_optimizer_batches():
  """
    Get list of proposed batches awaiting Hub Manager review.
    """
  try:
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
            SELECT
                batch_id,
                COUNT(*) as trip_count,
                SUM(sku_count) as total_skus,
                SUM(total_volume_ft3) as total_volume,
                SUM(total_weight_lb) as total_weight,
                MIN(planned_date) as planned_date,
                MIN(created_date) as created_date
            FROM transfer_trips
            WHERE status = 'Proposed'
            GROUP BY batch_id
            ORDER BY MIN(created_date) DESC
        """)

    batches = []
    for row in cursor.fetchall():
      batches.append({
        'batch_id': row['batch_id'],
        'trip_count': int(row['trip_count']) if row['trip_count'] else 0,
        'total_skus': int(row['total_skus']) if row['total_skus'] else 0,
        'total_volume': float(row['total_volume']) if row['total_volume'] else 0.0,
        'total_weight': float(row['total_weight']) if row['total_weight'] else 0.0,
        'planned_date': str(row['planned_date']) if row['planned_date'] else None,
        'created_date': str(row['created_date']) if row['created_date'] else None
      })

    conn.close()
    return {'success': True, 'batches': batches}

  except Exception as e:
    import traceback
    print(f"ERROR in get_pending_optimizer_batches: {traceback.format_exc()}")
    return {'success': False, 'error': str(e), 'batches': []}


@anvil.server.callable
def get_batch_details(batch_id):
  """
    Get full details of a specific batch for the Trip Editor.
    Returns trips with their line items.
    """
  try:
    conn = get_db_connection()
    cursor = conn.cursor()

    # Get trip headers
    cursor.execute("""
            SELECT
                trip_id, batch_id, trip_number, planned_date, planned_time,
                status, total_volume_ft3, total_weight_lb, sku_count,
                created_date, notes
            FROM transfer_trips
            WHERE batch_id = %s
            ORDER BY trip_number
        """, (batch_id,))

    trips = []
    for row in cursor.fetchall():
      trips.append({
        'trip_id': int(row['trip_id']),
        'batch_id': row['batch_id'],
        'trip_number': int(row['trip_number']) if row['trip_number'] else 0,
        'planned_date': str(row['planned_date']) if row['planned_date'] else None,
        'planned_time': row['planned_time'],
        'status': row['status'],
        'total_volume_ft3': float(row['total_volume_ft3']) if row['total_volume_ft3'] else 0,
        'total_weight_lb': float(row['total_weight_lb']) if row['total_weight_lb'] else 0,
        'sku_count': int(row['sku_count']) if row['sku_count'] else 0,
        'created_date': str(row['created_date']) if row['created_date'] else None,
        'notes': row['notes'],
        'items': []
      })

      # Get trip assignments
    cursor.execute("""
            SELECT
                assignment_id, trip_id, trip_number, sku, qty, planned_qty,
                adjusted_qty, priority_code, priority_sort, event_type, urgency,
                volume_ft3, weight_lb, location_code, hub_on_hand_at_assign,
                split_note, status, pick_notes
            FROM transfer_trip_assignments
            WHERE batch_id = %s
            ORDER BY trip_number, priority_sort, sku
        """, (batch_id,))

    # Map items to trips
    trip_map = {t['trip_id']: t for t in trips}

    for row in cursor.fetchall():
      trip_id = row['trip_id']
      if trip_id in trip_map:
        trip_map[trip_id]['items'].append({
          'assignment_id': int(row['assignment_id']),
          'trip_number': int(row['trip_number']) if row['trip_number'] else 0,
          'sku': row['sku'] or '',
          'qty': float(row['qty']) if row['qty'] else 0,
          'planned_qty': float(row['planned_qty']) if row['planned_qty'] else 0,
          'adjusted_qty': float(row['adjusted_qty']) if row['adjusted_qty'] else 0,
          'priority_code': row['priority_code'] or '',
          'priority_sort': int(row['priority_sort']) if row['priority_sort'] else 0,
          'event_type': int(row['event_type']) if row['event_type'] else 0,
          'urgency': row['urgency'] or '',
          'volume_ft3': float(row['volume_ft3']) if row['volume_ft3'] else 0,
          'weight_lb': float(row['weight_lb']) if row['weight_lb'] else 0,
          'location_code': row['location_code'] or '--',
          'hub_on_hand': float(row['hub_on_hand_at_assign']) if row['hub_on_hand_at_assign'] else 0,
          'split_note': row['split_note'] or '',
          'status': row['status'] or '',
          'pick_notes': row['pick_notes'] or ''
        })

    conn.close()

    # Get truck capacity for UI display
    conn = get_db_connection()
    truck_capacity = get_truck_capacity(conn)
    conn.close()

    return {
      'success': True,
      'batch_id': batch_id,
      'trips': trips,
      'truck_capacity': truck_capacity
    }

  except Exception as e:
    import traceback
    print(f"ERROR in get_batch_details: {traceback.format_exc()}")
    return {'success': False, 'error': str(e), 'trips': []}

def generate_pick_list_csv(conn, trip_id, batch_id, trip_number):
  """
    Generate a CSV pick list for a single trip.
    Returns CSV content as a string.
    """
  cursor = conn.cursor()

  # Get trip header info
  cursor.execute("""
        SELECT
            trip_id, batch_id, trip_number, planned_date, planned_time,
            total_volume_ft3, total_weight_lb, sku_count
        FROM transfer_trips
        WHERE trip_id = %s
    """, (trip_id,))

  trip = cursor.fetchone()
  if not trip:
    return None

    # Get trip line items sorted by location for efficient picking
  cursor.execute("""
        SELECT
            a.sku,
            COALESCE(p.part_desc, '') as part_desc,
            COALESCE(a.location_code, '--') as location_code,
            COALESCE(a.hub_on_hand_at_assign, 0) as hub_on_hand,
            COALESCE(a.adjusted_qty, a.qty) as ship_qty,
            a.priority_code,
            COALESCE(a.volume_ft3, 0) as volume_ft3,
            COALESCE(a.weight_lb, 0) as weight_lb,
            COALESCE(a.split_note, '') as notes
        FROM transfer_trip_assignments a
        LEFT JOIN part_specs p ON a.sku = p.sku
        WHERE a.trip_id = %s
        ORDER BY a.location_code, a.priority_sort, a.sku
    """, (trip_id,))

  items = cursor.fetchall()

  # Build CSV content
  output = io.StringIO()
  writer = csv.writer(output)

  # Header section
  writer.writerow(['TRANSFER PICK LIST'])
  writer.writerow([])
  writer.writerow(['Batch ID:', batch_id])
  writer.writerow(['Trip Number:', trip_number])
  writer.writerow(['Planned Date:', str(trip['planned_date']) if trip['planned_date'] else 'Not set'])
  writer.writerow(['Planned Time:', trip['planned_time'] or '08:00'])
  writer.writerow([])
  writer.writerow(['Total SKUs:', int(trip['sku_count'] or 0)])
  writer.writerow(['Total Volume:', f"{float(trip['total_volume_ft3'] or 0):,.1f} ft³"])
  writer.writerow(['Total Weight:', f"{float(trip['total_weight_lb'] or 0):,.1f} lb"])
  writer.writerow([])
  writer.writerow([])  # Extra blank row before detail

  # Column headers
  writer.writerow([
    'Location',
    'SKU',
    'Description',
    'Hub OH',
    'Ship Qty',
    'Priority',
    'Volume (ft³)',
    'Weight (lb)',
    'Notes'
  ])

  # Detail lines
  for item in items:
    writer.writerow([
      item['location_code'],
      item['sku'],
      item['part_desc'][:40] if item['part_desc'] else '',  # Truncate long descriptions
      int(float(item['hub_on_hand'] or 0)),
      int(float(item['ship_qty'] or 0)),
      item['priority_code'] or '',
      f"{float(item['volume_ft3'] or 0):.1f}",
      f"{float(item['weight_lb'] or 0):.1f}",
      item['notes'] or ''
    ])

    # Footer
  writer.writerow([])
  writer.writerow(['--- END OF PICK LIST ---'])

  return output.getvalue()


# ==============================================
# HELPER FUNCTION: generate_batch_pick_lists
# (Not decorated - internal use only)
# ==============================================
def generate_batch_pick_lists(conn, batch_id):
  """
    Generate pick list CSVs for all trips in a batch.
    Groups by (planned_date, trip_number) combination.
    Returns list of {filename, media, trip_number, trip_date}
    """
  cursor = conn.cursor()

  # Get unique (date, trip#) combinations
  cursor.execute("""
        SELECT DISTINCT planned_date, trip_number
        FROM transfer_trip_assignments
        WHERE batch_id = %s
        ORDER BY planned_date, trip_number
    """, (batch_id,))

  groups = cursor.fetchall()
  pick_lists = []

  for group in groups:
    trip_date = group['planned_date']
    trip_number = group['trip_number']

    # Get assignments for this group
    cursor.execute("""
            SELECT
                a.sku, a.location_code, a.hub_on_hand_at_assign,
                COALESCE(a.adjusted_qty, a.qty) as ship_qty,
                a.priority_code, a.volume_ft3, a.weight_lb, a.pick_notes,
                a.planned_time,
                p.part_desc
            FROM transfer_trip_assignments a
            LEFT JOIN part_specs p ON a.sku = p.sku
            WHERE a.batch_id = %s
              AND (a.planned_date = %s OR (a.planned_date IS NULL AND %s IS NULL))
              AND a.trip_number = %s
            ORDER BY a.location_code, a.sku
        """, (batch_id, trip_date, trip_date, trip_number))

    items = cursor.fetchall()
    if not items:
      continue

      # Calculate totals
    total_skus = len(items)
    total_volume = sum(float(i['volume_ft3'] or 0) for i in items)
    total_weight = sum(float(i['weight_lb'] or 0) for i in items)

    # Get planned time from first item
    planned_time = items[0]['planned_time'] if items else '08:00'
    if planned_time:
      planned_time = str(planned_time)[:5]

      # Build CSV content
    output = io.StringIO()
    writer = csv.writer(output)

    # Header section
    writer.writerow(['TRANSFER PICK LIST'])
    writer.writerow([])
    writer.writerow(['Batch ID:', batch_id])
    writer.writerow(['Trip Number:', trip_number])
    writer.writerow(['Planned Date:', str(trip_date) if trip_date else 'Not Set'])
    writer.writerow(['Planned Time:', planned_time])
    writer.writerow([])
    writer.writerow(['Total SKUs:', total_skus])
    writer.writerow(['Total Volume:', f"{total_volume:,.1f} ft³"])
    writer.writerow(['Total Weight:', f"{total_weight:,.1f} lb"])
    writer.writerow([])
    writer.writerow([])

    # Column headers
    writer.writerow(['Location', 'SKU', 'Description', 'Hub OH', 'Ship Qty',
                     'Priority', 'Volume (ft³)', 'Weight (lb)', 'Notes'])

    # Data rows
    for item in items:
      writer.writerow([
        item['location_code'] or '--',
        item['sku'],
        item['part_desc'] or '',
        int(item['hub_on_hand_at_assign'] or 0),
        int(item['ship_qty'] or 0),
        item['priority_code'] or '',
        f"{float(item['volume_ft3'] or 0):.1f}",
        f"{float(item['weight_lb'] or 0):.1f}",
        item['pick_notes'] or ''
      ])

    writer.writerow([])
    writer.writerow(['--- END OF PICK LIST ---'])

    csv_content = output.getvalue()
    output.close()

    # Create filename with date
    date_str = str(trip_date).replace('-', '') if trip_date else 'NoDate'
    filename = f"PickList_{batch_id}_Trip{trip_number}_{date_str}.csv"

    # Convert to Anvil media object
    media = anvil.BlobMedia('text/csv', csv_content.encode('utf-8'), name=filename)

    pick_lists.append({
      'filename': filename,
      'media': media,
      'trip_number': trip_number,
      'trip_date': str(trip_date) if trip_date else None
    })

  return pick_lists

@anvil.server.callable
def finalize_batch(batch_id):
  """
    Finalize all trips in a batch.
    Generates pick lists grouped by (planned_date, trip_number).

    v5.7: cascades ai_transfer_recommendations.status from 'Trip_Assigned'
          to 'Finalized' for every rec tied to this batch.
    v5.8: appends (v{__version__}) to the BATCH_FINALIZED log reason.
    """
  try:
    conn = get_db_connection()
    cursor = conn.cursor()

    # Check batch exists and is proposed
    cursor.execute("""
            SELECT COUNT(*) as cnt FROM transfer_trips
            WHERE batch_id = %s AND status = 'Proposed'
        """, (batch_id,))
    row = cursor.fetchone()
    if row['cnt'] == 0:
      conn.close()
      return {'success': False, 'error': 'Batch not found or already finalized'}

      # Update trip status
    cursor.execute("""
            UPDATE transfer_trips
            SET status = 'Finalized',
                finalized_date = GETDATE(),
                finalized_by = 'hub_manager'
            WHERE batch_id = %s AND status = 'Proposed'
        """, (batch_id,))

    # Update assignment status
    cursor.execute("""
            UPDATE transfer_trip_assignments
            SET status = 'Finalized'
            WHERE batch_id = %s AND status = 'Proposed'
        """, (batch_id,))

    # v5.7: Cascade rec status Trip_Assigned -> Finalized for all recs in this batch.
    # Rationale: once the batch is finalized, the Hub Manager enters the transfer
    # order into e21, which reduces hub.available. Advancing the rec status past
    # Trip_Assigned causes both branches of the PendingTransferQty CTE in
    # vw_unified_transfer_detection to fall through to ELSE 0, yielding the
    # doc Section 9 Finalized-state formulas:
    #   starlite_effective_on_hand = starlite_on_hand + e21transit_qty
    #   hub_effective_on_hand     = hub_on_hand
    # This prevents double-counting of the Hub depletion once e21 carries it.
    cursor.execute("""
            UPDATE r
            SET r.status = 'Finalized'
            FROM dbo.ai_transfer_recommendations r
            INNER JOIN dbo.transfer_trip_assignments tta
                ON tta.recommendation_id = r.recommendation_id
            WHERE tta.batch_id = %s
              AND r.status = 'Trip_Assigned'
        """, (batch_id,))
    rec_cascade_count = cursor.rowcount

    conn.commit()

    # Generate pick lists (one per date/trip combination)
    pick_lists = generate_batch_pick_lists(conn, batch_id)

    # Log the finalization — v5.8: reason includes module version tag
    # so every BATCH_FINALIZED row in ai_agent_log is attributable to
    # the code that produced it.
    cursor.execute("""
            INSERT INTO ai_agent_log (action_type, sku, old_value, new_value, reason, executed_by)
            VALUES ('BATCH_FINALIZED', NULL, %s, 'Finalized', %s, 'hub_manager')
        """, (
      batch_id,
      f"Generated {len(pick_lists)} pick list(s); "
      f"cascaded {rec_cascade_count} recs Trip_Assigned->Finalized "
      f"(v{__version__})"
    ))

    conn.commit()
    conn.close()

    return {
      'success': True,
      'message': f'Batch {batch_id} finalized. {len(pick_lists)} pick list(s) generated.',
      'pick_lists': pick_lists,
      'version': __version__,
    }

  except Exception as e:
    import traceback
    print(f"ERROR in finalize_batch: {traceback.format_exc()}")
    return {'success': False, 'error': str(e)}

# STEP 4: OPTIONAL - Add this function to allow re-downloading pick lists later
# -----------------------------------------------------------------------------
@anvil.server.callable
def download_pick_list(batch_id, trip_number):
  """
    Generate and return a single pick list for download.
    Can be called independently to re-download a pick list.
    """
  try:
    conn = get_db_connection()
    cursor = conn.cursor()

    # Get trip_id
    cursor.execute("""
            SELECT trip_id
            FROM transfer_trips
            WHERE batch_id = %s AND trip_number = %s
        """, (batch_id, trip_number))

    row = cursor.fetchone()
    if not row:
      conn.close()
      return {'success': False, 'error': 'Trip not found'}

    trip_id = row['trip_id']

    csv_content = generate_pick_list_csv(conn, trip_id, batch_id, trip_number)
    conn.close()

    if not csv_content:
      return {'success': False, 'error': 'Failed to generate pick list'}

    filename = f"PickList_{batch_id}_Trip{trip_number}.csv"
    media = anvil.BlobMedia(
      content_type='text/csv',
      content=csv_content.encode('utf-8'),
      name=filename
    )

    return {
      'success': True,
      'media': media,
      'filename': filename
    }

  except Exception as e:
    import traceback
    print(f"ERROR in download_pick_list: {traceback.format_exc()}")
    return {'success': False, 'error': str(e)}

@anvil.server.callable
def delete_batch(batch_id):
  """
    Delete a proposed batch (cannot delete finalized batches).

    v5.9: Cascade-revert any recs still at status='Trip_Assigned' for
    this batch back to 'Applied' so they stay visible to the next
    optimizer run. Prior to v5.9 this function deleted the trip + its
    assignments but left the underlying recs orphaned at Trip_Assigned
    with no trip — invisible in both Trip Editor and Transfer Approvals,
    requiring manual SQL to unstick them (see 2026-04-23 YS12I0150
    incident). This is the reverse of v5.7's Trip_Assigned -> Finalized
    cascade.

    Scope: only Trip_Assigned recs are reverted. Recs at Finalized or
    Delivered for this batch are never touched — those statuses reflect
    real-world state (pick list generated / driver confirmed) and must
    not be silently rolled back.
    """
  try:
    conn = get_db_connection()
    cursor = conn.cursor()

    # Check if batch is still proposed
    cursor.execute("""
            SELECT COUNT(*) as cnt FROM transfer_trips
            WHERE batch_id = %s AND status != 'Proposed'
        """, (batch_id,))
    row = cursor.fetchone()
    if row['cnt'] > 0:
      conn.close()
      return {'success': False, 'error': 'Cannot delete a finalized batch'}

    # v5.12.1 (2026-05-13): Collect recs that will be orphaned by this
    # delete, via the same JOIN-through-tta pattern that
    # revert_proposed_batches uses on line ~750.
    #
    # The v5.9 implementation was:
    #     SELECT recommendation_id FROM ai_transfer_recommendations
    #     WHERE batch_id = %s AND status = 'Trip_Assigned'
    # which assumed rec.batch_id = trip.batch_id. Those columns hold
    # different identifier spaces and never match:
    #   rec.batch_id  format: 'YYYYMMDD-<hash>' (AI review batch id)
    #   trip.batch_id format: 'YYYYMMDD_HHMMSS' (truck optimizer run id)
    # So the v5.9 cascade silently returned zero rows on every call,
    # the revert UPDATE was a no-op, and the DELETEs below proceeded
    # to orphan every Trip_Assigned rec on the batch. 11 orphans
    # accumulated by 2026-05-13; see
    # sql/cleanup_orphan_trip_assigned_recs.sql for the cleanup, and
    # the 2026-05-13 BS87FS0250 incident notes for context.
    #
    # The link between rec and trip lives in
    # transfer_trip_assignments — joining through it is the only
    # reliable way to find affected recs. tta.batch_id IS the trip's
    # batch_id (same column meaning across both tables), so the JOIN
    # condition tta.batch_id = trip.batch_id matches correctly.
    cursor.execute("""
            SELECT DISTINCT a.recommendation_id
            FROM   transfer_trip_assignments a
            INNER JOIN ai_transfer_recommendations r
                    ON r.recommendation_id = a.recommendation_id
            WHERE  a.batch_id           = %s
              AND  r.status             = 'Trip_Assigned'
              AND  a.recommendation_id IS NOT NULL
        """, (batch_id,))
    orphan_rec_ids = [r['recommendation_id'] for r in cursor.fetchall()]

    # v5.9: Revert those recs to Applied. Guard with
    # "AND status = 'Trip_Assigned'" so that if a concurrent process
    # flipped one to Finalized/Delivered between the SELECT and the
    # UPDATE, we don't step on it.
    reverted_count = 0
    if orphan_rec_ids:
      placeholders = ','.join(['%s'] * len(orphan_rec_ids))
      cursor.execute(
        f"""
                UPDATE ai_transfer_recommendations
                SET    status = 'Applied'
                WHERE  recommendation_id IN ({placeholders})
                  AND  status = 'Trip_Assigned'
            """,
        tuple(orphan_rec_ids)
      )
      reverted_count = len(orphan_rec_ids)

    # Delete assignments first (foreign key)
    cursor.execute("""
            DELETE FROM transfer_trip_assignments WHERE batch_id = %s
        """, (batch_id,))

    # Delete trip headers
    cursor.execute("""
            DELETE FROM transfer_trips WHERE batch_id = %s
        """, (batch_id,))

    # v5.9: Audit row summarizing the cascade. One row per batch_id
    # (not per rec) — keeps the log readable. Per-rec detail is still
    # recoverable because each rec's batch_id is preserved when status
    # flips back to Applied.
    cursor.execute("""
            INSERT INTO ai_agent_log (
                action_type, sku, old_value, new_value, reason, executed_by
            )
            VALUES ('BATCH_DELETED', NULL, %s, NULL, %s, 'trip_editor')
        """, (
      batch_id,
      (
        f"Batch {batch_id} deleted; "
        f"cascaded {reverted_count} rec(s) Trip_Assigned->Applied "
        f"(v{__version__})"
      )
    ))

    conn.commit()
    conn.close()

    if reverted_count:
      msg = (
        f'Batch {batch_id} deleted; '
        f'{reverted_count} rec(s) reverted to Applied and '
        f'ready for the next optimizer run.'
      )
    else:
      msg = f'Batch {batch_id} deleted.'

    return {
      'success': True,
      'message': msg,
      'reverted_count': reverted_count,
      'reverted_rec_ids': orphan_rec_ids,
      'version': __version__
    }

  except Exception as e:
    import traceback
    print(f"ERROR in delete_batch: {traceback.format_exc()}")
    return {'success': False, 'error': str(e)}

@anvil.server.callable
def update_assignment_qty(assignment_id, new_qty):
  """Update quantity for a trip assignment."""
  try:
    conn = get_db_connection()
    cursor = conn.cursor()

    # Get current assignment info for recalculating volume/weight
    cursor.execute("""
            SELECT a.qty, a.volume_ft3, a.weight_lb, a.trip_id, a.batch_id,
                   a.status, a.pick_line_id, pl.qty_picked,
                   t.total_volume_ft3, t.total_weight_lb
            FROM transfer_trip_assignments a
            JOIN transfer_trips t ON a.trip_id = t.trip_id
            LEFT JOIN transfer_pick_lines pl ON pl.pick_line_id = a.pick_line_id
            WHERE a.assignment_id = %s
        """, (assignment_id,))

    row = cursor.fetchone()
    if not row:
      conn.close()
      return {'success': False, 'error': 'Assignment not found'}

    # v5.28: status gate (pre-existing gap) - only Proposed rows are editable.
    if row['status'] != 'Proposed':
      conn.close()
      return {'success': False,
              'error': (f"Assignment is '{row['status']}' - only Proposed rows "
                        f"can be edited. Refresh the batch.")}

    # v5.28: can't ship more than was physically picked (C1/Option A).
    if row['pick_line_id'] is not None and row['qty_picked'] is not None \
    and float(new_qty) > float(row['qty_picked']) + 1e-9:
      conn.close()
      return {'success': False,
              'error': (f"Qty {float(new_qty):g} exceeds the physically picked "
                        f"{float(row['qty_picked']):g} for this line - cannot "
                        f"ship more than was picked.")}

    old_qty = float(row['qty']) if row['qty'] else 0
    old_vol = float(row['volume_ft3']) if row['volume_ft3'] else 0
    old_wt = float(row['weight_lb']) if row['weight_lb'] else 0
    trip_id = row['trip_id']
    trip_vol = float(row['total_volume_ft3']) if row['total_volume_ft3'] else 0
    trip_wt = float(row['total_weight_lb']) if row['total_weight_lb'] else 0

    # Calculate unit values
    unit_vol = old_vol / old_qty if old_qty > 0 else 0
    unit_wt = old_wt / old_qty if old_qty > 0 else 0

    # Calculate new totals
    new_vol = new_qty * unit_vol
    new_wt = new_qty * unit_wt
    vol_diff = new_vol - old_vol
    wt_diff = new_wt - old_wt

    # Update assignment
    cursor.execute("""
            UPDATE transfer_trip_assignments
            SET adjusted_qty = %s,
                volume_ft3 = %s,
                weight_lb = %s
            WHERE assignment_id = %s
        """, (new_qty, new_vol, new_wt, assignment_id))

    # Update trip totals
    cursor.execute("""
            UPDATE transfer_trips
            SET total_volume_ft3 = total_volume_ft3 + %s,
                total_weight_lb = total_weight_lb + %s
            WHERE trip_id = %s
        """, (vol_diff, wt_diff, trip_id))

    conn.commit()
    conn.close()

    return {
      'success': True,
      'new_volume': new_vol,
      'new_weight': new_wt
    }

  except Exception as e:
    import traceback
    print(f"ERROR in update_assignment_qty: {traceback.format_exc()}")
    return {'success': False, 'error': str(e)}


@anvil.server.callable
def update_assignment_trip(assignment_id, new_trip_number):
  """Move an assignment to a different trip."""
  try:
    conn = get_db_connection()
    cursor = conn.cursor()

    # Get current assignment and batch info
    cursor.execute("""
            SELECT a.trip_id, a.batch_id, a.volume_ft3, a.weight_lb, a.sku,
                   t.trip_number as old_trip_number
            FROM transfer_trip_assignments a
            JOIN transfer_trips t ON a.trip_id = t.trip_id
            WHERE a.assignment_id = %s
        """, (assignment_id,))

    row = cursor.fetchone()
    if not row:
      conn.close()
      return {'success': False, 'error': 'Assignment not found'}

    old_trip_id = row['trip_id']
    batch_id = row['batch_id']
    vol = float(row['volume_ft3']) if row['volume_ft3'] else 0
    wt = float(row['weight_lb']) if row['weight_lb'] else 0

    # Find the new trip_id (or create if doesn't exist)
    cursor.execute("""
            SELECT trip_id FROM transfer_trips
            WHERE batch_id = %s AND trip_number = %s
        """, (batch_id, new_trip_number))

    new_trip_row = cursor.fetchone()

    if new_trip_row:
      new_trip_id = new_trip_row['trip_id']
    else:
      # Create new trip
      cursor.execute("""
                INSERT INTO transfer_trips
                (batch_id, trip_number, planned_date, planned_time, status,
                 total_volume_ft3, total_weight_lb, sku_count, created_date, created_by)
                SELECT batch_id, %s, planned_date, planned_time, status,
                       0, 0, 0, GETDATE(), 'hub_manager'
                FROM transfer_trips
                WHERE trip_id = %s
            """, (new_trip_number, old_trip_id))

      cursor.execute("SELECT SCOPE_IDENTITY() AS trip_id")
      new_trip_id = cursor.fetchone()['trip_id']

      # Update assignment
    cursor.execute("""
            UPDATE transfer_trip_assignments
            SET trip_id = %s, trip_number = %s
            WHERE assignment_id = %s
        """, (new_trip_id, new_trip_number, assignment_id))

    # Update old trip totals (subtract)
    cursor.execute("""
            UPDATE transfer_trips
            SET total_volume_ft3 = total_volume_ft3 - %s,
                total_weight_lb = total_weight_lb - %s,
                sku_count = sku_count - 1
            WHERE trip_id = %s
        """, (vol, wt, old_trip_id))

    # Update new trip totals (add)
    cursor.execute("""
            UPDATE transfer_trips
            SET total_volume_ft3 = total_volume_ft3 + %s,
                total_weight_lb = total_weight_lb + %s,
                sku_count = sku_count + 1
            WHERE trip_id = %s
        """, (vol, wt, new_trip_id))

    conn.commit()
    conn.close()

    return {'success': True, 'new_trip_id': int(new_trip_id) if new_trip_id else None}

  except Exception as e:
    import traceback
    print(f"ERROR in update_assignment_trip: {traceback.format_exc()}")
    return {'success': False, 'error': str(e)}


@anvil.server.callable
def update_trip_date_time(trip_id, planned_date, planned_time):
  """Update planned date and time for a trip."""
  try:
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
            UPDATE transfer_trips
            SET planned_date = %s, planned_time = %s
            WHERE trip_id = %s
        """, (planned_date, planned_time, trip_id))

    conn.commit()
    conn.close()

    return {'success': True}

  except Exception as e:
    import traceback
    print(f"ERROR in update_trip_date_time: {traceback.format_exc()}")
    return {'success': False, 'error': str(e)}


# =============================================================================
# v5.18 NEW CALLABLE — remove assignment + cancel rec (zero-Hub-stock path)
# =============================================================================

# Active rec statuses that remove_assignment_cancel_rec may move to
# 'Cancelled'. Deliberately broad across the ACTIVE states (not just
# Trip_Assigned): cancelling the assignment without cancelling the rec
# recreates the immortal-rec loop this feature exists to kill. Terminal
# states (Delivered, Obsolete, Rejected, Cancelled) and on-hold (Paused)
# are never touched — state-guarded UPDATE, v5.9-incident lesson.
_CANCELLABLE_REC_STATUSES = ('Trip_Assigned', 'Applied', 'Auto-Approved', 'Approved')


@anvil.server.callable
def remove_assignment_cancel_rec(assignment_id, reason=None):
  """v5.18: Remove a Proposed assignment from its batch and move the
  underlying recommendation to terminal status 'Cancelled'.

  Triggered from trip_editor when the Hub Manager enters qty 0 on a row
  (RowTemplate2 v5.18 confirms first). Use case: pick slip printed but
  the SKU could not be physically located at the Hub — cycle count
  zeroed e21, EasyMorph zeroed Nexus, and the rec must NOT return to
  'Applied' (the leftover-revert and Cancel Batch paths would feed it
  straight back into the next optimizer run, forever).

  Steps (single transaction):
    1. Fetch the assignment; require tta.status = 'Proposed'.
    2. DELETE the tta row.
    3. Recalc the trip header totals; DELETE the trip if now empty.
    4. State-guarded UPDATE of the rec to 'Cancelled' (only from an
       ACTIVE status — see _CANCELLABLE_REC_STATUSES) with an appended
       note. 'Cancelled' is outside the detection view's active
       allow-list, so the phantom +qty drops out of
       starlite_effective_on_hand immediately, and with live Hub
       available = 0 the view reports 'Hub Empty' and will not raise a
       fresh rec until stock actually returns.
    5. Log ASSIGNMENT_CANCELLED to ai_agent_log.

  Returns flags the client uses to decide how to refresh:
    trip_deleted / batch_deleted — True when the removal emptied them.
  """
  try:
    conn = get_db_connection()
    cursor = conn.cursor()

    # ── Step 1: fetch + validate ──
    cursor.execute("""
        SELECT a.assignment_id, a.batch_id, a.trip_id, a.sku,
               a.qty, a.planned_qty, a.adjusted_qty,
               a.volume_ft3, a.weight_lb, a.status,
               a.recommendation_id, a.pick_line_id
        FROM transfer_trip_assignments a
        WHERE a.assignment_id = %s
    """, (assignment_id,))
    row = cursor.fetchone()
    if not row:
      conn.close()
      return {'success': False, 'error': 'Assignment not found'}
    if row['status'] != 'Proposed':
      conn.close()
      return {
        'success': False,
        'error': (
          f"Assignment is '{row['status']}', not 'Proposed' — only "
          f"un-finalized assignments can be removed this way. Refresh "
          f"the batch and try again."
        ),
      }

    batch_id = row['batch_id']
    trip_id = row['trip_id']
    sku = row['sku']
    rec_id = row['recommendation_id']
    vol = float(row['volume_ft3'] or 0)
    wt = float(row['weight_lb'] or 0)
    qty_display = int(float(row['adjusted_qty'] or row['planned_qty'] or row['qty'] or 0))

    # Live Hub stock for the audit note (LEFT-JOIN semantics via dict
    # default: a missing Hub row in inventory_current means zero —
    # EasyMorph drops the row entirely when a SKU is counted to zero).
    live = get_hub_availability(conn, [sku]).get(
      sku, {'available': 0.0, 'on_hand': 0.0})

    # ── Step 2: delete the assignment ──
    cursor.execute("""
        DELETE FROM transfer_trip_assignments WHERE assignment_id = %s
    """, (assignment_id,))

    # ── Step 3: recalc trip header; delete if empty ──
    trip_deleted = False
    batch_deleted = False
    cursor.execute("""
        SELECT COUNT(*) AS cnt FROM transfer_trip_assignments
        WHERE trip_id = %s
    """, (trip_id,))
    if int(cursor.fetchone()['cnt']) == 0:
      cursor.execute("DELETE FROM transfer_trips WHERE trip_id = %s", (trip_id,))
      trip_deleted = True
      cursor.execute("""
          SELECT COUNT(*) AS cnt FROM transfer_trips WHERE batch_id = %s
      """, (batch_id,))
      batch_deleted = int(cursor.fetchone()['cnt']) == 0
    else:
      cursor.execute("""
          UPDATE transfer_trips
          SET total_volume_ft3 = total_volume_ft3 - %s,
              total_weight_lb = total_weight_lb - %s,
              sku_count = sku_count - 1
          WHERE trip_id = %s
      """, (vol, wt, trip_id))

    # ── Step 4: cancel the rec (state-guarded) ──
    rec_cancelled = False
    old_rec_status = None
    if rec_id is not None:
      cursor.execute("""
          SELECT status FROM ai_transfer_recommendations
          WHERE recommendation_id = %s
      """, (rec_id,))
      rec_row = cursor.fetchone()
      old_rec_status = rec_row['status'] if rec_row else None

      note_reason = (reason or 'SKU could not be located at the Hub').strip()
      cancel_note = (
        f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M')}] "
        f"CANCELLED_NO_HUB_STOCK: removed from batch {batch_id} "
        f"(qty {qty_display}) via trip_editor qty-0. Live Hub "
        f"available={live['available']:.0f}, on_hand={live['on_hand']:.0f}. "
        f"Reason: {note_reason}. (v{__version__})"
      )
      status_placeholders = ','.join(['%s'] * len(_CANCELLABLE_REC_STATUSES))
      cursor.execute(f"""
          UPDATE ai_transfer_recommendations
          SET status = 'Cancelled',
              notes = CONCAT(COALESCE(notes, ''), %s)
          WHERE recommendation_id = %s
            AND status IN ({status_placeholders})
      """, (cancel_note, rec_id, *_CANCELLABLE_REC_STATUSES))
      rec_cancelled = cursor.rowcount > 0

    # ── Step 4b (v5.28): cancel the linked pick line so the staged
    # stock is formally released. The worker returns it to the shelf;
    # the pick event trail records why. Guarded - Picked/Short only.
    pick_line_cancelled = False
    if row.get('pick_line_id') is not None:
      cursor.execute("""
          UPDATE transfer_pick_lines
          SET status = 'Cancelled'
          WHERE pick_line_id = %s AND status IN ('Picked', 'Short')
      """, (row['pick_line_id'],))
      pick_line_cancelled = cursor.rowcount > 0
      if pick_line_cancelled:
        cursor.execute("""
            INSERT INTO transfer_pick_events
                (pick_id, pick_line_id, event_type, performed_by, note)
            SELECT pl.pick_id, pl.pick_line_id, 'CANCELLED', 'hub_manager', %s
            FROM transfer_pick_lines pl WHERE pl.pick_line_id = %s
        """, (
          f"Trip assignment {assignment_id} removed via qty-0; return "
          f"staged stock to shelf. (v{__version__})",
          row['pick_line_id'],
        ))

    # ── Step 5: audit log ──
    cursor.execute("""
        INSERT INTO ai_agent_log (
            action_type, sku, old_value, new_value, reason, executed_by
        )
        VALUES ('ASSIGNMENT_CANCELLED', %s, %s, %s, %s, 'hub_manager')
    """, (
      sku,
      old_rec_status or 'no-rec',
      'Cancelled' if rec_cancelled else 'assignment-only',
      (
        f"Assignment {assignment_id} (qty {qty_display}) removed from "
        f"batch {batch_id} via trip_editor qty-0. Rec "
        f"{rec_id if rec_id is not None else 'NULL'}: "
        + (
          f"{old_rec_status} -> Cancelled."
          if rec_cancelled else
          f"NOT changed (status was '{old_rec_status}')."
        )
        + f" Live Hub available={live['available']:.0f}. "
        + (f"Trip {trip_id} deleted (empty). " if trip_deleted else "")
        + (f"Batch {batch_id} now empty. " if batch_deleted else "")
        + f"(v{__version__})"
      ),
    ))

    conn.commit()
    conn.close()

    return {
      'success': True,
      'sku': sku,
      'recommendation_id': int(rec_id) if rec_id is not None else None,
      'rec_cancelled': rec_cancelled,
      'old_rec_status': old_rec_status,
      'trip_deleted': trip_deleted,
      'batch_deleted': batch_deleted,
      'message': (
        f"{sku} removed from batch"
        + (" and recommendation cancelled" if rec_cancelled else "")
        + "."
      ),
      'version': __version__,
    }

  except Exception as e:
    import traceback
    print(f"ERROR in remove_assignment_cancel_rec: {traceback.format_exc()}")
    return {'success': False, 'error': str(e)}


# =============================================================================
# v5.19 NEW CALLABLE — bulk-cancel zero-Hub-stock recs (stored procedure)
# =============================================================================

@anvil.server.callable
def cancel_zero_stock_recs(dry_run=True, executed_by='hub_manager'):
  """v5.19: Wrapper around dbo.sp_cancel_zero_hub_stock_recs.

  Cancels every active-queue rec (Applied / Auto-Approved) whose LIVE Hub
  availability is <= 0. These are exactly the recs the v5.18 pack-time
  guard skips — they never enter a batch, so the per-row qty-0 path can't
  reach them. Wired to btn_clear_zero_stock on trip_editor v5.19.

  Flow: the form calls dry_run=True first and shows the returned rows in
  a confirm dialog, then calls dry_run=False to execute. Both calls run
  the same candidate SELECT inside the proc, so what the Hub Manager
  confirms is what gets cancelled (modulo an EasyMorph refresh in the
  seconds between — the proc re-asserts the state guard on UPDATE).

  Safe by design: detection ignores zero-balance Hub SKUs, so once stock
  returns a FRESH recommendation is raised automatically. Cancelling
  never loses demand.

  Returns:
    {'success': True, 'dry_run': bool, 'count': int,
     'recs': [{'recommendation_id', 'sku', 'old_status', 'transfer_qty',
               'hub_available_live', 'hub_on_hand_live'}, ...],
     'message': str, 'version': str}

  Serialization note (v5.18.1 lesson): every value is int()/str()-cast
  before it goes into the return dict — pymssql DECIMALs do not survive
  the Anvil server/client boundary. The proc already CASTs to INT, but
  the casts here make this function safe even if the proc changes.
  """
  try:
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
      "EXEC dbo.sp_cancel_zero_hub_stock_recs @executed_by=%s, @dry_run=%s",
      (str(executed_by)[:50], 1 if dry_run else 0),
    )
    rows = cursor.fetchall()

    recs = [{
      'recommendation_id': int(r['recommendation_id']),
      'sku': str(r['sku']),
      'old_status': str(r['old_status']),
      'transfer_qty': int(r['transfer_qty'] or 0),
      'hub_available_live': int(r['hub_available_live'] or 0),
      'hub_on_hand_live': int(r['hub_on_hand_live'] or 0),
    } for r in rows]

    if not dry_run:
      conn.commit()
    conn.close()

    count = len(recs)
    if dry_run:
      message = (
        f"{count} zero-stock SKU(s) eligible to cancel."
        if count else
        "No zero-stock SKUs in the optimizer queue."
      )
    else:
      message = (
        f"Cancelled {count} zero-stock recommendation(s). Fresh recs will "
        f"be raised automatically when Hub stock returns."
        if count else
        "Nothing to cancel — no zero-stock SKUs in the optimizer queue."
      )

    return {
      'success': True,
      'dry_run': bool(dry_run),
      'count': count,
      'recs': recs,
      'message': message,
      'version': __version__,
    }

  except Exception as e:
    import traceback
    print(f"ERROR in cancel_zero_stock_recs: {traceback.format_exc()}")
    return {'success': False, 'error': str(e)}


# =============================================================================
# v5.8 NEW CALLABLES — partial carve-and-finalize flow
# =============================================================================

@anvil.server.callable
def finalize_selected_assignments(batch_id, assignment_ids, planned_date=None):
  """
  v5.8 "carve and finalize" flow — Hub Manager selected a subset of
  assignments in the trip_editor and hit Finalize Trips.

  Behavior:
    1. Validate the input: assignment_ids non-empty, all belong to
       batch_id, all currently Proposed.
    2. Short-circuit: if the caller selected *every* Proposed assignment
       in the batch, delegate to finalize_batch(batch_id). No carve,
       no leftover.
    3. Otherwise, carve the selected assignments into a new mini-batch
       (one Proposed trip, planned_date = user-picked date). Recalculate
       source-trip totals in the original batch; delete any now-empty
       source trips.
    4. Leftover handling (option b, per 2026-04-23 design decision):
       revert the leftover assignments' recommendation_id rows to
       status='Applied' and delete the leftover trip artifacts. The
       next run_truck_optimizer click will re-pack them from scratch.
    5. Call finalize_batch(new_batch_id) to finalize the carved trip —
       reuses the v5.7 cascade and pick-list generation.
    6. Log BATCH_PARTIAL_FINALIZED in ai_agent_log with a reason line
       that references both batch_ids, cardinality, and module version.
  """
  try:
    # --- Input validation ---
    if not assignment_ids:
      return {'success': False, 'error': 'No assignments selected'}

    if planned_date is None:
      # Defensive default: tomorrow (matches run_truck_optimizer default).
      planned_date = (datetime.now() + timedelta(days=1)).date()
    elif isinstance(planned_date, str):
      planned_date = datetime.strptime(planned_date, '%Y-%m-%d').date()

    # Coerce to int list (defend against stray strings from the client)
    try:
      assignment_ids = [int(a) for a in assignment_ids]
    except (TypeError, ValueError):
      return {'success': False, 'error': 'assignment_ids must be integers'}

    conn = get_db_connection()
    cursor = conn.cursor()

    # Verify the selected assignments all belong to batch_id AND are Proposed.
    placeholders = ','.join(['%s'] * len(assignment_ids))
    cursor.execute(f"""
        SELECT assignment_id
        FROM transfer_trip_assignments
        WHERE assignment_id IN ({placeholders})
          AND batch_id = %s
          AND status = 'Proposed'
    """, (*assignment_ids, batch_id))
    valid_rows = cursor.fetchall()
    valid_ids = {int(r['assignment_id']) for r in valid_rows}
    missing = set(assignment_ids) - valid_ids
    if missing:
      conn.close()
      return {
        'success': False,
        'error': (
          f"{len(missing)} assignment(s) could not be finalized — "
          f"they may no longer be in batch {batch_id} or are no "
          f"longer Proposed. Refresh the batch and try again."
        ),
      }

    # Count total Proposed assignments in the batch to detect the
    # "all selected" short-circuit case.
    cursor.execute("""
        SELECT COUNT(*) as cnt
        FROM transfer_trip_assignments
        WHERE batch_id = %s AND status = 'Proposed'
    """, (batch_id,))
    total_proposed = int(cursor.fetchone()['cnt'])

    if len(assignment_ids) >= total_proposed:
      # Caller selected every assignment in the batch — no carve needed.
      conn.close()
      result = finalize_batch(batch_id)
      if result.get('success'):
        result['carve_mode'] = 'all_selected_delegated'
        result['version'] = __version__
      return result

    # --- Carve: move selected assignments into a new mini-batch ---
    new_batch_id = generate_batch_id()

    # Unlikely collision guard — if the generated id already exists, wait
    # and append a carve suffix.
    cursor.execute(
      "SELECT COUNT(*) as cnt FROM transfer_trips WHERE batch_id = %s",
      (new_batch_id,),
    )
    while int(cursor.fetchone()['cnt']) > 0:
      import time as _time
      _time.sleep(0.001)
      new_batch_id = generate_batch_id() + '_c'  # disambiguate
      cursor.execute(
        "SELECT COUNT(*) as cnt FROM transfer_trips WHERE batch_id = %s",
        (new_batch_id,),
      )

    new_trip_id = _carve_assignments_to_new_batch(
      conn, batch_id, new_batch_id, assignment_ids, planned_date
    )

    # --- Leftover handling ---
    cursor.execute("""
        SELECT COUNT(*) as cnt
        FROM transfer_trip_assignments
        WHERE batch_id = %s
    """, (batch_id,))
    leftover_count = int(cursor.fetchone()['cnt'])

    leftover_reverted_count = 0
    if leftover_count == 0:
      leftover_action = 'deleted'
    else:
      leftover_reverted_count = _revert_leftover_batch_to_applied(
        conn, batch_id
      )
      leftover_action = 'reverted_to_applied'

    conn.commit()
    conn.close()

    # --- Finalize the carved batch (reuses v5.7 cascade) ---
    finalize_result = finalize_batch(new_batch_id)
    if not finalize_result.get('success'):
      return {
        'success': False,
        'error': (
          f"Carve succeeded (new batch {new_batch_id}) but "
          f"finalize of the new batch failed: "
          f"{finalize_result.get('error', 'unknown')}"
        ),
        'new_batch_id': new_batch_id,
      }

    # --- Log BATCH_PARTIAL_FINALIZED ---
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO ai_agent_log (
            action_type, sku, old_value, new_value, reason, executed_by
        )
        VALUES ('BATCH_PARTIAL_FINALIZED', NULL, %s, %s, %s, 'hub_manager')
    """, (
      batch_id,
      new_batch_id,
      (
        f"Carved {len(assignment_ids)} of {total_proposed} SKUs "
        f"from {batch_id} to {new_batch_id}; "
        f"leftover={leftover_action}"
        + (
          f" ({leftover_reverted_count} recs reverted to Applied)"
          if leftover_action == 'reverted_to_applied' else ""
        )
        + f" (v{__version__})"
      ),
    ))
    conn.commit()
    conn.close()

    return {
      'success': True,
      'new_batch_id': new_batch_id,
      'source_batch_id': batch_id,
      'carved_count': len(assignment_ids),
      'leftover_action': leftover_action,
      'leftover_reverted_count': leftover_reverted_count,
      'pick_lists': finalize_result.get('pick_lists', []),
      'message': (
        f"Finalized {len(assignment_ids)} SKUs as batch {new_batch_id}. "
        f"Leftover: {leftover_action}"
        + (
          f" ({leftover_reverted_count} recs back to Applied — "
          f"click Run Optimizer to re-pack them)."
          if leftover_action == 'reverted_to_applied' else "."
        )
      ),
      'version': __version__,
    }

  except Exception as e:
    import traceback
    print(f"ERROR in finalize_selected_assignments: {traceback.format_exc()}")
    return {'success': False, 'error': str(e)}


def _carve_assignments_to_new_batch(conn, source_batch_id, new_batch_id,
                                    assignment_ids, planned_date):
  """
  Internal helper — move selected assignments out of source_batch_id into
  a brand-new single-trip batch (new_batch_id) at planned_date.
  """
  cursor = conn.cursor()
  placeholders = ','.join(['%s'] * len(assignment_ids))

  # Step 1 — totals for the new trip header
  cursor.execute(f"""
      SELECT
          COALESCE(SUM(volume_ft3), 0) as vol,
          COALESCE(SUM(weight_lb), 0) as wt,
          COUNT(*) as cnt,
          MIN(planned_time) as pt
      FROM transfer_trip_assignments
      WHERE assignment_id IN ({placeholders})
  """, tuple(assignment_ids))
  totals = cursor.fetchone()
  new_vol = float(totals['vol'] or 0)
  new_wt  = float(totals['wt']  or 0)
  new_cnt = int(totals['cnt'])

  # transfer_trips.planned_time is varchar(5) (HH:MM). The source
  # column on transfer_trip_assignments can come back as a `time`
  # object or a longer string like '08:00:00.0' — normalize to
  # HH:MM before inserting to avoid a 2628 truncation error.
  raw_pt = totals['pt']
  planned_time = (str(raw_pt)[:5] if raw_pt else DEFAULT_TRIP_TIME)

  # Step 2 — new transfer_trips row
  cursor.execute("""
      INSERT INTO transfer_trips (
          batch_id, trip_number, planned_date, planned_time, status,
          total_volume_ft3, total_weight_lb, sku_count,
          created_date, created_by
      )
      VALUES (%s, 1, %s, %s, 'Proposed', %s, %s, %s, GETDATE(), 'partial_finalize')
  """, (new_batch_id, planned_date, planned_time,
        round(new_vol, 2), round(new_wt, 2), new_cnt))

  cursor.execute("SELECT SCOPE_IDENTITY() AS trip_id")
  new_trip_id = int(cursor.fetchone()['trip_id'])

  # Step 3 — move selected assignments to new batch/trip
  cursor.execute(f"""
      UPDATE transfer_trip_assignments
      SET batch_id = %s,
          trip_id = %s,
          trip_number = 1,
          planned_date = %s
      WHERE assignment_id IN ({placeholders})
  """, (new_batch_id, new_trip_id, planned_date, *assignment_ids))

  # Step 4 — recalc totals on every source trip in the original batch.
  cursor.execute("""
      UPDATE t
      SET t.total_volume_ft3 = COALESCE(x.vol, 0),
          t.total_weight_lb  = COALESCE(x.wt,  0),
          t.sku_count        = COALESCE(x.cnt, 0)
      FROM transfer_trips t
      LEFT JOIN (
          SELECT trip_id,
                 SUM(volume_ft3) as vol,
                 SUM(weight_lb)  as wt,
                 COUNT(*)        as cnt
          FROM transfer_trip_assignments
          WHERE batch_id = %s
          GROUP BY trip_id
      ) x ON x.trip_id = t.trip_id
      WHERE t.batch_id = %s
  """, (source_batch_id, source_batch_id))

  # Step 5 — delete source trips that are now empty
  cursor.execute("""
      DELETE t FROM transfer_trips t
      WHERE t.batch_id = %s
        AND NOT EXISTS (
            SELECT 1 FROM transfer_trip_assignments a
            WHERE a.trip_id = t.trip_id
        )
  """, (source_batch_id,))

  return new_trip_id


def _revert_leftover_batch_to_applied(conn, batch_id):
  """
  Internal helper — after carve, revert any assignments that remain in
  batch_id back to Applied status (on their ai_transfer_recommendations
  rows) and delete the leftover trip_assignments + transfer_trips rows.

  The Hub Manager then clicks Run Optimizer to re-pack them into fresh
  trips (option b, v5.8 2026-04-23 design decision).
  Returns: count of recs reverted.
  """
  cursor = conn.cursor()

  cursor.execute("""
      SELECT DISTINCT recommendation_id
      FROM transfer_trip_assignments
      WHERE batch_id = %s AND recommendation_id IS NOT NULL
  """, (batch_id,))
  rec_ids = [row['recommendation_id'] for row in cursor.fetchall()]

  if rec_ids:
    placeholders = ','.join(['%s'] * len(rec_ids))
    cursor.execute(f"""
        UPDATE ai_transfer_recommendations
        SET status = 'Applied'
        WHERE recommendation_id IN ({placeholders})
          AND status = 'Trip_Assigned'
    """, tuple(rec_ids))

  # Delete leftover assignments & trip headers for this batch.
  cursor.execute("""
      DELETE FROM transfer_trip_assignments WHERE batch_id = %s
  """, (batch_id,))
  cursor.execute("""
      DELETE FROM transfer_trips WHERE batch_id = %s
  """, (batch_id,))

  return len(rec_ids)