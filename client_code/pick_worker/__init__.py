# =============================================================================
# pick_worker/__init__.py
# -----------------------------------------------------------------------------
# Version:       1.0.2
# Last updated:  2026-08-30
# Hub Transfers v2.1 - Phase 5: shared-kiosk picker tablet (D1/D3).
#
# v1.0.2: the short-lived Truck tab (v1.1.0, 2026-08-30) is WITHDRAWN -
# truck loading lives in Trip Editor v3 on the dock PC (#dock), per
# Dave's ruling. This form is picking only, functionally identical to
# v1.0.0. Urgency chips were added to PickTaskRow (its own v1.1.0).
#
# One form, three stacked panels toggled by visibility:
#   cp_workers -> tap your name (hub_workers buttons; lead sees all picks)
#   cp_picks   -> My Picks cards (priority left-border from urgency_rank)
#   cp_detail  -> one card per line, location-first, per-tap server saves
#
# Every tap commits server-side immediately (record_pick_line), so a
# worker can walk away mid-pick and resume, and a dropped Wi-Fi link
# costs at most the tap in flight - the server's status guards make a
# retry safe (double-tap = clean "already recorded" error, re-synced by
# reload). No NavSidebar on purpose: the kiosk route is
# <app-url>#pick (see transfer_recs startup routing).
#
# Server callables used: get_workers, get_my_picks, get_pick_detail,
#   get_discrepancy_codes, record_pick_line, undo_line, complete_pick.
#
# Row -> form events:
#   rp_workers  : x-worker (worker_code, display_name)
#   rp_my_picks : x-open   (pick_id)
#   rp_tasks    : x-record (pick_line_id, qty, discrepancy_code, note)
#                 x-undo   (pick_line_id)
# =============================================================================

__version__       = "1.0.2"
__version_date__  = "2026-08-30"

from ._anvil_designer import pick_workerTemplate
from anvil import *
import anvil.server


class pick_worker(pick_workerTemplate):

  def __init__(self, **properties):
    self.init_components(**properties)

    self.worker_code = None
    self.worker_name = None
    self.current_pick_id = None
    self._codes = None          # discrepancy codes, fetched once per session

    self.rp_workers.set_event_handler('x-worker', self.worker_chosen)
    self.rp_my_picks.set_event_handler('x-open', self.open_pick)
    self.rp_tasks.set_event_handler('x-record', self.record_line)
    self.rp_tasks.set_event_handler('x-undo', self.undo_line)

    self.btn_switch_user.set_event_handler('click', self.switch_user)
    self.btn_kiosk_refresh.set_event_handler('click', self.refresh_current)
    self.btn_back_to_picks.set_event_handler('click', self.back_to_picks)
    self.btn_complete_pick.set_event_handler('click', self.complete_pick_click)

    self.show_workers()

  # ------------------------------------------------------------ screen mgmt

  def _show(self, panel):
    self.cp_workers.visible = panel == 'workers'
    self.cp_picks.visible = panel == 'picks'
    self.cp_detail.visible = panel == 'detail'

  def show_workers(self):
    self.worker_code = None
    self.worker_name = None
    self.current_pick_id = None
    self.lbl_worker_name.text = ""
    self.btn_switch_user.visible = False
    try:
      self.rp_workers.items = anvil.server.call('get_workers')
    except Exception as e:
      alert("Could not load workers: %s" % e)
      self.rp_workers.items = []
    self._show('workers')

  def show_my_picks(self):
    try:
      picks = anvil.server.call('get_my_picks', self.worker_code)
    except Exception as e:
      alert("Could not load picks: %s" % e)
      picks = []
    self.rp_my_picks.items = picks
    self.lbl_no_picks.visible = not picks
    self._show('picks')

  def show_detail(self, pick_id):
    if self._codes is None:
      try:
        self._codes = anvil.server.call('get_discrepancy_codes')
      except Exception as e:
        alert("Could not load reason codes: %s" % e)
        self._codes = []
    try:
      detail = anvil.server.call('get_pick_detail', pick_id)
    except Exception as e:
      alert("Could not load pick %s: %s" % (pick_id, e))
      self.show_my_picks()
      return
    self.current_pick_id = pick_id
    lines = detail['lines']
    resolved = sum(1 for l in lines if l['status'] in ('Picked', 'Short'))
    self.lbl_pick_cap.text = "Pick %s  —  %s of %s done" % (pick_id, resolved, len(lines))
    for l in lines:
      l['_codes'] = self._codes
      l['_worker'] = self.worker_code
    self.rp_tasks.items = lines

    unresolved = len(lines) - resolved
    self.btn_complete_pick.enabled = (unresolved == 0 and len(lines) > 0)
    self.btn_complete_pick.background = '#2E7D32' if self.btn_complete_pick.enabled else None
    self.btn_complete_pick.foreground = '#FFFFFF' if self.btn_complete_pick.enabled else None
    self.btn_complete_pick.text = ("COMPLETE PICK" if unresolved == 0
                                   else "COMPLETE PICK  (%s line%s left)"
                                   % (unresolved, '' if unresolved == 1 else 's'))
    self._show('detail')

  # -------------------------------------------------------------- top bar

  def switch_user(self, **event_args):
    self.show_workers()

  def refresh_current(self, **event_args):
    if self.cp_detail.visible and self.current_pick_id:
      self.show_detail(self.current_pick_id)
    elif self.cp_picks.visible and self.worker_code:
      self.show_my_picks()
    else:
      self.show_workers()

  def back_to_picks(self, **event_args):
    self.show_my_picks()

  # ---------------------------------------------------------------- events

  def worker_chosen(self, worker_code, display_name, **event_args):
    self.worker_code = worker_code
    self.worker_name = display_name
    self.lbl_worker_name.text = "Picking as: %s" % display_name
    self.btn_switch_user.visible = True
    self.show_my_picks()

  def open_pick(self, pick_id, **event_args):
    self.show_detail(pick_id)

  def record_line(self, pick_line_id, qty, discrepancy_code, note, **event_args):
    try:
      anvil.server.call('record_pick_line', pick_line_id, self.worker_code,
                        qty, discrepancy_code, note)
    except Exception as e:
      alert(str(e))
    self.show_detail(self.current_pick_id)

  def undo_line(self, pick_line_id, **event_args):
    try:
      anvil.server.call('undo_line', pick_line_id, self.worker_code)
    except Exception as e:
      alert(str(e))
    self.show_detail(self.current_pick_id)

  def complete_pick_click(self, **event_args):
    lines = self.rp_tasks.items or []
    shorts = [l for l in lines if l['status'] == 'Short']
    overs = [l for l in lines if l['status'] == 'Picked'
             and l.get('qty_picked') is not None
             and float(l['qty_picked']) > float(l['qty_planned'])]
    msg = "Complete Pick %s?" % self.current_pick_id
    if shorts:
      msg += "\n\nSHORT lines:\n" + "\n".join(
        "  %s: %g of %g (%s)" % (l['sku'], float(l['qty_picked'] or 0),
                                 float(l['qty_planned']), l.get('discrepancy_code') or '?')
        for l in shorts)
    if overs:
      msg += "\n\nOVER-picked lines:\n" + "\n".join(
        "  %s: %g of %g" % (l['sku'], float(l['qty_picked']), float(l['qty_planned']))
        for l in overs)
    if not confirm(msg):
      return
    try:
      anvil.server.call('complete_pick', self.current_pick_id, self.worker_code)
      Notification("Pick %s completed" % self.current_pick_id, style='success').show()
    except Exception as e:
      alert(str(e))
      self.show_detail(self.current_pick_id)
      return
    self.show_my_picks()
