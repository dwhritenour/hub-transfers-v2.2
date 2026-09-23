# =============================================================================
# pick_editor/PickLineRow/__init__.py
# -----------------------------------------------------------------------------
# Version: 1.0.1 (2026-08-01) - Description column removed (Dave: redundant).
# 1.0.0 (2026-08-01) - Hub Transfers v2.0 Phase 4.
# One row per line of the selected pick (item = a get_pick_detail line
# + '_editable' injected by the parent when the pick is Draft/Released).
# Qty edits fire on lost_focus (not change - keeps focus while typing,
# same lesson as RoutingStepRow2's tb_seq) and raise x-line-qty;
# lk_remove raises x-line-remove. The server enforces every rule; a
# rejected edit comes back as an alert + reload from the parent.
# =============================================================================

from ._anvil_designer import PickLineRowTemplate
from anvil import *

LINE_STATUS_COLORS = {
  'Assigned': '#9E9E9E',
  'Picked':   '#2E7D32',
  'Short':    '#EF6C00',
}


def _fmt_qty(v):
  if v is None:
    return ''
  try:
    f = float(v)
    return str(int(f)) if f == int(f) else "%.2f" % f
  except Exception:
    return str(v)


class PickLineRow(PickLineRowTemplate):

  def __init__(self, **properties):
    self.init_components(**properties)
    self.tb_qty.set_event_handler('lost_focus', self.qty_lost_focus)
    self.lk_remove.set_event_handler('click', self.remove_click)
    self.form_show()   # item is available at construction; render now

  def form_show(self, **event_args):
    item = self.item or {}
    self.lbl_sku.text = item.get('sku') or ''
    self.lbl_sku.bold = True
    self.lbl_loc.text = item.get('location_code') or '--'

    self.tb_qty.text = _fmt_qty(item.get('qty_planned'))
    editable = bool(item.get('_editable')) and item.get('status') == 'Assigned'
    self.tb_qty.enabled = editable
    self.lk_remove.visible = editable
    self.lk_remove.foreground = '#C62828'

    status = item.get('status') or ''
    self.lbl_status.text = status
    self.lbl_status.foreground = LINE_STATUS_COLORS.get(status)
    self.lbl_status.bold = status in ('Picked', 'Short')

    picked = item.get('qty_picked')
    self.lbl_picked.text = _fmt_qty(picked)
    if picked is not None and item.get('qty_planned') is not None:
      try:
        if float(picked) > float(item['qty_planned']):
          self.lbl_picked.foreground = '#6A1B9A'   # over-pick, C4
          self.lbl_picked.bold = True
        elif float(picked) < float(item['qty_planned']):
          self.lbl_picked.foreground = '#EF6C00'   # short
          self.lbl_picked.bold = True
      except Exception:
        pass
    self.lbl_picked_by.text = item.get('picked_by_name') or item.get('picked_by') or ''

  def qty_lost_focus(self, **event_args):
    if not self.tb_qty.enabled:
      return
    try:
      qty = float(self.tb_qty.text or 0)
    except Exception:
      qty = 0
    old = float(self.item.get('qty_planned') or 0)
    if qty == old:
      return
    self.parent.raise_event('x-line-qty',
                            pick_line_id=self.item['pick_line_id'], qty=qty)

  def remove_click(self, **event_args):
    if confirm("Remove %s from this pick? Its qty returns to the pool."
               % (self.item.get('sku') or 'this line')):
      self.parent.raise_event('x-line-remove',
                              pick_line_id=self.item['pick_line_id'])
