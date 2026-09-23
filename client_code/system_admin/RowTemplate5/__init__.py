# =============================================================================
# system_admin/RowTemplate5/__init__.py
# -----------------------------------------------------------------------------
# Version:       5.8.0
# Last updated:  2026-04-23
# Changelog (this module only):
#   5.8.0 (2026-04-23) Populated RowTemplate5 to render one row of the
#                      Server Module Versions grid. Labels pull from
#                      self.item keys: module, version, date, notes.
#                      Highlights:
#                        - 'not deployed'  -> red
#                        - 'unversioned'   -> muted gray
#                        - 'error'         -> red + bold
#                      so operators can see status at a glance.
# =============================================================================
__version__       = "5.8.0"
__version_date__  = "2026-04-23"
__version_notes__ = "Populated RowTemplate5 for Server Module Versions grid"

from ._anvil_designer import RowTemplate5Template
from anvil import *


class RowTemplate5(RowTemplate5Template):
  def __init__(self, **properties):
    self.init_components(**properties)

    if not self.item:
      return

    # Straight text fill.
    self.lbl_module.text  = str(self.item.get('module', '') or '')
    self.lbl_version.text = str(self.item.get('version', '') or '')
    self.lbl_date.text    = str(self.item.get('date', '') or '')
    self.lbl_notes.text   = str(self.item.get('notes', '') or '')

    # Colorize problem states so they jump out.
    version = (self.item.get('version') or '').lower()
    if version == 'not deployed':
      self.lbl_version.foreground = 'theme:On Error Container'
      self.lbl_version.bold = True
    elif version == 'error':
      self.lbl_version.foreground = 'theme:On Error Container'
      self.lbl_version.bold = True
    elif version == 'unversioned':
      # Muted — expected for pre-v5.8 modules until they're touched.
      self.lbl_version.foreground = 'theme:On Surface Variant'
