"""
Trip PDF Generator - Anvil Server Module
=========================================
Generates professional PDF trip assignment sheets for Hub Manager.
Uses ReportLab for PDF generation.

Features:
- Company header
- Batch summary with totals
- Visual utilization bars for each trip
- Color-coded priority rows
- Checkbox column for "Loaded" confirmation
- Trip subtotals
- Page numbers and footer
"""

import anvil.server
import anvil.media
from datetime import datetime
from decimal import Decimal
import io

# ReportLab imports
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.platypus import (
SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
PageBreak, KeepTogether
)
from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.graphics.charts.barcharts import HorizontalBarChart

# Import centralized configuration
from . import db_config


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_connection():
  """Get database connection from centralized config."""
  return db_config.get_nexus_connection()


def _now_eastern():
  """Current wall-clock time in US Eastern (EST/EDT) for printout display.
  Anvil servers run in UTC, so a bare datetime.now() prints UTC; convert to
  America/New_York (DST-aware) so timestamps match our local time.
  (Same helper as work_order_pdf_generator.py.)"""
  from datetime import timezone, timedelta
  utc_now = datetime.now(timezone.utc)
  try:
    from zoneinfo import ZoneInfo
    return utc_now.astimezone(ZoneInfo("America/New_York"))
  except Exception:
    # Fallback (no tz database): US Eastern DST rules -- 2nd Sunday of March
    # to 1st Sunday of November.
    import calendar
    from datetime import date
    y = utc_now.year
    def _nth_sunday(month, nth):
      suns = [wk[calendar.SUNDAY] for wk in calendar.monthcalendar(y, month)
              if wk[calendar.SUNDAY] != 0]
      return suns[nth - 1]
    dst_start = date(y, 3, _nth_sunday(3, 2))
    dst_end = date(y, 11, _nth_sunday(11, 1))
    is_dst = dst_start <= utc_now.date() < dst_end
    return utc_now.astimezone(timezone(timedelta(hours=-4 if is_dst else -5)))


def format_number(value, decimals=0):
  """Format number with commas."""
  if value is None:
    return "0"
  if isinstance(value, Decimal):
    value = float(value)
  if decimals == 0:
    return "{:,.0f}".format(value)
  return "{:,.{d}f}".format(value, d=decimals)


def get_priority_color(priority_code):
  """Return color for priority level."""
  priority_colors = {
    '1-CRITICAL': colors.Color(0.9, 0.2, 0.2),      # Red
    '2-HIGH': colors.Color(1.0, 0.6, 0.0),          # Orange
    '3-INV_CRIT': colors.Color(0.8, 0.3, 0.3),      # Dark red
    '4-MEDIUM': colors.Color(1.0, 0.8, 0.0),        # Yellow
    '5-WARNING': colors.Color(1.0, 0.9, 0.5),       # Light yellow
    '6-LOW': colors.Color(0.7, 0.85, 0.7),          # Light green
    '7-ROUTINE': colors.Color(0.85, 0.85, 0.85),    # Light gray
  }
  return priority_colors.get(priority_code, colors.white)


def get_priority_text_color(priority_code):
  """Return text color for priority level (for contrast)."""
  dark_text = ['1-CRITICAL', '2-HIGH', '3-INV_CRIT']
  if priority_code in dark_text:
    return colors.white
  return colors.black


# =============================================================================
# DATA RETRIEVAL
# =============================================================================

def get_trip_data(conn, batch_id=None):
  """
    Get trip assignment data from database.
    If batch_id is None, gets the most recent batch.
    """
  cursor = conn.cursor()

  # If no batch_id provided, get the most recent one
  if batch_id is None:
    cursor.execute("""
            SELECT TOP 1 batch_id 
            FROM dbo.transfer_trip_assignments 
            ORDER BY created_date DESC
        """)
    row = cursor.fetchone()
    if not row:
      return None, None, []
    batch_id = row['batch_id']

    # Get batch summary
  cursor.execute("""
        SELECT 
            batch_id,
            MIN(created_date) as created_date,
            COUNT(DISTINCT trip_number) as total_trips,
            COUNT(*) as total_items,
            SUM(qty) as total_units,
            SUM(volume_ft3) as total_volume,
            SUM(weight_lb) as total_weight
        FROM dbo.transfer_trip_assignments
        WHERE batch_id = %s
        GROUP BY batch_id
    """, (batch_id,))

  summary_row = cursor.fetchone()
  if not summary_row:
    return None, None, []

  summary = {
    'batch_id': summary_row['batch_id'],
    'created_date': summary_row['created_date'],
    'total_trips': summary_row['total_trips'],
    'total_items': summary_row['total_items'],
    'total_units': float(summary_row['total_units'] or 0),
    'total_volume': float(summary_row['total_volume'] or 0),
    'total_weight': float(summary_row['total_weight'] or 0)
  }

  # Get trip details with part descriptions
  cursor.execute("""
        SELECT 
            t.trip_number,
            t.priority_code,
            t.priority_sort,
            t.sku,
            COALESCE(p.part_desc, t.sku) as part_desc,
            t.qty,
            t.volume_ft3,
            t.weight_lb,
            t.event_type,
            t.urgency,
            t.split_note
        FROM dbo.transfer_trip_assignments t
        LEFT JOIN dbo.part_specs p ON t.sku = p.sku
        WHERE t.batch_id = %s
        ORDER BY t.trip_number, t.priority_sort, t.sku
    """, (batch_id,))

  items = []
  for row in cursor.fetchall():
    items.append({
      'trip_number': row['trip_number'],
      'priority_code': row['priority_code'],
      'priority_sort': row['priority_sort'],
      'sku': row['sku'],
      'part_desc': row['part_desc'] or row['sku'],
      'qty': float(row['qty'] or 0),
      'volume_ft3': float(row['volume_ft3'] or 0),
      'weight_lb': float(row['weight_lb'] or 0),
      'event_type': row['event_type'],
      'urgency': row['urgency'],
      'split_note': row['split_note']
    })

    # Get truck capacity from global_parameters
  cursor.execute("""
        SELECT parameter_name, parameter_value
        FROM dbo.global_parameters
        WHERE parameter_name IN ('truck_cap_ft3', 'truck_cap_lb')
    """)

  params = {}
  for row in cursor.fetchall():
    params[row['parameter_name']] = float(row['parameter_value'])

  truck_capacity = {
    'max_volume_ft3': params.get('truck_cap_ft3', 1456),
    'max_weight_lb': params.get('truck_cap_lb', 13000)
  }

  return summary, truck_capacity, items


def organize_by_trip(items, truck_capacity):
  """Organize items by trip number with subtotals."""
  trips = {}

  for item in items:
    trip_num = item['trip_number']
    if trip_num not in trips:
      trips[trip_num] = {
        'items': [],
        'total_qty': 0,
        'total_volume': 0,
        'total_weight': 0
      }

    trips[trip_num]['items'].append(item)
    trips[trip_num]['total_qty'] += item['qty']
    trips[trip_num]['total_volume'] += item['volume_ft3']
    trips[trip_num]['total_weight'] += item['weight_lb']

    # Calculate utilization percentages
  for trip_num, trip_data in trips.items():
    trip_data['volume_pct'] = (trip_data['total_volume'] / truck_capacity['max_volume_ft3']) * 100
    trip_data['weight_pct'] = (trip_data['total_weight'] / truck_capacity['max_weight_lb']) * 100

  return trips


# =============================================================================
# PDF GENERATION COMPONENTS
# =============================================================================

def create_utilization_bar(label, current, maximum, pct, width=4*inch):
  """Create a visual utilization bar."""
  # Create a drawing for the bar
  bar_height = 0.25 * inch
  drawing = Drawing(width, bar_height + 0.3*inch)

  # Background bar (gray)
  drawing.add(Rect(0, 0, width, bar_height, 
                   fillColor=colors.lightgrey, strokeColor=colors.grey))

  # Fill bar (color based on utilization)
  if pct >= 90:
    fill_color = colors.Color(0.2, 0.7, 0.3)  # Green - good utilization
  elif pct >= 70:
    fill_color = colors.Color(0.3, 0.6, 0.9)  # Blue - moderate
  else:
    fill_color = colors.Color(0.9, 0.7, 0.3)  # Orange - low utilization

  fill_width = min(width * (pct / 100), width)
  drawing.add(Rect(0, 0, fill_width, bar_height, 
                   fillColor=fill_color, strokeColor=None))

  # Border
  drawing.add(Rect(0, 0, width, bar_height, 
                   fillColor=None, strokeColor=colors.grey))

  return drawing


def create_header(styles, summary):
  """Create document header."""
  elements = []

  # Company name
  company_style = ParagraphStyle(
    'CompanyHeader',
    parent=styles['Heading1'],
    fontSize=24,
    spaceAfter=6,
    textColor=colors.Color(0.2, 0.3, 0.5)
  )
  elements.append(Paragraph("TITAN FLOW CONTROL", company_style))

  # Document title
  title_style = ParagraphStyle(
    'DocTitle',
    parent=styles['Heading2'],
    fontSize=16,
    spaceAfter=12
  )
  elements.append(Paragraph("Transfer Trip Assignment Sheet", title_style))

  # Separator line
  elements.append(Spacer(1, 6))

  # Info table
  gen_date = summary['created_date'].strftime('%m/%d/%Y %I:%M %p') if summary['created_date'] else 'N/A'

  info_data = [
    ['Generated:', gen_date, 'Batch ID:', summary['batch_id']],
    ['Total Trips:', str(summary['total_trips']), 
     'Total SKUs:', str(summary['total_items'])],
    ['Total Units:', format_number(summary['total_units']),
     'Total Volume:', format_number(summary['total_volume']) + ' ft³'],
  ]

  info_table = Table(info_data, colWidths=[1.2*inch, 2*inch, 1.2*inch, 2*inch])
  info_table.setStyle(TableStyle([
    ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
    ('FONTSIZE', (0, 0), (-1, -1), 10),
    ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
    ('FONTNAME', (2, 0), (2, -1), 'Helvetica-Bold'),
    ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
    ('ALIGN', (2, 0), (2, -1), 'RIGHT'),
    ('ALIGN', (1, 0), (1, -1), 'LEFT'),
    ('ALIGN', (3, 0), (3, -1), 'LEFT'),
    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ('TOPPADDING', (0, 0), (-1, -1), 4),
  ]))
  elements.append(info_table)

  elements.append(Spacer(1, 20))

  return elements


def create_trip_section(styles, trip_number, trip_data, total_trips, truck_capacity):
  """Create a trip section with header and item table."""
  elements = []

  # Trip header
  trip_header_style = ParagraphStyle(
    'TripHeader',
    parent=styles['Heading2'],
    fontSize=14,
    spaceBefore=12,
    spaceAfter=6,
    backColor=colors.Color(0.9, 0.9, 0.95),
    borderPadding=8
  )

  elements.append(Paragraph(
    f"TRIP {trip_number} of {total_trips}",
    trip_header_style
  ))

  # Utilization summary
  vol_pct = trip_data['volume_pct']
  wgt_pct = trip_data['weight_pct']

  util_text = (
    f"Volume: {format_number(trip_data['total_volume'])} / "
    f"{format_number(truck_capacity['max_volume_ft3'])} ft³ ({vol_pct:.0f}%)  |  "
    f"Weight: {format_number(trip_data['total_weight'])} / "
    f"{format_number(truck_capacity['max_weight_lb'])} lb ({wgt_pct:.0f}%)  |  "
    f"Items: {len(trip_data['items'])} SKUs"
  )

  util_style = ParagraphStyle(
    'UtilText',
    parent=styles['Normal'],
    fontSize=9,
    spaceAfter=10,
    textColor=colors.Color(0.3, 0.3, 0.3)
  )
  elements.append(Paragraph(util_text, util_style))

  # Item table
  # Header row
  table_data = [[
    '☐',  # Checkbox header (empty for header)
    'Priority',
    'SKU',
    'Description',
    'Qty',
    'Vol (ft³)',
    'Wt (lb)',
    'Notes'
  ]]

  # Data rows
  for item in trip_data['items']:
    # Truncate description if too long
    desc = item['part_desc']
    if len(desc) > 30:
      desc = desc[:27] + '...'

      # Build notes
    notes = []
    if item['split_note']:
      notes.append(item['split_note'])
    if item['urgency'] and item['urgency'] not in ['N/A', 'Normal']:
      notes.append(item['urgency'])
    notes_str = ', '.join(notes) if notes else ''

    table_data.append([
      '☐',  # Checkbox character
      item['priority_code'],
      item['sku'],
      desc,
      format_number(item['qty']),
      format_number(item['volume_ft3'], 1),
      format_number(item['weight_lb'], 1),
      notes_str
    ])

    # Subtotal row
  table_data.append([
    '',
    '',
    '',
    'TRIP SUBTOTAL:',
    format_number(trip_data['total_qty']),
    format_number(trip_data['total_volume'], 1),
    format_number(trip_data['total_weight'], 1),
    ''
  ])

  # Create table
  col_widths = [0.3*inch, 0.9*inch, 1.2*inch, 2.5*inch, 0.6*inch, 0.7*inch, 0.7*inch, 1.1*inch]

  item_table = Table(table_data, colWidths=col_widths, repeatRows=1)

  # Build table style
  table_style = [
    # Header styling
    ('BACKGROUND', (0, 0), (-1, 0), colors.Color(0.3, 0.4, 0.5)),
    ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
    ('FONTSIZE', (0, 0), (-1, 0), 9),
    ('ALIGN', (0, 0), (-1, 0), 'CENTER'),

    # Data row styling
    ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
    ('FONTSIZE', (0, 1), (-1, -1), 8),
    ('ALIGN', (0, 1), (0, -1), 'CENTER'),  # Checkbox column
    ('ALIGN', (4, 1), (6, -1), 'RIGHT'),   # Numeric columns

    # Grid
    ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
    ('LINEBELOW', (0, 0), (-1, 0), 1.5, colors.Color(0.3, 0.4, 0.5)),

    # Subtotal row
    ('BACKGROUND', (0, -1), (-1, -1), colors.Color(0.95, 0.95, 0.95)),
    ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
    ('LINEABOVE', (0, -1), (-1, -1), 1, colors.black),

    # Padding
    ('TOPPADDING', (0, 0), (-1, -1), 4),
    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ('LEFTPADDING', (0, 0), (-1, -1), 4),
    ('RIGHTPADDING', (0, 0), (-1, -1), 4),
  ]

  # Add priority-based row colors
  for i, item in enumerate(trip_data['items'], start=1):
    bg_color = get_priority_color(item['priority_code'])
    text_color = get_priority_text_color(item['priority_code'])
    # Lighten the background for readability
    light_bg = colors.Color(
      0.7 + bg_color.red * 0.3,
      0.7 + bg_color.green * 0.3,
      0.7 + bg_color.blue * 0.3
    )
    table_style.append(('BACKGROUND', (1, i), (1, i), light_bg))

  item_table.setStyle(TableStyle(table_style))

  elements.append(item_table)
  elements.append(Spacer(1, 20))

  return elements


def create_footer_section(styles):
  """Create signature/acknowledgment section."""
  elements = []

  elements.append(Spacer(1, 30))

  # Separator line
  line_data = [['─' * 100]]
  line_table = Table(line_data, colWidths=[7.5*inch])
  line_table.setStyle(TableStyle([
    ('FONTSIZE', (0, 0), (-1, -1), 6),
    ('TEXTCOLOR', (0, 0), (-1, -1), colors.grey),
  ]))
  elements.append(line_table)

  elements.append(Spacer(1, 10))

  # Signature section
  sig_style = ParagraphStyle(
    'SigStyle',
    parent=styles['Normal'],
    fontSize=10,
    spaceAfter=20
  )

  elements.append(Paragraph("ACKNOWLEDGMENT", sig_style))

  sig_data = [
    ['Loaded By:', '_' * 30, 'Date:', '_' * 20],
    ['Verified By:', '_' * 30, 'Time:', '_' * 20],
  ]

  sig_table = Table(sig_data, colWidths=[1*inch, 2.5*inch, 0.8*inch, 1.7*inch])
  sig_table.setStyle(TableStyle([
    ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
    ('FONTSIZE', (0, 0), (-1, -1), 10),
    ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
    ('FONTNAME', (2, 0), (2, -1), 'Helvetica-Bold'),
    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
    ('TOPPADDING', (0, 0), (-1, -1), 8),
    ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
  ]))
  elements.append(sig_table)

  return elements


# =============================================================================
# PAGE NUMBERING
# =============================================================================

def add_page_number(canvas, doc):
  """Add page numbers and footer to each page."""
  canvas.saveState()

  # Page number
  page_num = canvas.getPageNumber()
  text = f"Page {page_num}"

  canvas.setFont('Helvetica', 9)
  canvas.setFillColor(colors.grey)

  # Footer left - company name
  canvas.drawString(0.75*inch, 0.5*inch, "Titan Flow Control")

  # Footer center - page number
  canvas.drawCentredString(letter[0]/2, 0.5*inch, text)

  # Footer right - generation timestamp
  canvas.drawRightString(letter[0] - 0.75*inch, 0.5*inch,
                         _now_eastern().strftime('%m/%d/%Y %I:%M %p'))

  canvas.restoreState()


# =============================================================================
# MAIN PDF GENERATION
# =============================================================================

def generate_trip_pdf(batch_id=None):
  """
    Generate a PDF trip assignment sheet.
    
    Args:
        batch_id: Optional batch ID. If None, uses most recent batch.
    
    Returns:
        Anvil BlobMedia containing the PDF, or None if no data.
    """
  conn = get_connection()

  try:
    # Get data
    summary, truck_capacity, items = get_trip_data(conn, batch_id)

    if not summary or not items:
      conn.close()
      return None

      # Organize by trip
    trips = organize_by_trip(items, truck_capacity)

    # Create PDF buffer
    buffer = io.BytesIO()

    # Create document
    doc = SimpleDocTemplate(
      buffer,
      pagesize=letter,
      rightMargin=0.5*inch,
      leftMargin=0.5*inch,
      topMargin=0.75*inch,
      bottomMargin=0.75*inch
    )

    # Get styles
    styles = getSampleStyleSheet()

    # Build document elements
    elements = []

    # Header
    elements.extend(create_header(styles, summary))

    # Trip sections - each trip on its own page
    total_trips = len(trips)
    sorted_trip_nums = sorted(trips.keys())
    for i, trip_num in enumerate(sorted_trip_nums):
      trip_data = trips[trip_num]

      trip_elements = create_trip_section(
        styles, trip_num, trip_data, total_trips, truck_capacity
      )
      elements.extend(trip_elements)

      # Signature block for this trip
      elements.extend(create_footer_section(styles))

      # Page break after each trip except the last
      if i < total_trips - 1:
        elements.append(PageBreak())

    # Build PDF
    doc.build(elements, onFirstPage=add_page_number, onLaterPages=add_page_number)

    # Get PDF content
    pdf_content = buffer.getvalue()
    buffer.close()
    conn.close()

    # Create filename
    timestamp = _now_eastern().strftime('%Y%m%d_%H%M%S')
    filename = f"Trip_Assignment_{summary['batch_id']}_{timestamp}.pdf"

    # Return as Anvil media
    return anvil.BlobMedia('application/pdf', pdf_content, name=filename)

  except Exception as e:
    conn.close()
    raise e


# =============================================================================
# ANVIL CALLABLE FUNCTIONS
# =============================================================================

@anvil.server.callable
def download_trip_pdf(batch_id=None):
  """
    Generate and return trip assignment PDF.
    Called from client when btn_trip_pdf is clicked.
    
    Args:
        batch_id: Optional batch ID. If None, uses most recent batch.
    
    Returns:
        dict with status and either pdf_media or error message
    """
  try:
    pdf_media = generate_trip_pdf(batch_id)

    if pdf_media is None:
      return {
        'status': 'error',
        'message': 'No trip assignment data found'
      }

    return {
      'status': 'success',
      'pdf_media': pdf_media,
      'filename': pdf_media.name
    }

  except Exception as e:
    return {
      'status': 'error',
      'message': str(e)
    }


@anvil.server.callable
def get_available_batches(date_filter=None):
  """
    Get list of available batch IDs for dropdown selection, filtered by date.
    
    Args:
        date_filter: A date object to filter batches by created_date.
                     If None, returns all batches (most recent first).
    
    Returns:
        List of batch dictionaries with display info
    """
  conn = get_connection()
  cursor = conn.cursor()

  # Build the query with optional date filter
  # Now checks transfer_trips.status instead of ai_transfer_recommendations.status
  query = """
        WITH BatchStatus AS (
            SELECT 
                t.batch_id,
                MIN(t.created_date) as created_date,
                COUNT(DISTINCT ta.assignment_id) as item_count,
                COUNT(DISTINCT t.trip_number) as trip_count,
                SUM(ta.qty) as total_units,
                -- Count trips by their status in transfer_trips table
                SUM(CASE WHEN t.status = 'Planned' THEN 1 ELSE 0 END) as planned_count,
                SUM(CASE WHEN t.status = 'Finalized' THEN 1 ELSE 0 END) as finalized_count,
                SUM(CASE WHEN t.status = 'Shipped' THEN 1 ELSE 0 END) as shipped_count,
                SUM(CASE WHEN t.status = 'Received' THEN 1 ELSE 0 END) as received_count
            FROM dbo.transfer_trips t
            LEFT JOIN dbo.transfer_trip_assignments ta 
                ON t.batch_id = ta.batch_id AND t.trip_number = ta.trip_number
            GROUP BY t.batch_id
        )
        SELECT 
            batch_id,
            created_date,
            item_count,
            trip_count,
            total_units,
            planned_count,
            finalized_count,
            shipped_count,
            received_count,
            CASE 
                WHEN planned_count = 0 AND finalized_count > 0 THEN 'Finalized'
                WHEN shipped_count > 0 OR received_count > 0 THEN 'Completed'
                WHEN planned_count > 0 THEN 'Pending'
                ELSE 'Unknown'
            END as batch_status
        FROM BatchStatus
    """

  # Add date filter if provided
  if date_filter:
    query += " WHERE CAST(created_date AS DATE) = %s"
    query += " ORDER BY created_date DESC"
    cursor.execute(query, (date_filter,))
  else:
    query += " ORDER BY created_date DESC"
    cursor.execute(query)

  batches = []
  for row in cursor.fetchall():
    batch_status = row['batch_status']

    # Build status indicator for display
    if batch_status == 'Pending':
      status_icon = "🔵"  # Blue circle for pending
      status_text = f"{row['planned_count']} pending"
    elif batch_status == 'Finalized':
      status_icon = "✅"  # Checkmark for finalized
      status_text = "Finalized"
    elif batch_status == 'Completed':
      status_icon = "✅"  # Checkmark for completed
      status_text = "Complete"
    else:
      status_icon = "⚪"
      status_text = "Unknown"

    # Format date for display (shorter format)
    date_str = row['created_date'].strftime('%m/%d %H:%M') if row['created_date'] else 'N/A'

    batches.append({
      'batch_id': row['batch_id'],
      'created_date': row['created_date'].strftime('%Y-%m-%d %H:%M') if row['created_date'] else 'N/A',
      'item_count': row['item_count'],
      'trip_count': row['trip_count'],
      'total_units': float(row['total_units'] or 0),
      'batch_status': batch_status,
      'planned_count': row['planned_count'],
      'finalized_count': row['finalized_count'],
      'shipped_count': row['shipped_count'],
      'received_count': row['received_count'],
      'display': f"{status_icon} {row['batch_id']} - {date_str} ({row['trip_count']} trips, {row['item_count']} SKUs) - {status_text}"
    })

    # Limit to 20 results
    if len(batches) >= 20:
      break

  conn.close()
  return batches
