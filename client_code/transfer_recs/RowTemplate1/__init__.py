"""
RowTemplate1 - Transfer Recommendations Row Template  (Hub Transfers v2.2)
=========================================================================
Client-side code for individual rows in the DataGrid.

2026-10-25 (PAUSE alongside SNOOZE):
- Pending rows offer Approve / Reject / Snooze / Pause; paused rows
  (status='Paused', pause_kind='pause') stay on the Transfer Form with
  Resume / Approve / Reject / Snooze and a PAUSED Nd badge (amber < 5d,
  light red >= 5d).

2026-09-23 (v2.2 SNOOZE + ORDER HOLD):
- The decision dropdown offers Approve / Reject / SNOOZE (items set here in
  code; the designer's [Approve, Reject, Pause] is overridden). Choosing
  Snooze opens a dialog built in code (no designer work): amount to snooze
  (pre-filled with the held qty, or the whole request), until-date
  (pre-filled today + snooze_default_days, blank = until woken) and reason
  (pre-filled with the ORDER HOLD line). Whatever is NOT snoozed ships now
  - the row's Transfer Quantity box shows that number after the dialog.
  Apply sends decision 'Paused' + snooze_qty / snooze_until / pause_reason
  to bulk_update_decisions, which calls transfer_dashboard._snooze_rec.
- ORDER HOLD: when the server attached order_hold_text, the Sales Orders
  cell leads with it in dark red on a pink tint, held lines are tagged
  [HOLD], and the AI-said-0 pre-fill is NOT applied (0 is the right
  released shortfall; the manager types a qty or snoozes).
- The Reject snooze date picker (dp_snooze, v5.23) is retired: hidden.

2026-09-18 (with transfer_ai_review 5.35.2): when the AI quantity is 0 but
the calculated quantity is not, the calc is PRE-FILLED as the override
(amber box) so Approve works with one click - the box already showed the
calc, but nothing was sent unless the manager retyped it, and the server
then blocked the zero (YS12D0100 rec 3982, 2026-09-18).

Existing styling (unchanged):
- txt_sku amber/red background for Paused status (amber < 3 days, red > 3 days)
- txt_sku dark-orange + bold + reasoning amber-tinted for has_duplicate_warning

v5.17 (NEW, 2026-05-13): Time-urgency highlight for sales orders within 2 days.
- When time_urgency is 'CRITICAL' (overdue or shipping today) or 'HIGH'
  (within 2 days), txt_time_urgency gets dark-red bold text.
- Designed to compose cleanly with both Paused-status (different cell) and
  duplicate-warning (different cell) styling.
- Uses bold weight as the PRIMARY signal so the urgency cue does not depend
  on color perception alone — useful for users with red-green colorblindness.
  Color is secondary reinforcement, not the only signal.
- If dark red is not distinguishable, swap the color (alternatives listed
  at the v5.17 block below) — bold weight alone will still convey urgency.

Place this file in: client_code/transfer_recs/RowTemplate1/__init__.py
"""

from ._anvil_designer import RowTemplate1Template
from anvil import *
import anvil.server
from datetime import date, timedelta

_DECISIONS = ['Approve', 'Reject', 'Snooze', 'Pause']          # a Pending row
_DECISIONS_PAUSED = ['Resume', 'Approve', 'Reject', 'Snooze']  # a paused row
_PAUSE_BG = '#FFF3CD'        # amber: paused, fresh
_PAUSE_OLD_BG = '#FFCCCB'    # light red: paused 5+ days
_PAUSE_OLD_DAYS = 5
_HOLD_BG = '#FDE7E9'      # pink tint
_HOLD_FG = '#8B0000'      # dark red


def _num(v, default=0.0):
  try:
    return float(v)
  except Exception:
    return default


def _iso(d):
  """date -> 'YYYY-MM-DD' without strftime (Skulpt-safe)."""
  return '%04d-%02d-%02d' % (d.year, d.month, d.day) if d else None


class RowTemplate1(RowTemplate1Template):
  def __init__(self, **properties):
    # Set Form properties and Data Bindings
    self.init_components(**properties)
    self._display_qty = None          # what the qty box showed before a snooze

    self._is_paused = (self.item.get('status') == 'Paused' and self.item.get('pause_kind') == 'pause')
    self.dd_decision.items = _DECISIONS_PAUSED if self._is_paused else _DECISIONS

    # -----------------------------------------------------------------
    # Color-code SKU based on status
    # -----------------------------------------------------------------
    status = self.item.get('status')
    days_paused = self.item.get('days_paused')

    if self._is_paused:
      d = int(days_paused or 0)
      old = d >= _PAUSE_OLD_DAYS
      self.txt_sku.background = _PAUSE_OLD_BG if old else _PAUSE_BG
      self.txt_status.text = 'PAUSED %dd' % d
      self.txt_status.background = _PAUSE_OLD_BG if old else _PAUSE_BG
      self.txt_status.tooltip = ('Paused %d day(s). Resume, Approve, Reject or Snooze it from the '
                                 'dropdown. A pause older than %d days should have been a Snooze '
                                 'or a Reject.' % (d, _PAUSE_OLD_DAYS))

    # -----------------------------------------------------------------
    # v5.5: Duplicate-warning highlight
    # Tint the reasoning TextArea amber and emphasize the SKU so the
    # POSSIBLE DUPLICATE text at the top of the reasoning is impossible
    # to miss. Runs independently of (and after) the Paused branch so it
    # composes cleanly when both conditions apply.
    # -----------------------------------------------------------------
    if self.item.get('has_duplicate_warning'):
      self.ta_ai_reason.background = '#FFF3CD'   # light amber
      self.ta_ai_reason.foreground = '#664D03'   # dark amber for contrast
      self.txt_sku.foreground = '#CC6600'        # dark orange
      self.txt_sku.bold = True

    # -----------------------------------------------------------------
    # v5.17: Time-urgency highlight (within 2 days)
    # CRITICAL = shipping today or overdue. HIGH = within 2 days.
    # Both get bold + dark-red text on txt_time_urgency so the Hub
    # Manager can scan the page for urgent rows at a glance.
    #
    # Composes with Paused (txt_sku background) and duplicate warning
    # (txt_sku foreground + ta_ai_reason background) because all three
    # target different cells.
    #
    # Color choice rationale: bold weight is the primary accessibility
    # signal; dark red is a secondary reinforcement. If '#C62828' is
    # not visually distinguishable for you, try these alternatives —
    # bold weight still does the job in any of them:
    #   - '#B71C1C'  Material Red 900 (darker, more saturated)
    #   - '#1565C0'  Material Blue 800 (good for red-green colorblindness)
    #   - '#4A148C'  Material Purple 900 (very distinct from existing palette)
    # Or add a background tint as well, e.g.:
    #   self.txt_time_urgency.background = '#FFEBEE'   # very light pink
    # -----------------------------------------------------------------
    time_urg = self.item.get('time_urgency')
    if time_urg in ('CRITICAL', 'HIGH'):
      self.txt_time_urgency.foreground = '#C62828'   # Material Red 800
      self.txt_time_urgency.bold = True

    # -----------------------------------------------------------------
    # v2.2 ORDER HOLD: part of this request's demand is on Hold-status
    # sales orders. Lead the Sales Orders cell with the line, in red.
    # -----------------------------------------------------------------
    _hold_text = self.item.get('order_hold_text') or ''
    self._hold_qty = int(_num(self.item.get('order_hold_qty')))
    if _hold_text:
      self.ta_sales_orders.text = _hold_text + '\n' + str(self.item.get('sales_orders') or '')
      self.ta_sales_orders.background = _HOLD_BG
      self.ta_sales_orders.foreground = _HOLD_FG
      self.ta_sales_orders.tooltip = ("Held orders (e21 status 00) may never release. "
                                      "Confirm with Sales, then ship the released part "
                                      "or Snooze the held part.")

      # -----------------------------------------------------------------
      # Initialize dropdown based on current status
      # -----------------------------------------------------------------
    if status == 'Approved':
      self.dd_decision.selected_value = 'Approve'
    elif status == 'Rejected':
      self.dd_decision.selected_value = 'Reject'
    elif status == 'Paused':
      self.dd_decision.selected_value = None     # paused: no decision until the manager picks one
    else:
      self.dd_decision.selected_value = None  # Pending - no selection

      # -----------------------------------------------------------------
      # Initialize transfer quantity field
      # Show manager_override_qty if it exists, otherwise ai_recommended_qty
      # -----------------------------------------------------------------
    override_qty = self.item.get('manager_override_qty')
    if override_qty:
      display_qty = override_qty
    else:
      display_qty = self.item.get('ai_recommended_qty') or self.item.get('calculated_transfer_qty')
    if display_qty is not None:
      self.trans_qty.text = str(int(display_qty))
    self._display_qty = self.trans_qty.text

    # 2026-09-18: AI said 0 (or nothing) against a real calculated need.
    # Pre-fill the calc as the override so Approve is one click; amber box
    # + tooltip tell the manager this number is the calc, not the AI's.
    try:
      _ai_q = float(self.item.get('ai_recommended_qty') or 0)
      _calc_q = float(self.item.get('calculated_transfer_qty') or 0)
    except Exception:
      _ai_q, _calc_q = 0.0, 0.0
    if not override_qty and _ai_q <= 0 and _calc_q > 0 and self._hold_qty <= 0:
      self.item['transfer_qty_override'] = int(_calc_q)
      self.trans_qty.text = str(int(_calc_q))
      self._display_qty = self.trans_qty.text
      self.trans_qty.background = '#FFF3CD'   # light amber
      self.trans_qty.tooltip = ("AI recommended 0; the calculated qty (%d) is pre-filled. "
                                "Edit it or Approve as is." % int(_calc_q))
    elif not override_qty and _ai_q <= 0 and self._hold_qty > 0:
      # v2.2: 0 is the RELEASED shortfall - the need is on held orders.
      self.trans_qty.text = '0'
      self._display_qty = '0'
      self.trans_qty.background = _HOLD_BG
      self.trans_qty.tooltip = ("Released shortfall is 0 - the %d unit(s) needed are on held "
                                "orders. Snooze, or type a quantity to ship them anyway."
                                % self._hold_qty)

    # v5.23 (Phase 2): optional reject-snooze date picker. Wired defensively
    # so the row still loads before dp_snooze is added in the designer.
    if hasattr(self, 'dp_snooze'):
      self.dp_snooze.set_event_handler('change', self.dp_snooze_change)
    self._update_snooze_visibility()

    # =====================================================================
    # EVENT HANDLERS
    # =====================================================================

  @handle("dd_decision", "change")
  def dd_decision_change(self, **event_args):
    """
        When the user selects a decision from the dropdown,
        store it in self.item so the parent form can collect it.
        """
    selected = self.dd_decision.selected_value

    if selected in ('Approve', 'Reject', 'Pause', 'Resume', None):
      self._clear_snooze()
    if selected == 'Approve':
      self.item['decision'] = 'Approved'
    elif selected == 'Reject':
      self.item['decision'] = 'Rejected'
    elif selected == 'Pause':
      self.item['decision'] = 'Pause'
    elif selected == 'Resume':
      self.item['decision'] = 'Resume'
    elif selected == 'Snooze':
      if not self._snooze_dialog():
        # Cancelled: back to no decision.
        self.dd_decision.selected_value = None
        self._clear_snooze()
    else:
      self.item['decision'] = None

    self._update_snooze_visibility()

  # =====================================================================
  # v2.2 SNOOZE DIALOG (built in code - nothing to add in the designer)
  # =====================================================================

  def _eff_qty(self):
    """The quantity this request stands at right now (box, override, AI, calc)."""
    try:
      if (self.trans_qty.text or '').strip():
        return int(float(self.trans_qty.text))
    except Exception:
      pass
    for k in ('transfer_qty_override', 'manager_override_qty', 'ai_recommended_qty',
              'calculated_transfer_qty'):
      v = self.item.get(k)
      if v not in (None, ''):
        try:
          return int(float(v))
        except Exception:
          pass
    return 0

  def _clear_snooze(self):
    for k in ('snooze_qty', 'snooze_total', 'snooze_until', 'pause_reason'):
      self.item[k] = None
    if self.item.get('_snoozed'):
      self.item['_snoozed'] = False
      if getattr(self, '_is_paused', False):
        self.txt_status.text = 'PAUSED %dd' % int(self.item.get('days_paused') or 0)
        self.txt_status.background = (_PAUSE_OLD_BG if int(self.item.get('days_paused') or 0) >= _PAUSE_OLD_DAYS else _PAUSE_BG)
      else:
        self.txt_status.text = str(self.item.get('status') or '')
        self.txt_status.background = None
      self.trans_qty.text = self._display_qty or ''
      self.trans_qty.tooltip = ''

  def _snooze_dialog(self):
    """Amount / until / reason. Returns True when the manager confirmed."""
    sku = str(self.item.get('sku') or '')
    eff = self._eff_qty()
    if eff <= 0:
      # Nothing sized yet (AI 0 on a hold) - snooze the calc need instead.
      eff = int(_num(self.item.get('calculated_transfer_qty')))
    if eff <= 0:
      alert("This request has no quantity to snooze. Reject it instead.")
      return False
    hold = self._hold_qty
    default_qty = min(hold, eff) if hold > 0 else eff
    default_days = int(_num(self.item.get('snooze_default_days'), 7))
    hold_text = self.item.get('order_hold_text') or ''

    lbl_head = Label(text="Snooze %s - this request is %d pc(s)" % (sku, eff), bold=True, font_size=15)
    lbl_hold = Label(text=hold_text, foreground=_HOLD_FG, visible=bool(hold_text), font_size=12)
    tb_qty = TextBox(text=str(default_qty), type='number', placeholder='Amount to snooze')
    dp_until = DatePicker(date=date.today() + timedelta(days=default_days), format='%Y-%m-%d',
                          placeholder='Until (blank = until you wake it)')
    ta_reason = TextArea(text=(hold_text.split(' (')[0] if hold_text else ''),
                         placeholder='Reason (what Sales said, who you asked...)',
                         auto_expand=True)
    lbl_ship = Label(text='', italic=True, foreground='#595959')

    def _refresh(**e):
      try:
        q = int(float(tb_qty.text or 0))
      except Exception:
        q = 0
      ship = max(0, eff - q) if 0 < q < eff else (0 if q >= eff else eff)
      if q <= 0:
        lbl_ship.text = "Enter an amount between 1 and %d." % eff
      elif q >= eff:
        lbl_ship.text = "The whole request (%d) is snoozed. Nothing ships now." % eff
      else:
        lbl_ship.text = ("%d snoozed; the other %d are APPROVED and ship now (they go to the "
                         "pick pool when you click Apply)." % (q, ship))
    tb_qty.set_event_handler('change', _refresh)
    tb_qty.set_event_handler('lost_focus', _refresh)
    _refresh()

    panel = ColumnPanel(spacing_above='none', spacing_below='none')
    panel.add_component(lbl_head)
    panel.add_component(lbl_hold)
    panel.add_component(Label(text="Amount to snooze", bold=True))
    panel.add_component(tb_qty)
    panel.add_component(lbl_ship)
    panel.add_component(Label(text="Until", bold=True))
    panel.add_component(dp_until)
    panel.add_component(Label(text="Reason", bold=True))
    panel.add_component(ta_reason)

    while True:
      ok = alert(content=panel, title="Snooze transfer request", large=True,
                 buttons=[("Snooze", True), ("Cancel", False)], dismissible=False)
      if not ok:
        return False
      try:
        q = int(float(tb_qty.text or 0))
      except Exception:
        q = 0
      if q <= 0:
        alert("Enter an amount between 1 and %d." % eff)
        continue
      until = dp_until.date
      if until is not None and until < date.today():
        alert("The snooze date is in the past.")
        continue
      break

    q = min(q, eff)
    ship = eff - q
    self.item['decision'] = 'Paused'
    self.item['snooze_qty'] = q
    self.item['snooze_total'] = eff      # the number the manager saw; server splits against it
    self.item['snooze_until'] = _iso(until)
    self.item['pause_reason'] = (ta_reason.text or '').strip() or None
    self.item['_snoozed'] = True
    # Visual feedback on the row until Apply.
    until_txt = ('%d/%d' % (until.month, until.day)) if until else 'no date'
    self.txt_status.text = "SNOOZE %d -> %s" % (q, until_txt)
    self.txt_status.background = '#FFF3CD'
    self.trans_qty.text = str(ship)
    self.trans_qty.tooltip = ("%d ship now (approved on Apply); %d snoozed until %s."
                              % (ship, q, until_txt))
    return True

  @handle("trans_qty", "lost_focus")
  def trans_qty_lost_focus(self, **event_args):
    """
        When the user edits the transfer quantity,
        store the override value in self.item.
        """
    try:
      qty = int(self.trans_qty.text) if self.trans_qty.text else None
      self.item['transfer_qty_override'] = qty
    except ValueError:
      self.item['transfer_qty_override'] = None

  def dp_snooze_change(self, **event_args):
    """v5.23: store the optional 'snooze until' date on the row item. Used by
    the server only when the decision is Reject; blank = auto cooldown."""
    self.item['snooze_until'] = self.dp_snooze.date

  def _update_snooze_visibility(self):
    """v2.2: the Reject snooze picker is retired (Snooze has its own dialog).
    Kept so a designer that still has dp_snooze hides it."""
    if hasattr(self, 'dp_snooze'):
      self.dp_snooze.visible = False