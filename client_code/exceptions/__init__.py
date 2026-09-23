# =============================================================================
# exceptions/__init__.py
# -----------------------------------------------------------------------------
# Version:       2.0.0
# Last updated:  2026-09-05
# Hub Transfers v2.1 - #exceptions: THE HUB MANAGER'S EXCEPTION BOARD.
#
# Every stage of the transfer pipeline where something can sit, on one
# grid, ordered by stage then age:
#   1 Approved, not picked      auto-cancelled after the threshold
#   2 Picked, not loaded        never auto-closed: SHIPPED-CLOSE / RETURN TO STOCK
#   3 Departed, not delivered   never auto-delivered: CONFIRM DELIVERED
#   4 Open e21 transfer         read-only; Starlite receives / Hub ships
# Each row shows how long it has been open (Age) and how long until the
# system acts on it (Left) - negative = overdue, red.
#
# Designer-native (house pattern): columns and components live in the
# designer; text cells are bound labels; this code wires events, paints
# the stage/alert/age/left cells and dispatches row actions to the
# server. Reached via <app-url>#exceptions.
#
# Server (exceptions_dashboard v2): get_exceptions_board,
#   close_exception, return_to_stock, confirm_delivered, cancel_unpicked.
# Row -> form event: rp_exceptions : x-action
#   (action, recommendation_id, pick_line_id, trip_id, date, confirmed_by, note)
# =============================================================================

__version__       = "2.0.0"
__version_date__  = "2026-09-05"

from ._anvil_designer import exceptionsTemplate
from anvil import *
import anvil.server


def _fmt(v):
  try:
    f = float(v)
    return str(int(f)) if f == int(f) else "%.1f" % f
  except Exception:
    return str(v)


class exceptions(exceptionsTemplate):

  def __init__(self, **properties):
    self.init_components(**properties)
    self.nav.active = 'exceptions' 

    self.btn_exc_refresh.set_event_handler('click', self.refresh_click)
    self.rp_exceptions.set_event_handler('x-action', self.row_action)

    self.load()

  # ------------------------------------------------------------------ load

  def load(self):
    try:
      board = anvil.server.call('get_exceptions_board')
    except Exception as e:
      alert("Could not load the exception board: %s" % e)
      board = {'rows': [], 'summary': {}, 'pipeline_days': 2, 'e21_days': 2}

    rows = board.get('rows') or []
    for x in rows:
      x['qty_disp'] = _fmt(x.get('qty'))
      x['picked_disp'] = (x.get('since') or '')[:10]       # column "Since"
      x['pick_disp'] = x.get('ref') or ''                  # column "Ref"
      x['e21_disp'] = x.get('detail') or ''                # column "Detail"
      x['desc_disp'] = x.get('part_description') or ''
    self.rp_exceptions.items = rows

    s = board.get('summary') or {}
    def _c(stage):
      d = s.get(stage) or {}
      return int(d.get('count') or 0), int(d.get('overdue') or 0)
    a, ao = _c('APPROVED_NOT_PICKED')
    p, po = _c('PICKED_NOT_LOADED')
    l, lo = _c('LOADED_NOT_DELIVERED')
    e, eo = _c('E21_OPEN')
    self.lbl_exc_none.visible = not rows
    self.lbl_exc_sub.text = (
      "Approved not picked %d (%d overdue)  |  Picked not loaded %d (%d)  |  "
      "Departed not delivered %d (%d)  |  Open e21 %d (%d past %dd). "
      "Threshold %d day(s): stage 1 auto-cancels; stages 2-3 text the managers "
      "and wait for you; stage 4 is Starlite's (SO 16) or the Hub's (pre-ship)."
      % (a, ao, p, po, l, lo, e, eo, int(board.get('e21_days') or 2),
         int(board.get('pipeline_days') or 2)))

  def refresh_click(self, **event_args):
    self.load()

  # --------------------------------------------------------------- actions

  def row_action(self, action, recommendation_id=None, pick_line_id=None,
                 trip_id=None, date=None, confirmed_by=None, note=None,
                 **event_args):
    try:
      if action == 'close':
        r = anvil.server.call('close_exception', pick_line_id, date, confirmed_by, note)
        Notification("%s x%s closed as Delivered (shipped %s)"
                     % (r['sku'], _fmt(r['qty']), date), style='success', timeout=5).show()
      elif action == 'return':
        r = anvil.server.call('return_to_stock', pick_line_id, confirmed_by, note)
        Notification("%s x%s returned to stock%s; cycle count requested (%s text%s)"
                     % (r['sku'], _fmt(r['qty']),
                        ', rec cancelled' if r.get('rec_cancelled') else '',
                        r.get('sms_sent', 0), '' if r.get('sms_sent') == 1 else 's'),
                     style='success', timeout=6).show()
      elif action == 'deliver':
        r = anvil.server.call('confirm_delivered', trip_id, date, confirmed_by, note)
        Notification("Truck T%s delivered: %s line(s), %s rec(s) closed"
                     % (r['trip_id'], r['lines'], r['recs']), style='success', timeout=5).show()
      elif action == 'cancel_rec':
        r = anvil.server.call('cancel_unpicked', recommendation_id, confirmed_by, note)
        Notification("Rec %s cancelled - detector will re-raise if still needed"
                     % r['recommendation_id'], style='success', timeout=5).show()
      else:
        alert("Unknown action %r" % action)
    except Exception as e:
      alert(str(e))
    self.load()