"""
pipeline_monitor.py - Anvil Server Module (scheduled task, hourly)
==================================================================
Hub Transfers v2.1 - the automatic half of the #exceptions board.

Runs once an hour (Anvil scheduled task: run_pipeline_monitor). Reads
ONLY app-owned tables (recs, picks, trips), so it needs no EasyMorph
freshness gate - nothing here depends on an e21 snapshot.

What it does, per stage of the board (exceptions_dashboard v2):

  1  APPROVED, NOT PICKED  past pipeline_exception_days -> the rec is
     CANCELLED (exceptions_dashboard._cancel_unpicked_rec: only when no
     line has qty_picked > 0, no trip assignment, no In_Progress pick;
     untouched Assigned lines on Draft/Released picks go with it; an
     emptied pick is cancelled). Note on the rec, event on the line,
     ai_agent_log APPROVED_NOT_PICKED_CANCELLED. The detector
     re-recommends if Starlite still needs the SKU.

  2  PICKED, NOT LOADED  past the threshold -> ONE DIGEST text per run
     to the Hub Managers (PICK_EXCEPTIONS) listing every newly-stuck line;
     each line is escalated once, ever (dedupe: transfer_pick_events
     PIPELINE_ESCALATED per line). Nothing is changed - goods are on the
     dock; a human chooses SHIPPED-CLOSE or RETURN TO STOCK on the board.

  3  DEPARTED, NOT DELIVERED  past the threshold -> ONE text to the Hub
     Managers (LOAD_EXCEPTIONS) per trip, ever (dedupe: ai_agent_log
     PIPELINE_ESCALATED with the trip id). Nothing is changed; the board
     offers CONFIRM DELIVERED.

  4  e21 rows are read-only everywhere; the weekly monitor lists them.

Dave 2026-09-05: "terminate" paper, escalate goods; two days across
the board (global_parameters pipeline_exception_days).

Author: Claude (Dave Whritenour review)
Project: Hub Transfers v2.1
Created: 2026-09-05
"""

import anvil.server
from datetime import datetime

from . import db_config
from .exceptions_dashboard import _cancel_unpicked_rec, _param_int, _log_agent, _log_event

__version__       = "1.1.0"
__version_date__  = "2026-09-05"
__version_notes__ = (
  "1.1.0: stage-2 escalation is ONE digest text per run (was one text per "
  "line - 9 lines = 9 texts per manager); per-line PIPELINE_ESCALATED "
  "events kept for dedupe. Kill switch read on the monitor's own "
  "connection (sms_notifications.SMSConfig.is_sms_enabled(conn)). "
  "1.0.0: hourly pipeline monitor - auto-cancel of approved-never-picked "
  "recs past pipeline_exception_days; one-time escalation SMS for picked-"
  "not-loaded lines and departed-not-delivered trips past the threshold."
)

_DEFAULT_PIPELINE_DAYS = 2


@anvil.server.background_task
def run_pipeline_monitor():
  summary = {'cancelled': [], 'escalated_lines': 0, 'escalated_trips': 0,
             'sms_sent': 0, 'errors': []}
  conn = None
  try:
    conn = db_config.get_nexus_connection()
    days = _param_int(conn, 'pipeline_exception_days', _DEFAULT_PIPELINE_DAYS)
    summary['threshold_days'] = days
    cursor = conn.cursor()

    # ---------------------------------------------------------------- 1
    cursor.execute(
      """
        SELECT r.recommendation_id, r.sku
        FROM dbo.ai_transfer_recommendations r
        WHERE r.status IN ('Approved', 'Auto-Approved', 'Applied')
          AND r.reviewed_date IS NOT NULL
          AND DATEDIFF(day, r.reviewed_date, GETDATE()) >= %s
          AND NOT EXISTS (SELECT 1 FROM dbo.transfer_pick_lines x
                          WHERE x.recommendation_id = r.recommendation_id
                            AND x.qty_picked > 0)
          AND NOT EXISTS (SELECT 1 FROM dbo.transfer_pick_lines x
                          JOIN dbo.transfer_picks p ON p.pick_id = x.pick_id
                          WHERE x.recommendation_id = r.recommendation_id
                            AND x.status = 'Assigned' AND p.status = 'In_Progress')
          AND NOT EXISTS (SELECT 1 FROM dbo.transfer_trip_assignments t
                          WHERE t.recommendation_id = r.recommendation_id)
        ORDER BY r.reviewed_date
      """,
      (days,)
    )
    for rec in list(cursor.fetchall()):
      try:
        ok = _cancel_unpicked_rec(
          conn, rec['recommendation_id'], 'pipeline_monitor',
          'approved %s+ days ago and never picked (pipeline_exception_days = %s)' % (days, days))
        if ok:
          conn.commit()
          summary['cancelled'].append('%s (rec %s)' % (rec['sku'], rec['recommendation_id']))
        else:
          conn.rollback()
      except Exception as e:
        conn.rollback()
        summary['errors'].append('cancel rec %s: %s' % (rec['recommendation_id'], e))

    # ---------------------------------------------------------------- 2
    cursor.execute(
      """
        SELECT pl.pick_line_id, pl.pick_id, pl.sku,
               CAST(pl.qty_picked AS DECIMAL(18,4)) AS qty,
               DATEDIFF(day, pl.picked_date, GETDATE()) AS age_days
        FROM dbo.transfer_pick_lines pl
        JOIN dbo.transfer_picks p ON p.pick_id = pl.pick_id
        JOIN dbo.ai_transfer_recommendations r ON r.recommendation_id = pl.recommendation_id
        WHERE p.status = 'Completed' AND pl.qty_picked > 0
          AND pl.status IN ('Picked', 'Short')
          AND r.status IN ('Applied', 'Trip_Assigned')
          AND DATEDIFF(day, pl.picked_date, GETDATE()) >= %s
          AND NOT EXISTS (SELECT 1 FROM dbo.transfer_trip_assignments t
                          JOIN dbo.transfer_trips tr ON tr.trip_id = t.trip_id
                          WHERE t.pick_line_id = pl.pick_line_id AND tr.status <> 'Proposed')
          AND NOT EXISTS (SELECT 1 FROM dbo.transfer_pick_events ev
                          WHERE ev.pick_line_id = pl.pick_line_id
                            AND ev.event_type = 'PIPELINE_ESCALATED')
        ORDER BY pl.picked_date
      """,
      (days,)
    )
    stuck_lines = list(cursor.fetchall())

    # ---------------------------------------------------------------- 3
    cursor.execute(
      """
        SELECT t.trip_id, DATEDIFF(day, t.shipped_date, GETDATE()) AS age_days,
               t.finalized_by,
               (SELECT COUNT(*) FROM dbo.transfer_trip_assignments a WHERE a.trip_id = t.trip_id) AS lines
        FROM dbo.transfer_trips t
        WHERE t.status IN ('Finalized', 'Departed')
          AND t.shipped_date IS NOT NULL AND t.received_date IS NULL
          AND DATEDIFF(day, t.shipped_date, GETDATE()) >= %s
          AND EXISTS (SELECT 1 FROM dbo.transfer_trip_assignments a WHERE a.trip_id = t.trip_id)
          AND NOT EXISTS (SELECT 1 FROM dbo.ai_agent_log l
                          WHERE l.action_type = 'PIPELINE_ESCALATED'
                            AND CAST(l.old_value AS varchar(100)) = CONCAT('trip ', t.trip_id))
        ORDER BY t.shipped_date
      """,
      (days,)
    )
    stuck_trips = list(cursor.fetchall())

    if stuck_lines or stuck_trips:
      from . import sms_notifications as sn
      sms_on = False
      try:
        sms_on = sn.SMSConfig.is_sms_enabled(conn)
      except Exception:
        sms_on = False
      client = None
      if sms_on:
        try:
          client = sn.TwilioSMSClient()
        except Exception as e:
          summary['errors'].append('Twilio: %s' % e)

      if stuck_lines:
        # ONE digest per run. Twilio splits >160 chars into segments, so
        # list up to 12 lines; beyond that, say how many more.
        oldest = max(int(ln['age_days'] or 0) for ln in stuck_lines)
        items = ["%s x%g (P%s, %sd)" % (ln['sku'], float(ln['qty']), ln['pick_id'], ln['age_days'])
                 for ln in stuck_lines[:12]]
        more = len(stuck_lines) - len(items)
        msg = ("PICKED, NOT LOADED: %d line(s) on the dock with no truck for up to %d day(s): %s%s. "
               "Open #exceptions and choose SHIPPED-CLOSE or RETURN TO STOCK for each."
               % (len(stuck_lines), oldest, '; '.join(items),
                  (' ... +%d more' % more) if more > 0 else ''))
        sent = _send(conn, sn, client, 'PICK_EXCEPTIONS', msg,
                     sorted(set(ln['sku'] for ln in stuck_lines)))
        for ln in stuck_lines:
          note = ("picked %s day(s) ago, never loaded - managers texted (digest of %d line(s), %d SMS)"
                  % (ln['age_days'], len(stuck_lines), sent))
          _log_event(conn, ln['pick_id'], ln['pick_line_id'], 'PIPELINE_ESCALATED',
                     'pipeline_monitor', ln['qty'], note)
          _log_agent(conn, 'PIPELINE_ESCALATED', ln['sku'],
                     'pick_line %s' % ln['pick_line_id'], '%d SMS' % sent, note,
                     executed_by='pipeline_monitor')
          summary['escalated_lines'] += 1
        conn.commit()
        summary['sms_sent'] += sent

      for tr in stuck_trips:
        msg = ("DEPARTED, NOT DELIVERED for %s day(s): truck T%s (%s line(s), departed by %s) "
               "has no driver confirmation. Open #exceptions and CONFIRM DELIVERED, or "
               "find the truck." % (tr['age_days'], tr['trip_id'], tr['lines'],
                                    tr['finalized_by'] or '?'))
        sent = _send(conn, sn, client, 'LOAD_EXCEPTIONS', msg, [])
        _log_agent(conn, 'PIPELINE_ESCALATED', None, 'trip %s' % tr['trip_id'],
                   '%d SMS' % sent, msg, executed_by='pipeline_monitor')
        conn.commit()
        summary['escalated_trips'] += 1
        summary['sms_sent'] += sent

    if summary['cancelled'] or summary['escalated_lines'] or summary['escalated_trips']:
      _log_agent(conn, 'PIPELINE_MONITOR_RUN', None,
                 'cancelled %d; escalated %d line(s), %d trip(s)'
                 % (len(summary['cancelled']), summary['escalated_lines'], summary['escalated_trips']),
                 '%d SMS' % summary['sms_sent'],
                 ('cancelled: ' + ', '.join(summary['cancelled'][:12])) if summary['cancelled'] else '',
                 executed_by='pipeline_monitor')
      conn.commit()
  except Exception as e:
    summary['errors'].append(str(e))
    try:
      conn.rollback()
    except Exception:
      pass
  finally:
    try:
      if conn is not None:
        conn.close()
    except Exception:
      pass
  print('[pipeline_monitor] %s' % summary)
  return summary


def _send(conn, sn, client, ntype, msg, skus):
  if client is None:
    return 0
  contacts = (sn.get_hub_manager_phones(conn, ntype)
              or sn.get_hub_manager_phones(conn, 'PICK_EXCEPTIONS')
              or sn.get_hub_manager_phones(conn, 'NEW_RECOMMENDATIONS'))
  sent = 0
  for c in contacts:
    try:
      r = client.send_sms(c['phone'], msg)
      sn.log_sms_notification(conn, r, ntype, related_skus=skus)
      if r.get('success'):
        sent += 1
    except Exception as e:
      print('[pipeline_monitor] SMS to %s failed: %s' % (c.get('name'), e))
  return sent