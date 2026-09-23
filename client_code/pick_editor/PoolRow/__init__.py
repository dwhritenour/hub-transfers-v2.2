# =============================================================================
# pick_editor/PoolRow/__init__.py
# -----------------------------------------------------------------------------
# Version: 1.0.1 (2026-08-01) - Description column removed (Dave: redundant).
# 1.0.0 (2026-08-01) - Hub Transfers v2.0 Phase 4.
# One row per approved rec with remaining allocation (item = a
# get_pick_pool dict). The qty-to-assign box defaults to the remaining
# qty (partial allowed, plan section 5.1); selection + typed qty are
# written back onto the item dict (_selected / _assign_qty) so the
# parent's Add Selected reads one source of truth. Server re-validates
# everything - this row never enforces the allocation guard itself.
# =============================================================================

from ._anvil_designer import PoolRowTemplate
from anvil import *
import anvil.server

URGENCY_COLORS = {
  'CRITICAL': '#C62828',
  'HIGH':     '#EF6C00',
  'MEDIUM':   '#F9A825',
  'LOW':      '#2E7D32',
}


def _fmt_qty(v):
  try:
    f = float(v or 0)
    return str(int(f)) if f == int(f) else "%.2f" % f
  except Exception:
    return str(v or 0)


class PoolRow(PoolRowTemplate):

  def __init__(self, **properties):
    self.init_components(**properties)
    self.cb_selected.set_event_handler('change', self.selected_change)
    self.tb_assign_qty.set_event_handler('change', self.qty_change)
    # v1.1: pool-remove link (undo an erroneous approval). hasattr-guarded
    # so the row loads before lk_remove is added in the designer.
    if hasattr(self, 'lk_remove'):
      self.lk_remove.set_event_handler('click', self.lk_remove_click)
    self.form_show()   # item is available at construction; render now

  def form_show(self, **event_args):
    item = self.item or {}
    self.cb_selected.checked = bool(item.get('_selected'))
    self.lbl_rec_id.text = str(item.get('recommendation_id', ''))
    self.lbl_sku.text = item.get('sku') or ''
    self.lbl_sku.bold = True
    self.lbl_loc.text = item.get('location_code') or '--'
    self.lbl_req.text = _fmt_qty(item.get('calculated_qty'))
    self.lbl_appr.text = _fmt_qty(item.get('approved_qty'))
    self.lbl_assigned.text = _fmt_qty(item.get('assigned_qty'))
    self.lbl_remaining.text = _fmt_qty(item.get('remaining_qty'))
    self.lbl_remaining.bold = True

    urg = (item.get('base_urgency') or '').upper()
    self.lbl_urgency.text = urg
    self.lbl_urgency.foreground = URGENCY_COLORS.get(urg)
    self.lbl_urgency.bold = urg in ('CRITICAL', 'HIGH')

    approved_on = item.get('reviewed_date') or item.get('recommendation_date') or ''
    self.lbl_age.text = approved_on[:10]

    # Default assignment = full remaining (item may carry a prior edit).
    default_qty = item.get('_assign_qty')
    if default_qty is None:
      default_qty = item.get('remaining_qty') or 0
    self.tb_assign_qty.text = _fmt_qty(default_qty)

  def selected_change(self, **event_args):
    self.item['_selected'] = self.cb_selected.checked

  def qty_change(self, **event_args):
    try:
      qty = float(self.tb_assign_qty.text or 0)
    except Exception:
      qty = 0
    remaining = float(self.item.get('remaining_qty') or 0)
    if qty > remaining:
      # Soft clamp with visual cue; the server would reject it anyway.
      qty = remaining
      self.tb_assign_qty.text = _fmt_qty(qty)
    self.item['_assign_qty'] = qty

  def _parent_form(self):
    """Walk up to the pick_editor form (has load_pool)."""
    p = self.parent
    while p is not None and not hasattr(p, 'load_pool'):
      p = p.parent
    return p

  def lk_remove_click(self, **event_args):
    """v1.1: undo an erroneous approval -- returns the rec to Pending so it
    leaves the pool and reappears on the Transfer Form for a fresh decision."""
    item = self.item or {}
    sku = item.get('sku', '')
    if not confirm("Remove %s from the pool?\n\nThis undoes the approval and "
                   "returns it to the Transfer Form as Pending. (To keep it "
                   "from coming back, Reject it there.)" % sku):
      return
    try:
      result = anvil.server.call('remove_from_pool',
                                 item.get('recommendation_id'))
      if result.get('status') == 'success':
        Notification(result.get('message', 'Removed.')).show()
        form = self._parent_form()
        if form is not None:
          form.load_pool()
      else:
        alert('Could not remove from pool:\n\n'
              + str(result.get('message', 'unknown error')))
    except Exception as e:
      alert('Remove failed:\n\n' + str(e))