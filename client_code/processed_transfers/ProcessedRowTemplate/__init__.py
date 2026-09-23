from ._anvil_designer import ProcessedRowTemplateTemplate
from anvil import *
import anvil.server


class ProcessedRowTemplate(ProcessedRowTemplateTemplate):
  """One processed-SKU row. All fields set in code. The Note is a compact,
  clickable Link (lk_note) that opens the full text in a popup, so long
  audit-trail notes no longer stretch the row height."""
  def __init__(self, **properties):
    self.init_components(**properties)
    it = self.item or {}

    def s(v):
      return '' if v is None else str(v)

    self.txt_sku.text = s(it.get('sku'))
    self.txt_status.text = s(it.get('status'))
    self.txt_original_qty.text = s(it.get('original_qty'))
    self.txt_batch.text = s(it.get('batch_id'))
    self.txt_trip.text = s(it.get('trip_number'))
    self.txt_status_date.text = s(it.get('status_change_date'))[:10]

    av = it.get('altered_qty')
    self.txt_altered_qty.text = '—' if av is None else str(av)

    # v5.24: Reprint Slip link -- regenerates the batch's Picking Slip
    # without changing any statuses. Wired defensively so the row loads
    # before lk_reprint is added in the designer. Only shown for rows
    # whose batch actually went to the floor.
    if hasattr(self, 'lk_reprint'):
      printable = it.get('status') in ('Applied', 'Trip_Assigned',
                                       'Finalized', 'Delivered')
      if it.get('batch_id') and printable:
        self.lk_reprint.text = 'Reprint Slip'
        self.lk_reprint.visible = True
        self.lk_reprint.set_event_handler('click', self.lk_reprint_click)
      else:
        self.lk_reprint.visible = False

    # Note -> single-line preview link that opens the full note in a popup.
    self._note_full = s(it.get('note')).strip()
    if self._note_full:
      self.lk_note.text = 'Click for Notes'
      self.lk_note.visible = True
      self.lk_note.set_event_handler('click', self.lk_note_click)
    else:
      self.lk_note.text = ''
      self.lk_note.visible = False

  def lk_note_click(self, **event_args):
    alert(self._note_full,
          title='Note  —  ' + str((self.item or {}).get('sku', '')),
          large=True)

  def lk_reprint_click(self, **event_args):
    """v5.24: regenerate this batch's Picking Slip (read-only reprint)."""
    original = self.lk_reprint.text
    self.lk_reprint.text = 'Printing…'
    try:
      result = anvil.server.call('reprint_pick_list_for_batch',
                                 (self.item or {}).get('batch_id'))
      if result.get('status') == 'success':
        download(result['pdf_media'])
      else:
        alert('Could not reprint pick list:\n\n'
              + str(result.get('message', 'unknown error')))
    except Exception as e:
      alert('Reprint failed:\n\n' + str(e))
    finally:
      self.lk_reprint.text = original

