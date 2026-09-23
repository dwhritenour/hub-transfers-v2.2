"""
SMS DIAGNOSTIC - Add this to your Anvil server code temporarily.
Call from the Anvil shell:  anvil.server.call('diagnose_sms')

This tests every single step in the SMS notification chain
and reports exactly where it fails.
"""

import anvil.server
import anvil.secrets

@anvil.server.callable
def diagnose_sms():
  """
  Run this from the Anvil Python shell or a button click.
  It will return a step-by-step report of what works and what doesn't.
  """
  report = []

  def log(step, status, detail=""):
    entry = f"{'✅' if status == 'PASS' else '❌' if status == 'FAIL' else '⚠️'} Step {step}: {detail}"
    report.append(entry)
    print(entry)

  # =========================================================================
  # STEP 1: Can we import sms_notifications?
  # =========================================================================
  try:
    from . import sms_notifications
    log(1, "PASS", "sms_notifications imported successfully")
  except ImportError as e:
    log(1, "FAIL", f"ImportError: {e}")
    return '\n'.join(report)
  except Exception as e:
    log(1, "FAIL", f"Unexpected error importing sms_notifications: {e}")
    return '\n'.join(report)

  # =========================================================================
  # STEP 2: Is Twilio available?
  # =========================================================================
  if sms_notifications.TWILIO_AVAILABLE:
    log(2, "PASS", "Twilio library is installed")
  else:
    log(2, "FAIL", "Twilio library NOT installed (pip install twilio)")
    return '\n'.join(report)

  # =========================================================================
  # STEP 3: Is SMS enabled?
  # =========================================================================
  try:
    enabled = sms_notifications.SMSConfig.is_sms_enabled()
    if enabled:
      log(3, "PASS", "SMS is enabled")
    else:
      log(3, "FAIL", "SMS is DISABLED (check SMS_ENABLED secret)")
      return '\n'.join(report)
  except Exception as e:
    log(3, "FAIL", f"Error checking SMS enabled: {e}")

  # =========================================================================
  # STEP 4: Are Twilio credentials configured?
  # =========================================================================
  try:
    creds = sms_notifications.SMSConfig.get_twilio_credentials()
    sid_ok = bool(creds.get('account_sid'))
    token_ok = bool(creds.get('auth_token'))
    phone_ok = bool(creds.get('phone_number'))

    if sid_ok and token_ok and phone_ok:
      log(4, "PASS", f"Twilio credentials configured (from: {creds['phone_number']})")
    else:
      missing = []
      if not sid_ok: missing.append('TWILIO_ACCOUNT_SID')
      if not token_ok: missing.append('TWILIO_AUTH_TOKEN')
      if not phone_ok: missing.append('TWILIO_PHONE_NUMBER')
      log(4, "FAIL", f"Missing secrets: {', '.join(missing)}")
      return '\n'.join(report)
  except Exception as e:
    log(4, "FAIL", f"Error reading Twilio credentials: {e}")
    return '\n'.join(report)

  # =========================================================================
  # STEP 5: Can we get a database connection via sms_notifications?
  # =========================================================================
  try:
    conn = sms_notifications.get_db_connection()
    log(5, "PASS", "Database connection from sms_notifications.get_db_connection() works")
  except Exception as e:
    log(5, "FAIL", f"get_db_connection() failed: {e}")
    return '\n'.join(report)

  # =========================================================================
  # STEP 6: What does get_hub_manager_phones return?
  # This is the PRODUCTION query (filters by role and notification_type)
  # =========================================================================
  try:
    managers = sms_notifications.get_hub_manager_phones(conn, 'NEW_RECOMMENDATIONS')
    if managers:
      for m in managers:
        log(6, "PASS", f"Found manager: {m['name']} ({m['phone']}) role={m['role']}")
    else:
      log(6, "FAIL", "get_hub_manager_phones returned EMPTY LIST")

      # Run the raw query to show what's actually in the table
      cursor = conn.cursor()
      cursor.execute("""
        SELECT contact_name, phone_number, role, notification_types, is_active
        FROM dbo.sms_notification_contacts
        ORDER BY contact_name
      """)
      rows = cursor.fetchall()
      if rows:
        log("6b", "WARN", f"Raw table has {len(rows)} rows. Showing each:")
        for r in rows:
          log("6b", "WARN", 
              f"  name={r['contact_name']}, role='{r['role']}', "
              f"is_active={r['is_active']}, "
              f"notification_types='{r['notification_types']}'")
        log("6b", "WARN", 
            "REQUIRED: role must be exactly 'Hub_Manager' (case-sensitive), "
            "is_active must be 1, and notification_types must contain "
            "'NEW_RECOMMENDATIONS' or be NULL")
      else:
        log("6b", "FAIL", "sms_notification_contacts table is EMPTY")
      conn.close()
      return '\n'.join(report)
  except Exception as e:
    log(6, "FAIL", f"Error querying managers: {e}")
    conn.close()
    return '\n'.join(report)

  # =========================================================================
  # STEP 7: Find the most recent batch_id in ai_transfer_recommendations
  # =========================================================================
  try:
    cursor = conn.cursor()
    cursor.execute("""
      SELECT TOP 1 batch_id, COUNT(*) as cnt, 
             MIN(recommendation_date) as earliest,
             MAX(recommendation_date) as latest
      FROM dbo.ai_transfer_recommendations
      WHERE status IN ('Pending', 'Auto-Approved')
      GROUP BY batch_id
      ORDER BY MAX(recommendation_date) DESC
    """)
    row = cursor.fetchone()
    if row:
      log(7, "PASS", f"Most recent batch: {row['batch_id']} ({row['cnt']} Pending/Auto-Approved recs, dates: {row['earliest']} to {row['latest']})")
      test_batch_id = row['batch_id']
    else:
      log(7, "FAIL", "No Pending/Auto-Approved recommendations found in table")

      # Check what IS in the table
      cursor.execute("""
        SELECT status, COUNT(*) as cnt 
        FROM dbo.ai_transfer_recommendations 
        GROUP BY status
      """)
      for r in cursor.fetchall():
        log("7b", "WARN", f"  status='{r['status']}' count={r['cnt']}")
      conn.close()
      return '\n'.join(report)
  except Exception as e:
    log(7, "FAIL", f"Error querying recommendations: {e}")
    conn.close()
    return '\n'.join(report)

  # =========================================================================
  # STEP 8: Does get_new_recommendations_summary find the batch?
  # =========================================================================
  try:
    summary = sms_notifications.get_new_recommendations_summary(conn, test_batch_id)
    if summary['total_count'] > 0:
      log(8, "PASS", f"Summary for batch {test_batch_id}: {summary['total_count']} recs, "
          f"CRITICAL={summary['critical_count']}, HIGH={summary['high_count']}, "
          f"SKUs={summary['skus'][:5]}")
    else:
      log(8, "FAIL", f"get_new_recommendations_summary returned total_count=0 for batch {test_batch_id}")
      log("8b", "WARN", "This means the function opens its own connection and can't see the batch data, "
          "OR the batch_id format doesn't match")
      conn.close()
      return '\n'.join(report)
  except Exception as e:
    log(8, "FAIL", f"Error in get_new_recommendations_summary: {e}")
    conn.close()
    return '\n'.join(report)

  # =========================================================================
  # STEP 9: Can we create a TwilioSMSClient?
  # =========================================================================
  try:
    client = sms_notifications.TwilioSMSClient()
    log(9, "PASS", f"TwilioSMSClient created (test_mode={client.test_mode})")
  except Exception as e:
    log(9, "FAIL", f"TwilioSMSClient creation failed: {e}")
    conn.close()
    return '\n'.join(report)

  # =========================================================================
  # STEP 10: Build the message
  # =========================================================================
  try:
    # Pull dashboard URL from global_parameters_text so admins can swap
    # it from the system_admin form without a code change. Fall back to
    # the bit.ly short link if the row is missing (keeps SMS in a single
    # GSM-7 segment and avoids checkurl.ai-style URL wrapping).
    from . import system_settings
    dashboard_url = system_settings.get_text_parameter(
      'dashboard_url', 'https://bit.ly/4txYx5B'
    )
    message = sms_notifications.build_new_recommendations_message(summary, dashboard_url)
    log(10, "PASS", f"Message built ({len(message)} chars, url={dashboard_url}): {message[:80]}...")
  except Exception as e:
    log(10, "FAIL", f"Error building message: {e}")

  conn.close()

  # =========================================================================
  # SUMMARY
  # =========================================================================
  report.append("")
  report.append("=" * 60)
  fails = [r for r in report if '❌' in r]
  if fails:
    report.append(f"DIAGNOSIS: {len(fails)} failure(s) found. Fix the first ❌.")
  else:
    report.append("DIAGNOSIS: All steps passed! The SMS chain should work.")
    report.append("If it still doesn't send during AI review, the issue is likely:")
    report.append("  - The ImportError at line 1221 silently swallowing an error")
    report.append("  - Or the gate condition (inserted/updated_with_changes) being 0")

  return '\n'.join(report)