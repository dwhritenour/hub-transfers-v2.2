"""
ABCRowTemplate - ABC Classification Row Template
=================================================
Client-side code for individual rows in the ABC Classification DataGrid.

Applies conditional formatting based on:
  - Status (Pending/Pending_Review/Approved/Rejected/Applied)
  - Change type (PROMOTION/DEMOTION/UPDATE) with color coding
  - Confidence level (High/Medium/Low)

CHANGE 2026-05-11 (Lock terminal-state rows):
  * Rejected rows now disable dd_decision the same way Applied rows do.
    Rejected is terminal — Apply to Production does not transition
    Rejected -> Applied — so the dropdown should not be re-editable after
    the manager has chosen to reject the recommendation. Approved is left
    editable so the manager can still revert before Apply runs. Mirrors
    the same v61 fix in MinMaxRowTemplate.

Place this file in: client_code/abc_classification/ABCRowTemplate/__init__.py
"""

from ._anvil_designer import ABCRowTemplateTemplate
from anvil import *
import anvil.server


class ABCRowTemplate(ABCRowTemplateTemplate):
  def __init__(self, **properties):
    # Set Form properties and Data Bindings
    self.init_components(**properties)

    # -----------------------------------------------------------------
    # Status-based SKU highlighting
    # Uses color + bold for colorblind accessibility
    # -----------------------------------------------------------------
    status = self.item.get('status')

    if status == 'Pending':
      self.txt_sku.bold = True
    elif status == 'Pending_Review':
      self.txt_sku.foreground = '#FF9800'
      self.txt_sku.bold = True
    elif status == 'Approved':
      self.txt_sku.foreground = '#2196F3'
      self.txt_sku.bold = True
    elif status == 'Rejected':
      self.txt_sku.foreground = '#888888'
      self.txt_sku.italic = True
    elif status == 'Applied':
      self.txt_sku.foreground = '#4CAF50'
      self.txt_sku.bold = True

    # -----------------------------------------------------------------
    # Change type color coding
    # -----------------------------------------------------------------
    change_type = self.item.get('change_type', '')
    if change_type == 'DEMOTION':
      self.txt_change_type.foreground = '#FF9800'  # Orange
      self.txt_change_type.bold = True
    elif change_type == 'PROMOTION':
      self.txt_change_type.foreground = '#4CAF50'  # Green
      self.txt_change_type.bold = True
    elif change_type == 'UPDATE':
      self.txt_change_type.foreground = '#888888'  # Gray

    # -----------------------------------------------------------------
    # Confidence level styling
    # -----------------------------------------------------------------
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

    # -----------------------------------------------------------------
    # Status text color-coding
    # -----------------------------------------------------------------
    if status == 'Pending':
      self.txt_status.foreground = '#FF9800'
      self.txt_status.bold = True
    elif status == 'Pending_Review':
      self.txt_status.foreground = '#7B2FBE'  # Purple for colorblind accessibility
      self.txt_status.bold = True
    elif status == 'Approved':
      self.txt_status.foreground = '#2196F3'
    elif status == 'Rejected':
      self.txt_status.foreground = '#888888'
    elif status == 'Applied':
      self.txt_status.foreground = '#4CAF50'

    # -----------------------------------------------------------------
    # Decision dropdown initialization + terminal-state lockdown
    #
    # Terminal states (Rejected, Applied) lock dd_decision so the row
    # cannot be accidentally re-decided. Approved remains editable
    # because the manager may still want to revert before Apply runs.
    # -----------------------------------------------------------------
    if status == 'Approved':
      self.dd_decision.selected_value = 'Approve'
    elif status == 'Rejected':
      self.dd_decision.selected_value = 'Reject'
      # Rejected is terminal - lock the dropdown (was the bug: left
      # enabled, letting Rejected rows be re-edited).
      self.dd_decision.enabled = False
    elif status == 'Applied':
      # Applied items cannot be changed
      self.dd_decision.enabled = False
    else:
      # Pending/Pending_Review - no selection yet
      self.dd_decision.selected_value = None

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

    if selected == 'Approve':
      self.item['decision'] = 'Approved'
    elif selected == 'Reject':
      self.item['decision'] = 'Rejected'
    else:
      self.item['decision'] = None

  @handle("link_details", "click")
  def link_details_click(self, **event_args):
    """
    Show full details for this recommendation in a popup.
    Calls the server for enriched detail.
    """
    rec_id = self.item.get('recommendation_id')
    sku = self.item.get('sku', 'Unknown')

    result = anvil.server.call('get_classification_recommendation_detail', rec_id)

    if result['status'] == 'error':
      alert('Error loading details: ' + result.get('message', 'Unknown error'))
      return

    detail = result['detail']

    # Build detail text
    lines = []
    lines.append('SKU: ' + str(detail.get('sku', '')))
    lines.append('Change Type: ' + str(detail.get('change_type', '')))
    lines.append('')
    lines.append('--- Current vs New Classification ---')
    lines.append('Current Classification: ' + str(detail.get('current_classification', '')) + '  |  New Classification: ' + str(detail.get('new_classification', '')))
    lines.append('Current Total Sold: ' + str(detail.get('current_total_sold', '')) + '  |  New Total Sold: ' + str(detail.get('new_total_sold', '')))
    lines.append('Current Running Pct: ' + str(detail.get('current_running_pct', '')) + '%  |  New Running Pct: ' + str(detail.get('new_running_pct', '')) + '%')
    lines.append('Current Is Stocked: ' + ('Yes' if detail.get('current_is_stocked') else 'No') + '  |  New Is Stocked: ' + ('Yes' if detail.get('new_is_stocked') else 'No'))
    lines.append('')
    lines.append('--- Classification History ---')
    lines.append('Months as A Item: ' + str(detail.get('months_as_a', '')))
    lines.append('Classification Changes (12mo): ' + str(detail.get('classification_changes_12mo', '')))
    lines.append('')
    lines.append('--- Demand Pattern ---')
    lines.append('Demand Pattern: ' + str(detail.get('demand_pattern', '')))
    lines.append('Avg Daily Usage: ' + str(detail.get('avg_daily_usage', '')))
    lines.append('Coefficient of Variation: ' + str(detail.get('coefficient_of_variation', '')))
    lines.append('')
    lines.append('--- AI Analysis ---')
    lines.append('Confidence: ' + str(detail.get('confidence_level', '')))
    lines.append('')
    lines.append('Reasoning:')
    lines.append(str(detail.get('reasoning', 'No reasoning available')))

    alert('\n'.join(lines), title='Classification Detail - ' + sku, large=True)

