"""
abc_classification - ABC Classification Review Form
=====================================================
Client-side code for the Titan Flow Control ABC Classification Review form.

This form provides:
  - DataGrid of AI classification recommendations with filtering/search
  - Approve/Reject individual or bulk decisions
  - Apply approved changes to production (a_items_master)
  - Manual trigger for AI review with progress polling
  - SKU browser for ad-hoc classification visibility

Server modules: classification_ai_review.py, classification_dashboard.py

Place this file in: client_code/abc_classification/__init__.py
"""

from ._anvil_designer import abc_classificationTemplate
from anvil import *
import anvil.server
import anvil.media


class abc_classification(abc_classificationTemplate):
  def __init__(self, **properties):
    # Set Form properties and Data Bindings.
    self.init_components(**properties)
    self.nav.active = 'abc_classification'

    # Initialize task tracking for background AI review
    self.task_id = None

    # Wire up event handlers - Header
    self.btn_refresh.set_event_handler('click', self.btn_refresh_click)
    self.btn_run_review.set_event_handler('click', self.btn_run_review_click)
    self.btn_export.set_event_handler('click', self.btn_export_click)
    self.timer_check_task.set_event_handler('tick', self.timer_check_task_tick)

    # Wire up event handlers - Filters
    self.dd_status_filter.set_event_handler('change', self.dd_status_filter_change)
    self.tb_search.set_event_handler('pressed_enter', self.tb_search_pressed_enter)
    self.btn_clear_filters.set_event_handler('click', self.btn_clear_filters_click)

    # Wire up event handlers - Footer actions
    self.btn_approve_all.set_event_handler('click', self.btn_approve_all_click)
    self.btn_reject_all.set_event_handler('click', self.btn_reject_all_click)
    self.btn_apply_decisions.set_event_handler('click', self.btn_apply_decisions_click)
    self.btn_apply_production.set_event_handler('click', self.btn_apply_production_click)

    # Wire up event handlers - SKU Browser
    self.tb_sku_search.set_event_handler('pressed_enter', self.tb_sku_search_pressed_enter)
    self.dd_sku_results.set_event_handler('change', self.dd_sku_results_change)

    # Load initial data
    self.load_recommendations()

  # =========================================================================
  # DATA LOADING
  # =========================================================================

  def load_recommendations(self):
    """Fetch recommendations from server and populate the grid and summary cards."""
    # Get filter values
    status_filter = self.dd_status_filter.selected_value
    if status_filter == 'All' or status_filter is None:
      status_filter = None

    search_text = self.tb_search.text or ''
    if not search_text.strip():
      search_text = None

    # Call server
    result = anvil.server.call(
      'get_classification_recommendations',
      status_filter=status_filter,
      search_text=search_text
    )

    if result['status'] == 'success':
      self.rp_recommendations.items = result['recommendations']

      # Update record count
      count = len(result['recommendations'])
      self.lbl_record_count.text = str(count) + ' recommendations'

      # Update summary cards from counts
      counts = result.get('counts', {})
      self.lbl_pending_count.text = 'Pending: ' + str(counts.get('Pending', 0))
      self.lbl_approved_count.text = 'Approved: ' + str(counts.get('Approved', 0))
      self.lbl_rejected_count.text = 'Rejected: ' + str(counts.get('Rejected', 0))
      self.lbl_applied_count.text = 'Applied: ' + str(counts.get('Applied', 0))

      # Update last review date
      last_review = result.get('last_review_date')
      if last_review:
        try:
          from datetime import datetime
          dt = datetime.fromisoformat(last_review.replace('Z', '+00:00'))
          self.lbl_last_review.text = 'Last Review: ' + dt.strftime('%b %d, %I:%M %p')
        except Exception:
          self.lbl_last_review.text = 'Last Review: ' + str(last_review)[:16]
      else:
        self.lbl_last_review.text = 'Last Review: Never'
    else:
      alert('Error loading recommendations: ' + result.get('message', 'Unknown error'))

  def refresh_stats(self):
    """Lightweight stats refresh - updates cards without reloading the grid."""
    try:
      result = anvil.server.call('get_classification_review_stats')

      if result['status'] == 'success':
        self.lbl_pending_count.text = 'Pending: ' + str(result.get('pending', 0))
        self.lbl_approved_count.text = 'Approved: ' + str(result.get('approved', 0))
        self.lbl_rejected_count.text = 'Rejected: ' + str(result.get('rejected', 0))
        self.lbl_applied_count.text = 'Applied: ' + str(result.get('applied', 0))

        last_review = result.get('last_review_date')
        if last_review:
          try:
            from datetime import datetime
            dt = datetime.fromisoformat(last_review.replace('Z', '+00:00'))
            self.lbl_last_review.text = 'Last Review: ' + dt.strftime('%b %d, %I:%M %p')
          except Exception:
            self.lbl_last_review.text = 'Last Review: ' + str(last_review)[:16]
        else:
          self.lbl_last_review.text = 'Last Review: Never'
    except Exception as e:
      self.lbl_last_review.text = 'Last Review: --'

  # =========================================================================
  # FILTER / SEARCH HANDLERS
  # =========================================================================

  @handle("dd_status_filter", "change")
  def dd_status_filter_change(self, **event_args):
    """Filter changed - reload data."""
    self.load_recommendations()

  @handle("tb_search", "pressed_enter")
  def tb_search_pressed_enter(self, **event_args):
    """Search box enter pressed - reload data."""
    self.load_recommendations()

  @handle("btn_clear_filters", "click")
  def btn_clear_filters_click(self, **event_args):
    """Reset filters and reload."""
    self.dd_status_filter.selected_value = None
    self.tb_search.text = ''
    self.load_recommendations()

  @handle("btn_refresh", "click")
  def btn_refresh_click(self, **event_args):
    """Refresh button - reload recommendations and stats."""
    self.load_recommendations()
    Notification("Refreshed").show()

  # =========================================================================
  # EXPORT - A-items workbook (shared with minmax_review)
  # =========================================================================

  @handle("btn_export", "click")
  def btn_export_click(self, **event_args):
    """
    Generate the A-items Excel workbook (one sheet per series) and
    trigger a client-side download. Shared server function with the
    minmax_review form's Export button.
    """
    original_text = self.btn_export.text
    self.btn_export.enabled = False
    self.btn_export.text = 'Generating...'

    try:
      result = anvil.server.call('get_a_items_export')
    except Exception as e:
      alert(f'Export failed: {e}', title='Export Error')
      return
    finally:
      self.btn_export.enabled = True
      self.btn_export.text = original_text

    # Server returns either a BlobMedia (success) or {'status':'error',...}
    if isinstance(result, dict) and result.get('status') == 'error':
      alert(result.get('error', 'Unknown error'), title='Export Error')
      return

    try:
      anvil.media.download(result)
    except Exception as e:
      alert(f'Download failed: {e}', title='Export')

  # =========================================================================
  # MANUAL AI REVIEW TRIGGER
  # =========================================================================

  @handle("btn_run_review", "click")
  def btn_run_review_click(self, **event_args):
    """Manually trigger the Classification AI review process."""
    if not confirm("Run Classification AI review now?\n\nThis will auto-approve UPDATEs and AI-review PROMOTION/DEMOTION recommendations."):
      return

    # Disable button while running
    self.btn_run_review.enabled = False
    self.btn_run_review.text = "Starting..."

    try:
      result = anvil.server.call('trigger_classification_review')

      if result['status'] == 'launched':
        self.task_id = result['task_id']
        self.lbl_task_status.text = "Running..."
        self.lbl_task_status.visible = True
        self.btn_run_review.text = "Running..."

        # Start polling for status
        self.timer_check_task.interval = 2
      else:
        alert("Error starting review: " + result.get('message', 'Unknown error'))
        self.reset_review_button()

    except Exception as e:
      alert("Error: " + str(e))
      self.reset_review_button()

  @handle("timer_check_task", "tick")
  def timer_check_task_tick(self, **event_args):
    """Called every 2 seconds while an AI review task is running."""
    if not self.task_id:
      self.timer_check_task.interval = 0
      return

    try:
      status = anvil.server.call('get_classification_review_task_status', self.task_id)

      if status.get('is_running', False):
        # Still running - update status display
        current_sku = status.get('current_sku', '')
        change_type = status.get('change_type', '')
        progress = status.get('progress', 0)
        total = status.get('total_candidates', 0)

        if current_sku:
          self.lbl_task_status.text = "Reviewing: " + current_sku + " (" + change_type + ") (" + str(progress) + "/" + str(total) + ")"
        else:
          self.lbl_task_status.text = str(status.get('status', 'Running')) + "..."
      else:
        # Task completed
        self.timer_check_task.interval = 0

        summary = status.get('summary', '')
        if not summary:
          summary = 'Review finished'

        self.task_complete(summary)

    except Exception as e:
      self.timer_check_task.interval = 0
      self.task_complete("Error: " + str(e))

  def task_complete(self, message):
    """Called when the background task finishes."""
    self.task_id = None
    self.reset_review_button()

    # Show completion message
    self.lbl_task_status.text = "Done: " + message

    # Refresh data
    self.load_recommendations()

    # Show notification
    Notification("Review complete: " + message).show()

  def reset_review_button(self):
    """Reset the review button to its default state."""
    self.btn_run_review.enabled = True
    self.btn_run_review.text = "Run AI Review"

  # =========================================================================
  # BULK APPROVE / REJECT
  # =========================================================================

  @handle("btn_approve_all", "click")
  def btn_approve_all_click(self, **event_args):
    """Approve all Pending and Pending_Review recommendations."""
    # Count actionable items (both Pending and Pending_Review)
    pending = [
      item for item in self.rp_recommendations.items
      if item.get('status') in ('Pending', 'Pending_Review')
    ]

    if not pending:
      alert("No pending recommendations to approve.")
      return

    if not confirm("Approve all " + str(len(pending)) + " pending recommendations?"):
      return

    # Build updates list
    updates = [
      {'recommendation_id': item['recommendation_id'], 'decision': 'Approved'}
      for item in pending
    ]

    result = anvil.server.call('bulk_update_classification_decisions', updates)

    if result['status'] == 'success':
      Notification("Approved " + str(result['updated_count']) + " recommendations").show()
      self.load_recommendations()
    else:
      alert("Error: " + result.get('message', 'Unknown error'))

  @handle("btn_reject_all", "click")
  def btn_reject_all_click(self, **event_args):
    """Reject all Pending and Pending_Review recommendations."""
    # Count actionable items (both Pending and Pending_Review)
    pending = [
      item for item in self.rp_recommendations.items
      if item.get('status') in ('Pending', 'Pending_Review')
    ]

    if not pending:
      alert("No pending recommendations to reject.")
      return

    if not confirm("Reject all " + str(len(pending)) + " pending recommendations?"):
      return

    # Build updates list
    updates = [
      {'recommendation_id': item['recommendation_id'], 'decision': 'Rejected'}
      for item in pending
    ]

    result = anvil.server.call('bulk_update_classification_decisions', updates)

    if result['status'] == 'success':
      Notification("Rejected " + str(result['updated_count']) + " recommendations").show()
      self.load_recommendations()
    else:
      alert("Error: " + result.get('message', 'Unknown error'))

  # =========================================================================
  # APPLY DECISIONS (save individual row choices)
  # =========================================================================

  @handle("btn_apply_decisions", "click")
  def btn_apply_decisions_click(self, **event_args):
    """
    Collect individual row decisions and send to server.
    This saves Approve/Reject choices without applying to production.
    """
    # Collect decisions from all rows
    updates = []

    for item in self.rp_recommendations.items:
      decision = item.get('decision')
      if decision in ('Approved', 'Rejected'):
        updates.append({
          'recommendation_id': item['recommendation_id'],
          'decision': decision
        })

    if not updates:
      alert("No decisions to apply. Please select Approve or Reject for at least one item.")
      return

    # Count for confirmation
    approved_count = sum(1 for u in updates if u['decision'] == 'Approved')
    rejected_count = sum(1 for u in updates if u['decision'] == 'Rejected')

    confirm_msg = "Apply " + str(approved_count) + " approvals and " + str(rejected_count) + " rejections?"

    if not confirm(confirm_msg):
      return

    # Send bulk update to server
    result = anvil.server.call('bulk_update_classification_decisions', updates)

    if result['status'] == 'success':
      Notification("Updated " + str(result['updated_count']) + " recommendations").show()
      self.load_recommendations()
    else:
      alert("Error: " + result.get('message', 'Unknown error'))

  # =========================================================================
  # APPLY TO PRODUCTION
  # =========================================================================

  @handle("btn_apply_production", "click")
  def btn_apply_production_click(self, **event_args):
    """
    Apply all approved recommendations to production.
    This updates a_items_master and triggers the stored procedure.
    Separate from Apply Decisions on purpose - this is the production commit step.
    """
    if not confirm(
      "Apply all approved classification changes to production?\n\n"
      "This will:\n"
      "- Update a_items_master with new classifications\n"
      "- Update is_stocked flags\n"
      "- Log all changes to ai_agent_log"
    ):
      return

    try:
      result = anvil.server.call('apply_approved_classifications')

      if result['status'] == 'success':
        applied = result.get('applied_count', 0)
        if applied > 0:
          Notification("Applied " + str(applied) + " classification changes to production").show()
        else:
          Notification("No approved recommendations to apply").show()
        self.load_recommendations()
      else:
        alert("Error applying to production: " + result.get('message', 'Unknown error'))
    except Exception as e:
      alert("Apply to Production failed: " + str(e))

  # =========================================================================
  # SKU BROWSER
  # =========================================================================

  @handle("tb_sku_search", "pressed_enter")
  def tb_sku_search_pressed_enter(self, **event_args):
    """Search for SKUs and populate the results dropdown."""
    search_text = self.tb_sku_search.text or ''
    if len(search_text.strip()) < 2:
      self.dd_sku_results.visible = False
      return

    result = anvil.server.call('search_classification_skus', search_text.strip())

    if result['status'] == 'success' and result['skus']:
      # Build dropdown items as (display_text, value) tuples
      self.dd_sku_results.items = [
        (
          sku['sku'] + ' (' + sku.get('classification', '') + ', Sold: ' + str(int(sku.get('total_sold') or 0)) + ')',
          sku['sku']
        )
        for sku in result['skus']
      ]
      self.dd_sku_results.visible = True
      self.dd_sku_results.selected_value = None
    else:
      self.dd_sku_results.visible = False
      if result['status'] == 'error':
        Notification("Search error: " + result.get('message', '')).show()
      else:
        Notification("No SKUs found").show()

  @handle("dd_sku_results", "change")
  def dd_sku_results_change(self, **event_args):
    """Load full detail for the selected SKU."""
    sku = self.dd_sku_results.selected_value
    if not sku:
      return

    result = anvil.server.call('get_classification_sku_detail', sku)

    if result['status'] == 'error':
      alert("Error: " + result.get('message', 'Unknown error'))
      return

    sku_data = result.get('sku_data', {})
    demand = result.get('demand_data', {})
    history = result.get('history', [])

    # --- Classification Data ---
    self.lbl_sku_classification.visible = True
    self.lbl_sku_classification.text = 'Classification: ' + str(sku_data.get('classification', '--'))

    self.lbl_sku_total_sold.visible = True
    self.lbl_sku_total_sold.text = 'Total Sold: ' + str(int(sku_data.get('total_sold') or 0))

    self.lbl_sku_running_pct.visible = True
    self.lbl_sku_running_pct.text = 'Running Pct: ' + str(sku_data.get('running_pct', '--')) + '%'

    self.lbl_sku_is_stocked.visible = True
    is_stocked = sku_data.get('is_stocked')
    self.lbl_sku_is_stocked.text = 'Is Stocked: ' + ('Yes' if is_stocked else 'No')

    self.lbl_sku_months_a.visible = True
    self.lbl_sku_months_a.text = 'Months as A Item: ' + str(sku_data.get('months_as_a', '--'))

    # --- Demand Pattern Data ---
    self.lbl_sku_pattern.visible = True
    self.lbl_sku_pattern.text = 'Pattern: ' + str(demand.get('demand_pattern', '--'))

    self.lbl_sku_avg_daily.visible = True
    self.lbl_sku_avg_daily.text = 'Avg Daily Usage: ' + str(demand.get('avg_daily_usage', '--'))

    self.lbl_sku_cv.visible = True
    self.lbl_sku_cv.text = 'CV: ' + str(demand.get('coefficient_of_variation', '--'))

    # --- Recommendation History ---
    self.lbl_sku_history_header.visible = True
    if history:
      history_lines = []
      for h in history:
        line = (
          str(h.get('recommendation_date_display', '')) + '  |  '
          + str(h.get('change_type', '')) + '  |  '
          + 'Current: ' + str(h.get('current_classification', '')) + ' \u2192 New: ' + str(h.get('new_classification', '')) + '  |  '
          + str(h.get('status', '')) + '  |  '
          + str(h.get('confidence_level', ''))
        )
        history_lines.append(line)
      self.rt_sku_history.content = '\n'.join(history_lines)
    else:
      self.rt_sku_history.content = 'No recommendation history for this SKU.'
    self.rt_sku_history.visible = True  