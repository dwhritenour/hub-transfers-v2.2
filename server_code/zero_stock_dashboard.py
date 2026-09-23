"""
zero_stock_dashboard.py - Anvil Server Module
=============================================
Read-only "Zero Stock" viewer: SKUs that Starlite needs (from
vw_unified_transfer_detection) but the Hub cannot supply right now
(hub_available <= 0). This is effectively the Hub reorder worklist -- demand
that is real and waiting, blocked at the source.

Each row is flagged:
  Empty      -> hub_on_hand = 0 (Hub physically has none; needs restock)
  Committed  -> hub_on_hand > 0 but hub_available <= 0 (physical stock is
                already committed to pending transfers)

NOTE: suggested_transfer_qty is structurally 0 for these rows (the app can't
suggest transferring what the Hub lacks), so the "how much is needed" signal
here is DEMAND (demand_in_horizon / total_demand) and the negative Starlite
Available, not suggested_transfer_qty.

Header: SKU search, flag filter (All/Empty/Committed), sort. CSV export.
Built 2026-07-15 (v1). Companion client form: client_code/zero_stock.
"""
import anvil
import anvil.server
from datetime import datetime, date
from decimal import Decimal
import csv
import io

from . import db_config


# Urgency ordering used by the default sort (most pressing first)
_URG_RANK = ("CASE WHEN v.time_urgency = 'CRITICAL' OR v.inventory_urgency = 'CRITICAL' THEN 1 "
             "WHEN v.time_urgency = 'HIGH' THEN 2 "
             "WHEN v.inventory_urgency = 'WARNING' THEN 3 ELSE 4 END")

ZERO_STOCK_SORTS = {
  'urgency': _URG_RANK + ", v.earliest_ship_date ASC",
  'ship':    "v.earliest_ship_date ASC",
  'demand':  "v.demand_in_horizon DESC",
  'sku':     "v.sku ASC",
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


def _build_query(sku, flag, sort_by, top):
  """Return (sql, count_sql, params). Base rule: Hub can't supply (hub_available <= 0)."""
  where = ["ISNULL(v.hub_available, 0) <= 0"]
  params = []

  if sku:
    where.append("v.sku LIKE %s")
    params.append('%' + str(sku).strip() + '%')

  f = (flag or 'all').strip().lower()
  if f == 'empty':
    where.append("ISNULL(v.hub_on_hand, 0) <= 0")
  elif f == 'committed':
    where.append("ISNULL(v.hub_on_hand, 0) > 0")

  where_sql = " AND ".join(where)
  order = ZERO_STOCK_SORTS.get(sort_by, ZERO_STOCK_SORTS['urgency'])

  cols = (
    "v.sku, "
    "v.is_a_item AS abc, "
    "CASE WHEN ISNULL(v.hub_on_hand, 0) <= 0 THEN 'Empty' ELSE 'Committed' END AS hub_flag, "
    "v.demand_in_horizon, v.total_demand, "
    "v.earliest_ship_date, "
    "v.time_urgency, v.inventory_urgency, "
    "v.starlite_on_hand, v.starlite_available, "
    "v.hub_on_hand, v.hub_available"
  )
  frm = ("FROM dbo.vw_unified_transfer_detection v "
         "WHERE " + where_sql)

  sql = "SELECT TOP " + str(int(top)) + " " + cols + " " + frm + " ORDER BY " + order
  count_sql = "SELECT COUNT(*) AS n " + frm
  return sql, count_sql, params


@anvil.server.callable
def get_zero_stock(sku=None, flag='All', sort_by='urgency', max_rows=500):
  """Return needed-but-Hub-can't-supply SKUs matching the filters.

  Returns {'status':'success','rows':[...],'count':n,'total':N} or error dict.
  """
  try:
    conn = _get_connection()
    cur = conn.cursor()
    sql, count_sql, params = _build_query(sku, flag, sort_by, max_rows)
    cur.execute(count_sql, tuple(params))
    total = int(cur.fetchone()['n'])
    cur.execute(sql, tuple(params))
    rows = [_serialize(r) for r in cur.fetchall()]
    conn.close()
    return {'status': 'success', 'rows': rows, 'count': len(rows), 'total': total}
  except Exception as e:
    return {'status': 'error', 'rows': [], 'count': 0, 'total': 0, 'message': str(e)}


@anvil.server.callable
def export_zero_stock(sku=None, flag='All', sort_by='urgency'):
  """Return a CSV Media of the FULL filtered zero-stock list for download."""
  result = get_zero_stock(sku, flag, sort_by, max_rows=100000)
  buf = io.StringIO()
  w = csv.writer(buf)
  w.writerow(['SKU', 'ABC', 'Hub Flag',
              'Demand (Horizon)', 'Total Demand', 'Earliest Ship',
              'Time Urgency', 'Inv Urgency',
              'Starlite On-Hand', 'Starlite Available', 'Hub On-Hand'])
  for r in result.get('rows', []):
    esd = r.get('earliest_ship_date') or ''
    if esd:
      esd = str(esd)[:10]
    w.writerow([r.get('sku', ''), r.get('abc', ''),
                r.get('hub_flag', ''), r.get('demand_in_horizon', ''),
                r.get('total_demand', ''), esd,
                r.get('time_urgency', ''), r.get('inventory_urgency', ''),
                r.get('starlite_on_hand', ''), r.get('starlite_available', ''),
                r.get('hub_on_hand', '')])
  data = buf.getvalue().encode('utf-8-sig')
  fname = 'zero_stock_' + datetime.now().strftime('%Y%m%d_%H%M') + '.csv'
  return anvil.BlobMedia('text/csv', data, name=fname)
