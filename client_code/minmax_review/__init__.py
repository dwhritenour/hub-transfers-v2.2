"""
minmax_review - MinMax Review Form (v61 - 2026-05-11)
======================================================
Client-side code for the Titan Flow Control MinMax Review form.

CHANGES IN v61:
  * NEW: Submit Override workflow in the SKU Browser panel. Manager can
    enter override Min/Max + reason for ANY SKU and click "Submit
    Override". Backend stages a 'Modified' row (creating a new row if
    needed, or updating an existing Pending/Modified row in place).
    Staged row appears in the main grid; Apply to Production commits it.
  * dd_sku_results_change pre-populates tb_override_min / tb_override_max
    with the SKU's current values so the manager only has to edit what
    changed.
  * tb_override_reason is cleared on each SKU selection.

(Carries forward all v60 logic: Modified counter card, Modified filter
option, validation on Apply Decisions.)

Place this file in: client_code/minmax_review/__init__.py
"""

from ._anvil_designer import minmax_reviewTemplate
from anvil import *
import anvil.server
import anvil.media


class minmax_review(minmax_reviewTemplate):
  def __init__(self, **properties):
    self.init_components(**properties)
    self.nav.active = 'minmax_review'

    self.task_id = None

    # *** NEW v61 *** SKU Browser state
    # _sku_loaded   : True once a SKU has been selected at least once (so we
    #                 know whether the detail labels carry meaningful values).
    # _sku_collapsed: True when the manager has clicked Hide on the SKU
    #                 Browser to collapse it.
    self._sku_loaded = False
    self._sku_collapsed = False

    # Header
    self.btn_refresh.set_event_handler('click', self.btn_refresh_click)
    self.btn_run_review.set_event_handler('click', self.btn_run_review_click)
    self.btn_export.set_event_handler('click', self.btn_export_click)
    self.timer_check_task.set_event_handler('tick', self.timer_check_task_tick)

    # Filters
    self.dd_status_filter.set_event_handler('change', self.dd_status_filter_change)
    self.tb_search.set_event_handler('pressed_enter', self.tb_search_pressed_enter)
    self.btn_clear_filters.set_event_handler('click', self.btn_clear_filters_click)

    # Footer actions
    self.btn_approve_all.set_event_handler('click', self.btn_approve_all_click)
    self.btn_reject_all.set_event_handler('click', self.btn_reject_all_click)
    self.btn_apply_decisions.set_event_handler('click', self.btn_apply_decisions_click)
    self.btn_apply_production.set_event_handler('click', self.btn_apply_production_click)

    # SKU Browser
    self.tb_sku_search.set_event_handler('pressed_enter', self.tb_sku_search_pressed_enter)
    self.dd_sku_results.set_event_handler('change', self.dd_sku_results_change)

    # *** NEW v61 *** Submit Override button
    self.btn_submit_override.set_event_handler('click', self.btn_submit_override_click)

    # *** NEW v61 *** Hide / Show toggle for the SKU Browser panel.
    # Guarded with hasattr so the form still loads if the button hasn't
    # been added to the Designer yet.
    if hasattr(self, 'btn_toggle_sku_browser'):
      self.btn_toggle_sku_browser.set_event_handler(
        'click', self.btn_toggle_sku_browser_click
      )

    # Initial data
    self.load_recommendations()

  # =========================================================================
  # DATA LOADING
  # =========================================================================

  def load_recommendations(self):
    status_filter = self.dd_status_filter.selected_value
    if status_filter == 'All' or status_filter is None:
      status_filter = None

    search_text = self.tb_search.text or ''
    if not search_text.strip():
      search_text = None

    result = anvil.server.call(
      'get_minmax_recommendations',
      status_filter=status_filter,
      search_text=search_text
    )

    if result['status'] == 'success':
      self.rp_recommendations.items = result['recommendations']

      count = len(result['recommendations'])
      self.lbl_record_count.text = str(count) + ' recommendations'

      counts = result.get('counts', {})
      self.lbl_pending_count.text  = 'Pending: '  + str(counts.get('Pending', 0))
      self.lbl_modified_count.text = 'Modified: ' + str(counts.get('Modified', 0))
      self.lbl_approved_count.text = 'Approved: ' + str(counts.get('Approved', 0))
      self.lbl_rejected_count.text = 'Rejected: ' + str(counts.get('Rejected', 0))
      self.lbl_applied_count.text  = 'Applied: '  + str(counts.get('Applied', 0))

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
    try:
      result = anvil.server.call('get_minmax_review_stats')

      if result['status'] == 'success':
        self.lbl_pending_count.text  = 'Pending: '  + str(result.get('pending', 0))
        self.lbl_modified_count.text = 'Modified: ' + str(result.get('modified', 0))
        self.lbl_approved_count.text = 'Approved: ' + str(result.get('approved', 0))
        self.lbl_rejected_count.text = 'Rejected: ' + str(result.get('rejected', 0))
        self.lbl_applied_count.text  = 'Applied: '  + str(result.get('applied', 0))

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
    except Exception:
      self.lbl_last_review.text = 'Last Review: --'

  # =========================================================================
  # FILTER / SEARCH
  # =========================================================================

  @handle("dd_status_filter", "change")
  def dd_status_filter_change(self, **event_args):
    self.load_recommendations()

  @handle("tb_search", "pressed_enter")
  def tb_search_pressed_enter(self, **event_args):
    self.load_recommendations()

  @handle("btn_clear_filters", "click")
  def btn_clear_filters_click(self, **event_args):
    items = self.dd_status_filter.items or []
    if items:
      first = items[0]
      # items can be strings or (display, value) tuples
      self.dd_status_filter.selected_value = first[1] if isinstance(first, tuple) else first
    self.tb_search.text = ''
    self.load_recommendations()

  @handle("btn_refresh", "click")
  def btn_refresh_click(self, **event_args):
    self.load_recommendations()
    Notification("Refreshed").show()

  # =========================================================================
  # EXPORT
  # =========================================================================

  def btn_export_click(self, **event_args):
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

    if isinstance(result, dict) and result.get('status') == 'error':
      alert(result.get('error', 'Unknown error'), title='Export Error')
      return

    try:
      anvil.media.download(result)
    except Exception as e:
      alert(f'Download failed: {e}', title='Export')

  # =========================================================================
  # AI REVIEW TRIGGER
  # =========================================================================

  @handle("btn_run_review", "click")
  def btn_run_review_click(self, **event_args):
    if not confirm("Run MinMax AI review now?\n\nThis will analyze divergent SKUs and generate new recommendations."):
      return

    self.btn_run_review.enabled = False
    self.btn_run_review.text = "Starting..."

    try:
      result = anvil.server.call('trigger_minmax_review')

      if result['status'] == 'launched':
        self.task_id = result['task_id']
        self.lbl_task_status.text = "Running..."
        self.lbl_task_status.visible = True
        self.btn_run_review.text = "Running..."
        self.timer_check_task.interval = 2
      else:
        alert("Error starting review: " + result.get('message', 'Unknown error'))
        self.reset_review_button()

    except Exception as e:
      alert("Error: " + str(e))
      self.reset_review_button()

  @handle("timer_check_task", "tick")
  def timer_check_task_tick(self, **event_args):
    if not self.task_id:
      self.timer_check_task.interval = 0
      return

    try:
      status = anvil.server.call('get_minmax_review_task_status', self.task_id)

      if status.get('is_running', False):
        current_sku = status.get('current_sku', '')
        progress = status.get('progress', 0)
        total = status.get('total_candidates', 0)

        if current_sku:
          self.lbl_task_status.text = "Reviewing: " + current_sku + " (" + str(progress) + "/" + str(total) + ")"
        else:
          self.lbl_task_status.text = str(status.get('status', 'Running')) + "..."
      else:
        self.timer_check_task.interval = 0
        summary = status.get('summary', '')
        if not summary:
          summary = 'Review finished'
        self.task_complete(summary)

    except Exception as e:
      self.timer_check_task.interval = 0
      self.task_complete("Error: " + str(e))

  def task_complete(self, message):
    self.task_id = None
    self.reset_review_button()
    self.lbl_task_status.text = "Done: " + message
    self.load_recommendations()
    Notification("Review complete: " + message).show()

  def reset_review_button(self):
    self.btn_run_review.enabled = True
    self.btn_run_review.text = "Run AI Review"

  # =========================================================================
  # BULK APPROVE / REJECT
  # =========================================================================

  @handle("btn_approve_all", "click")
  def btn_approve_all_click(self, **event_args):
    pending = [
      item for item in self.rp_recommendations.items
      if item.get('status') == 'Pending'
    ]
    if not pending:
      alert("No pending recommendations to approve.")
      return
    if not confirm("Approve all " + str(len(pending)) + " pending recommendations?"):
      return

    updates = [
      {'recommendation_id': item['recommendation_id'], 'decision': 'Approved'}
      for item in pending
    ]
    result = anvil.server.call('bulk_update_minmax_decisions', updates)
    if result['status'] == 'success':
      Notification("Approved " + str(result['updated_count']) + " recommendations").show()
      self.load_recommendations()
    else:
      alert("Error: " + result.get('message', 'Unknown error'))

  @handle("btn_reject_all", "click")
  def btn_reject_all_click(self, **event_args):
    pending = [
      item for item in self.rp_recommendations.items
      if item.get('status') == 'Pending'
    ]
    if not pending:
      alert("No pending recommendations to reject.")
      return
    if not confirm("Reject all " + str(len(pending)) + " pending recommendations?"):
      return

    updates = [
      {'recommendation_id': item['recommendation_id'], 'decision': 'Rejected'}
      for item in pending
    ]
    result = anvil.server.call('bulk_update_minmax_decisions', updates)
    if result['status'] == 'success':
      Notification("Rejected " + str(result['updated_count']) + " recommendations").show()
      self.load_recommendations()
    else:
      alert("Error: " + result.get('message', 'Unknown error'))

  # =========================================================================
  # APPLY DECISIONS
  # =========================================================================

  @handle("btn_apply_decisions", "click")
  def btn_apply_decisions_click(self, **event_args):
    updates = []
    validation_errors = []

    for item in self.rp_recommendations.items:
      decision = item.get('decision')

      if decision == 'Approved' or decision == 'Rejected':
        updates.append({
          'recommendation_id': item['recommendation_id'],
          'decision': decision
        })

      elif decision == 'Modified':
        sku = item.get('sku', '?')
        omin = item.get('override_min')
        omax = item.get('override_max')
        reason = item.get('override_reason')

        row_problems = []
        try:
          omin_int = int(omin) if omin is not None else None
          omax_int = int(omax) if omax is not None else None
        except (ValueError, TypeError):
          row_problems.append("override_min/max must be numbers")
          omin_int = omax_int = None

        if omin_int is None or omax_int is None:
          row_problems.append("missing override Min or Max")
        else:
          if omin_int < 0 or omax_int < 0:
            row_problems.append("Min/Max cannot be negative")
          if omax_int < omin_int:
            row_problems.append("Max must be >= Min")

        if not reason or not str(reason).strip():
          row_problems.append("override reason is required")

        if row_problems:
          validation_errors.append(sku + ': ' + '; '.join(row_problems))
          continue

        updates.append({
          'recommendation_id': item['recommendation_id'],
          'decision': 'Modified',
          'override_min': omin_int,
          'override_max': omax_int,
          'override_reason': str(reason).strip(),
        })

    if validation_errors:
      alert(
        "Please fix these row(s) before applying:\n\n" + '\n'.join(validation_errors),
        title="Validation Errors"
      )
      return

    if not updates:
      alert("No decisions to apply. Please select Approve, Reject, or Modify for at least one item.")
      return

    approved_count = sum(1 for u in updates if u['decision'] == 'Approved')
    rejected_count = sum(1 for u in updates if u['decision'] == 'Rejected')
    modified_count = sum(1 for u in updates if u['decision'] == 'Modified')

    confirm_lines = []
    if approved_count > 0: confirm_lines.append("Approve " + str(approved_count))
    if rejected_count > 0: confirm_lines.append("Reject " + str(rejected_count))
    if modified_count > 0: confirm_lines.append("Modify " + str(modified_count))

    if not confirm("Apply: " + ', '.join(confirm_lines) + "?"):
      return

    result = anvil.server.call('bulk_update_minmax_decisions', updates)

    if result['status'] == 'success':
      Notification("Updated " + str(result['updated_count']) + " recommendations").show()
      self.load_recommendations()
    else:
      errors_info = result.get('errors')
      if errors_info:
        lines = [str(e.get('recommendation_id')) + ': ' + str(e.get('error')) for e in errors_info]
        alert("Server validation failed:\n\n" + '\n'.join(lines))
      else:
        alert("Error: " + result.get('message', 'Unknown error'))

  # =========================================================================
  # APPLY TO PRODUCTION
  # =========================================================================

  @handle("btn_apply_production", "click")
  def btn_apply_production_click(self, **event_args):
    if not confirm(
      "Apply all approved and modified MinMax changes to production?\n\n"
      "This will:\n"
      "- Update min_max_calculated with new min/max values\n"
      "  (Approved rows use AI recommendation; Modified rows use manager override)\n"
      "- Run usp_Update_MinMax_Calculated to re-baseline\n"
      "- Log all changes to ai_agent_log (override reasons preserved)"
    ):
      return

    try:
      result = anvil.server.call('apply_approved_minmax')

      if result['status'] == 'success':
        applied = result.get('applied_count', 0)
        if applied > 0:
          Notification("Applied " + str(applied) + " MinMax changes to production").show()
        else:
          Notification("No approved or modified recommendations to apply").show()
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
    search_text = self.tb_sku_search.text or ''
    if len(search_text.strip()) < 2:
      self.dd_sku_results.visible = False
      return

    result = anvil.server.call('search_minmax_skus', search_text.strip())

    if result['status'] == 'success' and result['skus']:
      self.dd_sku_results.items = [
        (
          sku['sku'] + ' (Min:' + str(int(sku.get('min_stock') or 0)) + ' Max:' + str(int(sku.get('max_stock') or 0)) + ')',
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
    """Load full detail for the selected SKU and seed the override inputs."""
    sku = self.dd_sku_results.selected_value
    if not sku:
      return

    result = anvil.server.call('get_minmax_sku_detail', sku)

    if result['status'] == 'error':
      alert("Error: " + result.get('message', 'Unknown error'))
      return

    sku_data = result.get('sku_data', {})
    baseline = result.get('baseline', {})
    inventory = result.get('inventory', {})
    history = result.get('history', [])

    # --- Production Values ---
    self.lbl_sku_prod_header.visible = True
    self.lbl_sku_min.text = 'Min: ' + str(int(sku_data.get('min_stock') or 0))
    self.lbl_sku_min.visible = True
    self.lbl_sku_max.text = 'Max: ' + str(int(sku_data.get('max_stock') or 0))
    self.lbl_sku_max.visible = True
    self.lbl_sku_pattern.text = 'Pattern: ' + str(sku_data.get('demand_pattern', '--'))
    self.lbl_sku_pattern.visible = True
    self.lbl_sku_avg_daily.text = 'Avg Daily Usage: ' + str(sku_data.get('avg_daily_usage', '--'))
    self.lbl_sku_avg_daily.visible = True

    ai_managed = sku_data.get('ai_managed')
    if ai_managed:
      self.lbl_sku_ai_managed.text = 'AI Managed: Yes (updated ' + str(sku_data.get('ai_updated_date_short', '--')) + ')'
    else:
      self.lbl_sku_ai_managed.text = 'AI Managed: No'
    self.lbl_sku_ai_managed.visible = True

    # --- Baseline Comparison ---
    self.lbl_sku_baseline_header.visible = True
    if baseline:
      self.lbl_sku_base_min.text = 'Baseline Min: ' + str(int(baseline.get('min_stock') or 0))
      self.lbl_sku_base_max.text = 'Baseline Max: ' + str(int(baseline.get('max_stock') or 0))
      min_dir = baseline.get('min_direction', '')
      max_dir = baseline.get('max_direction', '')
      min_pct = baseline.get('min_pct_change', 0)
      max_pct = baseline.get('max_pct_change', 0)
      self.lbl_sku_divergence.text = 'Divergence: Min ' + str(min_pct) + '% ' + min_dir + '  |  Max ' + str(max_pct) + '% ' + max_dir
    else:
      self.lbl_sku_base_min.text = 'Baseline Min: N/A'
      self.lbl_sku_base_max.text = 'Baseline Max: N/A'
      self.lbl_sku_divergence.text = 'Divergence: N/A'

    self.lbl_sku_base_min.visible = True
    self.lbl_sku_base_max.visible = True
    self.lbl_sku_divergence.visible = True

    # --- Inventory ---
    self.lbl_sku_inv_header.visible = True
    self.lbl_sku_starlite_oh.text = 'Starlite OH: ' + str(int(inventory.get('starlite_on_hand', 0))) + '  (Available: ' + str(int(inventory.get('starlite_available', 0))) + ')'
    self.lbl_sku_starlite_oh.visible = True
    self.lbl_sku_hub_oh.text = 'Hub OH: ' + str(int(inventory.get('hub_on_hand', 0)))
    self.lbl_sku_hub_oh.visible = True
    self.lbl_sku_days_supply.text = 'Days of Supply: ' + str(inventory.get('starlite_days_supply', 0))
    self.lbl_sku_days_supply.visible = True

    # --- Recommendation History ---
    self.lbl_sku_history_header.visible = True
    if history:
      history_lines = []
      for h in history:
        line = (
          str(h.get('recommendation_date_display', '')) + '  |  '
          + 'Min: ' + str(h.get('current_min', '')) + ' → ' + str(h.get('recommended_min', '')) + '  |  '
          + 'Max: ' + str(h.get('current_max', '')) + ' → ' + str(h.get('recommended_max', '')) + '  |  '
          + str(h.get('status', '')) + '  |  '
          + str(h.get('confidence_level', ''))
        )
        if h.get('override_min') is not None or h.get('override_max') is not None:
          line += (
            '  |  OVERRIDE Min: ' + str(h.get('override_min', '')) + ' Max: ' + str(h.get('override_max', ''))
            + ' (' + str(h.get('override_by', '')) + ': "' + str(h.get('override_reason', '')) + '")'
          )
        history_lines.append(line)
      self.rt_sku_history.content = '\n'.join(history_lines)
    else:
      self.rt_sku_history.content = 'No recommendation history for this SKU.'
    self.rt_sku_history.visible = True

    # --- *** NEW v61 *** Seed Manual Override inputs -----------------
    cur_min = int(sku_data.get('min_stock') or 0)
    cur_max = int(sku_data.get('max_stock') or 0)
    self.tb_override_min.text = str(cur_min)
    self.tb_override_max.text = str(cur_max)
    self.tb_override_reason.text = ''

    # Surface a note showing what the SKU currently is, so the manager has
    # quick context while typing in the override values.
    if hasattr(self, 'lbl_override_status'):
      self.lbl_override_status.text = (
        'Current production values for ' + sku
        + ' shown above. Edit Min/Max and add a reason to stage an override.'
      )

    # Mark that we now have a SKU loaded, then re-apply panel visibility
    # so the detail labels + override controls all show together. (If the
    # panel is currently collapsed, this still respects that — the
    # components stay hidden until the manager clicks Show.)
    self._sku_loaded = True
    self._apply_sku_browser_visibility()

  # =========================================================================
  # *** NEW v61 *** SKU BROWSER HIDE / SHOW
  # =========================================================================

  def _apply_sku_browser_visibility(self):
    """
    Single source of truth for which SKU Browser components are visible.

    Rules:
      * When the panel is collapsed, EVERYTHING below the title bar is
        hidden — search input, dropdown, detail labels, override
        controls.
      * When the panel is expanded:
          - tb_sku_search is always visible
          - dd_sku_results is visible if it has results
          - All detail labels + override controls are visible if a SKU
            has been loaded (self._sku_loaded == True), otherwise hidden
    """
    expanded = not self._sku_collapsed

    # Search input + dropdown
    self.tb_sku_search.visible = expanded
    has_results = bool(self.dd_sku_results.items)
    self.dd_sku_results.visible = expanded and has_results

    # Detail labels — only meaningful once a SKU has been loaded
    detail_show = expanded and self._sku_loaded

    detail_widgets = [
      'lbl_sku_prod_header',
      'lbl_sku_baseline_header',
      'lbl_sku_inv_header',
      'lbl_sku_history_header',
      'lbl_sku_min',
      'lbl_sku_max',
      'lbl_sku_pattern',
      'lbl_sku_avg_daily',
      'lbl_sku_ai_managed',
      'lbl_sku_base_min',
      'lbl_sku_base_max',
      'lbl_sku_divergence',
      'lbl_sku_starlite_oh',
      'lbl_sku_hub_oh',
      'lbl_sku_days_supply',
      'rt_sku_history',
      # Override controls (also visible-once-SKU-loaded)
      'lbl_override_header',
      'tb_override_min',
      'tb_override_max',
      'tb_override_reason',
      'btn_submit_override',
      'lbl_override_status',
    ]
    for name in detail_widgets:
      if hasattr(self, name):
        getattr(self, name).visible = detail_show

    # Toggle button label
    if hasattr(self, 'btn_toggle_sku_browser'):
      if self._sku_collapsed:
        self.btn_toggle_sku_browser.text = 'Show'
        self.btn_toggle_sku_browser.icon = 'fa:eye'
      else:
        self.btn_toggle_sku_browser.text = 'Hide'
        self.btn_toggle_sku_browser.icon = 'fa:eye-slash'

  @handle("btn_toggle_sku_browser", "click")
  def btn_toggle_sku_browser_click(self, **event_args):
    """Toggle the SKU Browser between collapsed and expanded states."""
    self._sku_collapsed = not self._sku_collapsed
    self._apply_sku_browser_visibility()

  # =========================================================================
  # *** NEW v61 *** SUBMIT MANUAL OVERRIDE
  # =========================================================================

  @handle("btn_submit_override", "click")
  def btn_submit_override_click(self, **event_args):
    """
    Stage a manual override for the SKU currently displayed in the SKU
    Browser. Validates inputs client-side first, then calls the server
    function which writes a 'Modified' row to ai_minmax_recommendations
    (updating any existing Pending/Modified row in place, or inserting a
    new row if none exists).

    After a successful submit, the override inputs are cleared and the
    main recommendations grid is reloaded so the manager can see the new
    Modified row and click Apply to Production when ready.
    """
    sku = self.dd_sku_results.selected_value
    if not sku:
      alert("Please search for and select a SKU in the SKU Browser before submitting an override.")
      return

    # Validate Min / Max
    try:
      omin = int((self.tb_override_min.text or '').strip())
      omax = int((self.tb_override_max.text or '').strip())
    except (ValueError, TypeError):
      alert("Override Min and Max must be whole numbers.")
      return

    if omin < 0 or omax < 0:
      alert("Override Min and Max cannot be negative.")
      return
    if omax < omin:
      alert("Override Max must be greater than or equal to Override Min.")
      return

    # Validate Reason
    reason = (self.tb_override_reason.text or '').strip()
    if not reason:
      alert("A reason is required for a manual override (packaging, storage, etc.).")
      return

    # Confirm
    if not confirm(
      "Stage manual override for " + sku + "?\n\n"
      "Min: " + str(omin) + "\n"
      "Max: " + str(omax) + "\n"
      "Reason: " + reason + "\n\n"
      "This will create or update a 'Modified' recommendation. Use\n"
      "'Apply to Production' to commit it to min_max_calculated."
    ):
      return

    # Disable button while submitting
    original = self.btn_submit_override.text
    self.btn_submit_override.enabled = False
    self.btn_submit_override.text = 'Submitting...'

    try:
      result = anvil.server.call(
        'submit_manual_minmax_override',
        sku, omin, omax, reason
      )
    except Exception as e:
      alert('Submit failed: ' + str(e))
      return
    finally:
      self.btn_submit_override.enabled = True
      self.btn_submit_override.text = original

    if result['status'] == 'success':
      Notification(result.get('message', 'Override staged')).show()
      # Clear inputs so the next selection starts clean
      self.tb_override_min.text = ''
      self.tb_override_max.text = ''
      self.tb_override_reason.text = ''
      if hasattr(self, 'lbl_override_status'):
        self.lbl_override_status.text = (
          'Override ' + str(result.get('action', 'staged')) + ' for ' + sku
          + '. Now visible in the main grid above.'
        )
      # Refresh the grid so the new Modified row appears
      self.load_recommendations()
      # Also refresh the SKU detail panel so history shows the new entry
      self.dd_sku_results_change()
    else:
      alert("Error: " + result.get('message', 'Unknown error'))