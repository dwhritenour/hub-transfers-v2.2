# =============================================================================
# pick_editor/OpenPickRow/__init__.py
# -----------------------------------------------------------------------------
# Version: 1.0.0 (2026-08-01) - Hub Transfers v2.0 Phase 4.
# One row per open pick (item = a get_open_picks dict). Aging rows
# (is_aging = 1, C9) get an amber age label; status text is color-coded
# to match the editor's chip colors. lk_open raises x-open-pick on the
# RepeatingPanel; the parent pick_editor loads the detail.
# =============================================================================

from ._anvil_designer import OpenPickRowTemplate
from anvil import *

STATUS_COLORS = {
  'Draft':       '#9E9E9E',
  'Released':    '#1565C0',
  'In_Progress': '#F9A825',
  'Completed':   '#2E7D32',
  'Cancelled':   '#C62828',
}


class OpenPickRow(OpenPickRowTemplate):

  def __init__(self, **properties):
    self.init_components(**properties)
    self.lk_open.set_event_handler('click', self.open_click)
    self.form_show()   # item is available at construction; render now
    # (idempotent if the designer also wires 'show')

  def form_show(self, **event_args):
    item = self.item or {}
    self.lbl_pick_id.text = str(item.get('pick_id', ''))
    self.lbl_pick_id.bold = True

    status = item.get('status') or ''
    self.lbl_status.text = status.replace('_', ' ')
    self.lbl_status.foreground = STATUS_COLORS.get(status, '#000000')
    self.lbl_status.bold = True

    self.lbl_worker.text = item.get('worker_name') or item.get('assigned_worker_code') or '—'
    self.lbl_progress.text = "%s/%s" % (item.get('resolved_count') or 0,
                                        item.get('line_count') or 0)
    shorts = item.get('short_count') or 0
    self.lbl_shorts.text = str(shorts) if shorts else ''
    self.lbl_shorts.foreground = '#C62828' if shorts else None

    # Age: show the released date-time (or '-' while Draft); amber when aging.
    released = item.get('released_date') or ''
    self.lbl_age.text = released[:16].replace('T', ' ') if released else '—'
    if item.get('is_aging'):
      self.lbl_age.background = '#FFF8E1'
      self.lbl_age.foreground = '#EF6C00'
      self.lbl_age.bold = True
    else:
      self.lbl_age.background = None
      self.lbl_age.foreground = None
      self.lbl_age.bold = False

    created = item.get('created_date') or ''
    self.lbl_created.text = created[:16].replace('T', ' ')

  def open_click(self, **event_args):
    self.parent.raise_event('x-open-pick', pick_id=self.item['pick_id'])
