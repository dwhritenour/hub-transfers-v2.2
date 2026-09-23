from ._anvil_designer import processed_transfersTemplate
from anvil import *
import anvil.server
from datetime import date, timedelta


class processed_transfers(processed_transfersTemplate):
  """Processed SKUs viewer for the Hub Manager (read-only).

  Header: SKU search, date range, status filter, sort, and an Export button.
  Grid rp_processed uses ProcessedRowTemplate for the detail rows.
  """
  def __init__(self, **properties):
    self.init_components(**properties)
    if hasattr(self, 'nav'):
      self.nav.active = 'processed_transfers'

    # Sort dropdown: (display text, sort_by value sent to the server)
    self.dd_sort.items = [
      ('Date (newest first)', 'date_desc'),
      ('Date (oldest first)', 'date_asc'),
      ('SKU', 'sku'),
      ('Status', 'status'),
    ]
    self.dd_sort.selected_value = 'date_desc'

    # Status dropdown: 'All' + the distinct non-Pending statuses actually present
    try:
      self.dd_status.items = anvil.server.call('get_processed_status_options')
    except Exception:
      self.dd_status.items = ['All']
    self.dd_status.selected_value = 'All'

    # Optional default range (leave blank = all time). Uncomment to default to 30d:
    # self.dp_date_from.date = date.today() - timedelta(days=30)
    # self.dp_date_to.date = date.today()

    # Wire events
    self.btn_search.set_event_handler('click', self.load_data)
    self.btn_export.set_event_handler('click', self.export_data)
    self.dd_sort.set_event_handler('change', self.load_data)
    self.dd_status.set_event_handler('change', self.load_data)

    self.load_data()

  def _filters(self):
    sku = (self.txt_search_sku.text or '').strip() or None
    return {
      'sku': sku,
      'date_from': self.dp_date_from.date,
      'date_to': self.dp_date_to.date,
      'status': self.dd_status.selected_value,
      'sort_by': self.dd_sort.selected_value or 'date_desc',
    }

  def load_data(self, **event_args):
    # Fail-safe: never let a server hiccup stop the form from opening.
    try:
      f = self._filters()
      result = anvil.server.call('get_processed_transfers', **f)
      if result.get('status') == 'success':
        self.rp_processed.items = result['rows']
        _cnt = result['count']
        _tot = result.get('total', _cnt)
        if _tot > _cnt:
          self.lbl_count.text = ('Showing ' + str(_cnt) + ' of ' + str(_tot)
                                 + '  —  narrow by SKU, date, or status')
        else:
          self.lbl_count.text = str(_tot) + ' processed SKUs'
      else:
        self.rp_processed.items = []
        self.lbl_count.text = 'Error: ' + str(result.get('message', 'unknown'))
    except Exception as e:
      self.rp_processed.items = []
      self.lbl_count.text = 'Load failed: ' + str(e)

  def export_data(self, **event_args):
    f = self._filters()
    try:
      media = anvil.server.call('export_processed_transfers', **f)
      download(media)
    except Exception as e:
      alert('Export failed:\n\n' + str(e))

