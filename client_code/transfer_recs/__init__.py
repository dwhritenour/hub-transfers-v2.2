from ._anvil_designer import transfer_recsTemplate
from anvil import *
import anvil.server

# 2026-09-07: SKU + status filters (Dave: "with this many SKUs ... the
# transfer_rec form needs two filters"). Designer components (no event
# bindings - wired here): tb_filter_sku (TextBox), dd_filter_status
# (DropDown), btn_filter_clear (Button), lbl_filter_count (Label).
# The full list stays in self._all_rows; the grid shows the filtered
# subset of the SAME dicts, so a decision made on a row keeps its value
# when the row is filtered out and Apply still sends it. Reject All only
# rejects the rows currently visible when a filter is active.
#
# 2026-09-23 (v2.2 SNOOZE + ORDER HOLD): snoozed (Paused) recs no longer
# appear here - they live on the Snoozed SKUs form (#snoozed), so the
# 'Paused' filter is gone and 'Pending' is implied. New filter 'Order Hold'
# = rows whose demand is partly on Hold-status sales orders (the red ORDER
# HOLD line). Apply sends Snooze decisions (amount / until / reason from the
# row's dialog) and reports what shipped and what was parked.
#
# 2026-10-25 (PAUSE alongside SNOOZE): one-click paused recs (status='Paused',
# pause_kind='pause') stay on this form below Pending rows. Status filter
# restores 'Pending' and 'Paused'; optional dd_filter_urgency filters by
# urgency rank; Apply handles 'Pause' and 'Resume' decisions; stats line
# displays Paused and Snoozed counts separately.
_STATUS_FILTERS = ['All', 'Pending', 'Paused', 'Warned', 'Order Hold']
_URGENCY_FILTERS = ['All urgencies', 'Critical + High', 'Medium', 'Low']
_URG_RANK = {'CRITICAL': 4, 'HIGH': 3, 'MEDIUM': 2, 'WARNING': 2, 'ELEVATED': 2, 'LOW': 1, 'NORMAL': 1}


class transfer_recs(transfer_recsTemplate):
  def __init__(self, **properties):
    # Set Form properties and Data Bindings.
    self.init_components(**properties)
    self.nav.active = 'transfer_recs'

    # v2.0/v2.1 kiosk + dock routes: stations bookmark the app URL with
    # a hash and land on their screen instead of the manager UI.
    #   #pick    -> warehouse picker kiosk (Phase 5)
    #   #deliver -> driver delivery screen (go-live request, 2026-08-02)
    #   #dock    -> Trip Editor v3, the dock's tool (2026-08-30)
    #   #exceptions -> Hub Manager exception view
    _hash = get_url_hash()
    if _hash == 'pick':
      open_form('pick_worker')
      return
    if _hash == 'deliver':
      open_form('driver_deliver')
      return
    if _hash == 'dock':
      open_form('trip_editor')
      return

    # Initialize task tracking
    self.task_id = None
    self._all_rows = []

    # Filters (designer components; wired here, never in the designer)
    self.dd_filter_status.items = _STATUS_FILTERS
    self.dd_filter_status.selected_value = 'All'
    self.tb_filter_sku.set_event_handler('change', self.filter_changed)
    self.tb_filter_sku.set_event_handler('pressed_enter', self.filter_changed)
    self.dd_filter_status.set_event_handler('change', self.filter_changed)
    if hasattr(self, 'dd_filter_urgency'):
      self.dd_filter_urgency.items = _URGENCY_FILTERS
      self.dd_filter_urgency.selected_value = 'All urgencies'
      self.dd_filter_urgency.set_event_handler('change', self.filter_changed)
    self.btn_filter_clear.set_event_handler('click', self.filter_clear_click)

    # Wire up event handlers
    self.btn_refresh.set_event_handler('click', self.btn_refresh_click)
    self.btn_run_review.set_event_handler('click', self.btn_run_review_click)
    self.btn_apply.set_event_handler('click', self.btn_apply_click)
    self.btn_reject_all.set_event_handler('click', self.btn_reject_all_click)
    self.timer_check_task.set_event_handler('tick', self.timer_check_task_tick)

    # Load initial data
    self.load_transfers()
    self.refresh_review_stats()

  # =========================================================================
  # DATA LOADING
  # =========================================================================

  def load_transfers(self):
    """Fetch pending transfers from the server and populate the grid."""
    result = anvil.server.call('get_pending_transfers')
    if result['status'] == 'success':
      self._all_rows = list(result['rows'])
      self.apply_filters()
    else:
      alert("Error loading transfers: " + result['message'])

  # =========================================================================
  # FILTERS
  # =========================================================================

  def _urg_rank(self, r):
    best = 0
    for k in ('time_urgency', 'inventory_urgency'):
      best = max(best, _URG_RANK.get(str(r.get(k) or '').strip().upper(), 0))
    return best

  def _filter_active(self):
    return bool((self.tb_filter_sku.text or '').strip()) or \
    (self.dd_filter_status.selected_value or 'All') != 'All' or \
    (hasattr(self, 'dd_filter_urgency') and (self.dd_filter_urgency.selected_value or 'All urgencies') != 'All urgencies')

  def apply_filters(self):
    """Show the subset of self._all_rows matching the SKU text and status."""
    needle = (self.tb_filter_sku.text or '').strip().upper()
    status = self.dd_filter_status.selected_value or 'All'
    rows = []
    for r in self._all_rows:
      if needle and needle not in str(r.get('sku') or '').upper():
        continue
      if status == 'Warned' and not r.get('has_duplicate_warning'):
        continue
      if status == 'Order Hold' and not r.get('order_hold_qty'):
        continue
      if status == 'Pending' and r.get('status') != 'Pending':
        continue
      if status == 'Paused' and r.get('status') != 'Paused':
        continue
      urg = (self.dd_filter_urgency.selected_value if hasattr(self, 'dd_filter_urgency') else None) or 'All urgencies'
      rank = self._urg_rank(r)
      if urg == 'Critical + High' and rank < 3:
        continue
      if urg == 'Medium' and rank != 2:
        continue
      if urg == 'Low' and rank > 1:
        continue
      rows.append(r)
    self.rp_transfers.items = rows
    total = len(self._all_rows)
    if self._filter_active():
      self.lbl_filter_count.text = "Showing %d of %d" % (len(rows), total)
    else:
      paused = sum(1 for r in self._all_rows if r.get('status') == 'Paused')
      self.lbl_filter_count.text = "%d open (%d paused)" % (total, paused) if paused else "%d open" % total

  def filter_changed(self, **event_args):
    self.apply_filters()

  def filter_clear_click(self, **event_args):
    self.tb_filter_sku.text = ''
    self.dd_filter_status.selected_value = 'All'
    if hasattr(self, 'dd_filter_urgency'):
      self.dd_filter_urgency.selected_value = 'All urgencies'
    self.apply_filters()


  @handle("btn_refresh", "click")
  def btn_refresh_click(self, **event_args):
    """Refresh button - reload pending transfers and stats."""
    self.load_transfers()
    self.refresh_review_stats()
    Notification("Refreshed").show()

    # =========================================================================
    # REVIEW STATUS DISPLAY
    # =========================================================================

  def refresh_review_stats(self):
    """Fetch and display the last review run statistics."""
    try:
      stats = anvil.server.call('get_last_review_stats')

      if stats['status'] == 'success':
        # Format the timestamp nicely
        last_run = stats.get('last_run', '')
        if last_run:
          try:
            from datetime import datetime
            dt = datetime.fromisoformat(last_run.replace('Z', '+00:00'))
            last_run_display = dt.strftime('%b %d, %I:%M %p')
          except:
            last_run_display = last_run[:16]
        else:
          last_run_display = 'Never'

        # Include last run summary if available
        last_summary = stats.get('last_run_summary', '')
        if last_summary:
          self.lbl_last_run.text = "Last run: " + last_run_display + " (" + last_summary + ")"
        else:
          self.lbl_last_run.text = "Last run: " + last_run_display

        # Training mode: All items go to Pending for review
        self.lbl_stats.text = (
          f"Pending: {stats.get('pending', 0)} | "
          f"Paused: {stats.get('paused_hold', 0)} | "
          f"Snoozed: {stats.get('paused', 0)} | "
          f"Approved: {stats.get('approved', 0)} | "
          f"Applied: {stats.get('applied', 0)}"
        )
      elif stats['status'] == 'no_data':
        self.lbl_last_run.text = "Last run: Never"
        self.lbl_stats.text = "No reviews yet"
      else:
        self.lbl_last_run.text = "Last run: --"
        self.lbl_stats.text = "Error loading stats"
    except Exception as e:
      self.lbl_last_run.text = "Last run: --"
      self.lbl_stats.text = f"Error: {e}"

    # =========================================================================
    # MANUAL REVIEW TRIGGER
    # =========================================================================


  @handle("btn_run_review", "click")
  def btn_run_review_click(self, **event_args):
    """Manually trigger the AI review process."""
    if not confirm("Run transfer review now?\n\nThis will check for new transfer candidates and process them with AI."):
      return

      # Disable button while running
    self.btn_run_review.enabled = False
    self.btn_run_review.text = "Starting..."

    try:
      result = anvil.server.call('trigger_transfer_review')

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
      alert(f"Error: {e}")
      self.reset_review_button()


  @handle("timer_check_task", "tick")
  def timer_check_task_tick(self, **event_args):
    """Called every 2 seconds while a review task is running."""
    if not self.task_id:
      self.timer_check_task.interval = 0
      return

    try:
      status = anvil.server.call('get_review_task_status', self.task_id)

      if status.get('error'):
        self.timer_check_task.interval = 0
        self.task_complete("Error: " + status['error'])
        return

      state = status.get('state', {})

      if status.get('is_running', False):
        # Still running - update status display
        task_status = state.get('status', 'Running')
        progress = state.get('progress', '')
        current_sku = state.get('current_sku', '')

        if current_sku:
          self.lbl_task_status.text = task_status + ": " + current_sku + " (" + progress + ")"
        else:
          self.lbl_task_status.text = task_status + "..."
      else:
        # Task completed
        self.timer_check_task.interval = 0

        results = state.get('results', {})
        ai = results.get('ai_reviewed', 0)
        skipped = results.get('skipped', 0)
        errors = results.get('errors', 0)

        summary = f"AI Reviewed: {ai}"
        if skipped > 0:
          summary = f"{summary}, Skipped: {skipped}"
        if errors > 0:
          summary = f"{summary}, Errors: {errors}"

        self.task_complete(summary)

    except Exception as e:
      self.timer_check_task.interval = 0
      self.task_complete(f"Error: {e}")

  def task_complete(self, message):
    """Called when the background task finishes."""
    self.task_id = None
    self.reset_review_button()

    # Show completion message
    self.lbl_task_status.text = "Done: " + message

    # Refresh data
    self.refresh_review_stats()
    self.load_transfers()

    # Show notification
    Notification("Review complete: " + message).show()

  def reset_review_button(self):
    """Reset the review button to its default state."""
    self.btn_run_review.enabled = True
    self.btn_run_review.text = "Run Review Now"

    # =========================================================================
    # APPLY DECISIONS
    # =========================================================================

  @handle("btn_apply", "click")
  def btn_apply_click(self, **event_args):
    """
        Apply Decisions button:
        1. Collect all rows where a decision was made (Approve/Reject/Paused)
        2. Send to server for bulk update
        3. If any Approved, generate a Pick List PDF for download
           (temporary replacement for the CSV export while the e21
            tie-in is still pending)
        4. Refresh the grid
        """
    # Collect decisions from ALL rows - including ones a filter is hiding.
    # (The grid shows a subset of the same dicts, so a decision made before
    # the filter changed is still on the row.)
    updates = []
    hidden_decisions = 0
    visible_ids = set(r['recommendation_id'] for r in self.rp_transfers.items)

    for item in self._all_rows:
      decision = item.get('decision')
      if decision in ('Approved', 'Rejected', 'Paused', 'Pause', 'Resume') and item['recommendation_id'] not in visible_ids:
        hidden_decisions += 1
      if decision in ('Approved', 'Rejected', 'Paused', 'Pause', 'Resume'):
        update = {
          'recommendation_id': item['recommendation_id'],
          'decision': decision
        }
        # Include quantity override if provided
        qty_override = item.get('transfer_qty_override')
        if qty_override is not None and decision not in ('Paused', 'Pause', 'Resume'):
          update['transfer_qty'] = qty_override
        # v2.2: Snooze = decision 'Paused' + what the row's dialog collected.
        if decision == 'Paused':
          update['snooze_qty'] = item.get('snooze_qty')
          update['snooze_total'] = item.get('snooze_total')
          update['snooze_until'] = item.get('snooze_until')
          update['pause_reason'] = item.get('pause_reason')
        updates.append(update)

    if not updates:
      alert("No decisions to apply. Please select Approve, Reject, Snooze, Pause or Resume for at least one item.")
      return

      # Confirm with user
    approved_count = sum(1 for u in updates if u['decision'] == 'Approved')
    rejected_count = sum(1 for u in updates if u['decision'] == 'Rejected')
    paused_count = sum(1 for u in updates if u['decision'] == 'Paused')
    pause_count  = sum(1 for u in updates if u['decision'] == 'Pause')
    resume_count = sum(1 for u in updates if u['decision'] == 'Resume')

    confirm_msg = f"Apply {approved_count} approvals, {rejected_count} rejections"
    if paused_count > 0:
      confirm_msg += f", and {paused_count} snooze(s)"
    if pause_count > 0:
      confirm_msg += f", {pause_count} pause(s)"
    if resume_count > 0:
      confirm_msg += f", {resume_count} resume(s)"
    confirm_msg += "?"
    if hidden_decisions > 0:
      confirm_msg += (f"\n\n{hidden_decisions} of these decision(s) are on rows the "
                      f"current filter is hiding - they will be applied too.")

    if not confirm(confirm_msg):
      return

      # Send bulk update to server
    result = anvil.server.call('bulk_update_decisions', updates)

    if result['status'] == 'error':
      alert(f"Error: {result['message']}")
      return

      # Show result
    Notification(f"Updated {result['updated_count']} recommendations").show()

    # v2.0.1: zero-qty approvals are blocked server-side (they would be
    # invisible to the Pick Editor). Surface them loudly instead of the
    # old silent "success".
    if result.get('blocked_zero_qty'):
      alert(result.get('message') or
            ("%d rec(s) not approved - the AI recommended 0; "
             "set a transfer qty first."
             % len(result['blocked_zero_qty'])))

    # v2.2: say what each snooze did (whole rec parked, or split: part
    # approved now + the rest parked on the Snoozed SKUs form).
    _snoozed = result.get('snoozed') or []
    _paused = result.get('paused') or []
    _resumed = result.get('resumed') or []
    _split_ship = sum(int(x.get('ship_qty') or 0) for x in _snoozed if x.get('mode') == 'split')
    if _snoozed or _paused or _resumed:
      lines = []
      for x in _snoozed:
        if x.get('mode') == 'split':
          lines.append("%s: %d approved now, %d snoozed until %s"
                       % (x.get('sku'), int(x.get('ship_qty') or 0),
                          int(x.get('snooze_qty') or 0), x.get('until')))
        else:
          lines.append("%s: %d snoozed until %s"
                       % (x.get('sku'), int(x.get('snooze_qty') or 0), x.get('until')))
      for x in _paused:
        lines.append("%s paused" % x.get('sku'))
      for x in _resumed:
        lines.append("%s resumed" % x.get('sku'))
      Notification(("Snoozed - see the Snoozed SKUs form:\n" if _snoozed else "") + "\n".join(lines),
                   timeout=8).show()
    if result.get('snooze_errors'):
      alert(result.get('message') or 'Some snoozes were refused.')

    # v2.0 (B3): approvals stay 'Approved' and appear in the Pick
    # Editor's pool - pick creation is what "applies" them now, and
    # slips print per pick. The old apply_transfers batch step is gone.
    if approved_count > 0 or _split_ship > 0:
      Notification(
        f"{approved_count + sum(1 for x in _snoozed if x.get('mode') == 'split')} "
        f"approval(s) sent to the Pick Editor pool."
      ).show()

      # Refresh the grid to show remaining pending items
    self.load_transfers()
    self.refresh_review_stats()

  # v2.0 (B3): generate_pick_list() / generate_csv() retired. The
  # apply_transfers batch step no longer exists in this app's flow -
  # approvals feed the Pick Editor pool and slips print per pick
  # (download_pick_slip_for_pick).

    # =========================================================================
    # REJECT ALL
    # =========================================================================

  def btn_reject_all_click(self, **event_args):
    """Reject All button - mark the Pending rows on screen as Rejected.

    With a filter active only the VISIBLE Pending rows are rejected (their
    ids are sent); with no filter every Pending rec is, as before. Paused
    rows are never touched by Reject All (server rule).
    """
    visible = [r for r in self.rp_transfers.items if r.get('status') == 'Pending']
    pending_count = len(visible)

    if pending_count == 0:
      alert("No pending transfers to reject" +
            (" in the current filter." if self._filter_active() else "."))
      return

    if self._filter_active():
      msg = (f"Reject the {pending_count} Pending transfer(s) currently shown "
             f"(filter: SKU '{(self.tb_filter_sku.text or '').strip()}', "
             f"status {self.dd_filter_status.selected_value or 'All'})?")
      ids = [r['recommendation_id'] for r in visible]
    else:
      msg = f"Reject all {pending_count} pending transfers?"
      ids = None

    if not confirm(msg):
      return

    result = anvil.server.call('reject_pending', ids)

    if result['status'] == 'success':
      Notification(f"Rejected {result['rejected_count']} transfers").show()
      self.load_transfers()
      self.refresh_review_stats()
    else:
      alert(f"Error: {result['message']}")

  @handle("btn_transfer_form", "click")
  def btn_transfer_form_click(self, **event_args):
    """This method is called when the button is clicked"""
    # TODO: Implement transfer form functionality or remove button from UI
    Notification("Transfer form not yet implemented").show()

  @handle("timer_check_task", "show")
  def timer_check_task_show(self, **event_args):
    """This method is called when this timer's form is shown on the screen (or it is added to a visible form)"""
    pass

  @handle("timer_check_task", "hide")
  def timer_check_task_hide(self, **event_args):
    """This method is called when this timer's form is hidden from the screen (or it is removed from a visible form)"""
    pass