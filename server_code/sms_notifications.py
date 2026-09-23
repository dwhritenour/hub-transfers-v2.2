# =============================================================================
# SMS_NOTIFICATIONS.PY - Titan AI Inventory Management System
# =============================================================================
# SMS notification module for alerting Hub Managers about transfer recommendations
# Uses Twilio API for message delivery
#
# Integration: Anvil Server Module
# Database: SQL Server (nexus)
#
# Setup Requirements:
#   1. pip install twilio
#   2. Add Anvil Secrets:
#      - TWILIO_ACCOUNT_SID
#      - TWILIO_AUTH_TOKEN
#      - TWILIO_PHONE_NUMBER (your Twilio number in E.164 format)
#   3. Add Hub Manager phone numbers to global_parameters or a new table
#   4. global_parameters.sms_enabled = 1  (THE KILL SWITCH - see SMSConfig)
#
# 2026-09-05 (transfer-logic review M11): in-app kill switch.
#   SMSConfig.is_sms_enabled() now reads global_parameters.sms_enabled
#   FIRST and FAILS CLOSED: row missing, value 0, or any DB error -> no
#   texts. The Anvil Secret SMS_ENABLED='false' still disables as a second,
#   deploy-level off. TwilioSMSClient enforces the same switch at the point
#   of send, so no path (diagnostic test sends included) can bypass it.
#   Flip it in System Admin -> Global Parameters -> sms_enabled (1 / 0).
#
# Author: Dave Whritenour
# Project: Year of Automation - Phase 1
# Created: January 2026
# =============================================================================

import anvil.secrets
import anvil.server
from datetime import datetime
from typing import Optional, List, Dict, Any

# Import centralized configuration
from . import db_config

# Twilio import - install with: pip install twilio
try:
  from twilio.rest import Client
  from twilio.base.exceptions import TwilioRestException
  TWILIO_AVAILABLE = True
except ImportError:
  TWILIO_AVAILABLE = False
  print("WARNING: Twilio not installed. Run: pip install twilio")


# =============================================================================
# CONFIGURATION
# =============================================================================

class SMSConfig:
  """
    SMS Configuration - pulls from Anvil Secrets

    Required Anvil Secrets:
        - TWILIO_ACCOUNT_SID: Your Twilio Account SID
        - TWILIO_AUTH_TOKEN: Your Twilio Auth Token
        - TWILIO_PHONE_NUMBER: Your Twilio phone number (E.164 format: +1XXXXXXXXXX)

    Optional Anvil Secrets:
        - SMS_ENABLED: Set to 'false' to disable SMS at the deploy level
        - SMS_TEST_MODE: Set to 'true' to only log, not send (default: false)

    Kill switch (2026-09-05): dbo.global_parameters.sms_enabled. 1 = texts
    go out; 0 or MISSING = nothing is sent, anywhere in the app. Editable
    in System Admin. Checked before the Secret; both must allow.
    """

  KILL_SWITCH_PARAM = 'sms_enabled'

  @staticmethod
  def get_twilio_credentials() -> Dict[str, str]:
    """Retrieve Twilio credentials from Anvil Secrets"""
    return {
      'account_sid': anvil.secrets.get_secret('TWILIO_ACCOUNT_SID'),
      'auth_token': anvil.secrets.get_secret('TWILIO_AUTH_TOKEN'),
      'phone_number': anvil.secrets.get_secret('TWILIO_PHONE_NUMBER')
    }

  @staticmethod
  def is_sms_enabled(conn=None) -> bool:
    """
        THE gate every sender checks. FAILS CLOSED.

        1. global_parameters.sms_enabled must exist and be 1. Missing row,
           0, NULL, or any database error -> False (and a server-log line
           saying why, so a silent Hub is never a mystery).
        2. Then the Anvil Secret SMS_ENABLED: 'false' -> False.

        Pass an open nexus connection to reuse it; otherwise one is opened
        and closed here.
    """
    own = conn is None
    try:
      if own:
        conn = db_config.get_nexus_connection()
      cursor = conn.cursor()
      cursor.execute(
        "SELECT parameter_value FROM dbo.global_parameters WHERE parameter_name = %s",
        (SMSConfig.KILL_SWITCH_PARAM,))
      row = cursor.fetchone()
      if row is None:
        print("[SMS] kill switch: global_parameters.%s is MISSING - SMS OFF (fail closed)"
              % SMSConfig.KILL_SWITCH_PARAM)
        return False
      val = row['parameter_value'] if isinstance(row, dict) else row[0]
      if val is None or float(val) < 1:
        print("[SMS] kill switch: %s = %s - SMS OFF" % (SMSConfig.KILL_SWITCH_PARAM, val))
        return False
    except Exception as e:
      print("[SMS] kill switch: could not read global_parameters.%s (%s) - SMS OFF (fail closed)"
            % (SMSConfig.KILL_SWITCH_PARAM, e))
      return False
    finally:
      if own and conn is not None:
        try:
          conn.close()
        except Exception:
          pass

    try:
      enabled = anvil.secrets.get_secret('SMS_ENABLED')
      if enabled and enabled.lower() == 'false':
        print("[SMS] Anvil Secret SMS_ENABLED = 'false' - SMS OFF")
        return False
    except Exception:
      pass  # Secret absent = no deploy-level override
    return True

  @staticmethod
  def is_test_mode() -> bool:
    """Check if running in test mode (log only, don't send)"""
    try:
      test_mode = anvil.secrets.get_secret('SMS_TEST_MODE')
      return test_mode.lower() == 'true' if test_mode else False
    except:
      return False


# =============================================================================
# TWILIO CLIENT
# =============================================================================

class TwilioSMSClient:
  """
    Twilio SMS Client wrapper with error handling and logging
    """

  def __init__(self):
    if not TWILIO_AVAILABLE:
      raise ImportError("Twilio library not installed. Run: pip install twilio")

    creds = SMSConfig.get_twilio_credentials()
    self.account_sid = creds['account_sid']
    self.auth_token = creds['auth_token']
    self.from_number = creds['phone_number']

    if not all([self.account_sid, self.auth_token, self.from_number]):
      raise ValueError("Missing Twilio credentials in Anvil Secrets")

    self.client = Client(self.account_sid, self.auth_token)
    self.test_mode = SMSConfig.is_test_mode()
    # Kill switch, evaluated once per client (= once per batch of texts).
    # send_sms refuses when it is off, so nothing can bypass the gate.
    self.enabled = SMSConfig.is_sms_enabled()

  def send_sms(self, to_number: str, message: str) -> Dict[str, Any]:
    """
        Send an SMS message

        Args:
            to_number: Recipient phone number in E.164 format (+1XXXXXXXXXX)
            message: Message content (max 1600 chars, auto-splits at 160)

        Returns:
            Dict with success status, message SID, and any error info
        """
    # Validate phone number format
    if not to_number.startswith('+'):
      to_number = '+1' + to_number.replace('-', '').replace(' ', '').replace('(', '').replace(')', '')

      # Truncate message if too long
    if len(message) > 1600:
      message = message[:1597] + '...'

    result = {
      'success': False,
      'to_number': to_number,
      'from_number': self.from_number,
      'message_length': len(message),
      'segments': (len(message) // 160) + 1,
      'timestamp': datetime.now().isoformat(),
      'test_mode': self.test_mode
    }

    if not self.enabled:
      result['status'] = 'disabled'
      result['error'] = ('SMS is OFF: global_parameters.sms_enabled is 0/missing '
                         'or Secret SMS_ENABLED is false')
      print("[SMS] NOT sent to %s - kill switch is off" % to_number)
      return result

    if self.test_mode:
      # Test mode - just log, don't actually send
      result['success'] = True
      result['sid'] = 'TEST_MODE_' + datetime.now().strftime('%Y%m%d%H%M%S')
      result['status'] = 'test_mode'
      result['message_preview'] = message[:100] + '...' if len(message) > 100 else message
      print(f"[TEST MODE] SMS to {to_number}: {message[:100]}...")
      return result

    try:
      msg = self.client.messages.create(
        body=message,
        from_=self.from_number,
        to=to_number
      )

      result['success'] = True
      result['sid'] = msg.sid
      result['status'] = msg.status
      result['price'] = msg.price
      result['price_unit'] = msg.price_unit

    except TwilioRestException as e:
      result['success'] = False
      result['error_code'] = e.code
      result['error_message'] = str(e.msg)
      print(f"Twilio Error [{e.code}]: {e.msg}")

    except Exception as e:
      result['success'] = False
      result['error_message'] = str(e)
      print(f"SMS Error: {str(e)}")

    return result


# =============================================================================
# DATABASE FUNCTIONS
# =============================================================================

def get_db_connection():
  """Get database connection using existing db_config pattern"""
  return db_config.get_nexus_connection()


def log_sms_notification(conn, result: Dict, notification_type: str,
                         related_batch_id: Optional[str] = None,
                         related_skus: Optional[List[str]] = None):
  """
    Log SMS notification to ai_agent_log table

    Args:
        conn: Database connection
        result: Result dict from send_sms
        notification_type: Type of notification (e.g., 'NEW_RECOMMENDATIONS')
        related_batch_id: Optional batch_id for reference
        related_skus: Optional list of related SKUs
    """
  cursor = conn.cursor()

  action_type = f"SMS_{notification_type}"
  old_value = None
  new_value = f"To: {result.get('to_number')}, Status: {result.get('status', 'unknown')}"

  if result.get('success'):
    reason = f"SID: {result.get('sid')}"
  else:
    reason = f"Error: {result.get('error_message', 'Unknown error')}"

  if related_skus:
    reason += f" | SKUs: {', '.join(related_skus[:5])}"
    if len(related_skus) > 5:
      reason += f"... (+{len(related_skus) - 5} more)"

  executed_by = f"SMS_SYSTEM|Batch:{related_batch_id}" if related_batch_id else "SMS_SYSTEM"

  # 2026-09-05 (review M10): action_timestamp was never written here -
  # ~17,400 SMS audit rows carry NULL and are invisible to every
  # time-windowed query. Explicit GETDATE() until the column gets a default.
  insert_query = """
        INSERT INTO dbo.ai_agent_log
        (action_type, sku, old_value, new_value, reason, action_timestamp, executed_by)
        VALUES (%s, %s, %s, %s, %s, GETDATE(), %s)
    """

  cursor.execute(insert_query, (
    action_type,
    related_skus[0] if related_skus else None,
    old_value,
    new_value,
    reason,
    executed_by
  ))

  conn.commit()


def get_hub_manager_phones(conn, notification_type: str = 'NEW_RECOMMENDATIONS') -> List[Dict[str, str]]:
  """
    Get Hub Manager phone numbers from sms_notification_contacts table

    Table: sms_notification_contacts
        - contact_name: Manager's name
        - phone_number: Phone in E.164 format (+19107350000)
        - role: 'Hub_Manager', 'Supervisor', etc.
        - notification_types: Comma-separated list of notification types
        - is_active: Whether contact should receive notifications

    Args:
        conn: Database connection
        notification_type: Type of notification to filter by (default: NEW_RECOMMENDATIONS)

    Returns:
        List of dicts with 'phone' and 'name' keys
    """
  cursor = conn.cursor()

  query = """
        SELECT contact_name, phone_number, role, notification_types
        FROM dbo.sms_notification_contacts
        WHERE is_active = 1
          AND (notification_types LIKE %s OR notification_types IS NULL)
        ORDER BY contact_name
    """

  # Build the LIKE pattern for notification type
  like_pattern = f'%{notification_type}%'

  cursor.execute(query, (like_pattern,))
  rows = cursor.fetchall()

  managers = []
  for row in rows:
    phone = str(row['phone_number']).strip()

    managers.append({
      'phone': phone,
      'name': row['contact_name'],
      'role': row['role']
    })

  return managers


def get_pending_recommendation_count(conn) -> int:
  """Get count of Pending recommendations"""
  cursor = conn.cursor()
  cursor.execute("""
        SELECT COUNT(*) as cnt
        FROM dbo.ai_transfer_recommendations
        WHERE status = 'Pending'
    """)
  row = cursor.fetchone()
  return row['cnt'] if row else 0


def get_new_recommendations_summary(conn, batch_id: str,
                                    only_skus: Optional[List[str]] = None) -> Dict[str, Any]:
  """
    Get summary of new recommendations from a specific batch

    only_skus (2026-09-06, transfer_ai_review 5.34.3): the SKUs INSERTED
    this batch. batch_id alone is not "new" - update_recommendation
    re-stamps batch_id on any existing rec whose values changed, so a
    batch_id-only count named days-old recs as new. When only_skus is
    given, the summary is restricted to them; an empty list means
    nothing is new and total_count is 0.

    Returns:
        Dict with count, SKUs, urgency breakdown, etc.
  """
  cursor = conn.cursor()

  if only_skus is not None and len(only_skus) == 0:
    return {'total_count': 0, 'critical_count': 0, 'high_count': 0,
            'type1_count': 0, 'type2_count': 0, 'skus': [], 'batch_id': batch_id}

  sku_filter = ''
  params = [batch_id]
  if only_skus:
    sku_filter = ' AND sku IN (' + ','.join(['%s'] * len(only_skus)) + ')'
    params.extend(only_skus)

  query = """
        SELECT
            COUNT(*) as total_count,
            SUM(CASE WHEN base_urgency = 'CRITICAL' THEN 1 ELSE 0 END) as critical_count,
            SUM(CASE WHEN base_urgency = 'HIGH' THEN 1 ELSE 0 END) as high_count,
            SUM(CASE WHEN event_type = 1 THEN 1 ELSE 0 END) as type1_count,
            SUM(CASE WHEN event_type = 2 THEN 1 ELSE 0 END) as type2_count
        FROM dbo.ai_transfer_recommendations
        WHERE batch_id = %s
          AND status IN ('Pending', 'Auto-Approved')
    """ + sku_filter

  cursor.execute(query, tuple(params))
  row = cursor.fetchone()

  # Get list of SKUs
  cursor.execute("""
        SELECT sku, base_urgency, event_type
        FROM dbo.ai_transfer_recommendations
        WHERE batch_id = %s
          AND status IN ('Pending', 'Auto-Approved')
    """ + sku_filter + """
        ORDER BY
            CASE base_urgency
                WHEN 'CRITICAL' THEN 1
                WHEN 'HIGH' THEN 2
                ELSE 3
            END,
            event_type
    """, tuple(params))

  skus = [r['sku'] for r in cursor.fetchall()]

  return {
    'total_count': row['total_count'] or 0,
    'critical_count': row['critical_count'] or 0,
    'high_count': row['high_count'] or 0,
    'type1_count': row['type1_count'] or 0,
    'type2_count': row['type2_count'] or 0,
    'skus': skus,
    'batch_id': batch_id
  }


# =============================================================================
# NOTIFICATION FUNCTIONS
# =============================================================================

def build_new_recommendations_message(summary: Dict[str, Any],
                                      dashboard_url: Optional[str] = None) -> str:
  """
    Build SMS message for new transfer recommendations.

    Design notes (SMS encoding):
      - GSM-7 encoding = 160 chars per segment; UCS-2 = 70 chars per segment.
      - Any non-GSM character (emoji, curly quote, en-dash, etc.) forces the
        whole message into UCS-2 and more-than-doubles the segment count.
      - Multi-segment messages can be mangled by carrier/provider URL
        scanners (e.g. checkurl.ai wrapping), so this template sticks to
        plain GSM-7 characters only.
      - The dashboard URL is placed on its own line near the top so it
        lands inside segment 1 even if trailing content overflows.

    Args:
        summary: Dict from get_new_recommendations_summary
        dashboard_url: Optional URL to the dashboard

    Returns:
        Formatted SMS message (GSM-7 safe)
    """
  total = summary['total_count']
  critical = summary['critical_count']
  high = summary['high_count']

  lines = []

  # Main alert line (plural-aware, ASCII only)
  rec_word = "recommendation" if total == 1 else "recommendations"
  lines.append(f"Titan AI: {total} new transfer {rec_word} for review.")

  # Dashboard link near the top so it stays in segment 1
  if dashboard_url:
    lines.append(dashboard_url)

  # Urgency breakdown
  if critical > 0 or high > 0:
    urgency_parts = []
    if critical > 0:
      urgency_parts.append(f"{critical} CRITICAL")
    if high > 0:
      urgency_parts.append(f"{high} HIGH")
    lines.append(", ".join(urgency_parts))

  # Event type breakdown
  type1 = summary['type1_count']
  type2 = summary['type2_count']
  if type1 > 0 and type2 > 0:
    lines.append(f"{type1} Min/Max, {type2} Sales Order")

  # Sample SKUs (first 3)
  skus = summary['skus'][:3]
  if skus:
    sku_line = f"SKUs: {', '.join(skus)}"
    if len(summary['skus']) > 3:
      sku_line += f" (+{len(summary['skus']) - 3} more)"
    lines.append(sku_line)

  return "\n".join(lines)


def send_new_recommendations_notification(batch_id: str,
                                          dashboard_url: Optional[str] = None,
                                          test_phone: Optional[str] = None,
                                          only_skus: Optional[List[str]] = None) -> Dict[str, Any]:
  """
    Send SMS notification to Hub Managers about new transfer recommendations

    Args:
        batch_id: The batch_id of the new recommendations
        dashboard_url: Optional URL to include in message
        test_phone: Optional - if provided, sends only to this number (for testing)
        only_skus: SKUs inserted this batch (5.34.3) - the text names only these

    Returns:
        Dict with results for each recipient
    """
  if not SMSConfig.is_sms_enabled():
    return {
      'success': False,
      'reason': 'SMS disabled via SMS_ENABLED secret',
      'sent_count': 0
    }

  conn = get_db_connection()

  try:
    # Get recommendation summary
    summary = get_new_recommendations_summary(conn, batch_id, only_skus=only_skus)

    if summary['total_count'] == 0:
      return {
        'success': True,
        'reason': 'No new recommendations to notify about',
        'sent_count': 0
      }

      # Build message
    message = build_new_recommendations_message(summary, dashboard_url)

    # Get recipients
    if test_phone:
      recipients = [{'phone': test_phone, 'name': 'Test Recipient', 'role': 'Test'}]
    else:
      recipients = get_hub_manager_phones(conn, 'NEW_RECOMMENDATIONS')

    if not recipients:
      return {
        'success': False,
        'reason': 'No Hub Manager phone numbers configured',
        'sent_count': 0
      }

      # Initialize Twilio client
    sms_client = TwilioSMSClient()

    # Send to each recipient
    results = {
      'success': True,
      'sent_count': 0,
      'failed_count': 0,
      'recipients': [],
      'batch_id': batch_id,
      'message_preview': message[:100] + '...' if len(message) > 100 else message
    }

    for recipient in recipients:
      phone = recipient['phone']
      name = recipient['name']

      result = sms_client.send_sms(phone, message)
      result['recipient_name'] = name

      # Log to database
      log_sms_notification(
        conn,
        result,
        'NEW_RECOMMENDATIONS',
        batch_id,
        summary['skus'][:5]
      )

      if result['success']:
        results['sent_count'] += 1
      else:
        results['failed_count'] += 1
        results['success'] = False  # Mark overall as failed if any fail

      results['recipients'].append(result)

    return results

  finally:
    conn.close()


# =============================================================================
# DELIVERY DISCREPANCY (v5.8 - Option C)
# =============================================================================

def build_delivery_discrepancy_message(trip_id: int,
                                       discrepancies: List[Dict[str, Any]],
                                       driver_name: Optional[str] = None,
                                       batch_id: Optional[str] = None,
                                       trip_number: Optional[int] = None
                                      ) -> str:
  """
  Build a GSM-7-safe SMS body for a delivery discrepancy.

  Follows the same encoding rules as build_new_recommendations_message
  (see that function's docstring): plain ASCII, short line lengths, key
  content near the top so a long message still lands the useful bits in
  the first segment.

  Example output:

      Titan: delivery mismatch trip 20260422-31039ad1 #2
      YS58I0400: planned 50, delivered 30
      YT12K0222: planned 12, delivered 0 - damaged box
      Driver: J Smith  2026-04-22 14:02

  Args:
      trip_id:       transfer_trips.trip_id being confirmed
      discrepancies: list of dicts with keys 'sku', 'planned_qty',
                     'delivered_qty', 'note' (driver's free-text note)
      driver_name:   human-readable name for the last line
      batch_id:      optional batch_id for a friendlier header
      trip_number:   optional trip_number for a friendlier header

  Returns:
      Formatted SMS message (GSM-7 safe)
  """
  lines = []

  # Header - include batch/trip if we have them, else just the trip_id
  if batch_id and trip_number is not None:
    header = f"Titan: delivery mismatch trip {batch_id} #{trip_number}"
  else:
    header = f"Titan: delivery mismatch trip_id={trip_id}"
  lines.append(header)

  # Per-SKU discrepancies, capped so we stay inside reasonable segment
  # counts even if the driver flags every line.
  MAX_LINES = 5
  shown = discrepancies[:MAX_LINES]
  for d in shown:
    sku       = d.get('sku', '?')
    planned   = d.get('planned_qty', 0)
    delivered = d.get('delivered_qty', 0)
    note      = (d.get('note') or '').strip()
    body = f"{sku}: planned {planned}, delivered {delivered}"
    if note:
      body += f" - {note}"
    lines.append(body)

  if len(discrepancies) > MAX_LINES:
    lines.append(f"(+{len(discrepancies) - MAX_LINES} more)")

  # Driver signature line with timestamp
  stamp = datetime.now().strftime('%Y-%m-%d %H:%M')
  if driver_name:
    lines.append(f"Driver: {driver_name}  {stamp}")
  else:
    lines.append(f"Time: {stamp}")

  return "\n".join(lines)


@anvil.server.callable
def send_delivery_discrepancy_notification(trip_id: int,
                                           discrepancies: List[Dict[str, Any]],
                                           driver_name: Optional[str] = None,
                                           test_phone: Optional[str] = None
                                          ) -> Dict[str, Any]:
  """
  Send a DELIVERY_DISCREPANCY SMS to every Hub Manager subscribed to
  that notification type in sms_notification_contacts.

  Mirrors send_new_recommendations_notification in structure:
    - SMSConfig.is_sms_enabled() gate
    - get_hub_manager_phones(conn, 'DELIVERY_DISCREPANCY')
    - TwilioSMSClient().send_sms(phone, message)
    - log_sms_notification(conn, result, 'DELIVERY_DISCREPANCY',
                           related_batch_id=<batch_id>,
                           related_skus=[<skus>])

  Called from driver_delivery.submit_delivery_confirmation AFTER the DB
  commit, so an SMS failure can never roll back a valid delivery.

  Args:
      trip_id:       transfer_trips.trip_id being reported
      discrepancies: per-SKU dicts (see build_delivery_discrepancy_message)
      driver_name:   optional, included in the SMS signature line
      test_phone:    optional override - sends only to this number for tests

  Returns:
      Same shape as send_new_recommendations_notification's return.
  """
  if not SMSConfig.is_sms_enabled():
    return {
      'success': False,
      'reason': 'SMS disabled via SMS_ENABLED secret',
      'sent_count': 0,
    }

  if not discrepancies:
    return {
      'success': True,
      'reason': 'No discrepancies - nothing to notify',
      'sent_count': 0,
    }

  conn = get_db_connection()

  try:
    # Pull a bit of trip context so the SMS header is human-friendly
    cursor = conn.cursor()
    cursor.execute("""
        SELECT batch_id, trip_number
        FROM dbo.transfer_trips
        WHERE trip_id = %s
    """, (trip_id,))
    trip_row = cursor.fetchone()
    batch_id    = trip_row['batch_id']    if trip_row else None
    trip_number = trip_row['trip_number'] if trip_row else None

    message = build_delivery_discrepancy_message(
      trip_id=trip_id,
      discrepancies=discrepancies,
      driver_name=driver_name,
      batch_id=batch_id,
      trip_number=trip_number,
    )

    if test_phone:
      recipients = [{'phone': test_phone, 'name': 'Test Recipient',
                     'role': 'Test'}]
    else:
      recipients = get_hub_manager_phones(conn, 'DELIVERY_DISCREPANCY')

    if not recipients:
      return {
        'success': False,
        'reason': 'No Hub Manager phone numbers configured for '
        'DELIVERY_DISCREPANCY',
        'sent_count': 0,
      }

    sms_client = TwilioSMSClient()

    skus_for_log = [d.get('sku', '?') for d in discrepancies if d.get('sku')]

    results = {
      'success': True,
      'sent_count': 0,
      'failed_count': 0,
      'recipients': [],
      'trip_id': trip_id,
      'batch_id': batch_id,
      'message_preview': message[:100] + '...' if len(message) > 100 else message,
    }

    for recipient in recipients:
      phone = recipient['phone']
      name  = recipient['name']

      result = sms_client.send_sms(phone, message)
      result['recipient_name'] = name

      log_sms_notification(
        conn,
        result,
        'DELIVERY_DISCREPANCY',
        related_batch_id=batch_id,
        related_skus=skus_for_log[:5],
      )

      if result['success']:
        results['sent_count'] += 1
      else:
        results['failed_count'] += 1
        results['success'] = False

      results['recipients'].append(result)

    return results

  finally:
    conn.close()


# =============================================================================
# DELIVERY RECEIPT (every delivery, not just discrepancies)
# =============================================================================

def build_delivery_receipt_message(trip_id: int,
                                   assignments_delivered: int,
                                   discrepancy_count: int = 0,
                                   driver_name: Optional[str] = None,
                                   batch_id: Optional[str] = None,
                                   trip_number: Optional[int] = None
                                  ) -> str:
  """
  Build a GSM-7-safe SMS body for a delivery receipt.

  Sent on EVERY delivery so the Hub Manager has a running record of what
  the driver is doing. Kept short (target one segment, < 160 chars) so
  the Hub Manager can skim the whole history at a glance.

  Example output (clean delivery):

      Titan: delivery complete trip 20260422-31039ad1 #2
      12 SKUs delivered, all match planned.
      Driver: J Smith  2026-04-22 14:02

  Example output (delivery with discrepancies):

      Titan: delivery complete trip 20260422-31039ad1 #2
      12 SKUs delivered, 2 with variances.
      Driver: J Smith  2026-04-22 14:02

  Note: when discrepancy_count > 0, the Hub Manager will ALSO receive a
  DELIVERY_DISCREPANCY SMS with per-SKU detail — we don't duplicate that
  detail here. This message is just the "it happened" receipt.

  Args:
      trip_id:                transfer_trips.trip_id
      assignments_delivered:  how many line items got marked delivered
      discrepancy_count:      0 for a clean delivery; >0 for a mixed one
      driver_name:            human-readable name for the signature line
      batch_id:               optional, for a friendlier header
      trip_number:            optional, for a friendlier header

  Returns:
      Formatted SMS message (GSM-7 safe)
  """
  lines = []

  if batch_id and trip_number is not None:
    header = f"Titan: delivery complete trip {batch_id} #{trip_number}"
  else:
    header = f"Titan: delivery complete trip_id={trip_id}"
  lines.append(header)

  if discrepancy_count > 0:
    lines.append(
      f"{assignments_delivered} SKUs delivered, "
      f"{discrepancy_count} with variances."
    )
  else:
    lines.append(
      f"{assignments_delivered} SKUs delivered, all match planned."
    )

  stamp = datetime.now().strftime('%Y-%m-%d %H:%M')
  if driver_name:
    lines.append(f"Driver: {driver_name}  {stamp}")
  else:
    lines.append(f"Time: {stamp}")

  return "\n".join(lines)


@anvil.server.callable
def send_delivery_receipt_notification(trip_id: int,
                                       assignments_delivered: int,
                                       discrepancy_count: int = 0,
                                       driver_name: Optional[str] = None,
                                       test_phone: Optional[str] = None
                                      ) -> Dict[str, Any]:
  """
  Send a DELIVERY_RECEIPT SMS to every Hub Manager subscribed to that
  notification type in sms_notification_contacts.

  Sent on EVERY successful delivery (not just divergent ones). Mirrors
  send_delivery_discrepancy_notification in structure:
    - SMSConfig.is_sms_enabled() gate
    - get_hub_manager_phones(conn, 'DELIVERY_RECEIPT')
    - TwilioSMSClient().send_sms(phone, message)
    - log_sms_notification(conn, result, 'DELIVERY_RECEIPT', ...)

  Called from driver_delivery.submit_delivery_confirmation AFTER the DB
  commit, so an SMS failure can never roll back a valid delivery.

  If the Hub Manager has NOT subscribed to DELIVERY_RECEIPT in
  sms_notification_contacts.notification_types, the function returns
  sent_count=0 with a descriptive reason and callers treat that as
  "SMS intentionally skipped" — not an error.

  Args:
      trip_id:                transfer_trips.trip_id
      assignments_delivered:  rowcount from the delivery update
      discrepancy_count:      0 for clean deliveries; >0 for divergent
      driver_name:            optional, goes on the signature line
      test_phone:             optional override for tests

  Returns:
      Same shape as send_delivery_discrepancy_notification's return.
  """
  if not SMSConfig.is_sms_enabled():
    return {
      'success': False,
      'reason': 'SMS disabled via SMS_ENABLED secret',
      'sent_count': 0,
    }

  conn = get_db_connection()

  try:
    # Pull trip context for a human-friendly header
    cursor = conn.cursor()
    cursor.execute("""
        SELECT batch_id, trip_number
        FROM dbo.transfer_trips
        WHERE trip_id = %s
    """, (trip_id,))
    trip_row = cursor.fetchone()
    batch_id    = trip_row['batch_id']    if trip_row else None
    trip_number = trip_row['trip_number'] if trip_row else None

    message = build_delivery_receipt_message(
      trip_id=trip_id,
      assignments_delivered=assignments_delivered,
      discrepancy_count=discrepancy_count,
      driver_name=driver_name,
      batch_id=batch_id,
      trip_number=trip_number,
    )

    if test_phone:
      recipients = [{'phone': test_phone, 'name': 'Test Recipient',
                     'role': 'Test'}]
    else:
      recipients = get_hub_manager_phones(conn, 'DELIVERY_RECEIPT')

    if not recipients:
      return {
        'success': True,   # not an error - just no one subscribed
        'reason': 'No Hub Manager phone numbers configured for '
        'DELIVERY_RECEIPT',
        'sent_count': 0,
        'trip_id': trip_id,
        'batch_id': batch_id,
      }

    sms_client = TwilioSMSClient()

    results = {
      'success': True,
      'sent_count': 0,
      'failed_count': 0,
      'recipients': [],
      'trip_id': trip_id,
      'batch_id': batch_id,
      'message_preview': message[:100] + '...' if len(message) > 100 else message,
    }

    for recipient in recipients:
      phone = recipient['phone']
      name  = recipient['name']

      result = sms_client.send_sms(phone, message)
      result['recipient_name'] = name

      log_sms_notification(
        conn,
        result,
        'DELIVERY_RECEIPT',
        related_batch_id=batch_id,
        related_skus=None,
      )

      if result['success']:
        results['sent_count'] += 1
      else:
        results['failed_count'] += 1
        results['success'] = False

      results['recipients'].append(result)

    return results

  finally:
    conn.close()


# =============================================================================
# ANVIL SERVER FUNCTIONS
# =============================================================================

@anvil.server.callable
def send_test_sms(to_number: str, message: str = None) -> Dict[str, Any]:
  """
    Anvil-callable function to send a test SMS

    Args:
        to_number: Phone number to send to (E.164 format or 10-digit)
        message: Optional custom message (default: test message)

    Returns:
        Dict with send result
    """
  if message is None:
    message = (
      f"🧪 Titan AI Test Message\n"
      f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
      f"SMS integration is working!"
    )

  try:
    client = TwilioSMSClient()
    result = client.send_sms(to_number, message)

    # Log to database
    conn = get_db_connection()
    try:
      log_sms_notification(conn, result, 'TEST_MESSAGE')
    finally:
      conn.close()

    return result

  except Exception as e:
    return {
      'success': False,
      'error_message': str(e)
    }


@anvil.server.callable
def send_test_sms_from_db(send_to_all: bool = True) -> Dict[str, Any]:
  """
    Anvil-callable function to send a test SMS using phone numbers from database.
    Reads from sms_notification_contacts table.

    Args:
        send_to_all: If True (default), sends to all active contacts in sms_notification_contacts.
                     If False, sends only to the first Hub Manager.

    Returns:
        Dict with:
          - success: bool
          - sent: number of messages sent
          - recipients: list of names/phones that received the test
          - errors: list of any errors
          - message: summary message
    """
  result = {
    'success': False,
    'sent': 0,
    'recipients': [],
    'errors': [],
    'message': ''
  }

  try:
    # Check if Twilio is configured
    if not TWILIO_AVAILABLE:
      result['errors'].append('Twilio library not installed')
      result['message'] = 'Twilio library not installed. Run: pip install twilio'
      return result

      # Get database connection
    conn = get_db_connection()

    try:
      # Get recipients from database
      if send_to_all:
        # Send to ALL active contacts regardless of role or notification type
        cursor = conn.cursor()
        cursor.execute("""
          SELECT contact_name, phone_number, role
          FROM dbo.sms_notification_contacts
          WHERE is_active = 1
          ORDER BY contact_name
        """)
        rows = cursor.fetchall()
        recipients = [{'phone': str(r['phone_number']).strip(),
                       'name': r['contact_name'],
                       'role': r['role']} for r in rows]
      else:
        # Send only to first Hub Manager (quick single-recipient test)
        recipients = get_hub_manager_phones(conn, 'NEW_RECOMMENDATIONS')[:1]

      if not recipients:
        result['errors'].append('No active contacts found in sms_notification_contacts table')
        result['message'] = 'No active contacts found. Add contacts to sms_notification_contacts table.'
        return result

        # Build test message
      test_message = (
        f"🧪 Titan AI Test Message\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"SMS integration is working!\n"
        f"Sending to {len(recipients)} recipient(s)"
      )

      # Initialize Twilio client
      client = TwilioSMSClient()

      # Send to each recipient
      for manager in recipients:
        phone = manager['phone']
        name = manager.get('name', 'Unknown')

        try:
          send_result = client.send_sms(phone, test_message)

          if send_result.get('success'):
            result['sent'] += 1
            result['recipients'].append({
              'name': name,
              'phone': phone,
              'message_sid': send_result.get('message_sid')
            })

            # Log success
            log_sms_notification(conn, send_result, 'TEST_MESSAGE_FROM_DB')
          else:
            result['errors'].append(f"{name} ({phone}): {send_result.get('error_message', 'Unknown error')}")

        except Exception as e:
          result['errors'].append(f"{name} ({phone}): {str(e)}")

          # Build summary message
      if result['sent'] > 0:
        result['success'] = True
        names = [r['name'] for r in result['recipients']]
        result['message'] = f"Test SMS sent to: {', '.join(names)}"
      else:
        result['message'] = 'Failed to send test SMS. Check errors.'

    finally:
      conn.close()

  except Exception as e:
    result['errors'].append(str(e))
    result['message'] = f'Error: {str(e)}'

  return result


@anvil.server.callable
def get_sms_contacts() -> Dict[str, Any]:
  """
    Anvil-callable function to get all SMS contacts from the database.
    Useful for displaying in the UI.

    Returns:
        Dict with:
          - success: bool
          - contacts: list of contact dicts
          - message: summary or error message
    """
  result = {
    'success': False,
    'contacts': [],
    'message': ''
  }

  try:
    conn = get_db_connection()

    try:
      cursor = conn.cursor()

      query = """
                SELECT
                    contact_id,
                    contact_name,
                    phone_number,
                    role,
                    notification_types,
                    is_active,
                    notes,
                    created_date,
                    last_updated
                FROM dbo.sms_notification_contacts
                ORDER BY is_active DESC, role, contact_name
            """

      cursor.execute(query)
      rows = cursor.fetchall()

      for row in rows:
        result['contacts'].append({
          'contact_id': row['contact_id'],
          'name': row['contact_name'],
          'phone': row['phone_number'],
          'role': row['role'],
          'notification_types': row['notification_types'],
          'is_active': bool(row['is_active']),
          'notes': row['notes'],
          'created_date': row['created_date'].isoformat() if row['created_date'] else None,
          'last_updated': row['last_updated'].isoformat() if row['last_updated'] else None
        })

      result['success'] = True
      result['message'] = f"Found {len(result['contacts'])} contacts"

    finally:
      conn.close()

  except Exception as e:
    result['message'] = f'Error: {str(e)}'

  return result


@anvil.server.callable
def notify_new_recommendations(batch_id: str,
                               dashboard_url: str = None,
                               test_mode: bool = False) -> Dict[str, Any]:
  """
    Anvil-callable function to send notifications about new recommendations

    Args:
        batch_id: The batch_id of the recommendations
        dashboard_url: Optional dashboard URL
        test_mode: If True, sends to test number only

    Returns:
        Dict with notification results
    """
  # Get test phone from secrets if in test mode
  test_phone = None
  if test_mode:
    try:
      test_phone = anvil.secrets.get_secret('SMS_TEST_PHONE')
    except:
      pass

  return send_new_recommendations_notification(
    batch_id=batch_id,
    dashboard_url=dashboard_url,
    test_phone=test_phone
  )


@anvil.server.callable
def get_sms_status() -> Dict[str, Any]:
  """
    Anvil-callable function to check SMS configuration status

    Returns:
        Dict with configuration status and any issues
    """
  status = {
    'twilio_available': TWILIO_AVAILABLE,
    'sms_enabled': SMSConfig.is_sms_enabled(),
    'test_mode': SMSConfig.is_test_mode(),
    'credentials_configured': False,
    'hub_managers_configured': False,
    'hub_manager_count': 0,
    'issues': []
  }

  # Check credentials
  try:
    creds = SMSConfig.get_twilio_credentials()
    status['credentials_configured'] = all([
      creds.get('account_sid'),
      creds.get('auth_token'),
      creds.get('phone_number')
    ])

    if not creds.get('account_sid'):
      status['issues'].append('Missing TWILIO_ACCOUNT_SID secret')
    if not creds.get('auth_token'):
      status['issues'].append('Missing TWILIO_AUTH_TOKEN secret')
    if not creds.get('phone_number'):
      status['issues'].append('Missing TWILIO_PHONE_NUMBER secret')

  except Exception as e:
    status['issues'].append(f'Error reading credentials: {str(e)}')

    # Check Hub Manager phones from sms_notification_contacts table
  try:
    conn = get_db_connection()
    managers = get_hub_manager_phones(conn)
    conn.close()

    status['hub_manager_count'] = len(managers)
    status['hub_managers_configured'] = len(managers) > 0

    if not managers:
      status['issues'].append('No active Hub_Manager contacts in sms_notification_contacts table')

  except Exception as e:
    status['issues'].append(f'Error checking Hub Manager phones: {str(e)}')

  return status


# =============================================================================
# INTEGRATION HELPER FOR transfer_ai_review.py
# =============================================================================

def should_send_notification(results: Dict) -> bool:
  """
    Determine if we should send an SMS notification based on review results

    Args:
        results: The results dict from run_transfer_review()

    Returns:
        True if notification should be sent
    """
  # 5.34.3 (2026-09-06): NEW rows only. 'ai_reviewed' fired on every batch
  # (the AI re-reviews every open rec each run) - that was the 15-minute
  # text storm. Auto-approved criticals are inserted rows, so they are
  # covered by 'inserted'.
  return results.get('inserted', 0) > 0


def integrate_sms_notification(batch_id: str, results: Dict,
                               dashboard_url: str = None) -> Optional[Dict]:
  """
    Integration function to call from transfer_ai_review.py

    Add this call at the end of run_transfer_review():

        from sms_notifications import integrate_sms_notification
        sms_result = integrate_sms_notification(batch_id, results, dashboard_url)
        results['sms_notification'] = sms_result

    Args:
        batch_id: Current batch_id
        results: Results dict from the review
        dashboard_url: URL to the Anvil dashboard

    Returns:
        Dict with SMS notification results, or None if not sent
    """
  if not should_send_notification(results):
    return None

  if not SMSConfig.is_sms_enabled():
    return {'skipped': True, 'reason': 'SMS disabled'}

  try:
    return send_new_recommendations_notification(
      batch_id=batch_id,
      dashboard_url=dashboard_url,
      only_skus=results.get('inserted_skus')   # 5.34.3: name only what is new
    )
  except Exception as e:
    return {
      'success': False,
      'error': str(e)
    }


# =============================================================================
# STANDALONE TESTING
# =============================================================================

if __name__ == "__main__":
  """
    Standalone test - requires environment variables instead of Anvil Secrets

    Set these environment variables for local testing:
        TWILIO_ACCOUNT_SID=your_sid
        TWILIO_AUTH_TOKEN=your_token
        TWILIO_PHONE_NUMBER=+1XXXXXXXXXX
    """
  import os

  # Override SMSConfig for local testing
  class LocalTestConfig:
    @staticmethod
    def get_twilio_credentials():
      return {
        'account_sid': os.environ.get('TWILIO_ACCOUNT_SID'),
        'auth_token': os.environ.get('TWILIO_AUTH_TOKEN'),
        'phone_number': os.environ.get('TWILIO_PHONE_NUMBER')
      }

    @staticmethod
    def is_sms_enabled(conn=None):
      return True

    @staticmethod
    def is_test_mode():
      return os.environ.get('SMS_TEST_MODE', 'false').lower() == 'true'

    # Replace config
  SMSConfig.get_twilio_credentials = LocalTestConfig.get_twilio_credentials
  SMSConfig.is_sms_enabled = LocalTestConfig.is_sms_enabled
  SMSConfig.is_test_mode = LocalTestConfig.is_test_mode

  print("="*60)
  print("SMS Notifications - Local Test")
  print("="*60)

  # Check configuration
  creds = SMSConfig.get_twilio_credentials()
  print(f"\nAccount SID: {'Configured' if creds.get('account_sid') else 'MISSING'}")
  print(f"Auth Token: {'Configured' if creds.get('auth_token') else 'MISSING'}")
  print(f"Phone Number: {creds.get('phone_number') or 'MISSING'}")
  print(f"Test Mode: {SMSConfig.is_test_mode()}")

  if all(creds.values()):
    test_phone = creds['phone_number']  # Same number for testing
    test_message = (
      f"🧪 Titan AI Test\n"
      f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
      f"SMS integration working!"
    )

    print(f"\nSending test to: {test_phone}")
    print(f"Message: {test_message}")

    try:
      client = TwilioSMSClient()
      result = client.send_sms(test_phone, test_message)

      print(f"\nResult:")
      for key, value in result.items():
        print(f"  {key}: {value}")

    except Exception as e:
      print(f"\nError: {e}")
  else:
    print("\n⚠️  Missing credentials - cannot test")
    print("Set environment variables:")
    print("  export TWILIO_ACCOUNT_SID=your_sid")
    print("  export TWILIO_AUTH_TOKEN=your_token")
    print("  export TWILIO_PHONE_NUMBER=+1XXXXXXXXXX")