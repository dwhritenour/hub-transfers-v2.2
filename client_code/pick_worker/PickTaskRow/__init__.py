# =============================================================================
# pick_worker/PickTaskRow/__init__.py
# -----------------------------------------------------------------------------
# Version: 1.2.0 (2026-09-10) - Hub Transfers v2.1.
# v1.2.0: ON HOLD warning (Hub policy: location 100 = quality hold, never
# picked for transfers). When the line's SKU has held units at the Hub
# (pick_dashboard 1.4.0 supplies on_hold + hold_location) the description
# line turns red: "25 ON HOLD in loc 100 - do NOT pick from there".
# One card per pick line (item = a get_pick_detail line + '_codes' /
# '_worker' injected by the parent). Location-first layout; status
# colors grey (Assigned) / green (Picked) / amber (Short).
#
# v1.1.0: urgency chips (Hub request 2026-08-30) - TIME and INV urgency
# from the line's recommendation (pick_dashboard 1.2.0 supplies
# time_urgency / inventory_urgency on every line), color-ranked so all
# Hub employees see the same urgency language everywhere (#pick, #dock,
# and the coming Daily Ship Plan app).
#
# Two actions:
#   ✔ FULL QTY  -> records qty_planned in one tap.
#   QTY / ISSUE -> inline entry: numeric qty (defaults to planned) +
#                  one reason button per discrepancy code. Reason is
#                  required only when qty < planned (server enforces
#                  too); over-pick within the cap needs no reason.
# Undo shows on resolved lines until the pick completes.
# All validation of consequence happens server-side; this card only
# collects the tap.
# =============================================================================

from ._anvil_designer import PickTaskRowTemplate
from anvil import *

STATUS_BG = {
  'Assigned': '#F5F5F5',
  'Picked':   '#E8F5E9',
  'Short':    '#FFF8E1',
}

URG_COLORS = {'CRITICAL': '#C62828', 'HIGH': '#EF6C00', 'WARNING': '#EF6C00',
              'MEDIUM': '#F9A825', 'ELEVATED': '#F9A825'}


def _fmt(v):
  try:
    f = float(v)
    return str(int(f)) if f == int(f) else "%.2f" % f
  except Exception:
    return str(v)


class PickTaskRow(PickTaskRowTemplate):

  def __init__(self, **properties):
    self.init_components(**properties)
    self._selected_code = None
    self._reason_buttons = []

    self.btn_full.set_event_handler('click', self.full_click)
    self.btn_issue.set_event_handler('click', self.issue_click)
    self.btn_issue_save.set_event_handler('click', self.issue_save)
    self.btn_issue_cancel.set_event_handler('click', self.issue_cancel)
    self.lk_undo.set_event_handler('click', self.undo_click)

    self.render()

  # ---------------------------------------------------------------- render

  def render(self):
    item = self.item or {}
    status = item.get('status') or 'Assigned'

    self.lbl_task_sku.text = item.get('sku') or ''
    self.lbl_task_qty.text = "QTY %s" % _fmt(item.get('qty_planned'))
    self.lbl_task_loc.text = item.get('location_code') or 'NO RELEASED LOCATION'
    desc = item.get('part_description') or ''
    held = 0.0
    try:
      held = float(item.get('on_hold') or 0)
    except Exception:
      held = 0.0
    if held > 0:
      self.lbl_task_desc.text = ("%s ON HOLD in loc %s - do NOT pick from there.   %s"
                                 % (_fmt(held), item.get('hold_location') or '100', desc))
      self.lbl_task_desc.foreground = '#C62828'
      self.lbl_task_desc.bold = True
    else:
      self.lbl_task_desc.text = desc
      self.lbl_task_desc.foreground = '#595959'
      self.lbl_task_desc.bold = False
    self.cp_task_card.background = STATUS_BG.get(status, '#F5F5F5')

    # v1.1.0 urgency chips
    def _chip(lbl, prefix, value):
      v = (value or '').upper()
      lbl.text = "%s: %s" % (prefix, v) if v else ""
      lbl.foreground = URG_COLORS.get(v, '#2E7D32')
    _chip(self.lbl_task_turg, "TIME", item.get('time_urgency'))
    _chip(self.lbl_task_iurg, "INV", item.get('inventory_urgency'))

    resolved = status in ('Picked', 'Short')
    self.btn_full.visible = not resolved
    self.btn_issue.visible = not resolved
    self.cp_issue.visible = False
    self.lk_undo.visible = resolved
    self.lk_undo.foreground = '#595959'

    if resolved:
      picked = _fmt(item.get('qty_picked'))
      planned = _fmt(item.get('qty_planned'))
      who = item.get('picked_by_name') or item.get('picked_by') or ''
      if status == 'Short':
        txt = "SHORT — %s of %s (%s)" % (picked, planned,
                                         item.get('discrepancy_code') or '?')
        self.lbl_task_result.foreground = '#EF6C00'
      elif float(item.get('qty_picked') or 0) > float(item.get('qty_planned') or 0):
        txt = "OVER — picked %s of %s" % (picked, planned)
        self.lbl_task_result.foreground = '#6A1B9A'
      else:
        txt = "Picked %s" % picked
        self.lbl_task_result.foreground = '#2E7D32'
      if who:
        txt += "  — " + who
      self.lbl_task_result.text = txt
      self.lbl_task_result.bold = True
    else:
      self.lbl_task_result.text = ""

  # ---------------------------------------------------------------- taps

  def full_click(self, **event_args):
    self.parent.raise_event('x-record',
                            pick_line_id=self.item['pick_line_id'],
                            qty=float(self.item['qty_planned']),
                            discrepancy_code=None, note=None)

  def issue_click(self, **event_args):
    self.tb_actual_qty.text = _fmt(self.item.get('qty_planned'))
    self._selected_code = None
    self._build_reason_buttons()
    self.cp_issue.visible = True
    self.btn_full.visible = False
    self.btn_issue.visible = False

  def issue_cancel(self, **event_args):
    self.cp_issue.visible = False
    self.btn_full.visible = True
    self.btn_issue.visible = True

  def issue_save(self, **event_args):
    try:
      qty = float(self.tb_actual_qty.text or 0)
    except Exception:
      alert("Enter a number")
      return
    planned = float(self.item.get('qty_planned') or 0)
    if qty < 0:
      alert("Quantity cannot be negative")
      return
    if qty < planned and not self._selected_code:
      alert("Tap a reason for the short pick")
      return
    self.parent.raise_event('x-record',
                            pick_line_id=self.item['pick_line_id'],
                            qty=qty,
                            discrepancy_code=self._selected_code,
                            note=None)

  def undo_click(self, **event_args):
    if confirm("Undo %s?" % (self.item.get('sku') or 'this line')):
      self.parent.raise_event('x-undo', pick_line_id=self.item['pick_line_id'])

  # ------------------------------------------------------- reason buttons

  def _build_reason_buttons(self):
    self.fp_reasons.clear()
    self._reason_buttons = []
    for code in (self.item or {}).get('_codes') or []:
      b = Button(text=code['description'], role='outlined-button')
      b.tag = code['discrepancy_code']
      b.set_event_handler('click', self._reason_click)
      self.fp_reasons.add_component(b)
      self._reason_buttons.append(b)

  def _reason_click(self, sender, **event_args):
    self._selected_code = sender.tag
    for b in self._reason_buttons:
      chosen = b is sender
      b.background = '#1565C0' if chosen else None
      b.foreground = '#FFFFFF' if chosen else None