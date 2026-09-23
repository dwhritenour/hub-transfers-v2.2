# =============================================================================
# pick_worker/MyPickRow/__init__.py
# -----------------------------------------------------------------------------
# Version: 1.0.0 (2026-08-02) - Hub Transfers v2.0 Phase 5.
# One card per Released/In_Progress pick (item = a get_my_picks dict).
# Priority color = worst line urgency (urgency_rank from server,
# 1=CRITICAL red, 2=HIGH orange, 3=MEDIUM amber, 4/None=LOW green) as
# the card's left border, per plan section 5.2.
# =============================================================================

from ._anvil_designer import MyPickRowTemplate
from anvil import *

RANK_COLORS = {1: '#C62828', 2: '#EF6C00', 3: '#F9A825', 4: '#2E7D32'}


class MyPickRow(MyPickRowTemplate):

  def __init__(self, **properties):
    self.init_components(**properties)
    self.btn_open_pick.set_event_handler('click', self.open_click)

    item = self.item or {}
    self.lbl_pick_num.text = "Pick %s" % item.get('pick_id', '?')
    self.lbl_pick_progress.text = "%s / %s lines" % (item.get('resolved_count') or 0,
                                                     item.get('line_count') or 0)
    status = (item.get('status') or '').replace('_', ' ')
    worker = item.get('worker_name') or ''
    self.lbl_pick_status.text = ("%s — %s" % (status, worker)) if worker else status

    color = RANK_COLORS.get(item.get('urgency_rank'), '#2E7D32')
    # Left "priority stripe": Anvil ColumnPanels have no per-side border
    # property, so we use a thin full border in the priority color.
    self.cp_pick_card.border = '3px solid %s' % color

  def open_click(self, **event_args):
    self.parent.raise_event('x-open', pick_id=self.item['pick_id'])
