"""
processed_transfers_dashboard.py - Anvil Server Module
======================================================
Read-only "Processed SKUs" view for the Hub Manager.

Shows every transfer recommendation that has moved past Pending, with:
  - search by SKU (partial), status-change date range, and status
  - sort by date / SKU / status
  - CSV export (removes the copy-paste-into-Excel workaround)

Returns a true `total` match count alongside the (capped) `rows`, so the UI
can show "Showing N of TOTAL" and make filtering feel responsive. Grid is
capped at max_rows (default 500) for load performance; export pulls all.

Fields per row:
  sku, status_change_date, status,
  original_qty  (ai_recommended_qty),
  altered_qty   (manager_override_qty -- real column name has a TRAILING SPACE,
                 so it is quoted as [manager_override_qty ]),
  batch_id, trip_number, note (notes, or pause_reason as fallback)

status_change_date = COALESCE(reviewed_date, recommendation_date). A true
per-status-change timestamp is a future enhancement (agreed 2026-07-15).
"""
import anvil
import anvil.server
from datetime import datetime, date
from decimal import Decimal
import csv
import io

from . import db_config


# Whitelisted sort options -> ORDER BY fragment (prevents SQL injection via sort)
PROCESSED_SORTS = {
  'date_desc': 'status_change_date DESC',
  'date_asc':  'status_change_date ASC',
  'sku':       'sku ASC, status_change_date DESC',
  'status':    'status ASC, status_change_date DESC',
}


def _get_connection():
  return db_config.get_nexus_connection()


def _make_serializable(v):
  if isinstance(v, Decimal):
    f = float(v)
    return int(f) if f.is_integer() else f
  if isinstance(v, (datetime, date)):
    return v.isoformat()
  return v


def _serialize(row):
  return {k: _make_serializable(v) for k, v in row.items()}


def _build_query(sku, date_from, date_to, status, sort_by, top):
  """Return (sql, count_sql, params). Base rule: everything except Pending.
  Both queries share the exact same WHERE + params."""
  date_expr = "COALESCE(r.reviewed_date, r.recommendation_date)"
  where = ["r.status <> 'Pending'"]
  params = []

  if sku:
    where.append("r.sku LIKE %s")
    params.append('%' + str(sku).strip() + '%')
  if date_from:
    where.append(date_expr + " >= %s")
    params.append(date_from)
  if date_to:
    where.append(date_expr + " < DATEADD(day, 1, CAST(%s AS date))")
    params.append(date_to)
  if status and str(status).lower() not in ('all', ''):
    where.append("r.status = %s")
    params.append(status)

  where_sql = " AND ".join(where)
  order = PROCESSED_SORTS.get(sort_by, PROCESSED_SORTS['date_desc'])

  sql = (
    "SELECT TOP " + str(int(top)) + " "
    "r.recommendation_id, r.sku, r.status, "
    + date_expr + " AS status_change_date, "
    "r.ai_recommended_qty AS original_qty, "
    "r.[manager_override_qty ] AS altered_qty, "
    "r.batch_id, "
    "(SELECT TOP 1 tta.trip_number FROM dbo.transfer_trip_assignments tta "
    "   WHERE tta.recommendation_id = r.recommendation_id "
    "   ORDER BY tta.assignment_id DESC) AS trip_number, "
    "COALESCE(NULLIF(LTRIM(RTRIM(CAST(r.notes AS nvarchar(max)))), ''), "
    "         CAST(r.pause_reason AS nvarchar(max)), '') AS note "
    "FROM dbo.ai_transfer_recommendations r "
    "WHERE " + where_sql + " "
    "ORDER BY " + order
  )
  count_sql = ("SELECT COUNT(*) AS n FROM dbo.ai_transfer_recommendations r "
               "WHERE " + where_sql)
  return sql, count_sql, params


@anvil.server.callable
def get_processed_transfers(sku=None, date_from=None, date_to=None,
                            status=None, sort_by='date_desc', max_rows=500):
  """Return processed (non-Pending) transfer recs matching the filters.

  Returns {'status':'success','rows':[...],'count':n,'total':N} where `total`
  is the full match count (rows is capped at max_rows), or an error dict.
  """
  try:
    conn = _get_connection()
    cur = conn.cursor()
    sql, count_sql, params = _build_query(sku, date_from, date_to, status,
                                          sort_by, max_rows)
    cur.execute(count_sql, tuple(params))
    total = int(cur.fetchone()['n'])
    cur.execute(sql, tuple(params))
    rows = [_serialize(r) for r in cur.fetchall()]
    conn.close()
    return {'status': 'success', 'rows': rows, 'count': len(rows), 'total': total}
  except Exception as e:
    return {'status': 'error', 'rows': [], 'count': 0, 'total': 0, 'message': str(e)}


@anvil.server.callable
def get_processed_status_options():
  """Distinct non-Pending statuses present, for the filter dropdown."""
  try:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT status FROM dbo.ai_transfer_recommendations "
                "WHERE status <> 'Pending' ORDER BY status")
    vals = [r['status'] for r in cur.fetchall()]
    conn.close()
    return ['All'] + vals
  except Exception:
    return ['All']


@anvil.server.callable
def export_processed_transfers(sku=None, date_from=None, date_to=None,
                               status=None, sort_by='date_desc'):
  """Return a CSV Media of the FULL filtered result (not capped) for download."""
  result = get_processed_transfers(sku, date_from, date_to, status, sort_by,
                                   max_rows=100000)
  buf = io.StringIO()
  w = csv.writer(buf)
  w.writerow(['SKU', 'Status Change Date', 'Status',
              'Original Qty (AI)', 'Altered Qty (Override)',
              'Batch', 'Trip', 'Note'])
  for r in result.get('rows', []):
    scd = r.get('status_change_date') or ''
    if scd:
      scd = str(scd)[:10]
    note = (r.get('note') or '').replace('\r', ' ').replace('\n', ' ').strip()
    w.writerow([r.get('sku', ''), scd, r.get('status', ''),
                r.get('original_qty', ''), r.get('altered_qty', ''),
                r.get('batch_id', ''), r.get('trip_number', ''), note])
  data = buf.getvalue().encode('utf-8-sig')  # BOM => Excel opens UTF-8 cleanly
  fname = 'processed_skus_' + datetime.now().strftime('%Y%m%d_%H%M') + '.csv'
  return anvil.BlobMedia('text/csv', data, name=fname)
