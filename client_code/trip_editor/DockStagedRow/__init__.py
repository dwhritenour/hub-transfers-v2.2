# =============================================================================
# trip_editor/DockStagedRow/__init__.py
# -----------------------------------------------------------------------------
# Version: 2.1.0 (2026-08-31) - Trip Editor v3.1.1: designer components.
# One STAGED completed-pick line as a DataGrid row (container:
# DataRowPanel). Plain text columns (SKU, qty, lb, ft3, pick, location,
# description) render automatically from the item's *_disp data keys via
# the grid's column data_key mapping. The behavioral cells - the select
# checkbox (column sel), staged-age badge (age) and two urgency labels
# (turg / iurg) - are DESIGNER components placed in their columns, so
# Dave can manipulate them in the designer; this code only wires events
# and paints values/colors.
# Checkbox toggles the line onto/off the truck being planned; the parent
# recomputes the capacity summary on x-toggle.
# =============================================================================

from ._anvil_designer import DockStagedRowTemplate
from anvil import *

URG_COLORS = {'CRITICAL': '#C62828', 'HIGH': '#EF6C00', 'WARNING': '#EF6C00',
              'MEDIUM': '#F9A825', 'ELEVATED': '#F9A825'}


class DockStagedRow(DockStagedRowTemplate):

  def __init__(self, **properties):
    self.init_components(**properties)

    # Text columns come from item data keys automatically.
    try:
      self.auto_display_data = True
    except Exception:
      pass

    # cb_sel / lbl_age / lbl_turg / lbl_iurg are designer components,
    # each placed in its grid column (sel / age / turg / iurg).
    self.cb_sel.set_event_handler('change', self.sel_changed)

    self.render()

  def _urg(self, lbl, value):
    v = (value or '').upper()
    lbl.text = v if v else '-'
    lbl.foreground = URG_COLORS.get(v, '#2E7D32')

  def render(self):
    item = self.item or {}
    self.cb_sel.checked = bool(item.get('_sel'))
    self._urg(self.lbl_turg, item.get('time_urgency'))
    self._urg(self.lbl_iurg, item.get('inventory_urgency'))

    hours = int(item.get('age_hours') or 0)
    if hours >= 48:
      self.lbl_age.text = "%dd staged" % (hours // 24)
      self.lbl_age.foreground = '#C62828'
    elif hours >= 24:
      self.lbl_age.text = "%dh staged" % hours
      self.lbl_age.foreground = '#EF6C00'
    else:
      self.lbl_age.text = "%dh" % hours
      self.lbl_age.foreground = '#2E7D32'

    sugg = bool(item.get('is_suggested'))
    self.background = '#EEF4FB' if sugg else None
    self.tooltip = 'On the optimizer proposal' if sugg else ''

  def sel_changed(self, **event_args):
    self.item['_sel'] = bool(self.cb_sel.checked)
    self.parent.raise_event('x-toggle',
                            pick_line_id=self.item['pick_line_id'],
                            selected=self.item['_sel'])
