"""
Classification AI Review - Anvil Background Task
=================================================
Ported from classification_ai_review.py command-line script for Anvil execution.
Uses centralized db_config for database and API connections.

RECOMMENDATION STATUS LIFECYCLE
===============================
Status Flow (non-UPDATE):
  Pending → Pending_Review → Approved or Rejected → (terminal)

Status Flow (UPDATE):
  Pending → (auto-approve) → Approved → (terminal)

Status Definitions:
  - Pending:         Initial AI recommendation awaiting preliminary review
  - Pending_Review:  AI flagged for human review (high-risk changes)
  - Approved:        AI or human approved the recommendation
  - Rejected:        Human rejected the recommendation (terminal)

CLASSIFICATION CHANGE TYPES
============================
  - PROMOTION:  SKU moving from B/C classification to A
  - DEMOTION:   SKU moving from A to B/C classification
  - UPDATE:     A Item metrics refreshed, no classification change

AUTO-APPROVAL RULES
====================
UPDATE records are automatically approved as low-risk:
  - Reasoning: 'Continuing A Item - metrics refreshed. Auto-approved (low risk).'
  - Status: Approved
  - Reviewed_by: AI_AGENT_AUTO
  - Confidence: High

For PROMOTION/DEMOTION, AI and thresholds determine if human review required:
  - Bouncer Detection: >2 classification changes in 12 months → Pending_Review
  - Premature Demotion: A-to-B demotion with <3 months tenure → Pending_Review
  - Low Confidence: confidence_level='Low' → Pending_Review
  - Otherwise: Auto-approve to Approved status

BOUNCER DEFINITION
==================
SKUs that flip between classifications too frequently are "bouncers" and need
human review. Threshold: > 2 changes in past 12 months indicates unstable demand.
"""

import anvil.server
from datetime import datetime, date
from decimal import Decimal
import json
import uuid
import time

# Import centralized configuration
from . import db_config


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_connection():
  """Get database connection from centralized config."""
  return db_config.get_nexus_connection()


def make_serializable(value):
  """Convert non-JSON-serializable types to serializable equivalents."""
  if value is None:
    return None
  elif isinstance(value, (datetime, date)):
    return value.isoformat()
  elif isinstance(value, Decimal):
    return float(value)
  elif isinstance(value, bytes):
    return value.decode('utf-8', errors='replace')
  else:
    return value


# =============================================================================
# DATABASE FUNCTIONS - Data Retrieval
# =============================================================================

def get_pending_recommendations(conn, include_updates=False):
  """
  Get all Pending recommendations from ai_classification_recommendations.

  By default excludes UPDATE records (metrics refreshes). Pass include_updates=True
  to include them (for bulk auto-approval processing).

  Returns list of dicts with: recommendation_id, sku, change_type, current_*,
  new_*, months_as_a_item, classification_changes_12mo, status
  """
  if include_updates:
    # Include all Pending records (for auto-approval batch)
    query = """
      SELECT
          recommendation_id, sku, change_type,
          current_classification, current_total_sold, current_running_pct,
          current_is_stocked,
          new_classification, new_total_sold, new_running_pct,
          new_is_stocked,
          months_as_a_item, classification_changes_12mo,
          recommendation_date, status,
          confidence_level, reasoning, reviewed_by, reviewed_date
      FROM dbo.ai_classification_recommendations
      WHERE status = 'Pending'
      ORDER BY recommendation_date DESC
    """
  else:
    # Exclude UPDATE records (only PROMOTION/DEMOTION for AI review)
    query = """
      SELECT
          recommendation_id, sku, change_type,
          current_classification, current_total_sold, current_running_pct,
          current_is_stocked,
          new_classification, new_total_sold, new_running_pct,
          new_is_stocked,
          months_as_a_item, classification_changes_12mo,
          recommendation_date, status,
          confidence_level, reasoning, reviewed_by, reviewed_date
      FROM dbo.ai_classification_recommendations
      WHERE status = 'Pending' AND change_type IN ('PROMOTION', 'DEMOTION')
      ORDER BY recommendation_date DESC
    """

  cursor = conn.cursor()
  cursor.execute(query)
  results = list(cursor.fetchall())
  return results


def get_pending_update_count(conn):
  """Count of Pending UPDATE records (for monitoring)."""
  query = """
    SELECT COUNT(*) as update_count
    FROM dbo.ai_classification_recommendations
    WHERE status = 'Pending' AND change_type = 'UPDATE'
  """
  cursor = conn.cursor()
  cursor.execute(query)
  row = cursor.fetchone()
  return row['update_count'] if row else 0


def bulk_approve_updates(conn):
  """
  Auto-approve all Pending UPDATE records with low-risk reasoning.
  Returns count of records updated.
  """
  query = """
    UPDATE dbo.ai_classification_recommendations
    SET
        status = 'Approved',
        reviewed_by = 'AI_AGENT_AUTO',
        reviewed_date = GETDATE(),
        confidence_level = 'High',
        reasoning = 'Continuing A Item - metrics refreshed. Auto-approved (low risk).'
    WHERE status = 'Pending' AND change_type = 'UPDATE'
  """
  cursor = conn.cursor()
  cursor.execute(query)
  conn.commit()
  return cursor.rowcount


def get_sku_sales_history(conn, sku, months=12):
  """
  Get monthly sales history for a SKU from usage_data.
  Returns list of dicts with: month, total_qty, order_count
  """
  query = """
    SELECT
        YEAR(usage_date) as year,
        MONTH(usage_date) as month,
        SUM(CAST(qty_sold AS DECIMAL(18,4))) as total_qty,
        COUNT(DISTINCT usage_date) as days_sold
    FROM dbo.usage_data
    WHERE sku = %s
      AND usage_date >= DATEADD(MONTH, %s, CAST(GETDATE() AS DATE))
    GROUP BY YEAR(usage_date), MONTH(usage_date)
    ORDER BY year DESC, month DESC
  """
  cursor = conn.cursor()
  cursor.execute(query, (sku, -months))
  rows = list(cursor.fetchall())

  # Calculate monthly rolling stats
  sales_by_month = []
  for row in rows:
    sales_by_month.append({
      'year': int(row['year']),
      'month': int(row['month']),
      'total_qty': float(row['total_qty'] or 0),
      'days_sold': int(row['days_sold'] or 0)
    })

  return sales_by_month


def get_classification_history(conn, sku):
  """
  Get classification change history from ai_agent_log.
  Filters for action_type = 'CLASSIFICATION_CHANGE'.
  Returns list of dicts with: action_timestamp, old_value, new_value, reason
  """
  query = """
    SELECT
        action_timestamp, old_value, new_value, reason
    FROM dbo.ai_agent_log
    WHERE sku = %s AND action_type = 'CLASSIFICATION_CHANGE'
    ORDER BY action_timestamp DESC
  """
  cursor = conn.cursor()
  cursor.execute(query, (sku,))
  rows = list(cursor.fetchall())

  history = []
  for row in rows:
    history.append({
      'timestamp': row['action_timestamp'].isoformat() if row['action_timestamp'] else None,
      'old_classification': row['old_value'],
      'new_classification': row['new_value'],
      'reason': row['reason']
    })

  return history


def get_sku_demand_pattern(conn, sku):
  """
  Get demand pattern context from min_max_calculated.
  Returns dict with: demand_pattern, coefficient_of_variation,
  avg_daily_usage, days_with_sales, days_of_data
  """
  query = """
    SELECT
        demand_pattern,
        coefficient_of_variation,
        avg_daily_usage,
        days_with_sales,
        days_of_data
    FROM dbo.min_max_calculated
    WHERE sku = %s
  """
  cursor = conn.cursor()
  cursor.execute(query, (sku,))
  row = cursor.fetchone()

  if row:
    return {
      'demand_pattern': row['demand_pattern'] or 'Unknown',
      'coefficient_of_variation': float(row['coefficient_of_variation'] or 0),
      'avg_daily_usage': float(row['avg_daily_usage'] or 0),
      'days_with_sales': int(row['days_with_sales'] or 0),
      'days_of_data': int(row['days_of_data'] or 0)
    }

  return {
    'demand_pattern': 'Unknown',
    'coefficient_of_variation': 0,
    'avg_daily_usage': 0,
    'days_with_sales': 0,
    'days_of_data': 0
  }


def log_action(conn, action_type, sku, old_value, new_value, reason, executed_by='classification_ai_review.py'):
  """
  Log an action to ai_agent_log for audit trail.
  """
  query = """
    INSERT INTO dbo.ai_agent_log (
        action_type, sku, old_value, new_value, reason,
        action_timestamp, executed_by
    )
    VALUES (%s, %s, %s, %s, %s, GETDATE(), %s)
  """
  cursor = conn.cursor()
  cursor.execute(query, (action_type, sku, old_value, new_value, reason, executed_by))
  conn.commit()


# =============================================================================
# AI PROMPT BUILDER
# =============================================================================

def build_classification_prompt(rec, sales_history, classification_history, demand_pattern):
  """
  Build AI analysis prompt for classification recommendation review.

  The AI evaluates:
  - A Items: Top 85% of unit sales volume (must be stocked)
  - PROMOTION: SKU moving from B/C to A (requires high volume evidence)
  - DEMOTION: SKU dropping from A to B (watch for premature decisions)
  - Bouncers: SKUs that flip classifications frequently (unstable demand)
  - Seasonal patterns: Consider upcoming seasonal factors

  Prompt asks for JSON with confidence_level, reasoning, and recommendation.
  """
  sku = rec['sku']
  change_type = rec['change_type']

  # Current values
  current_class = rec['current_classification']
  current_sold = int(rec['current_total_sold'] or 0)
  current_pct = float(rec['current_running_pct'] or 0)
  current_stocked = 'Yes' if rec['current_is_stocked'] else 'No'

  # Recommended values
  new_class = rec['new_classification']
  new_sold = int(rec['new_total_sold'] or 0)
  new_pct = float(rec['new_running_pct'] or 0)
  new_stocked = 'Yes' if rec['new_is_stocked'] else 'No'

  # History
  months_as_item = int(rec['months_as_a_item'] or 0)
  changes_12mo = int(rec['classification_changes_12mo'] or 0)

  # Build sales history string
  sales_str = ""
  for month_data in sales_history[:6]:  # Last 6 months
    sales_str = (sales_str + "  - " + str(month_data['month']) + "/" +
                 str(month_data['year']) + ": " +
                 str(int(month_data['total_qty'])) + " units (" +
                 str(month_data['days_sold']) + " days)\n")

  # Build classification history string
  class_history_str = ""
  for change in classification_history[:5]:  # Last 5 changes
    class_history_str = (class_history_str + "  - " +
                         str(change['timestamp']).split('T')[0] + ": " +
                         str(change['old_classification']) + " → " +
                         str(change['new_classification']) + " (" +
                         str(change['reason']) + ")\n")

  if not class_history_str:
    class_history_str = "  - No prior classification changes\n"

  # Demand pattern info
  cv = demand_pattern['coefficient_of_variation']
  pattern = demand_pattern['demand_pattern']
  avg_daily = demand_pattern['avg_daily_usage']

  prompt = """You are an inventory classification AI for Titan Flow Control. Your role is to
evaluate SKU classification recommendations (A/B/C based on ABC analysis - Annual Usage Value).

CLASSIFICATION DEFINITION:
- A Items: Top ~85% of total unit sales volume (must be stocked, frequent replenishment)
- B Items: Middle ~10% (moderately stocked, periodic replenishment)
- C Items: Bottom ~5% (minimal stock, order-to-stock basis)

SKU: """ + str(sku) + """
Change Type: """ + str(change_type) + """

CURRENT STATUS:
- Classification: """ + str(current_class) + """
- Total Units Sold (12mo): """ + str(current_sold) + """
- Running % of Total Volume: """ + str(round(current_pct, 2)) + """%
- Currently Stocked: """ + str(current_stocked) + """
- Months as Item: """ + str(months_as_item) + """

RECOMMENDED CHANGE:
- New Classification: """ + str(new_class) + """
- Projected Units Sold: """ + str(new_sold) + """
- Projected Running %: """ + str(round(new_pct, 2)) + """%
- Should Be Stocked: """ + str(new_stocked) + """

SALES HISTORY (Last 6 months):
""" + sales_str + """

CLASSIFICATION CHANGE HISTORY:
""" + class_history_str + """

DEMAND PATTERN CONTEXT:
- Pattern Type: """ + str(pattern) + """
- Coefficient of Variation: """ + str(round(cv, 2)) + """%
- Avg Daily Usage: """ + str(round(avg_daily, 4)) + """ units/day
- Days with Sales: """ + str(demand_pattern['days_with_sales']) + """

BOUNCER DETECTION:
- Classification changes in past 12 months: """ + str(changes_12mo) + """
- Bouncer threshold: > 2 changes = unstable demand = requires human review

DECISION GUIDELINES:
1. PROMOTION (B/C to A):
   - Requires clear evidence of increased volume reaching top 85%
   - Check for sustained trend over 3+ months, not single spike
   - Be cautious of seasonal or cyclical upticks that may not persist
   - Verify adequate supply chain capacity for A-level stocking

2. DEMOTION (A to B/C):
   - Verify volume truly dropped out of top 85%
   - Check if recent (last 3 months) or sustained trend
   - Recent A items with temporary dips may not warrant immediate demotion
   - Consider: Is this seasonal? Will demand recover?

3. BOUNCER DETECTION:
   - If > 2 changes in 12 months: This SKU is unstable
   - Flag for human review regardless of recommendation
   - Unstable items need supply chain attention, not more classification changes

4. CONFIDENCE ASSESSMENT:
   - High: Clear trend (sustained 3+ months), stable pattern
   - Medium: Trend emerging, moderate change in volume
   - Low: Recent change, uncertain trend, high variability

YOUR TASK:
Analyze whether the recommended classification change is appropriate based on:
- Evidence in sales history (trend direction, duration, magnitude)
- Demand pattern stability
- Change frequency (bouncer detection)
- Tenure as item (demotions of young items may be premature)

Respond in JSON format only:
{
    "confidence_level": "High/Medium/Low",
    "recommendation": "Approve/Review",
    "reasoning": "<2-3 paragraph explanation of your analysis>",
    "flags": "<any concerns: bouncer, premature demotion, seasonal spike, etc>",
    "learning_point": "<one key ABC classification principle this illustrates>"
}

Notes:
- "Approve" = auto-approve to Approved status (auto-approved recs are low-risk)
- "Review" = flag as Pending_Review for human decision
"""
  return prompt


# =============================================================================
# AI ANALYSIS
# =============================================================================

def analyze_with_ai(client, prompt, max_retries=3):
  """
  Send prompt to Claude API and parse JSON response.
  Includes exponential backoff retry for transient errors (529 overloaded,
  rate limits, network timeouts).
  Returns parsed dict or error dict.
  """
  last_error = None

  for attempt in range(max_retries):
    try:
      response = client.messages.create(
        model=db_config.AI_MODEL,
        max_tokens=db_config.AI_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}]
      )

      response_text = response.content[0].text.strip()

      # Extract JSON from response (handle markdown code blocks)
      if '```json' in response_text:
        response_text = response_text.split('```json')[1].split('```')[0].strip()
      elif '```' in response_text:
        response_text = response_text.split('```')[1].split('```')[0].strip()

      result = json.loads(response_text)

      # Validate required fields
      required = ['confidence_level', 'recommendation', 'reasoning']
      for field in required:
        if field not in result:
          return {'error': 'Missing field: ' + field, 'raw_response': response_text}

      return result

    except json.JSONDecodeError as e:
      # JSON parse errors are not transient - no retry
      return {'error': 'JSON parse error: ' + str(e), 'raw_response': response_text}
    except Exception as e:
      last_error = str(e)
      # Retry on transient errors: 529 (overloaded), 429 (rate limit),
      # 500/502/503 (server errors), network/timeout issues
      is_transient = any(code in last_error for code in ['529', '429', '500', '502', '503', 'timeout', 'ConnectionError'])
      if is_transient and attempt < max_retries - 1:
        wait_seconds = 2 ** (attempt + 1)  # 2s, 4s, 8s
        print('Retrying SKU after ' + str(wait_seconds) + 's (attempt ' + str(attempt + 1) + '/' + str(max_retries) + '): ' + last_error)
        time.sleep(wait_seconds)
        continue
      else:
        return {'error': 'AI API error: ' + last_error}

  return {'error': 'AI API error after ' + str(max_retries) + ' retries: ' + last_error}


# =============================================================================
# DATABASE FUNCTIONS - Update Results
# =============================================================================

def determine_status(ai_result, rec, thresholds):
  """
  Apply business rules to determine final status for the recommendation.

  Rules:
  1. If AI says 'Review' → Pending_Review
  2. If confidence 'Low' → Pending_Review
  3. If classification_changes_12mo > threshold → Pending_Review (bouncer)
  4. If DEMOTION and months_as_a_item < threshold → Pending_Review (premature)
  5. Otherwise → Approved

  Returns: 'Approved' or 'Pending_Review'
  """
  # Rule 1: AI recommendation
  if ai_result.get('recommendation', '').strip() == 'Review':
    return 'Pending_Review'

  # Rule 2: Confidence level
  if ai_result.get('confidence_level', '').strip() == 'Low':
    return 'Pending_Review'

  # Rule 3: Bouncer detection
  changes_12mo = int(rec.get('classification_changes_12mo') or 0)
  max_changes = thresholds.get('max_classification_changes_12mo', 2)
  if changes_12mo > max_changes:
    return 'Pending_Review'

  # Rule 4: Premature demotion
  if rec.get('change_type') == 'DEMOTION':
    months_as_item = int(rec.get('months_as_a_item') or 0)
    min_months = thresholds.get('min_months_as_a_item_for_demotion', 3)
    if months_as_item < min_months:
      return 'Pending_Review'

  # All checks passed - auto-approve
  return 'Approved'


def update_recommendation(conn, recommendation_id, confidence_level, reasoning, status, reviewed_by='AI_AGENT'):
  """
  Update a recommendation in ai_classification_recommendations with AI results.
  """
  query = """
    UPDATE dbo.ai_classification_recommendations
    SET
        confidence_level = %s,
        reasoning = %s,
        status = %s,
        reviewed_by = %s,
        reviewed_date = GETDATE()
    WHERE recommendation_id = %s
  """
  cursor = conn.cursor()
  cursor.execute(query, (confidence_level, reasoning, status, reviewed_by, recommendation_id))
  conn.commit()


# =============================================================================
# MAIN BACKGROUND TASK
# =============================================================================

@anvil.server.callable
def trigger_classification_review():
  """
  Start the Classification AI review as a background task.
  Called from the UI 'Run Classification Review' button.
  Returns a dict with status and task_id for polling.
  """
  try:
    task = anvil.server.launch_background_task('run_classification_review')
    return {
      'status': 'launched',
      'task_id': task.get_id()
    }
  except Exception as e:
    return {
      'status': 'error',
      'message': str(e)
    }


@anvil.server.background_task
def run_classification_review():
  """
  Main orchestrator for the Classification AI review process.

  Flow:
  1. Bulk auto-approve all Pending UPDATE records
  2. Get all Pending PROMOTION/DEMOTION recommendations
  3. For each recommendation:
     a. Get sales history, classification history, demand pattern
     b. Build AI prompt with context
     c. Call Claude API for analysis
     d. Determine final status based on business rules
     e. Update recommendation with AI results
     f. Check for duplicate Pending records (skip if exists)
  4. Log completion to ai_agent_log
  5. Return summary results
  """
  batch_id = 'CLASS-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + str(uuid.uuid4())[:8]

  results = {
    'batch_id': batch_id,
    'updates_approved': 0,
    'promotions_demotions_reviewed': 0,
    'approved': 0,
    'pending_review': 0,
    'skipped_duplicate': 0,
    'errors': 0,
    'error_skus': []
  }

  # Thresholds for auto-approval logic
  thresholds = {
    'max_classification_changes_12mo': 2,
    'min_months_as_a_item_for_demotion': 3,
    'sales_drop_pct_cliff': 50
  }

  conn = None

  try:
    conn = get_connection()
    client = db_config.get_anthropic_client()

    # =========================================================================
    # Step 1: Bulk auto-approve UPDATE records
    # =========================================================================
    anvil.server.task_state['status'] = 'Auto-approving UPDATE records'
    update_count = get_pending_update_count(conn)

    if update_count > 0:
      approved_count = bulk_approve_updates(conn)
      results['updates_approved'] = approved_count
      log_action(
        conn, 'Classification_Updates_Auto_Approved', None,
        batch_id,
        str(approved_count) + ' UPDATE records auto-approved (low risk)',
        'Batch: ' + batch_id,
        'classification_ai_review.py'
      )

    # =========================================================================
    # Step 2: Get PROMOTION/DEMOTION recommendations
    # =========================================================================
    anvil.server.task_state['status'] = 'Loading recommendations for review'
    recommendations = get_pending_recommendations(conn, include_updates=False)
    total_recs = len(recommendations)
    anvil.server.task_state['total_recommendations'] = total_recs

    if total_recs == 0:
      anvil.server.task_state['status'] = 'Complete - no recommendations to review'
      log_action(
        conn, 'Classification_Review_Completed', None,
        batch_id,
        'No PROMOTION/DEMOTION recommendations pending. ' + str(update_count) + ' UPDATE records auto-approved.',
        'Complete',
        'classification_ai_review.py'
      )
      results['status'] = 'complete'
      return results

    # =========================================================================
    # Step 3: Process each recommendation
    # =========================================================================
    for i, rec in enumerate(recommendations):
      sku = rec['sku']
      rec_id = rec['recommendation_id']
      change_type = rec['change_type']
      progress = str(i + 1) + '/' + str(total_recs)
      anvil.server.task_state['status'] = 'Reviewing ' + sku + ' (' + change_type + ') (' + progress + ')'
      anvil.server.task_state['current_sku'] = sku
      anvil.server.task_state['change_type'] = change_type
      anvil.server.task_state['progress'] = i + 1

      try:
        # Check for duplicate pending records (skip if already under review)
        check_query = """
          SELECT COUNT(*) as count
          FROM dbo.ai_classification_recommendations
          WHERE sku = %s AND recommendation_id != %s AND status IN ('Pending_Review', 'Approved')
        """
        check_cursor = conn.cursor()
        check_cursor.execute(check_query, (sku, rec_id))
        check_row = check_cursor.fetchone()
        if check_row and check_row['count'] > 0:
          results['skipped_duplicate'] += 1
          log_action(
            conn, 'Classification_Review_Skipped', sku,
            rec_id,
            'Duplicate pending/approved record exists',
            'Batch: ' + batch_id,
            'classification_ai_review.py'
          )
          continue

        # Get context data
        sales_history = get_sku_sales_history(conn, sku, months=12)
        classification_history = get_classification_history(conn, sku)
        demand_pattern = get_sku_demand_pattern(conn, sku)

        # Build prompt and call AI
        prompt = build_classification_prompt(rec, sales_history, classification_history, demand_pattern)
        ai_result = analyze_with_ai(client, prompt)

        if 'error' in ai_result:
          results['errors'] += 1
          results['error_skus'].append(sku + ': ' + ai_result['error'])
          log_action(
            conn, 'Classification_Review_Error', sku,
            rec_id,
            ai_result['error'],
            'Batch: ' + batch_id,
            'classification_ai_review.py'
          )
          continue

        # Determine final status based on business rules
        final_status = determine_status(ai_result, rec, thresholds)
        confidence = ai_result.get('confidence_level', 'Medium')
        reasoning = ai_result.get('reasoning', '')
        flags = ai_result.get('flags', '')

        if flags:
          reasoning = reasoning + '\n\nFLAGS: ' + flags

        learning = ai_result.get('learning_point', '')
        if learning:
          reasoning = reasoning + '\n\nLEARNING: ' + learning

        # Update recommendation
        update_recommendation(
          conn, rec_id,
          confidence, reasoning, final_status,
          reviewed_by='AI_AGENT'
        )

        # Track results
        if final_status == 'Approved':
          results['approved'] += 1
        else:
          results['pending_review'] += 1

        results['promotions_demotions_reviewed'] += 1

        # Log the review
        log_action(
          conn, 'Classification_Review_Completed_SKU', sku,
          rec['change_type'],
          'Status: ' + final_status + ', Confidence: ' + confidence,
          'Batch: ' + batch_id,
          'classification_ai_review.py'
        )

        # Brief pause between API calls
        time.sleep(0.5)

      except Exception as e:
        results['errors'] += 1
        results['error_skus'].append(sku + ': ' + str(e))
        print('Error processing SKU ' + sku + ': ' + str(e))

    # =========================================================================
    # Step 4: Log completion
    # =========================================================================
    summary = (str(results['updates_approved']) + ' UPDATEs auto-approved, ' +
               str(results['promotions_demotions_reviewed']) + ' PROMOTION/DEMOTION reviewed (' +
               str(results['approved']) + ' approved, ' +
               str(results['pending_review']) + ' pending human review), ' +
               str(results['skipped_duplicate']) + ' skipped duplicates, ' +
               str(results['errors']) + ' errors')

    log_action(
      conn, 'Classification_Review_Completed', None,
      batch_id,
      summary,
      'Batch review completed',
      'classification_ai_review.py'
    )

    # =========================================================================
    # Final status
    # =========================================================================
    anvil.server.task_state['status'] = 'Complete'
    anvil.server.task_state['summary'] = summary
    results['status'] = 'complete'

    return results

  except Exception as e:
    error_msg = 'Classification review failed: ' + str(e)
    anvil.server.task_state['status'] = 'Error: ' + str(e)

    # Try to log the failure
    try:
      if conn:
        log_action(
          conn, 'Classification_Review_Failed', None,
          batch_id, error_msg,
          'System error',
          'classification_ai_review.py'
        )
    except:
      pass

    raise

  finally:
    if conn:
      try:
        conn.close()
      except:
        pass
