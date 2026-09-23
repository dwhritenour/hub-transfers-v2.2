# =============================================================================
# snoozed/SnoozedRow/__init__.py
# -----------------------------------------------------------------------------
# Version: 1.0.1 (2026-09-23) - Hub Transfers v2.2 #snoozed row.
# 1.0.1: Description column removed (Dave) so the Action column is on
#        screen without scrolling; the description is the SKU's tooltip.
#        lbl_s_desc is optional - the code tolerates its absence.
# One snoozed request (container: DataRowPanel). Every cell is a DESIGNER
# label whose text this code sets in render() - no data bindings needed:
#   lbl_s_sku      SKU (bold; dark red when an ORDER HOLD applies;
#                  tooltip = description + ORDER HOLD line)
#   lbl_s_qty      quantity snoozed
#   lbl_s_until    until-date, or 'no date'
#   lbl_s_left     days left ('today' / 'OVERDUE' red / 'n/a' when no date)
#   lbl_s_reason   why (pause_reason), who / when underneath
#   lbl_s_star     LIVE Starlite available   (from the view, now)
#   lbl_s_hub      LIVE Hub available
#   lbl_s_demand   LIVE demand in horizon
#   lbl_s_urg      LIVE urgency, or 'NOT NEEDED' when the view no longer
#                  flags the SKU (the next review will obsolete it)
#   the actions cell:
#     fp_s_actions : btn_s_wake "WAKE NOW", btn_s_date "CHANGE DATE",
#                    btn_s_reject "REJECT"
#     fp_s_entry (hidden until a button is clicked): dp_s_until (DatePicker,
#       shown only for CHANGE DATE), tb_s_note (optional), btn_s_save
#       CONFIRM / btn_s_cancel CANCEL.
# Raises x-action(action, recommendation_id, until, note) on the parent.
# =============================================================================

from ._anvil_designer import SnoozedRowTemplate
from anvil import *
from datetime import date


def _iso(d):
  """date -> 'YYYY-MM-DD' without strftime (Skulpt-safe)."""
  return '%04d-%02d-%02d' % (d.year, d.month, d.day) if d else None


def _from_iso(v):
  try:
    y, m, d = str(v)[:10].split('-')
    return date(int(y), int(m), int(d))
  except Exception:
    return None

_HOLD_FG = '#8B0000'
_URG_COLOR = {'CRITICAL': '#C62828', 'HIGH': '#EF6C00', 'WARNING': '#EF6C00',
              'MEDIUM': '#F9A825', 'LOW': '#2E7D32', 'OK': '#2E7D32'}


def _fmt(v):
  try:
    f = float(v)
    return str(int(f)) if f == int(f) else "%.1f" % f
  except Exception:
    return '-' if v is None else str(v)


def _d10(v):
  return (str(v) or '')[:10] if v else ''


class SnoozedRow(SnoozedRowTemplate):

  def __init__(self, **properties):
    self.init_components(**properties)
    self._pending_action = None

    self.btn_s_wake.set_event_handler('click', lambda **e: self.start('wake'))
    self.btn_s_date.set_event_handler('click', lambda **e: self.start('redate'))
    self.btn_s_reject.set_event_handler('click', lambda **e: self.start('reject'))
    self.btn_s_save.set_event_handler('click', self.save_click)
    self.btn_s_cancel.set_event_handler('click', self.cancel_click)

    self.render()

  def render(self):
    item = self.item or {}
    hold_text = item.get('order_hold_text') or ''

    desc = str(item.get('part_description') or '')
    self.lbl_s_sku.text = str(item.get('sku') or '')
    self.lbl_s_sku.bold = True
    self.lbl_s_sku.foreground = _HOLD_FG if hold_text else '#000000'
    self.lbl_s_sku.tooltip = '\n'.join(x for x in (desc, hold_text) if x)
    if hasattr(self, 'lbl_s_desc'):          # 1.0.1: column optional
      self.lbl_s_desc.text = desc
    self.lbl_s_qty.text = _fmt(item.get('snoozed_qty'))

    until = item.get('snooze_until')
    self.lbl_s_until.text = _d10(until) if until else 'no date'
    self.lbl_s_until.italic = not until
    left = item.get('days_left')
    if until is None or left is None:
      self.lbl_s_left.text = 'n/a'
      self.lbl_s_left.foreground = '#595959'
    else:
      left = int(left)
      if left < 0:
        self.lbl_s_left.text = 'OVERDUE %dd' % (-left)
        self.lbl_s_left.foreground = '#C62828'
      elif left == 0:
        self.lbl_s_left.text = 'today'
        self.lbl_s_left.foreground = '#C62828'
      else:
        self.lbl_s_left.text = '%dd' % left
        self.lbl_s_left.foreground = '#EF6C00' if left <= 1 else '#2E7D32'

    reason = (item.get('pause_reason') or '').strip() or '(no reason given)'
    who = item.get('reviewed_by') or ''
    when = _d10(item.get('paused_date'))
    self.lbl_s_reason.text = reason + ('\n' + ' - '.join(x for x in (who, when) if x) if (who or when) else '')
    self.lbl_s_reason.foreground = _HOLD_FG if hold_text else '#000000'
    self.lbl_s_reason.tooltip = hold_text or (item.get('sales_orders') or '')

    # Live numbers (the view, right now) vs what the rec was parked on.
    still = bool(item.get('still_needed'))
    self.lbl_s_star.text = _fmt(item.get('live_starlite_available') if still else item.get('rec_starlite_available'))
    self.lbl_s_hub.text = _fmt(item.get('live_hub_available') if still else item.get('rec_hub_available'))
    self.lbl_s_demand.text = _fmt(item.get('live_demand')) if still else '-'
    for lbl in (self.lbl_s_star, self.lbl_s_hub, self.lbl_s_demand):
      lbl.italic = not still
      lbl.tooltip = ('live from the detection view' if still
                     else 'as parked - the view no longer flags this SKU')
    if still:
      urg = str(item.get('live_time_urgency') or '')
      inv = str(item.get('live_inventory_urgency') or '')
      self.lbl_s_urg.text = urg + (' / ' + inv if inv else '')
      self.lbl_s_urg.foreground = _URG_COLOR.get(urg, '#424242')
      self.lbl_s_urg.tooltip = 'time urgency / inventory urgency; live calc qty %s' % _fmt(item.get('live_calc_qty'))
    else:
      self.lbl_s_urg.text = 'NOT NEEDED'
      self.lbl_s_urg.foreground = '#595959'
      self.lbl_s_urg.tooltip = 'The view no longer needs a transfer for this SKU; the next review marks it Obsolete.'
    self.lbl_s_urg.bold = True

    self.fp_s_entry.visible = False
    self.fp_s_actions.visible = True

  # ---------------------------------------------------------------- clicks

  def start(self, action):
    item = self.item or {}
    self._pending_action = action
    self.dp_s_until.visible = (action == 'redate')
    if action == 'redate':
      self.dp_s_until.date = _from_iso(item.get('snooze_until'))
    self.tb_s_note.text = ''
    self.fp_s_actions.visible = False
    self.fp_s_entry.visible = True

  def cancel_click(self, **event_args):
    self._pending_action = None
    self.render()

  def save_click(self, **event_args):
    item = self.item or {}
    action = self._pending_action
    if not action:
      self.render()
      return
    until = None
    if action == 'redate':
      until = self.dp_s_until.date
      if until is not None and until < date.today():
        alert("The snooze date is in the past.")
        return
    note = (self.tb_s_note.text or '').strip()
    sku = item.get('sku'); qty = _fmt(item.get('snoozed_qty'))
    prompts = {
      'wake':   "Wake %s x%s now?\n\nIt goes back to the Transfer Form as Pending and is re-reviewed with fresh numbers on the next run." % (sku, qty),
      'redate': "Snooze %s x%s until %s?" % (sku, qty, _iso(until) if until else 'you wake it (no date)'),
      'reject': "Reject %s x%s?\n\nThe request is closed. It is not raised again while the need is unchanged." % (sku, qty),
    }
    if not confirm(prompts.get(action, "Proceed?")):
      return
    self.parent.raise_event('x-action',
                            action=action,
                            recommendation_id=item.get('recommendation_id'),
                            until=_iso(until),
                            note=note)
