from ._anvil_designer import SkuLineTemplateTemplate
from anvil import *


class SkuLineTemplate(SkuLineTemplateTemplate):
  def __init__(self, **properties):
    self.init_components(**properties)

    item = self.item or {}
    self.lbl_sku.text      = str(item.get('sku', ''))
    self.lbl_desc.text     = str(item.get('part_desc') or '')
    planned = item.get('planned_qty', 0) or 0
    self.lbl_planned.text  = f"Planned: {int(planned) if float(planned).is_integer() else planned}"

    # Default delivered qty = planned (most common case)
    delivered = item.get('delivered_qty', planned)
    self.txt_delivered.text = str(int(delivered) if float(delivered).is_integer() else delivered)
    self.txt_note.text      = item.get('delivery_note', '') or ''

    self.txt_delivered.add_event_handler('change', self.txt_delivered_change)
    self.txt_note.add_event_handler('change', self.txt_note_change)

  # Mutating self.item writes through to the parent form's active_lines list
  # (same object, by reference). No server round-trip per keystroke.

  def txt_delivered_change(self, **event_args):
    try:
      self.item['delivered_qty'] = float(self.txt_delivered.text or 0)
    except (TypeError, ValueError):
      # leave the previous value — final validation happens on Submit
      pass

  def txt_note_change(self, **event_args):
    self.item['delivery_note'] = (self.txt_note.text or '').strip()

    # Any code you write here will run before the form opens.
