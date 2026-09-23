"""
driver_delivery — Anvil client form

Single-form UI with three internal views:
  view_state = 'home'    → cp_home      (trip list)
  view_state = 'confirm' → cp_confirm   (per-SKU grid)
  view_state = 'success' → cp_success   (post-submit receipt)

The two RepeatingPanels consume child templates:
  rp_trips.item_template  = driver_delivery.TripRowTemplate
  rp_lines.item_template  = driver_delivery.SkuLineTemplate

Server calls (all in server_code/driver_delivery.py):
  list_trips_awaiting_delivery()
  get_trip_assignments_for_delivery(trip_id)
  submit_delivery_confirmation(trip_id, lines, driver_name)
"""

from ._anvil_designer import driver_deliverTemplate
from anvil import *
import anvil.server


class driver_deliver(driver_deliverTemplate):
  def __init__(self, **properties):
    self.init_components(**properties)

    # Per-form state
    self.view_state = 'home'
    self.active_trip = None          # dict returned by list_trips_awaiting_delivery
    self.active_lines = []           # list of dicts from get_trip_assignments_for_delivery


    # Wire events
    self.btn_refresh.set_event_handler('click', self.btn_refresh_click)
    self.btn_back.set_event_handler('click', self.btn_back_click)
    self.btn_submit.set_event_handler('click', self.btn_submit_click)
    self.btn_deliver_another.set_event_handler('click', self.btn_deliver_another_click)

    # v2.0 Phase 7: fail-closed delivery auth needs an identity. Build
    # a "Delivering as" picker in code (no designer change) from the
    # same hub_workers list the pick kiosk uses, and hold the trip list
    # until a name is tapped.
    self.worker_code = None
    self._dd_worker = DropDown(include_placeholder=True,
                               placeholder='Delivering as… (tap your name)')
    try:
      _workers = anvil.server.call('get_workers')
    except Exception:
      _workers = []
    self._dd_worker.items = [(w['display_name'], w['worker_code'])
                             for w in _workers]
    self._dd_worker.set_event_handler('change', self._worker_changed)
    self.cp_home.add_component(self._dd_worker, index=0)

    # Initial state: prompt for identity instead of loading trips.
    self.load_trips()

  def _worker_changed(self, **event_args):
    self.worker_code = self._dd_worker.selected_value
    self.load_trips()

  # ========================================================================
  # VIEW SWAPPING
  # ========================================================================

  def _show_view(self, name):
    self.view_state = name
    self.cp_home.visible    = (name == 'home')
    self.cp_confirm.visible = (name == 'confirm')
    self.cp_success.visible = (name == 'success')

  # ========================================================================
  # HOME VIEW — trip list
  # ========================================================================

  def load_trips(self):
    # v2.0 Phase 7: no identity -> no data (mirrors the server's
    # fail-closed gate, with a friendlier prompt).
    if not getattr(self, 'worker_code', None):
      self.rp_trips.items = []
      self.lbl_empty_state.text = "Tap your name above to see trips awaiting delivery."
      self.lbl_empty_state.visible = True
      self._show_view('home')
      return
    try:
      trips = anvil.server.call('list_trips_awaiting_delivery',
                                worker_code=self.worker_code) or []
    except anvil.server.PermissionDenied as e:
      alert(str(e))
      trips = []
    except Exception as e:
      alert(f"Error loading trips: {e}")
      trips = []

    self.rp_trips.items = trips
    self.lbl_empty_state.visible = (len(trips) == 0)
    self._show_view('home')

  def btn_refresh_click(self, **event_args):
    self.load_trips()
    Notification("Refreshed").show()

  def on_trip_selected(self, trip):
    """Called by TripRowTemplate when the driver taps a trip row."""
    self.active_trip = trip
    try:
      response = anvil.server.call(
        'get_trip_assignments_for_delivery',
        trip['trip_id'],
        worker_code=self.worker_code
      ) or {}
    except Exception as e:
      alert(f"Error loading trip assignments: {e}")
      return

    # Server returns an envelope: {'trip': {...}, 'lines': [...]} on success,
    # or {'error': '...'} on a soft failure (trip not Finalized, etc.).
    if response.get('error'):
      alert(response['error'])
      return

    lines = response.get('lines', []) or []

    # Each line has: assignment_id, sku, part_desc, planned_qty, ...
    # SkuLineTemplate will mutate these in place for delivered_qty / note.
    for line in lines:
      if line.get('delivered_qty') is None:
        line['delivered_qty'] = line.get('planned_qty')
      if line.get('delivery_note') is None:
        line['delivery_note'] = ''

    self.active_lines = lines
    self.rp_lines.items = self.active_lines

    # Defensive empty-state: the orphan-trip cleanup + server EXISTS
    # filter should prevent this from ever happening, but if a trip
    # somehow reaches the driver with no assignment rows, show a
    # clear message and disable Submit instead of rendering blank.
    is_empty = (len(self.active_lines) == 0)
    self.lbl_confirm_empty.visible = is_empty
    self.rp_lines.visible = not is_empty
    self.btn_submit.enabled = not is_empty

    # Header like "Trip #7 — 2026-04-24 08:00  (Batch 42)"
    planned_date = trip.get('planned_date') or ''
    planned_time = trip.get('planned_time') or ''
    self.lbl_confirm_title.text = (
      f"Trip #{trip.get('trip_number', '?')} — "
      f"{planned_date} {planned_time}  "
      f"(Batch {trip.get('batch_id', '?')})"
    )
    self.txt_driver_name.text = ""
    self._show_view('confirm')

  # ========================================================================
  # CONFIRM VIEW — submit handler
  # ========================================================================

  def btn_back_click(self, **event_args):
    self._show_view('home')

  def btn_submit_click(self, **event_args):
    if not self.active_trip:
      return

    # Build payload: one entry per assignment line
    # SkuLineTemplate mutates each self.item dict in self.active_lines, so
    # we can just read back the list we already hold.
    lines = []
    for line in self.active_lines:
      try:
        dq = float(line.get('delivered_qty', 0) or 0)
      except (TypeError, ValueError):
        alert(f"Invalid quantity on {line['sku']} — please enter a number.")
        return
      if dq < 0:
        alert(f"Quantity on {line['sku']} cannot be negative.")
        return
      lines.append({
        'assignment_id': line['assignment_id'],
        'delivered_qty': dq,
        # Server-side key is 'note' (see submit_delivery_confirmation in
        # server_code/driver_delivery.py). Keep the local UI key as
        # 'delivery_note' but send what the server expects.
        'note': (line.get('delivery_note') or '').strip() or None,
      })

    if not lines:
      alert("No lines to submit.")
      return

    # Confirm if anything diverges from planned
    discrepancies = [
      l for l, orig in zip(lines, self.active_lines)
      if float(l['delivered_qty']) != float(orig.get('planned_qty', 0) or 0)
      or l['note']
    ]
    if discrepancies:
      if not confirm(
        f"{len(discrepancies)} line(s) differ from planned. "
        "The Hub Manager will be notified. Continue?"
      ):
        return

    try:
      result = anvil.server.call(
        'submit_delivery_confirmation',
        self.active_trip['trip_id'],
        lines,
        (self.txt_driver_name.text or '').strip() or None,
        worker_code=self.worker_code,
      )
    except anvil.server.PermissionDenied as e:
      alert(str(e))
      return
    except Exception as e:
      alert(f"Error submitting delivery: {e}")
      return

    if not result or not result.get('success'):
      alert(f"Delivery not recorded: {result.get('error', 'unknown error')}")
      return

    # Success view
    # Server returns 'assignments_delivered' (rowcount from the DB update).
    self.lbl_success_detail.text = (
      f"Trip #{self.active_trip.get('trip_number', '?')} recorded. "
      f"{result.get('assignments_delivered', len(lines))} line(s) delivered. "
      + ("Hub Manager notified of discrepancies." if discrepancies else "")
    )
    self._show_view('success')

  # ========================================================================
  # SUCCESS VIEW
  # ========================================================================

  def btn_deliver_another_click(self, **event_args):
    self.active_trip = None
    self.active_lines = []
    self.load_trips()

  @handle("btn_home", "click")
  def btn_home_click(self, **event_args):
    # Clear #deliver first or transfer_recs' startup routing bounces back here.
    set_url_hash('')
    open_form('transfer_recs')