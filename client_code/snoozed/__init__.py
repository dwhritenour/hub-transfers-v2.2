# =============================================================================
# snoozed/__init__.py
# -----------------------------------------------------------------------------
# Version:       1.1.0
# Last updated:  2026-09-24
# Hub Transfers v2.2 - #snoozed: THE SNOOZED SKUs FORM.
#
# Every transfer request the Hub Manager has snoozed (status 'Paused'),
# soonest wake-up first, with the numbers it was parked on and the LIVE
# numbers from the detection view so you can see whether the picture has
# changed. Snoozed requests never appear on the Transfer Form; they come
# back on their own when the until-date arrives (or when woken here), and
# they disappear (Obsolete, logged) when the SKU no longer needs a transfer.
#
# Designer-native (house pattern): the DataGrid, its columns and the row's
# labels/buttons live in the designer; this code wires events, paints the
# cells and dispatches row actions to the server. No event bindings in the
# designer. See documents/Snooze_Instructions.md for the component list.
#
# 1.1.0 (2026-09-24): SKU search, same as the Transfer Form. Designer
#   components (no bindings): FlowPanel fp_sn_filters holding TextBox
#   tb_sn_sku, Button btn_sn_clear, Label lbl_sn_count. The list is loaded
#   once (self._all_rows) and filtered client-side as you type.
#
# Server (transfer_dashboard v2.2): get_snoozed_transfers, wake_snoozed,
#   resnooze, reject_snoozed.
# Row -> form event: rp_snoozed : x-action (action, recommendation_id,
#   until, note)
# =============================================================================

__version__       = "1.1.0"
__version_date__  = "2026-09-24"

from ._anvil_designer import snoozedTemplate
from anvil import *
import anvil.server


def _fmt(v):
  try:
    f = float(v)
    return str(int(f)) if f == int(f) else "%.1f" % f
  except Exception:
    return '' if v is None else str(v)


# 2026-09-23 (Dave): with the grid scrolling sideways it is easy to lose the
# line you are working on. Every other row gets a light grey background.
# The DataRowPanel's own background property is used, so no theme change.
_ZEBRA = '#EEF1F5'
_FREEZE_ROLES = ['tonal-data-grid', 'horizontal-scroll', 'freeze-first-col']


def _zebra(rp):
  """Every other row: role 'zebra-row' (theme.css shades the row AND its
  frozen first cell) plus the background property as a belt-and-braces
  fallback for a theme without the rule."""
  try:
    for i, row in enumerate(rp.get_components()):
      row.background = _ZEBRA if i % 2 else ''
      try:
        row.role = 'zebra-row' if i % 2 else None
      except Exception:
        pass
  except Exception:
    pass


def _freeze_first_col(dg):
  """theme.css .anvil-role-freeze-first-col: first column sticks while the
  grid scrolls sideways. Set here so the designer needs no change."""
  try:
    dg.role = list(_FREEZE_ROLES)
  except Exception:
    pass


class snoozed(snoozedTemplate):

  def __init__(self, **properties):
    self.init_components(**properties)
    self.nav.active = 'snoozed'

    self.btn_sn_refresh.set_event_handler('click', self.refresh_click)
    self.rp_snoozed.set_event_handler('x-action', self.row_action)
    _freeze_first_col(self.dg_snoozed)

    # 1.1.0: SKU search (components optional, so the form still loads if
    # they are not in the designer yet)
    self._all_rows = []
    if hasattr(self, 'tb_sn_sku'):
      self.tb_sn_sku.set_event_handler('change', self.filter_changed)
      self.tb_sn_sku.set_event_handler('pressed_enter', self.filter_changed)
    if hasattr(self, 'btn_sn_clear'):
      self.btn_sn_clear.set_event_handler('click', self.filter_clear_click)

    self.load()

  # ------------------------------------------------------------------ load

  def load(self):
    try:
      result = anvil.server.call('get_snoozed_transfers')
    except Exception as e:
      alert("Could not load the snoozed list: %s" % e)
      result = {'status': 'error', 'rows': []}
    if result.get('status') != 'success':
      alert("Could not load the snoozed list: %s" % result.get('message', 'unknown error'))
    rows = result.get('rows') or []
    self._all_rows = rows
    self.apply_filter()
    self.lbl_sn_none.visible = not rows

    dated = sum(1 for r in rows if r.get('snooze_until'))
    due_today = sum(1 for r in rows if r.get('days_left') is not None and int(r['days_left']) <= 0)
    holds = sum(1 for r in rows if r.get('order_hold_qty'))
    gone = sum(1 for r in rows if not r.get('still_needed'))
    self.lbl_sn_sub.text = (
      "%d snoozed  |  %d with a date (%d due today)  |  %d without a date  |  "
      "%d on held sales orders  |  %d no longer needed (will obsolete on the next review)."
      "  A snoozed request wakes on its date, or here with WAKE NOW; it is re-reviewed "
      "with fresh numbers and returns to the Transfer Form."
      % (len(rows), dated, due_today, len(rows) - dated, holds, gone))
    if result.get('woken'):
      Notification("%d snooze(s) reached their date and went back to the Transfer Form."
                   % result['woken'], timeout=5).show()

  def refresh_click(self, **event_args):
    self.load()

  # -------------------------------------------------------------- filter

  def _needle(self):
    if not hasattr(self, 'tb_sn_sku'):
      return ''
    return (self.tb_sn_sku.text or '').strip().upper()

  def apply_filter(self):
    """Show the subset of self._all_rows whose SKU contains the search text."""
    needle = self._needle()
    rows = [r for r in self._all_rows
            if not needle or needle in str(r.get('sku') or '').upper()]
    self.rp_snoozed.items = rows
    _zebra(self.rp_snoozed)
    if hasattr(self, 'lbl_sn_count'):
      total = len(self._all_rows)
      if needle:
        self.lbl_sn_count.text = ("Showing %d of %d" % (len(rows), total)
                                  if rows else "No snoozed SKU matches '%s'" % needle)
      else:
        self.lbl_sn_count.text = "%d snoozed" % total

  def filter_changed(self, **event_args):
    self.apply_filter()

  def filter_clear_click(self, **event_args):
    if hasattr(self, 'tb_sn_sku'):
      self.tb_sn_sku.text = ''
    self.apply_filter()

  # --------------------------------------------------------------- actions

  def row_action(self, action, recommendation_id=None, until=None, note=None, **event_args):
    try:
      if action == 'wake':
        r = anvil.server.call('wake_snoozed', recommendation_id, note)
        if r.get('status') != 'success':
          raise Exception(r.get('message'))
        Notification("%s is back on the Transfer Form (re-reviewed on the next run)."
                     % r['sku'], style='success', timeout=5).show()
      elif action == 'redate':
        r = anvil.server.call('resnooze', recommendation_id, until, note)
        if r.get('status') != 'success':
          raise Exception(r.get('message'))
        Notification("%s now snoozed until %s." % (r['sku'], r['until']),
                     style='success', timeout=5).show()
      elif action == 'reject':
        r = anvil.server.call('reject_snoozed', recommendation_id, note)
        if r.get('status') != 'success':
          raise Exception(r.get('message'))
        Notification("%s rejected." % r['sku'], style='success', timeout=5).show()
      else:
        alert("Unknown action %r" % action)
    except Exception as e:
      alert(str(e))
    self.load()