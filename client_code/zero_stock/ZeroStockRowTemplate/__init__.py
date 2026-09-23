from ._anvil_designer import ZeroStockRowTemplateTemplate
from anvil import *


class ZeroStockRowTemplate(ZeroStockRowTemplateTemplate):
  """One zero-stock row. All fields set in code (the designer row needs no
  data bindings)."""
  def __init__(self, **properties):
    self.init_components(**properties)
    it = self.item or {}

    def s(v):
      return '' if v is None else str(v)

    self.txt_sku.text = s(it.get('sku'))
    self.txt_abc.text = s(it.get('abc'))
    self.txt_flag.text = s(it.get('hub_flag'))
    self.txt_demand.text = s(it.get('demand_in_horizon'))
    self.txt_total_demand.text = s(it.get('total_demand'))
    self.txt_ship.text = s(it.get('earliest_ship_date'))[:10]
    self.txt_time_urg.text = s(it.get('time_urgency'))
    self.txt_inv_urg.text = s(it.get('inventory_urgency'))
    self.txt_star_oh.text = s(it.get('starlite_on_hand'))
    self.txt_star_avail.text = s(it.get('starlite_available'))
    self.txt_hub_oh.text = s(it.get('hub_on_hand'))

    # 'Empty' = Hub physically out (needs restock): bold + dark red (#C62828,
    # Material Red 800). Bold is the primary cue so it does not rely on color.
    if it.get('hub_flag') == 'Empty':
      self.txt_flag.foreground = '#C62828'
      self.txt_flag.bold = True
