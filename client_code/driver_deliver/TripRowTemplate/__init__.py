from ._anvil_designer import TripRowTemplateTemplate
from anvil import *


class TripRowTemplate(TripRowTemplateTemplate):
  def __init__(self, **properties):
    self.init_components(**properties)

    t = self.item or {}
    date = t.get('planned_date') or ''
    time = t.get('planned_time') or ''
    batch = t.get('batch_id', '?')
    sku_count = t.get('sku_count', '?')

    self.lbl_title.text = f"Trip #{t.get('trip_number', '?')}  —  {date}"
    self.lbl_sub.text   = f"{time}  ·  Batch {batch}  ·  {sku_count} SKU(s)"

    self.btn_select.set_event_handler('click', self.btn_select_click)

  def _parent_form(self):
    """Walk up the parent chain to reach the driver_delivery form."""
    p = self.parent
    while p:
      if hasattr(p, 'on_trip_selected'):
        return p
      p = getattr(p, 'parent', None)
    return None

  def btn_select_click(self, **event_args):
    parent = self._parent_form()
    if parent and self.item:
      parent.on_trip_selected(self.item)
