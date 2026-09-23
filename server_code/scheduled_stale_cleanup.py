"""
Scheduled Stale Cleanup - Anvil Server Module
==============================================
Runs on an Anvil Scheduled Task to clean up stale transfer recommendations
and notify Hub Managers via SMS when recommendations are obsoleted.

Schedule: Run ~15 minutes after nexus_hourly EasyMorph sync
          (e.g., XX:15 every hour on weekdays)

Purpose:
  - Marks Pending/Paused recommendations as 'Obsolete' when the SKU
    no longer appears in vw_unified_transfer_detection with transfer_needed = 'Yes'
  - Sends SMS notification to Hub Managers so they know items were removed
  - Logs all actions to ai_agent_log for audit trail

Author: Dave Whritenour
Project: Year of Automation - Phase 1
Created: February 2026
"""

import anvil.server
from datetime import datetime

# Import centralized config and existing modules
from . import db_config
from .transfer_ai_review import mark_stale_recommendations_obsolete
from .sms_notifications import (
TwilioSMSClient,
SMSConfig,
get_hub_manager_phones,
log_sms_notification
)


# =============================================================================
# LOGGING
# =============================================================================

def log_action(conn, action_type, sku, old_value, new_value, reason):
  """Log action to ai_agent_log table"""
  query = """
    INSERT INTO dbo.ai_agent_log (
        action_type, sku, old_value, new_value, reason, action_timestamp, executed_by
    )
    VALUES (%s, %s, %s, %s, %s, GETDATE(), %s)
  """
  cursor = conn.cursor()
  cursor.execute(query, (action_type, sku, old_value, new_value, reason, 'scheduled_stale_cleanup'))
  conn.commit()


# =============================================================================
# SMS NOTIFICATION
# =============================================================================

def build_stale_cleanup_message(stale_result):
  """
  Build SMS message for stale recommendation cleanup notification.
  
  Args:
      stale_result: dict from mark_stale_recommendations_obsolete()
                    {'obsoleted': int, 'skus': list}
  
  Returns:
      str: Formatted SMS message
  """
  count = stale_result['obsoleted']
  skus = stale_result['skus']
  timestamp = datetime.now().strftime('%I:%M %p')

  msg = f"🔄 Titan Transfer Cleanup ({timestamp})\n"
  msg += f"{count} recommendation{'s' if count != 1 else ''} obsoleted:\n"

  # Show up to 5 SKUs in the message
  display_skus = skus[:5]
  for sku in display_skus:
    msg += f"  • {sku}\n"

  if len(skus) > 5:
    msg += f"  + {len(skus) - 5} more\n"

  msg += "Conditions changed - no longer need transfer."

  return msg


def notify_stale_cleanup(conn, stale_result, batch_id):
  """
  Send SMS notifications to Hub Managers about obsoleted recommendations.
  
  Args:
      conn: Database connection
      stale_result: dict from mark_stale_recommendations_obsolete()
      batch_id: Batch ID for logging
  
  Returns:
      dict: Notification results
  """
  notification_results = {
    'sms_sent': 0,
    'sms_failed': 0,
    'sms_skipped': False
  }

  # Check if SMS is enabled
  if not SMSConfig.is_sms_enabled():
    notification_results['sms_skipped'] = True
    print("[Stale Cleanup] SMS disabled - skipping notification")
    return notification_results

  # Get Hub Manager contacts
  # Use 'STALE_CLEANUP' notification type - managers can opt in/out
  managers = get_hub_manager_phones(conn, 'STALE_CLEANUP')

  if not managers:
    # Fall back to NEW_RECOMMENDATIONS contacts if no STALE_CLEANUP specific ones
    managers = get_hub_manager_phones(conn, 'NEW_RECOMMENDATIONS')

  if not managers:
    print("[Stale Cleanup] No SMS contacts found")
    return notification_results

  # Build message
  message = build_stale_cleanup_message(stale_result)

  # Send to each manager
  try:
    sms_client = TwilioSMSClient()

    for manager in managers:
      try:
        result = sms_client.send_sms(manager['phone'], message)

        # Log the SMS
        log_sms_notification(
          conn, result, 'STALE_CLEANUP',
          related_batch_id=batch_id,
          related_skus=stale_result['skus']
        )

        if result['success']:
          notification_results['sms_sent'] += 1
          print(f"[Stale Cleanup] SMS sent to {manager['name']}")
        else:
          notification_results['sms_failed'] += 1
          print(f"[Stale Cleanup] SMS failed for {manager['name']}: {result.get('error_message')}")

      except Exception as e:
        notification_results['sms_failed'] += 1
        print(f"[Stale Cleanup] SMS error for {manager['name']}: {str(e)}")

  except Exception as e:
    print(f"[Stale Cleanup] Twilio client error: {str(e)}")
    notification_results['sms_failed'] = len(managers)

  return notification_results


# =============================================================================
# MAIN SCHEDULED FUNCTION
# =============================================================================

@anvil.server.background_task
def scheduled_stale_cleanup():
  """
  Scheduled task to clean up stale transfer recommendations.
  
  Run this on an Anvil Scheduled Task, ideally ~15 minutes after 
  the nexus_hourly EasyMorph sync to ensure inventory data is fresh.
  
  Returns:
      dict: Summary of actions taken
  """
  print(f"[Stale Cleanup] Starting at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

  summary = {
    'status': 'success',
    'timestamp': datetime.now().isoformat(),
    'obsoleted': 0,
    'skus': [],
    'sms_sent': 0,
    'sms_failed': 0,
    'message': ''
  }

  conn = None
  try:
    conn = db_config.get_nexus_connection()
    batch_id = f"CLEANUP-{datetime.now().strftime('%Y%m%d-%H%M')}"

    # Run the stale recommendation cleanup
    stale_result = mark_stale_recommendations_obsolete(conn, batch_id)
    summary['obsoleted'] = stale_result['obsoleted']
    summary['skus'] = stale_result['skus']

    if stale_result['obsoleted'] > 0:
      # Log the cleanup action
      log_action(
        conn, 'Scheduled_Stale_Cleanup', None,
        f"Count: {stale_result['obsoleted']}",
        f"SKUs: {', '.join(stale_result['skus'][:10])}{'...' if len(stale_result['skus']) > 10 else ''}",
        f"Batch: {batch_id}"
      )

      # Send SMS notification
      sms_results = notify_stale_cleanup(conn, stale_result, batch_id)
      summary['sms_sent'] = sms_results['sms_sent']
      summary['sms_failed'] = sms_results['sms_failed']

      summary['message'] = (
        f"Obsoleted {stale_result['obsoleted']} stale recommendation(s). "
        f"SMS sent: {sms_results['sms_sent']}"
      )
      print(f"[Stale Cleanup] {summary['message']}")

    else:
      summary['message'] = "No stale recommendations found."
      print(f"[Stale Cleanup] {summary['message']}")

  except Exception as e:
    summary['status'] = 'error'
    summary['message'] = str(e)
    print(f"[Stale Cleanup] ERROR: {str(e)}")

  finally:
    if conn:
      try:
        conn.close()
      except:
        pass

  print(f"[Stale Cleanup] Complete at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
  return summary