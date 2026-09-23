from ._anvil_designer import NavSidebarTemplate
from anvil import *
import anvil.server
import anvil.js


class NavSidebar(NavSidebarTemplate):
  # Maps a page identifier to the nav button that should be highlighted.
  _ACTIVE_BUTTONS = {
    'transfer_recs':          'btn_transfer_form',
    'pick_editor':            'btn_pick_editor',
    'trip_editor':            'btn_trip_form',
    'driver_deliver':         'btn_deliver',
    'exceptions':             'btn_exceptions',
    'snoozed':                'btn_snoozed',        # v2.2: Snoozed SKUs form
    'processed_transfers':    'btn_processed',
    'zero_stock':             'btn_zerostock',
    'abc_classification':     'btn_abcclass',
    'minmax_review':          'btn_minmax',
    'system_admin':           'btn_sys_admin_form',
  }

  # One handler per button -> one open_form target. Defined ONLY here.
  _NAV_TARGETS = {
    'btn_transfer_form':  'transfer_recs',
    'btn_pick_editor':    'pick_editor',
    'btn_trip_form':      'trip_editor',
    'btn_deliver':        'driver_deliver',
    'btn_exceptions':     'exceptions',
    'btn_snoozed':        'snoozed',              # v2.2 (button added in the designer)
    'btn_processed':      'processed_transfers',
    'btn_zerostock':      'zero_stock',
    'btn_abcclass':       'abc_classification',
    'btn_minmax':         'minmax_review',
    'btn_sys_admin_form': 'system_admin',
  }

  # External apps -> open in a NEW TAB. open_form only navigates within
  # THIS app; another developer's published app is its own Anvil session,
  # so the browser handles it and our user keeps their place here.
  _EXTERNAL_TARGETS = {
    'btn_titan_ai': 'https://titan-chat.anvil.app/',
    'btn_change_log': 'https://titan-change-log.anvil.app/',
  }

  def __init__(self, active=None, **properties):
    self.init_components(**properties)
    self._active = active

    # Wire every nav button to its open_form target in one loop.
    for btn_name, target in self._NAV_TARGETS.items():
      btn = getattr(self, btn_name, None)
      if btn is not None:
        btn.set_event_handler('click', self._make_nav_handler(target))

    # Wire external-app buttons to open in a new browser tab.
    # (This loop was MISSING — _EXTERNAL_TARGETS was defined but never
    # iterated, so btn_titan_ai did nothing. Fixed 2026-08-06.)
    for btn_name, url in self._EXTERNAL_TARGETS.items():
      btn = getattr(self, btn_name, None)
      if btn is not None:
        btn.set_event_handler('click', self._make_external_handler(url))

    self._highlight_active()

  def _make_nav_handler(self, target_form):
    def handler(**event_args):
      # Routed screens (#deliver, #dock, ...) leave their hash in the URL;
      # transfer_recs' startup routing would bounce straight back to it.
      set_url_hash('')
      open_form(target_form)
    return handler

  def _make_external_handler(self, url):
    def handler(**event_args):
      # Runs synchronously inside the click gesture, so popup
      # blockers allow the new tab.
      anvil.js.window.open(url, '_blank')
    return handler

  def _highlight_active(self):
    btn_name = self._ACTIVE_BUTTONS.get(self._active)
    if not btn_name:
      return
    btn = getattr(self, btn_name, None)
    if btn is None:
      return
    # You are already on this page: highlight it and stop it navigating.
    btn.bold = True
    btn.background = '#1565C0'
    btn.foreground = '#FFFFFF'
    btn.enabled = False

  @property
  def active(self):
    return self._active

  @active.setter
  def active(self, value):
    self._active = value
    self._highlight_active()
