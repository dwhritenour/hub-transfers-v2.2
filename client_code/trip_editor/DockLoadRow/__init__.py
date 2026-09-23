# =============================================================================
# trip_editor/DockLoadRow/__init__.py
# -----------------------------------------------------------------------------
# Version: 2.1.0 (2026-08-31) - Trip Editor v3.1.1: designer components.
# One finalized-truck line as a DataGrid row (container: DataRowPanel).
# Plain text columns (SKU, qty, lb, ft3, location) auto-render from the
# item's *_disp data keys. The urgency labels (columns turg / iurg) and
# the actions cell (column actions: fp_actions + fp_short_entry) are
# DESIGNER components so they can be manipulated in the designer; this
# code only wires events and paints state. The actions cell:
#   ✔ LOADED  -> full target qty in one click
#   SHORT     -> inline qty + reason dropdown (server texts managers, D-3)
#   Remove    -> back to STAGED with a typed reason
#   Undo      -> clears a recorded load until departure
# Row background: white = to load, green = loaded, amber = short.
# =============================================================================

from ._anvil_designer import DockLoadRowTemplate
from anvil import *

URG_COLORS = {'CRITICAL': '#C62828', 'HIGH': '#EF6C00', 'WARNING': '#EF6C00',
              'MEDIUM': '#F9A825', 'ELEVATED': '#F9A825'}


def _fmt(v):
  try:
    f = float(v)
    return str(int(f)) if f == int(f) else "%.1f" % f
  except Exception:
    return str(v)


class DockLoadRow(DockLoadRowTemplate):

  def __init__(self, **properties):
    self.init_components(**properties)

    try:
      self.auto_display_data = True
    except Exception:
      pass

    # All components are designer components: lbl_turg / lbl_iurg in
    # columns turg / iurg; fp_actions (LOADED, SHORT, result label,
    # Undo, Remove) and fp_short_entry (qty, reason, SAVE, CANCEL -
    # hidden until SHORT) both in column actions.
    self.btn_l_loaded.set_event_handler('click', self.loaded_click)
    self.btn_l_short.set_event_handler('click', self.short_click)
    self.btn_l_short_save.set_event_handler('click', self.short_save)
    self.btn_l_short_cancel.set_event_handler('click', self.short_cancel)
    self.lk_l_undo.set_event_handler('click', self.undo_click)
    self.lk_l_remove.set_event_handler('click', self.remove_click)

    self.render()

  def _urg(self, lbl, value):
    v = (value or '').upper()
    lbl.text = v if v else '-'
    lbl.foreground = URG_COLORS.get(v, '#2E7D32')

  # ---------------------------------------------------------------- render

  def render(self):
    item = self.item or {}
    loaded = item.get('loaded_date') is not None
    is_short = loaded and item.get('load_discrepancy_code')

    self._urg(self.lbl_turg, item.get('time_urgency'))
    self._urg(self.lbl_iurg, item.get('inventory_urgency'))
    self.background = ('#FFF8E1' if is_short
                       else ('#E8F5E9' if loaded else None))

    self.fp_short_entry.visible = False
    self.fp_actions.visible = True
    self.btn_l_loaded.visible = not loaded
    self.btn_l_short.visible = not loaded
    self.lk_l_remove.visible = not loaded
    self.lk_l_undo.visible = loaded

    if loaded:
      who = item.get('loaded_by_name') or item.get('loaded_by') or ''
      if is_short:
        self.lbl_l_result.text = "SHORT %s of %s (%s) — %s" % (
          _fmt(item.get('loaded_qty')), _fmt(item.get('target_qty')),
          item.get('load_discrepancy_code'), who)
        self.lbl_l_result.foreground = '#EF6C00'
      else:
        self.lbl_l_result.text = "Loaded %s — %s" % (
          _fmt(item.get('loaded_qty')), who)
        self.lbl_l_result.foreground = '#2E7D32'
      self.lbl_l_result.bold = True
    else:
      self.lbl_l_result.text = ""

  # ---------------------------------------------------------------- clicks

  def loaded_click(self, **event_args):
    self.parent.raise_event('x-load',
                            assignment_id=self.item['assignment_id'])

  def short_click(self, **event_args):
    self.tb_l_qty.text = _fmt(self.item.get('target_qty'))
    self.dd_l_reason.items = [(c['description'], c['discrepancy_code'])
                              for c in (self.item.get('_codes') or [])]
    self.dd_l_reason.selected_value = None
    self.fp_actions.visible = False
    self.fp_short_entry.visible = True

  def short_cancel(self, **event_args):
    self.render()

  def short_save(self, **event_args):
    try:
      qty = float(self.tb_l_qty.text or 0)
    except Exception:
      alert("Enter a number")
      return
    target = float(self.item.get('target_qty') or 0)
    if qty < 0:
      alert("Quantity cannot be negative")
      return
    if qty >= target:
      alert("That is not short of %s — use ✔ LOADED for a full load"
            % _fmt(target))
      return
    if not self.dd_l_reason.selected_value:
      alert("Pick a reason for the short load")
      return
    self.parent.raise_event('x-short',
                            assignment_id=self.item['assignment_id'],
                            qty=qty,
                            discrepancy_code=self.dd_l_reason.selected_value,
                            note=None)

  def undo_click(self, **event_args):
    if confirm("Undo the load record for %s?" % (self.item.get('sku') or 'this line')):
      self.parent.raise_event('x-unload',
                              assignment_id=self.item['assignment_id'])

  def remove_click(self, **event_args):
    tb = TextBox(placeholder="Why is it coming off? (e.g. won't fit)")
    panel = ColumnPanel()
    panel.add_component(Label(
      text="Remove %s from this truck? It returns to STAGED."
      % (self.item.get('sku') or 'this line'), bold=True))
    panel.add_component(tb)
    if alert(content=panel, title="Remove from truck",
             buttons=[("REMOVE", True), ("CANCEL", False)]):
      reason = (tb.text or '').strip()
      if not reason:
        alert("A reason is required to remove a line.")
        return
      self.parent.raise_event('x-remove',
                              assignment_id=self.item['assignment_id'],
                              reason=reason)
