# =============================================================================
# app_versions.py
# -----------------------------------------------------------------------------
# Version:       5.8.2
# Last updated:  2026-04-29
# Changelog (this module only):
#   5.8.0 (2026-04-23) NEW — Layer 2 aggregator for the versioning scheme.
#                      Reads __version__ / __version_date__ / __version_notes__
#                      from every known server module and returns a single
#                      list the system_admin form can render. Handles
#                      missing modules and missing version attributes
#                      without raising.
#   5.8.1 (2026-04-29) Added 'minmax_ai_review' to _KNOWN_MODULES. That
#                      module was brought into the versioning scheme at
#                      5.12.0 alongside the is_stocked_at_starlite
#                      column-drop migration.
#   5.8.2 (2026-04-29) Added 'classification_dashboard' to _KNOWN_MODULES.
#                      Brought into the versioning scheme at 5.12.0 with
#                      the addition of get_a_items_export().
# =============================================================================

__version__       = "5.8.2"
__version_date__  = "2026-04-29"
__version_notes__ = "Registry update: added classification_dashboard"

import anvil.server


# Server modules to probe. If a module isn't deployed (or is renamed), we
# surface it as {version: 'not deployed'} rather than crashing the grid.
# Add new modules here when they're created.
_KNOWN_MODULES = [
  'transfer_truck_optimizer',
  'transfer_ai_review',
  'pick_list_pdf_generator',
  'sms_notifications',
  'sms_diagnostic',
  'system_settings',
  'driver_delivery',
  'minmax_ai_review',
  'classification_dashboard',
  'pick_dashboard',
  'app_versions',  # self-reference is fine — it shows this module's version
]


def _probe(mod_name):
  """Try to import mod_name and read its version constants.

    Returns a dict — always. If the module is missing or doesn't expose
    version constants, the returned dict has version='not deployed' or
    'unversioned' so the UI shows something useful rather than breaking.
    """
  try:
    mod = __import__(mod_name)
  except ImportError:
    return {
      'module':  mod_name,
      'version': 'not deployed',
      'date':    '',
      'notes':   'Module not found in server code',
    }
  except Exception as e:  # noqa: BLE001
    return {
      'module':  mod_name,
      'version': 'error',
      'date':    '',
      'notes':   f'Import failed: {type(e).__name__}: {e}',
    }

  version = getattr(mod, '__version__', None)
  if version is None:
    # Module is deployed but predates the versioning scheme. Per Dave's
    # 2026-04-23 decision, we only version files we touch from v5.8
    # forward, so "unversioned" is expected for older modules until
    # they're updated.
    return {
      'module':  mod_name,
      'version': 'unversioned',
      'date':    '',
      'notes':   'Predates versioning scheme (v5.8+ only)',
    }

  return {
    'module':  mod_name,
    'version': str(version),
    'date':    str(getattr(mod, '__version_date__', '')),
    'notes':   str(getattr(mod, '__version_notes__', '')),
  }


@anvil.server.callable
def get_server_versions():
  """Return a list of {module, version, date, notes} for every known
    server module. Ordered as listed in _KNOWN_MODULES so the UI is stable
    across calls.

    Client usage:
        rows = anvil.server.call('get_server_versions')
        dg_versions.items = rows
    """
  return [_probe(name) for name in _KNOWN_MODULES]
