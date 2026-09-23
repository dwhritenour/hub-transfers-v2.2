# =============================================================================
# system_admin/__init__.py
# -----------------------------------------------------------------------------
# Version:       5.8.0
# Last updated:  2026-04-23
# Changelog (this module only):
#   5.8.0 (2026-04-23) Added Server Module Versions card:
#                        - load_versions() calls get_server_versions()
#                          on the server (Layer 2 aggregator in
#                          app_versions.py).
#                        - btn_refresh_versions click handler re-pulls
#                          the list so ops can confirm a deploy took
#                          effect without reloading the form.
#                        - load_versions() runs automatically at form
#                          init, after global parameters, inside its
#                          own try so a missing app_versions module
#                          doesn't break the settings page.
#   5.7.x and earlier     (unversioned — predates versioning scheme)
# =============================================================================
__version__       = "5.8.0"
__version_date__  = "2026-04-23"
__version_notes__ = "Added Server Module Versions grid and refresh handler"

from ._anvil_designer import system_adminTemplate
from anvil import *
import anvil.server


class system_admin(system_adminTemplate):

  # Mapping of NUMERIC textboxes to rows in dbo.global_parameters.
  PARAM_MAP = {
    'txt_a_item_threshold': 'a_item_threshold_pct',
    'txt_lead_time': 'lead_time_days',
    'txt_cycle_review': 'review_cycle_days',
    'txt_safety_days': 'safety_days',
    'txt_service_level': 'service_level_pct',
    'txt_transfer_horizon': 'transfer_planning_horizon_days',
    'txt_truck_cap_ft3': 'truck_cap_ft3',
    'txt_truck_cap_vol': 'truck_cap_lb',
  }

  # Mapping of TEXT textboxes to rows in dbo.global_parameters_text.
  # Kept separate from PARAM_MAP because text params go through a
  # different server-side helper and different validation (non-empty,
  # 500-char ceiling) rather than float() coercion.
  TEXT_PARAM_MAP = {
    'txt_dashboard_url': 'dashboard_url',
  }

  def __init__(self, **properties):
    self.init_components(**properties)
    self.nav.active = 'system_admin'

    # Flag to prevent triggering updates during initial load
    self._loading = True

    # Load current parameter values
    self.load_parameters()

    self._loading = False

    # v5.8 — pull the Server Module Versions grid after global params
    # are settled. Wrapped so a missing app_versions module on the
    # server (e.g. not yet deployed) doesn't break the whole form.
    try:
      self.load_versions()
    except Exception as e:
      print(f"[system_admin] initial load_versions failed: {e}")

    
  # ===== Parameter loading =====

  def load_parameters(self):
    """Load all global parameters (numeric + text) from database and populate textboxes."""
    # --- numeric params ---
    try:
      params = anvil.server.call('get_global_parameters')

      for textbox_name, param_name in self.PARAM_MAP.items():
        textbox = getattr(self, textbox_name, None)
        if textbox and param_name in params:
          # Format to 2 decimal places, removing unnecessary trailing zeros
          value = params[param_name]['value']
          textbox.text = f"{value:.2f}".rstrip('0').rstrip('.')

    except Exception as e:
      alert(f"Error loading parameters: {e}")

    # --- text params ---
    # Wrapped in its own try so a missing global_parameters_text table
    # (e.g. migration not yet applied) doesn't blank the numeric fields.
    try:
      text_params = anvil.server.call('get_global_parameters_text')

      for textbox_name, param_name in self.TEXT_PARAM_MAP.items():
        textbox = getattr(self, textbox_name, None)
        if textbox and param_name in text_params:
          textbox.text = text_params[param_name]['value']

    except Exception as e:
      # Non-fatal: text params are secondary; log and move on.
      print(f"[system_admin] load text parameters failed: {e}")

  def save_parameter(self, textbox_name):
    """Save a single NUMERIC parameter when its textbox value changes."""
    if self._loading:
      return

    param_name = self.PARAM_MAP.get(textbox_name)
    if not param_name:
      return

    textbox = getattr(self, textbox_name, None)
    if not textbox:
      return

    new_value = textbox.text

    # Don't save if empty
    if not new_value or new_value.strip() == '':
      return

    try:
      result = anvil.server.call('update_global_parameter', param_name, new_value)

      if result['success']:
        # Brief visual feedback (optional)
        Notification(f"✓ {param_name} updated", style="success", timeout=2).show()
      else:
        alert(f"Error: {result['error']}")
        # Reload the original value
        self.load_parameters()

    except Exception as e:
      alert(f"Error saving parameter: {e}")

  def save_text_parameter(self, textbox_name):
    """Save a single TEXT parameter (global_parameters_text) when its textbox value changes."""
    if self._loading:
      return

    param_name = self.TEXT_PARAM_MAP.get(textbox_name)
    if not param_name:
      return

    textbox = getattr(self, textbox_name, None)
    if not textbox:
      return

    new_value = textbox.text

    # Don't save if empty — server will reject anyway, but avoid the round-trip.
    if not new_value or new_value.strip() == '':
      return

    try:
      result = anvil.server.call('update_global_parameter_text', param_name, new_value)

      if result['success']:
        Notification(f"✓ {param_name} updated", style="success", timeout=2).show()
      else:
        alert(f"Error: {result['error']}")
        # Reload the original value from the server
        self.load_parameters()

    except Exception as e:
      alert(f"Error saving parameter: {e}")

  # ===== v5.8 — Server Module Versions =====

  def load_versions(self):
    """Pull the Layer 2 aggregator output and bind it to dg_versions.

    get_server_versions() returns a list of dicts:
        {module, version, date, notes}
    — one row per module listed in app_versions._KNOWN_MODULES. Missing
    or unversioned modules come back with sentinel 'not deployed' or
    'unversioned' strings so the grid always has something to show.
    """
    try:
      rows = anvil.server.call('get_server_versions')
    except Exception as e:
      # Leave the grid empty rather than crash the form. Surface the
      # error so the operator knows why the grid is blank.
      alert(f"Could not load server versions: {e}")
      self.rp_versions.items = []
      return

    self.rp_versions.items = rows or []

  # ===== Change Event Handlers =====

  @handle("txt_a_item_threshold", "lost_focus")
  def txt_a_item_threshold_lost_focus(self, **event_args):
    self.save_parameter('txt_a_item_threshold')

  @handle("txt_lead_time", "lost_focus")
  def txt_lead_time_lost_focus(self, **event_args):
    self.save_parameter('txt_lead_time')

  @handle("txt_cycle_review", "lost_focus")
  def txt_cycle_review_lost_focus(self, **event_args):
    self.save_parameter('txt_cycle_review')

  @handle("txt_safety_days", "lost_focus")
  def txt_safety_days_lost_focus(self, **event_args):
    self.save_parameter('txt_safety_days')

  @handle("txt_service_level", "lost_focus")
  def txt_service_level_lost_focus(self, **event_args):
    self.save_parameter('txt_service_level')

  @handle("txt_transfer_horizon", "lost_focus")
  def txt_transfer_horizon_lost_focus(self, **event_args):
    self.save_parameter('txt_transfer_horizon')

  @handle("txt_truck_cap_ft3", "lost_focus")
  def txt_truck_cap_ft3_lost_focus(self, **event_args):
    self.save_parameter('txt_truck_cap_ft3')

  @handle("txt_truck_cap_vol", "lost_focus")
  def txt_truck_cap_vol_lost_focus(self, **event_args):
    self.save_parameter('txt_truck_cap_vol')

  # ----- text params -----

  @handle("txt_dashboard_url", "lost_focus")
  def txt_dashboard_url_lost_focus(self, **event_args):
    self.save_text_parameter('txt_dashboard_url')

  # ===== Apply Button Handler =====

  @handle("btn_apply", "click")
  def btn_apply_click(self, **event_args):
    """Manual apply button - saves all parameters (numeric + text)."""
    for textbox_name in self.PARAM_MAP.keys():
      self.save_parameter(textbox_name)
    for textbox_name in self.TEXT_PARAM_MAP.keys():
      self.save_text_parameter(textbox_name)
    Notification("All parameters saved!", style="success").show()

  # ===== v5.8 — Versions refresh =====

  @handle("btn_refresh_versions", "click")
  def btn_refresh_versions_click(self, **event_args):
    """Re-pull the Server Module Versions grid on demand."""
    self.load_versions()
    Notification("Versions refreshed", style="success", timeout=2).show()