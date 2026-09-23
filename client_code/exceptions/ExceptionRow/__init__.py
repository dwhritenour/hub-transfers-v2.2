# =============================================================================
# exceptions/ExceptionRow/__init__.py
# -----------------------------------------------------------------------------
# Version: 2.0.0 (2026-09-05) - Hub Transfers v2.1 #exceptions board.
# One board row (container: DataRowPanel). Text cells (SKU, qty, since,
# ref, detail) are DESIGNER labels bound to the item's keys. This code owns:
#   lbl_x_stage  (column stage)  - stage badge, colored per stage
#   lbl_x_alert  (column alert)  - red ⚠ when the e21 tripwire fired
#   lbl_x_days   (column days)   - age in days (amber at 1, red at threshold)
#   lbl_x_left   (column left)   - days until the system acts; "OVERDUE" red
#   the actions cell (column actions):
#     fp_x_actions : btn_x_close      "SHIPPED-CLOSE"     stage 2
#                    btn_x_return     "RETURN TO STOCK"   stage 2
#                    btn_x_deliver    "CONFIRM DELIVERED" stage 3
#                    btn_x_cancel_rec "CANCEL REC"        stage 1
#                    (stage 4 shows no buttons - lbl_x_owner says who acts)
#     fp_x_entry (hidden until a button is clicked): tb_x_date (YYYY-MM-DD,
#       shown only for SHIPPED-CLOSE / CONFIRM DELIVERED), tb_x_by
#       (initials), tb_x_note (optional), btn_x_save CONFIRM / btn_x_cancel.
# All validation of consequence is server-side; this row collects the
# entry and raises x-action(action, ids..., date, confirmed_by, note).
# =============================================================================

from ._anvil_designer import ExceptionRowTemplate
from anvil import *

_STAGE_STYLE = {
  'APPROVED_NOT_PICKED':  ('1 APPROVED',  '#1565C0'),
  'PICKED_NOT_LOADED':    ('2 PICKED',    '#6A1B9A'),
  'LOADED_NOT_DELIVERED': ('3 DEPARTED',  '#00695C'),
  'E21_OPEN':             ('4 e21 OPEN',  '#5D4037'),
}


class ExceptionRow(ExceptionRowTemplate):

  def __init__(self, **properties):
    self.init_components(**properties)
    self._pending_action = None

    self.btn_x_close.set_event_handler('click', lambda **e: self.start('close', needs_date=True))
    self.btn_x_return.set_event_handler('click', lambda **e: self.start('return', needs_date=False))
    self.btn_x_deliver.set_event_handler('click', lambda **e: self.start('deliver', needs_date=True))
    self.btn_x_cancel_rec.set_event_handler('click', lambda **e: self.start('cancel_rec', needs_date=False))
    self.btn_x_save.set_event_handler('click', self.save_click)
    self.btn_x_cancel.set_event_handler('click', self.cancel_click)

    self.render()

  def render(self):
    item = self.item or {}
    stage = item.get('stage') or ''
    label, color = _STAGE_STYLE.get(stage, (stage, '#424242'))
    self.lbl_x_stage.text = label
    self.lbl_x_stage.foreground = color

    if item.get('has_alert'):
      self.lbl_x_alert.text = "⚠"
      self.lbl_x_alert.foreground = '#C62828'
      self.lbl_x_alert.tooltip = "e21 tripwire: e21 transfer activity with no departed trip"
    else:
      self.lbl_x_alert.text = ""

    age = int(item.get('age_days') or 0)
    left = item.get('days_left')
    overdue = bool(item.get('overdue'))
    self.lbl_x_days.text = "%dd" % age
    self.lbl_x_days.foreground = ('#C62828' if overdue
                                  else '#EF6C00' if age >= 1 else '#2E7D32')
    if left is None:
      self.lbl_x_left.text = "-"
      self.lbl_x_left.foreground = '#595959'
    elif left == -999:
      self.lbl_x_left.text = "n/a"
      self.lbl_x_left.foreground = '#595959'
    elif left < 0:
      self.lbl_x_left.text = "OVERDUE %dd" % (-left)
      self.lbl_x_left.foreground = '#C62828'
    elif left == 0:
      self.lbl_x_left.text = "today"
      self.lbl_x_left.foreground = '#C62828'
    else:
      self.lbl_x_left.text = "%dd" % left
      self.lbl_x_left.foreground = '#EF6C00' if left == 1 else '#2E7D32'
    self.lbl_x_left.tooltip = item.get('auto_action') or ''

    # Which buttons apply to this stage.
    self.btn_x_close.visible = (stage == 'PICKED_NOT_LOADED')
    self.btn_x_return.visible = (stage == 'PICKED_NOT_LOADED')
    self.btn_x_deliver.visible = (stage == 'LOADED_NOT_DELIVERED')
    self.btn_x_cancel_rec.visible = (stage == 'APPROVED_NOT_PICKED'
                                     and 'being picked' not in (item.get('detail') or ''))
    self.lbl_x_owner.text = (("%s: %s" % (item.get('owner'),
                                          'receive the e21 order' if item.get('owner') == 'Starlite'
                                          else 'ship it or cancel the e21 line'))
                             if stage == 'E21_OPEN' else '')

    self.fp_x_entry.visible = False
    self.fp_x_actions.visible = True

  # ---------------------------------------------------------------- clicks

  def start(self, action, needs_date):
    item = self.item or {}
    self._pending_action = action
    self.tb_x_date.visible = needs_date
    self.tb_x_date.text = (item.get('since') or '')[:10] if needs_date else ''
    self.tb_x_by.text = ''
    self.tb_x_note.text = ''
    self.fp_x_actions.visible = False
    self.fp_x_entry.visible = True

  def cancel_click(self, **event_args):
    self._pending_action = None
    self.render()

  def save_click(self, **event_args):
    item = self.item or {}
    action = self._pending_action
    if not action:
      self.render()
      return
    date_txt = (self.tb_x_date.text or '').strip() if self.tb_x_date.visible else None
    confirmed_by = (self.tb_x_by.text or '').strip()
    if self.tb_x_date.visible and not date_txt:
      alert("Enter the date (YYYY-MM-DD).")
      return
    if not confirmed_by:
      alert("Enter your initials - every action here records who confirmed it.")
      return

    sku = item.get('sku'); qty = item.get('qty_disp'); ref = item.get('ref')
    prompts = {
      'close':      "Close %s x%s (%s) as SHIPPED %s without a trip?\n\nOnly if the goods truly left the Hub." % (sku, qty, ref, date_txt),
      'return':     "Return %s x%s (%s) to the Hub shelf?\n\nThe line is cancelled, the rec is cancelled if nothing else holds it, and Inventory Control is texted to cycle count." % (sku, qty, ref),
      'deliver':    "Confirm truck %s delivered at Starlite on %s?\n\nEvery line and rec on it closes as Delivered - same as the driver's confirmation." % (ref, date_txt),
      'cancel_rec': "Cancel rec for %s x%s (%s)?\n\nNothing has been picked. The detector re-recommends if Starlite still needs it." % (sku, qty, ref),
    }
    if not confirm(prompts.get(action, "Proceed?")):
      return
    self.parent.raise_event('x-action',
                            action=action,
                            recommendation_id=item.get('recommendation_id'),
                            pick_line_id=item.get('pick_line_id'),
                            trip_id=item.get('trip_id'),
                            date=date_txt,
                            confirmed_by=confirmed_by,
                            note=(self.tb_x_note.text or '').strip())