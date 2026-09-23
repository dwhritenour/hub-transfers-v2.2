from ._anvil_designer import zero_stockTemplate
from anvil import *
import anvil.server


class zero_stock(zero_stockTemplate):
  """Zero Stock viewer (read-only): SKUs Starlite needs that the Hub can't
  supply right now -- effectively the Hub reorder worklist.

  Header: SKU search, Hub-shortfall flag filter, sort, and Export.
  Grid rp_zero uses ZeroStockRowTemplate for the detail rows.
  """
  def __init__(self, **properties):
    self.init_components(**properties)
    if hasattr(self, 'nav'):
      self.nav.active = 'zero_stock'

    # Flag filter: (display, value sent to server)
    self.dd_flag.items = [
      ('All Hub shortfalls', 'All'),
      ('Empty only (needs restock)', 'Empty'),
      ('Committed only', 'Committed'),
    ]
    self.dd_flag.selected_value = 'All'

    # Sort: (display, value)
    self.dd_sort.items = [
      ('Urgency (most pressing)', 'urgency'),
      ('Earliest ship date', 'ship'),
      ('Demand (largest first)', 'demand'),
      ('SKU', 'sku'),
    ]
    self.dd_sort.selected_value = 'urgency'

    self.btn_search.set_event_handler('click', self.load_data)
    self.btn_export.set_event_handler('click', self.export_data)
    self.dd_flag.set_event_handler('change', self.load_data)
    self.dd_sort.set_event_handler('change', self.load_data)

    self.load_data()

  def _filters(self):
    sku = (self.txt_search_sku.text or '').strip() or None
    return {
      'sku': sku,
      'flag': self.dd_flag.selected_value or 'All',
      'sort_by': self.dd_sort.selected_value or 'urgency',
    }

  def load_data(self, **event_args):
    # Fail-safe: never let a server hiccup stop the form from opening.
    try:
      f = self._filters()
      result = anvil.server.call('get_zero_stock', **f)
      if result.get('status') == 'success':
        self.rp_zero.items = result['rows']
        c = result['count']
        t = result.get('total', c)
        if t > c:
          self.lbl_count.text = ('Showing ' + str(c) + ' of ' + str(t)
                                 + '  —  narrow with the filters')
        else:
          self.lbl_count.text = str(t) + ' SKUs blocked at the Hub'
      else:
        self.rp_zero.items = []
        self.lbl_count.text = 'Error: ' + str(result.get('message', 'unknown'))
    except Exception as e:
      self.rp_zero.items = []
      self.lbl_count.text = 'Load failed: ' + str(e)

  def export_data(self, **event_args):
    try:
      media = anvil.server.call('export_zero_stock', **self._filters())
      download(media)
    except Exception as e:
      alert('Export failed:\n\n' + str(e))
