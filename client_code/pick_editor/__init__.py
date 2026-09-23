# =============================================================================
# pick_editor/__init__.py
# -----------------------------------------------------------------------------
# Version:       1.0.0
# Last updated:  2026-08-01
# Hub Transfers v2.0 - Phase 4: Pick Editor (Hub Manager / Lead, D2).
#
# Two-pane editor per the redesign plan (section 5.1): Open Picks panel on
# top, Unassigned Pool left/middle, the selected pick underneath. All
# business rules live server-side in pick_dashboard.py (v1.0.1+) - this
# form only orchestrates calls and renders results, so every action
# survives a mid-click refresh or a second manager on the same screen
# (server errors surface as alerts, never silent state drift).
#
# Server callables used: get_pick_pool, get_open_picks, get_pick_detail,
#   get_workers, create_pick, add_lines_to_pick, update_line_qty,
#   remove_line, assign_worker, release_pick, cancel_pick, reopen_pick,
#   download_pick_slip_for_pick.
#
# Row -> form events (raised on the RepeatingPanels):
#   rp_open_picks : x-open-pick   (pick_id)
#   rp_pick_lines : x-line-qty    (pick_line_id, qty)
#                   x-line-remove (pick_line_id)
#
# Dates arrive as ISO strings (server serialize_row); we display slices
# of them ([:10] / [:16]) instead of parsing - Anvil's client Python has
# limited datetime support and the pick flow never needs date math here.
# =============================================================================

__version__       = "1.0.0"
__version_date__  = "2026-08-01"

from ._anvil_designer import pick_editorTemplate
from anvil import *
import anvil.server
import anvil.media

STATUS_COLORS = {
  'Draft':       '#9E9E9E',
  'Released':    '#1565C0',
  'In_Progress': '#F9A825',
  'Completed':   '#2E7D32',
  'Cancelled':   '#C62828',
}


class pick_editor(pick_editorTemplate):

  def __init__(self, **properties):
    self.init_components(**properties)
    if hasattr(self, 'nav'):
      try:
        self.nav.active = 'pick_editor'
      except Exception:
        pass

    self.current_pick_id = None
    self.current_pick_status = None
    self._pool_rows = []      # unfiltered pool cache for client-side search

    self.rp_open_picks.set_event_handler('x-open-pick', self.open_pick)
    self.rp_pick_lines.set_event_handler('x-line-qty', self.line_qty_changed)
    self.rp_pick_lines.set_event_handler('x-line-remove', self.line_removed)

    # All component events wired here in code - the designer shell carries
    # no event bindings (ANVIL_AI_PROMPT_7 is layout-only).
    self.btn_new_pick.set_event_handler('click', self.btn_new_pick_click)
    self.btn_refresh.set_event_handler('click', self.btn_refresh_click)
    self.cb_show_completed.set_event_handler('change', self.cb_show_completed_change)
    self.tb_pool_search.set_event_handler('change', self.tb_pool_search_change)
    self.cb_pool_select_all.set_event_handler('change', self.cb_pool_select_all_change)
    self.btn_add_selected.set_event_handler('click', self.btn_add_selected_click)
    self.dd_worker.set_event_handler('change', self.dd_worker_change)
    self.btn_release.set_event_handler('click', self.btn_release_click)
    self.btn_print_slip.set_event_handler('click', self.btn_print_slip_click)
    self.btn_cancel_pick.set_event_handler('click', self.btn_cancel_pick_click)
    self.btn_reopen.set_event_handler('click', self.btn_reopen_click)

    self.load_workers()
    self.refresh_all()

  # ---------------------------------------------------------------- loading

  def refresh_all(self):
    self.load_open_picks()
    self.load_pool()
    if self.current_pick_id is not None:
      self.load_pick(self.current_pick_id)
    else:
      self._render_pick_header()
      self.rp_pick_lines.items = []
    self._update_buttons()

  def load_workers(self):
    try:
      workers = anvil.server.call('get_workers')
    except Exception as e:
      alert("Could not load workers: %s" % e)
      workers = []
    self.dd_worker.items = [(w['display_name'], w['worker_code']) for w in workers]

  def load_open_picks(self):
    include_done = bool(getattr(self, 'cb_show_completed', None)
                        and self.cb_show_completed.checked)
    try:
      picks = anvil.server.call('get_open_picks', include_recent_completed=include_done)
    except Exception as e:
      alert("Could not load picks: %s" % e)
      picks = []
    self.rp_open_picks.items = picks

  def load_pool(self):
    try:
      self._pool_rows = anvil.server.call('get_pick_pool')
    except Exception as e:
      alert("Could not load pool: %s" % e)
      self._pool_rows = []
    self._apply_pool_filter()

  def _apply_pool_filter(self):
    term = (self.tb_pool_search.text or '').strip().lower()
    if term:
      rows = [r for r in self._pool_rows
              if term in str(r['sku']).lower()
              or term in str(r['recommendation_id'])
              or term in str(r.get('location_code') or '').lower()
              or term in str(r.get('part_description') or '').lower()]
    else:
      rows = self._pool_rows
    self.rp_pool.items = rows

  def load_pick(self, pick_id):
    try:
      detail = anvil.server.call('get_pick_detail', pick_id)
    except Exception as e:
      alert("Could not load pick %s: %s" % (pick_id, e))
      self.current_pick_id = None
      self.current_pick_status = None
      self._render_pick_header()
      self.rp_pick_lines.items = []
      self._update_buttons()
      return
    header = detail['header']
    self.current_pick_id = header['pick_id']
    self.current_pick_status = header['status']
    self._render_pick_header(header)
    editable = header['status'] in ('Draft', 'Released')
    lines = detail['lines']
    for ln in lines:
      ln['_editable'] = editable
    self.rp_pick_lines.items = lines
    self._update_buttons()

  # -------------------------------------------------------------- rendering

  def _render_pick_header(self, header=None):
    if not header:
      self.lbl_pick_title.text = "No pick selected"
      self.lbl_pick_status.text = ""
      self.lbl_pick_status.background = None
      self.dd_worker.selected_value = None
      return
    self.lbl_pick_title.text = "Pick %s  (created %s by %s)" % (
      header['pick_id'],
      (header.get('created_date') or '')[:10],
      header.get('created_by') or '?')
    status = header['status']
    self.lbl_pick_status.text = status.replace('_', ' ')
    self.lbl_pick_status.background = STATUS_COLORS.get(status, '#9E9E9E')
    self.lbl_pick_status.foreground = '#FFFFFF'
    self.dd_worker.selected_value = header.get('assigned_worker_code')

  def _update_buttons(self):
    s = self.current_pick_status
    has_pick = self.current_pick_id is not None
    self.btn_release.enabled = has_pick and s == 'Draft'
    self.btn_print_slip.enabled = has_pick and s in ('Draft', 'Released', 'In_Progress', 'Completed')
    self.btn_cancel_pick.enabled = has_pick and s in ('Draft', 'Released', 'In_Progress')
    self.btn_reopen.enabled = has_pick and s == 'Completed'
    self.dd_worker.enabled = has_pick and s in ('Draft', 'Released', 'In_Progress')
    self.btn_add_selected.enabled = (not has_pick) or s in ('Draft', 'Released')

  # ----------------------------------------------------------- panel events

  def cb_show_completed_change(self, **event_args):
    self.load_open_picks()

  def btn_refresh_click(self, **event_args):
    self.refresh_all()

  def tb_pool_search_change(self, **event_args):
    self._apply_pool_filter()

  def cb_pool_select_all_change(self, **event_args):
    checked = self.cb_pool_select_all.checked
    for row in self.rp_pool.items or []:
      row['_selected'] = checked
    self.rp_pool.items = list(self.rp_pool.items or [])

  def open_pick(self, pick_id, **event_args):
    self.load_pick(pick_id)

  # ----------------------------------------------------------- pick actions

  def btn_new_pick_click(self, **event_args):
    try:
      pick_id = anvil.server.call('create_pick', created_by='Hub Manager')
    except Exception as e:
      alert("Could not create pick: %s" % e)
      return
    self.load_open_picks()
    self.load_pick(pick_id)

  def btn_add_selected_click(self, **event_args):
    lines = []
    for row in self.rp_pool.items or []:
      if row.get('_selected'):
        qty = row.get('_assign_qty')
        if qty is None:
          qty = row.get('remaining_qty') or 0
        lines.append({'recommendation_id': row['recommendation_id'],
                      'qty': float(qty)})
    if not lines:
      alert("Nothing selected in the pool.")
      return

    if self.current_pick_id is None or self.current_pick_status not in ('Draft', 'Released'):
      try:
        self.current_pick_id = anvil.server.call('create_pick', created_by='Hub Manager')
        self.current_pick_status = 'Draft'
      except Exception as e:
        alert("Could not create pick: %s" % e)
        return

    try:
      result = anvil.server.call('add_lines_to_pick', self.current_pick_id, lines,
                                 performed_by='Hub Manager')
    except Exception as e:
      alert("Add failed: %s" % e)
      self.refresh_all()
      return
    if result.get('errors'):
      alert("Added %s line(s); %s problem(s):\n\n%s"
            % (result.get('added', 0), len(result['errors']),
               "\n".join(result['errors'])))
    self.cb_pool_select_all.checked = False
    self.refresh_all()

  def dd_worker_change(self, **event_args):
    if self.current_pick_id is None or not self.dd_worker.selected_value:
      return
    try:
      anvil.server.call('assign_worker', self.current_pick_id,
                        self.dd_worker.selected_value, performed_by='Hub Manager')
    except Exception as e:
      alert("Assign failed: %s" % e)
    self.load_open_picks()
    self.load_pick(self.current_pick_id)

  def btn_release_click(self, **event_args):
    if self.current_pick_id is None:
      return
    if not confirm("Release pick %s to the assigned worker's tablet?" % self.current_pick_id):
      return
    try:
      anvil.server.call('release_pick', self.current_pick_id, performed_by='Hub Manager')
    except Exception as e:
      alert("Release failed: %s" % e)
    self.refresh_all()

  def btn_print_slip_click(self, **event_args):
    if self.current_pick_id is None:
      return
    try:
      result = anvil.server.call('download_pick_slip_for_pick', self.current_pick_id)
    except Exception as e:
      alert("Slip failed: %s" % e)
      return
    if result.get('status') == 'success':
      anvil.media.download(result['pdf_media'])
    else:
      alert(result.get('message', 'Slip generation failed.'))

  def btn_cancel_pick_click(self, **event_args):
    if self.current_pick_id is None:
      return
    if not confirm("Cancel pick %s? Its unpicked lines return to the pool. "
                   "(Blocked if any line is already recorded.)" % self.current_pick_id):
      return
    try:
      anvil.server.call('cancel_pick', self.current_pick_id, performed_by='Hub Manager')
      self.current_pick_id = None
      self.current_pick_status = None
    except Exception as e:
      alert("Cancel failed: %s" % e)
    self.refresh_all()

  def btn_reopen_click(self, **event_args):
    if self.current_pick_id is None:
      return
    if not confirm("Reopen completed pick %s for corrections?" % self.current_pick_id):
      return
    try:
      anvil.server.call('reopen_pick', self.current_pick_id, performed_by='Hub Manager')
    except Exception as e:
      alert("Reopen failed: %s" % e)
    self.load_open_picks()
    self.load_pick(self.current_pick_id)

  # ------------------------------------------------------------ line events

  def line_qty_changed(self, pick_line_id, qty, **event_args):
    try:
      anvil.server.call('update_line_qty', pick_line_id, qty, performed_by='Hub Manager')
    except Exception as e:
      alert("Qty change rejected: %s" % e)
    self.load_pool()
    if self.current_pick_id is not None:
      self.load_pick(self.current_pick_id)

  def line_removed(self, pick_line_id, **event_args):
    try:
      anvil.server.call('remove_line', pick_line_id, performed_by='Hub Manager')
    except Exception as e:
      alert("Remove rejected: %s" % e)
    self.refresh_all()
