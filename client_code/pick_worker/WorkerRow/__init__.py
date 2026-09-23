# =============================================================================
# pick_worker/WorkerRow/__init__.py
# -----------------------------------------------------------------------------
# Version: 1.0.0 (2026-08-02) - Hub Transfers v2.0 Phase 5.
# One fat button per active worker (item = a get_workers dict). Leads
# get a star so the crew can spot who can see every pick.
# =============================================================================

from ._anvil_designer import WorkerRowTemplate
from anvil import *


class WorkerRow(WorkerRowTemplate):

  def __init__(self, **properties):
    self.init_components(**properties)
    self.btn_worker.set_event_handler('click', self.worker_click)
    item = self.item or {}
    name = item.get('display_name') or item.get('worker_code') or '?'
    self.btn_worker.text = ("★ %s" % name) if item.get('is_lead') else name

  def worker_click(self, **event_args):
    self.parent.raise_event('x-worker',
                            worker_code=self.item['worker_code'],
                            display_name=self.item.get('display_name')
                            or self.item['worker_code'])
