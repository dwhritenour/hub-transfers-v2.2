"""
MinMaxRowTemplate - MinMax Review Row Template (v61 - 2026-05-11)
==================================================================
Client-side code for individual rows in the MinMax recommendations DataGrid.

CHANGES IN v61 (Lock terminal-state rows):
  * Rejected rows now disable dd_decision, txt_rec_min, and txt_rec_max
    the same way Applied rows do. Rejected is terminal — it does not
    transition to Applied when Apply to Production runs — so the row
    should not be re-editable after the manager has chosen to reject it.
  * Approved and Modified remain editable so the manager can still revert
    or amend their decision before Apply to Production runs.
  * tb_reason was already hidden for non-Modify states via
    _set_reason_visibility(); no change needed there.

CHANGES IN v60 (Manual Min/Max Override support):
  * txt_rec_min and txt_rec_max are now editable.
  * dd_decision now offers 'Modify' alongside Approve / Reject.
  * tb_reason text box surfaces when a row is in Modify state and holds the
    required override justification.
  * Editing either Rec Min or Rec Max from the AI value AUTO-flips the
    decision dropdown to 'Modify' and reveals tb_reason. The original AI
    values are preserved in self.item under 'ai_recommended_min' and
    'ai_recommended_max' so we can compare and so reverting to those values
    while in Modify state cleanly switches back to no-decision.
  * Pre-populates from existing override_min / override_max / override_reason
    when the row is already Modified or Applied (so the manager sees the
    actual applied values, not the AI's original recommendation).

Place this file in: client_code/minmax_review/MinMaxRowTemplate/__init__.py
"""

from ._anvil_designer import MinMaxRowTemplateTemplate
from anvil import *
import anvil.server


class MinMaxRowTemplate(MinMaxRowTemplateTemplate):
  def __init__(self, **properties):
    # Set Form properties and Data Bindings
    self.init_components(**properties)

    # ------------------------------------------------------------------
    # Preserve the AI values on the item so we can compare against
    # user edits later. (recommended_min/max in self.item may already
    # have been overwritten in a prior render pass.)
    # ------------------------------------------------------------------
    if 'ai_recommended_min' not in self.item:
      self.item['ai_recommended_min'] = self.item.get('recommended_min')
    if 'ai_recommended_max' not in self.item:
      self.item['ai_recommended_max'] = self.item.get('recommended_max')

    status = self.item.get('status')

    # ------------------------------------------------------------------
    # Decide which values to display in the Rec Min / Rec Max cells.
    #   * If row is Modified / Applied with an override, show the override
    #   * Otherwise show the AI recommendation
    # ------------------------------------------------------------------
    override_min = self.item.get('override_min')
    override_max = self.item.get('override_max')
    has_override = (override_min is not None) or (override_max is not None)

    if has_override and status in ('Modified', 'Applied'):
      display_min = override_min if override_min is not None else self.item.get('ai_recommended_min')
      display_max = override_max if override_max is not None else self.item.get('ai_recommended_max')
    else:
      display_min = self.item.get('ai_recommended_min')
      display_max = self.item.get('ai_recommended_max')

    self.txt_rec_min.text = '' if display_min is None else str(int(display_min))
    self.txt_rec_max.text = '' if display_max is None else str(int(display_max))

    # ------------------------------------------------------------------
    # Status-based SKU highlighting
    # ------------------------------------------------------------------
    if status == 'Pending':
      self.txt_sku.bold = True
    elif status == 'Approved':
      self.txt_sku.foreground = '#2196F3'
      self.txt_sku.bold = True
    elif status == 'Rejected':
      self.txt_sku.foreground = '#888888'
      self.txt_sku.italic = True
    elif status == 'Modified':
      # Distinct purple/teal so managers can spot manually-overridden rows
      self.txt_sku.foreground = '#7B1FA2'
      self.txt_sku.bold = True
    elif status == 'Applied':
      self.txt_sku.foreground = '#4CAF50'
      self.txt_sku.bold = True

    # ------------------------------------------------------------------
    # Change direction color-coding with arrows.
    # Compare displayed values vs current values.
    # ------------------------------------------------------------------
    self.format_change_cell(
      self.txt_rec_min, display_min, self.item.get('current_min')
    )
    self.format_change_cell(
      self.txt_rec_max, display_max, self.item.get('current_max')
    )
    self.format_pct_cell(self.txt_min_pct, self.item.get('min_pct_change'))
    self.format_pct_cell(self.txt_max_pct, self.item.get('max_pct_change'))

    # ------------------------------------------------------------------
    # Confidence level styling
    # ------------------------------------------------------------------
    confidence = self.item.get('confidence_level', '')
    if confidence == 'High':
      self.txt_confidence.foreground = '#388E3C'
      self.txt_confidence.bold = True
    elif confidence == 'Medium':
      self.txt_confidence.foreground = '#FF9800'
      self.txt_confidence.bold = True
    elif confidence == 'Low':
      self.txt_confidence.foreground = '#D32F2F'
      self.txt_confidence.bold = True

    # ------------------------------------------------------------------
    # Status text color-coding
    # ------------------------------------------------------------------
    if status == 'Pending':
      self.txt_status.foreground = '#FF9800'
      self.txt_status.bold = True
    elif status == 'Approved':
      self.txt_status.foreground = '#2196F3'
    elif status == 'Rejected':
      self.txt_status.foreground = '#888888'
    elif status == 'Modified':
      self.txt_status.foreground = '#7B1FA2'
      self.txt_status.bold = True
    elif status == 'Applied':
      self.txt_status.foreground = '#4CAF50'

    # ------------------------------------------------------------------
    # Decision dropdown initialization + terminal-state lockdown
    #
    # Terminal states (Rejected, Applied) lock dd_decision, txt_rec_min,
    # txt_rec_max so the row cannot be accidentally re-decided. Approved
    # and Modified remain editable because the manager may still want to
    # revert or amend before Apply to Production runs.
    # ------------------------------------------------------------------
    if status == 'Approved':
      self.dd_decision.selected_value = 'Approve'
    elif status == 'Rejected':
      self.dd_decision.selected_value = 'Reject'
      # v61: Rejected is terminal - lock controls (was the bug: these
      # were left enabled, letting Rejected rows be re-edited).
      self.dd_decision.enabled = False
      self.txt_rec_min.enabled = False
      self.txt_rec_max.enabled = False
    elif status == 'Modified':
      self.dd_decision.selected_value = 'Modify'
    elif status == 'Applied':
      # Applied items cannot be changed
      self.dd_decision.enabled = False
      self.txt_rec_min.enabled = False
      self.txt_rec_max.enabled = False
    else:
      # Pending - no selection yet
      self.dd_decision.selected_value = None

    # ------------------------------------------------------------------
    # Reason textbox initialization
    #   * Pre-populate with existing override_reason if any
    #   * Visible whenever the row is in Modify state
    # ------------------------------------------------------------------
    existing_reason = self.item.get('override_reason')
    if existing_reason:
      self.tb_reason.text = existing_reason

    self._set_reason_visibility()

    # Also seed the item's working override values so apply_decisions can
    # pick them up without forcing the user to re-type if they're just
    # confirming the existing override.
    if status == 'Modified':
      if override_min is not None and self.item.get('override_min') is None:
        self.item['override_min'] = override_min
      if override_max is not None and self.item.get('override_max') is None:
        self.item['override_max'] = override_max
      self.item['decision'] = 'Modified'

  # =====================================================================
  # FORMATTING HELPERS
  # =====================================================================

  def format_change_cell(self, textbox, new_val, old_val):
    """
    Format a recommended value cell with directional arrow and color.
    NOTE: For an editable textbox we keep the raw number in .text but
    color/bold it based on direction. We do NOT prepend an arrow because
    that would corrupt the editable value the user sees.
    """
    try:
      new_num = float(new_val or 0)
      old_num = float(old_val or 0)
    except (ValueError, TypeError):
      return

    if new_num > old_num:
      textbox.foreground = '#D32F2F'
      textbox.bold = True
    elif new_num < old_num:
      textbox.foreground = '#388E3C'
      textbox.bold = True
    else:
      textbox.foreground = '#888888'

  def format_pct_cell(self, textbox, pct_val):
    """
    Format a percentage change cell with directional arrow and color.
    """
    try:
      pct = float(pct_val or 0)
    except (ValueError, TypeError):
      return

    if pct > 0:
      textbox.text = '↑ ' + str(pct) + '%'
      textbox.foreground = '#D32F2F'
      textbox.bold = True
    elif pct < 0:
      textbox.text = '↓ ' + str(abs(pct)) + '%'
      textbox.foreground = '#388E3C'
      textbox.bold = True
    else:
      textbox.text = '0%'
      textbox.foreground = '#888888'

  # =====================================================================
  # INTERNAL: visibility / state helpers
  # =====================================================================

  def _is_modify_active(self):
    """True if the current dropdown state represents a manual override."""
    return self.dd_decision.selected_value == 'Modify'

  def _set_reason_visibility(self):
    """Show or hide tb_reason based on current Modify state."""
    self.tb_reason.visible = self._is_modify_active()

  def _read_int(self, textbox):
    """Try to read an int from a textbox; return None if blank/invalid."""
    txt = (textbox.text or '').strip()
    if txt == '':
      return None
    try:
      return int(float(txt))
    except (ValueError, TypeError):
      return None

  def _values_differ_from_ai(self):
    """True if the user has typed values different from the AI recommendation."""
    ai_min = self.item.get('ai_recommended_min')
    ai_max = self.item.get('ai_recommended_max')
    cur_min = self._read_int(self.txt_rec_min)
    cur_max = self._read_int(self.txt_rec_max)

    try:
      if cur_min is not None and int(ai_min) != cur_min:
        return True
    except (ValueError, TypeError):
      pass
    try:
      if cur_max is not None and int(ai_max) != cur_max:
        return True
    except (ValueError, TypeError):
      pass

    return False

  def _sync_item_from_inputs(self):
    """
    Pull current input values into self.item so the parent form's
    'Apply Decisions' collector can read them.
    """
    decision = self.dd_decision.selected_value

    if decision == 'Approve':
      self.item['decision'] = 'Approved'
      self.item['override_min'] = None
      self.item['override_max'] = None
      self.item['override_reason'] = None

    elif decision == 'Reject':
      self.item['decision'] = 'Rejected'
      self.item['override_min'] = None
      self.item['override_max'] = None
      self.item['override_reason'] = None

    elif decision == 'Modify':
      self.item['decision'] = 'Modified'
      self.item['override_min'] = self._read_int(self.txt_rec_min)
      self.item['override_max'] = self._read_int(self.txt_rec_max)
      self.item['override_reason'] = (self.tb_reason.text or '').strip()

    else:
      self.item['decision'] = None

  # =====================================================================
  # EVENT HANDLERS
  # =====================================================================

  @handle("dd_decision", "change")
  def dd_decision_change(self, **event_args):
    """
    When the user explicitly picks a decision from the dropdown, update
    item state and toggle the reason field as appropriate. If they pick
    Modify but haven't changed Rec Min / Rec Max from the AI values yet,
    we leave the cells with the AI values - they'll still need to type
    new values (or it'll be a no-op override).
    """
    self._set_reason_visibility()
    self._sync_item_from_inputs()

  @handle("txt_rec_min", "change")
  def txt_rec_min_change(self, **event_args):
    """User edited the Rec Min cell - auto-flip to Modify if changed."""
    self._handle_value_edit()

  @handle("txt_rec_min", "lost_focus")
  def txt_rec_min_lost_focus(self, **event_args):
    self._handle_value_edit()

  @handle("txt_rec_max", "change")
  def txt_rec_max_change(self, **event_args):
    self._handle_value_edit()

  @handle("txt_rec_max", "lost_focus")
  def txt_rec_max_lost_focus(self, **event_args):
    self._handle_value_edit()

  @handle("tb_reason", "change")
  def tb_reason_change(self, **event_args):
    self._sync_item_from_inputs()

  @handle("tb_reason", "lost_focus")
  def tb_reason_lost_focus(self, **event_args):
    self._sync_item_from_inputs()

  def _handle_value_edit(self):
    """
    Shared logic for when Rec Min or Rec Max is edited:
      * If the typed values differ from the AI recommendation, auto-flip
        decision to 'Modify' and reveal the reason field.
      * If the typed values are reverted to match the AI recommendation
        AND the row was in Modify, clear the decision (manager has un-done
        the override).
    """
    differs = self._values_differ_from_ai()

    if differs:
      if self.dd_decision.selected_value != 'Modify':
        self.dd_decision.selected_value = 'Modify'
    else:
      # Values match the AI rec. If currently in Modify, drop back to no
      # decision so the manager has to consciously re-pick a state.
      if self.dd_decision.selected_value == 'Modify':
        self.dd_decision.selected_value = None
        self.tb_reason.text = ''

    self._set_reason_visibility()
    self._sync_item_from_inputs()

  @handle("link_details", "click")
  def link_details_click(self, **event_args):
    """Show full details for this recommendation in a popup."""
    rec_id = self.item.get('recommendation_id')
    sku = self.item.get('sku', 'Unknown')

    result = anvil.server.call('get_minmax_recommendation_detail', rec_id)

    if result['status'] == 'error':
      alert('Error loading details: ' + result.get('message', 'Unknown error'))
      return

    detail = result['detail']

    # Build detail text
    lines = []
    lines.append('SKU: ' + str(detail.get('sku', '')))
    lines.append('')
    lines.append('--- Current vs Recommended ---')
    lines.append('Current Min: ' + str(detail.get('current_min', '')) + '  |  Recommended Min: ' + str(detail.get('recommended_min', '')))
    lines.append('Current Max: ' + str(detail.get('current_max', '')) + '  |  Recommended Max: ' + str(detail.get('recommended_max', '')))

    # NEW v60: show override info if present
    if detail.get('override_min') is not None or detail.get('override_max') is not None:
      lines.append('')
      lines.append('--- Manager Override ---')
      lines.append('Override Min: ' + str(detail.get('override_min', '')) + '  |  Override Max: ' + str(detail.get('override_max', '')))
      lines.append('Override By: ' + str(detail.get('override_by', '')) + '  |  Date: ' + str(detail.get('override_date', '')))
      lines.append('Reason: ' + str(detail.get('override_reason', '')))

    lines.append('')
    lines.append('--- Days of Supply ---')
    lines.append('Current Min DoS: ' + str(detail.get('current_min_dos', '')) + '  |  Recommended Min DoS: ' + str(detail.get('recommended_min_dos', '')))
    lines.append('Current Max DoS: ' + str(detail.get('current_max_dos', '')) + '  |  Recommended Max DoS: ' + str(detail.get('recommended_max_dos', '')))
    if detail.get('override_min_dos') is not None:
      lines.append('Override Min DoS: ' + str(detail.get('override_min_dos', '')) + '  |  Override Max DoS: ' + str(detail.get('override_max_dos', '')))
    lines.append('')
    lines.append('--- Demand Statistics ---')
    lines.append('Demand Pattern: ' + str(detail.get('demand_pattern', '')))
    lines.append('Avg Daily Usage: ' + str(detail.get('avg_daily_usage', '')))
    lines.append('Std Dev: ' + str(detail.get('std_dev', '')))
    lines.append('CV: ' + str(detail.get('coefficient_of_variation', '')))
    lines.append('Avg Order Size: ' + str(detail.get('avg_order_size', '')))
    lines.append('Total Orders: ' + str(detail.get('total_orders', '')))
    lines.append('Days with Sales: ' + str(detail.get('days_with_sales', '')) + ' / ' + str(detail.get('days_of_data', '')))
    lines.append('Lead Time: ' + str(detail.get('lead_time', '')) + ' days')
    lines.append('Safety Days: ' + str(detail.get('safety_days', '')))
    lines.append('')
    lines.append('--- AI Analysis ---')
    lines.append('Confidence: ' + str(detail.get('confidence_level', '')))
    lines.append('Batch: ' + str(detail.get('batch_id', '')))
    lines.append('')
    lines.append('Reasoning:')
    lines.append(str(detail.get('reasoning', 'No reasoning available')))

    alert('\n'.join(lines), title='MinMax Detail - ' + sku, large=True)