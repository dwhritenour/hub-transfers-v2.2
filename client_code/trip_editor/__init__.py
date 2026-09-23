# =============================================================================
# trip_editor/__init__.py
# -----------------------------------------------------------------------------
# Version:       3.2.1
# Last updated:  2026-08-30
# Hub Transfers v2.1 - Trip Editor v3: THE DOCK'S TOOL.
#
# Ruling (Dave, 2026-08-30): only dock personnel use the Trip Editor.
# The Hub Manager's job ends at pick completion; the people loading the
# truck - on a full PC at the dock - own the whole trip stage. Reached
# from the app menu or directly via <app-url>#dock (mirrors #pick).
#
# The flow is FINALIZE -> LOAD -> DEPART ("finalize before reality"):
#   PANEL A (staged): every completed-pick line not on a live trip, as a
#     tabular grid - qty, weight, volume, pick #, staged-age badge, and
#     TIME/INVENTORY URGENCY on every row (Hub request). The capacity
#     summary live-updates against truck_cap_ft3 / truck_cap_lb as lines
#     are checked, BEFORE anything is committed. v3.2.0: the Lead plans
#     by hand - no optimizer buttons (a pre-v2.1 Proposed batch, if any,
#     still pre-checks lines via is_suggested).
#     FINALIZE TRUCK creates the trip (Finalized) and the manifest.
#   PANEL B (load): the same lines with load actions - ✔ LOADED, SHORT
#     (reason + manager SMS, D-3), Remove (back to staged), Undo.
#     TRUCK DEPARTED refuses until every line is resolved, then requires
#     a per-line "still at the Hub" acknowledgment for anything left in
#     STAGED (server re-verifies - the gate cannot be skipped).
#
# No role gating (D-5): any active worker; every action records who.
# One open truck per worker (D-6, server-enforced).
#
# Server callables used (load_dashboard): get_workers (pick_dashboard),
#   get_staged_lines, get_dock_capacity, get_open_truck, get_truck,
#   finalize_truck, record_line_loaded, record_short_load,
#   undo_line_load, remove_line_from_truck, depart_truck,
#   download_manifest, get_load_discrepancy_codes.
#
# Row -> form events:
#   rp_dock_staged : x-toggle (pick_line_id, selected)
#   rp_dock_load   : x-load / x-short / x-unload / x-remove
#
# v3.0.0 replaces the manager-era trip editor (proposed-batch review,
# partial finalize, zero-stock clear, cancel-rec) - those manager
# decisions live elsewhere; the dock removes lines from trucks, never
# kills recommendations.
#
# v3.1.0 (Dave, 2026-08-30): both grids rebuilt on DataGrid
# (tonal-data-grid, like the original trip editor) - the DataGrid owns
# the header and column alignment. Plain text columns render from *_disp
# data keys; the row forms build only the behavioral/colored cells.
#
# v3.1.1 (Dave, 2026-08-31): columns moved INTO THE DESIGNER (dg_staged /
# dg_load columns + rows_per_page are designer properties now, editable
# there like the original trip editor). This code no longer assigns
# columns; the column ids in the designer are load-bearing - the row
# forms place components by them (sel, age, turg, iurg, actions) and
# the data_keys map the *_disp values this form injects.
#
# v3.1.2 (Dave, 2026-08-31): tighter still-at-Hub ack dialog (no
# vertical spacing between the item checkboxes); dropped the unused
# loc_disp injection for the load grid (its Location column was removed
# in the designer - the units are no longer at that location).
#
# v3.2.0 (Dave, 2026-09-01, pre-cutover): RUN OPTIMIZER and CONSOLIDATE
# removed from the dock UI. The Hub Lead planning each trip at the
# truck - live weight/volume vs capacity, urgency colors, age badges -
# IS the optimizer now. transfer_truck_optimizer stays server-side,
# dormant; one button brings it back if the pilot wants a suggestion
# helper. is_suggested tint logic kept (inert without Proposed batches).
#
# v3.2.1 (Dave, 2026-09-04): HOME button in the top bar - clears the
# #dock URL hash BEFORE opening the startup form (transfer_recs routes
# on the hash at startup; without clearing it, Home would bounce
# straight back here).
# =============================================================================

__version__       = "3.2.1"
__version_date__  = "2026-08-30"

from ._anvil_designer import trip_editorTemplate
from anvil import *
import anvil.server
import anvil.media

def _fmt(v):
  try:
    f = float(v)
    return str(int(f)) if f == int(f) else "%.1f" % f
  except Exception:
    return str(v)


class trip_editor(trip_editorTemplate):

  def __init__(self, **properties):
    self.init_components(**properties)

    self.worker_code = None
    self.worker_name = None
    self.current_trip_id = None
    self._codes = None
    self._capacity = None

    self.dd_dock_worker.set_event_handler('change', self.worker_changed)
    self.btn_dock_refresh.set_event_handler('click', self.refresh_click)
    self.btn_dock_home.set_event_handler('click', self.home_click)

    self.rp_dock_staged.set_event_handler('x-toggle', self.staged_toggled)
    self.btn_select_none.set_event_handler('click', self.select_none_click)
    self.btn_finalize_truck.set_event_handler('click', self.finalize_click)

    self.rp_dock_load.set_event_handler('x-load', self.load_line)
    self.rp_dock_load.set_event_handler('x-short', self.short_line)
    self.rp_dock_load.set_event_handler('x-unload', self.unload_line)
    self.rp_dock_load.set_event_handler('x-remove', self.remove_line)
    self.btn_manifest.set_event_handler('click', self.manifest_click)
    self.btn_depart.set_event_handler('click', self.depart_click)

    try:
      workers = anvil.server.call('get_workers')
      self.dd_dock_worker.items = [(w['display_name'], w['worker_code'])
                                   for w in workers]
    except Exception as e:
      alert("Could not load workers: %s" % e)

  # ------------------------------------------------------------ helpers

  def _show(self, panel):
    self.cp_dock_staged.visible = panel == 'staged'
    self.cp_dock_load.visible = panel == 'load'

  def _capacity_params(self):
    if self._capacity is None:
      try:
        self._capacity = anvil.server.call('get_dock_capacity')
      except Exception:
        self._capacity = {'volume_ft3': 1456.0, 'weight_lb': 13000.0}
    return self._capacity

  def _load_codes(self):
    if self._codes is None:
      try:
        self._codes = anvil.server.call('get_load_discrepancy_codes')
      except Exception as e:
        alert("Could not load reason codes: %s" % e)
        self._codes = []
    return self._codes

  # ------------------------------------------------------------ screens

  def enter_dock(self):
    """After a worker is chosen: resume an open truck or show staged."""
    try:
      open_truck = anvil.server.call('get_open_truck', self.worker_code)
    except Exception as e:
      alert("Could not check for an open truck: %s" % e)
      return
    if open_truck:
      self._render_load(open_truck)
    else:
      self.show_staged()

  def show_staged(self):
    try:
      staged = anvil.server.call('get_staged_lines')
    except Exception as e:
      alert("Could not load staged lines: %s" % e)
      staged = []
    for s in staged:
      s['_sel'] = bool(s.get('is_suggested'))
      s['qty_disp'] = _fmt(s.get('qty'))
      s['wt_disp'] = _fmt(s.get('total_weight_lb'))
      s['vol_disp'] = _fmt(s.get('total_volume_ft3'))
      s['pick_disp'] = "P%s" % s.get('pick_id')
      s['loc_disp'] = s.get('location_code') or '--'
      s['desc_disp'] = s.get('part_description') or ''
    self.rp_dock_staged.items = staged
    self.lbl_no_staged.visible = not staged
    self.lbl_staged_title.text = ("Staged for transfer — %s line%s"
                                  % (len(staged), '' if len(staged) == 1 else 's'))
    self._update_capacity_summary()
    self._show('staged')

  def _update_capacity_summary(self):
    cap = self._capacity_params()
    sel = [s for s in (self.rp_dock_staged.items or []) if s.get('_sel')]
    vol = sum(float(s.get('total_volume_ft3') or 0) for s in sel)
    wt = sum(float(s.get('total_weight_lb') or 0) for s in sel)

    def paint(lbl, label, used, limit, unit):
      pct = (used / limit * 100.0) if limit else 0.0
      lbl.text = "%s: %s / %s %s  (%.0f%%)" % (
        label, "{:,.0f}".format(used), "{:,.0f}".format(limit), unit, pct)
      lbl.foreground = ('#C62828' if pct > 100
                        else '#EF6C00' if pct >= 80 else '#2E7D32')

    paint(self.lbl_util_vol, "Volume", vol, cap['volume_ft3'], "ft³")
    paint(self.lbl_util_wt, "Weight", wt, cap['weight_lb'], "lb")

    n = len(sel)
    self.btn_finalize_truck.enabled = n > 0
    self.btn_finalize_truck.text = ("FINALIZE TRUCK  (%s line%s)"
                                    % (n, '' if n == 1 else 's')) if n \
    else "FINALIZE TRUCK  (check lines)"

  def show_load(self, trip_id):
    try:
      truck = anvil.server.call('get_truck', trip_id)
    except Exception as e:
      alert("Could not load truck %s: %s" % (trip_id, e))
      self.show_staged()
      return
    self._render_load(truck)

  def _render_load(self, truck):
    codes = self._load_codes()
    header = truck['header']
    lines = truck['lines']
    self.current_trip_id = header['trip_id']
    resolved = sum(1 for l in lines if l.get('loaded_date'))
    self.lbl_truck_title.text = ("TRUCK %s  —  %s of %s loaded"
                                 % (header['trip_id'], resolved, len(lines)))
    self.lbl_truck_sub.text = ("Finalized by %s — load against this list; "
                               "the manifest is the shipping paper."
                               % (header.get('finalized_by_name')
                                  or header.get('finalized_by') or '?'))
    for l in lines:
      l['_codes'] = codes
      l['_worker'] = self.worker_code
      l['qty_disp'] = _fmt(l.get('target_qty'))
      l['wt_disp'] = _fmt(l.get('total_weight_lb'))
      l['vol_disp'] = _fmt(l.get('total_volume_ft3'))
    self.rp_dock_load.items = lines

    unresolved = len(lines) - resolved
    self.btn_depart.enabled = (unresolved == 0 and len(lines) > 0)
    self.btn_depart.background = '#2E7D32' if self.btn_depart.enabled else None
    self.btn_depart.foreground = '#FFFFFF' if self.btn_depart.enabled else None
    self.btn_depart.text = ("TRUCK DEPARTED" if unresolved == 0
                            else "TRUCK DEPARTED  (%s line%s left)"
                            % (unresolved, '' if unresolved == 1 else 's'))
    self._show('load')

  # ------------------------------------------------------------ top bar

  def worker_changed(self, **event_args):
    code = self.dd_dock_worker.selected_value
    if not code:
      return
    self.worker_code = code
    for name, c in self.dd_dock_worker.items:
      if c == code:
        self.worker_name = name
    self.lbl_dock_worker.text = "Working as: %s" % (self.worker_name or code)
    self.enter_dock()

  def refresh_click(self, **event_args):
    self._capacity = None
    if self.cp_dock_load.visible and self.current_trip_id:
      self.show_load(self.current_trip_id)
    elif self.worker_code:
      self.enter_dock()

  def home_click(self, **event_args):
    # Clear #dock first or transfer_recs' startup routing bounces back.
    set_url_hash('')
    open_form('transfer_recs')

  # ------------------------------------------------------------ staged

  def staged_toggled(self, pick_line_id, selected, **event_args):
    for s in (self.rp_dock_staged.items or []):
      if s['pick_line_id'] == pick_line_id:
        s['_sel'] = selected
    self._update_capacity_summary()

  def select_none_click(self, **event_args):
    for s in (self.rp_dock_staged.items or []):
      s['_sel'] = False
    self.rp_dock_staged.items = self.rp_dock_staged.items
    self._update_capacity_summary()

  def finalize_click(self, **event_args):
    if not self.worker_code:
      alert("Select your name first.")
      return
    selected = [s for s in (self.rp_dock_staged.items or []) if s.get('_sel')]
    if not selected:
      alert("Check the lines going on this truck first.")
      return
    cap = self._capacity_params()
    vol = sum(float(s.get('total_volume_ft3') or 0) for s in selected)
    wt = sum(float(s.get('total_weight_lb') or 0) for s in selected)
    msg = ("Finalize this truck with %s line(s)?\n\n"
           "Volume: %s / %s ft³\nWeight: %s / %s lb\n\n"
           "The trip record is created NOW, before loading. Load only "
           "what is on this list." %
           (len(selected), "{:,.0f}".format(vol),
            "{:,.0f}".format(cap['volume_ft3']),
            "{:,.0f}".format(wt), "{:,.0f}".format(cap['weight_lb'])))
    if vol > cap['volume_ft3'] or wt > cap['weight_lb']:
      msg = "⚠ OVER TRUCK CAPACITY.\n\n" + msg
    if not confirm(msg):
      return
    try:
      result = anvil.server.call('finalize_truck', self.worker_code,
                                 [s['pick_line_id'] for s in selected])
    except Exception as e:
      alert("Finalize failed: %s" % e)
      self.show_staged()
      return
    Notification("Truck %s finalized — load against the list"
                 % result['trip_id'], style='success').show()
    self.show_load(result['trip_id'])

  # ------------------------------------------------------------ loading

  def load_line(self, assignment_id, **event_args):
    try:
      anvil.server.call('record_line_loaded', assignment_id, self.worker_code)
    except Exception as e:
      alert(str(e))
    self.show_load(self.current_trip_id)

  def short_line(self, assignment_id, qty, discrepancy_code, note, **event_args):
    try:
      anvil.server.call('record_short_load', assignment_id, self.worker_code,
                        qty, discrepancy_code, note)
    except Exception as e:
      alert(str(e))
    self.show_load(self.current_trip_id)

  def unload_line(self, assignment_id, **event_args):
    try:
      anvil.server.call('undo_line_load', assignment_id, self.worker_code)
    except Exception as e:
      alert(str(e))
    self.show_load(self.current_trip_id)

  def remove_line(self, assignment_id, reason, **event_args):
    try:
      anvil.server.call('remove_line_from_truck', assignment_id,
                        self.worker_code, reason)
    except Exception as e:
      alert(str(e))
    self.show_load(self.current_trip_id)

  def manifest_click(self, **event_args):
    try:
      pdf = anvil.server.call('download_manifest', self.current_trip_id)
      anvil.media.download(pdf)
    except Exception as e:
      alert("Manifest failed: %s" % e)

  # ------------------------------------------------------------ depart

  def depart_click(self, **event_args):
    if not confirm("Truck %s is leaving?" % self.current_trip_id):
      return
    self._attempt_depart(acked=[])

  def _attempt_depart(self, acked):
    try:
      result = anvil.server.call('depart_truck', self.current_trip_id,
                                 self.worker_code, acked)
    except Exception as e:
      alert(str(e))
      self.show_load(self.current_trip_id)
      return
    if result.get('departed'):
      Notification("Truck %s departed — %s line(s), %s short, %s staged "
                   "acknowledged" % (self.current_trip_id, result['lines'],
                                     result['shorts'], result['acknowledged']),
                   style='success', timeout=6).show()
      self.current_trip_id = None
      self.enter_dock()
      return
    self._ack_dialog(result.get('needs_ack') or [])

  def _ack_dialog(self, staged):
    panel = ColumnPanel()
    panel.add_component(Label(
      text="These staged items are NOT on this truck. Tick each one to "
      "confirm it is still at the Hub:", bold=True))
    boxes = []
    for s in staged:
      cb = CheckBox(text="%s  ×%g  (pick %s, %s)"
                    % (s['sku'], float(s['qty']), s['pick_id'],
                       s.get('location_code') or '--'),
                    spacing_above='none', spacing_below='none')
      cb.tag = s['pick_line_id']
      panel.add_component(cb)
      boxes.append(cb)
    if alert(content=panel, title="Still at the Hub?", large=True,
             buttons=[("CONFIRM ALL TICKED", True), ("CANCEL", False)]):
      unticked = [b for b in boxes if not b.checked]
      if unticked:
        alert("%s item(s) not ticked. Every staged item must be confirmed "
              "(or loaded on a truck) before departure." % len(unticked))
        self.show_load(self.current_trip_id)
        return
      self._attempt_depart(acked=[b.tag for b in boxes])
    else:
      self.show_load(self.current_trip_id)