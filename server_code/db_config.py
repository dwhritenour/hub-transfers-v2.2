"""
db_config.py - Anvil Server Module
==================================
Titan AI Inventory Management System
Centralized database connection management for Anvil app.

Uses Anvil Secrets for credentials (Settings → Secrets).

Required Secrets:
    - NEXUS_SERVER
    - NEXUS_DATABASE
    - NEXUS_USERNAME
    - NEXUS_PASSWORD
    - ANTHROPIC_API_KEY
    - HUB_MANAGER_EMAIL

Usage:
    from . import db_config
    
    # For database operations
    conn = db_config.get_nexus_connection()
    cursor = conn.cursor()
    
    # For AI operations
    client = db_config.get_anthropic_client()
    
    # For email
    email = db_config.get_hub_manager_email()
"""

import pymssql
import anvil.secrets


# =============================================================================
# DATABASE CONNECTIONS
# =============================================================================

def get_nexus_connection():
  """
    Returns a pymssql connection to the nexus database.
    
    Use for:
        - Direct SQL execution
        - Cursor operations
        - Transaction management
    
    Example:
        conn = db_config.get_nexus_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM ai_transfer_recommendations WHERE sku = %s", (sku,))
        conn.commit()
        conn.close()
    """
  return pymssql.connect(
    server=anvil.secrets.get_secret('NEXUS_SERVER'),
    user=anvil.secrets.get_secret('NEXUS_USERNAME'),
    password=anvil.secrets.get_secret('NEXUS_PASSWORD'),
    database=anvil.secrets.get_secret('NEXUS_DATABASE'),
    port='1433',
    as_dict=True
  )


# =============================================================================
# ANTHROPIC API CLIENT
# =============================================================================

def get_anthropic_client():
  """
    Returns a configured Anthropic client for Claude API calls.
    
    Example:
        client = db_config.get_anthropic_client()
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}]
        )
    """
  from anthropic import Anthropic
  return Anthropic(api_key=anvil.secrets.get_secret('ANTHROPIC_API_KEY'))


def get_anthropic_api_key():
  """Returns the Anthropic API key directly (if needed for other libraries)."""
  return anvil.secrets.get_secret('ANTHROPIC_API_KEY')


# =============================================================================
# EMAIL CONFIGURATION
# =============================================================================

def get_hub_manager_email():
  """Returns the Hub Manager email address for notifications."""
  return anvil.secrets.get_secret('HUB_MANAGER_EMAIL')


# =============================================================================
# CONSTANTS
# =============================================================================

# AI Model Configuration
AI_MODEL = 'claude-opus-4-8'
AI_MAX_TOKENS = 2000

# Transfer Order Constants (for e21 import)
TRANSFER_CONSTANTS = {
  'cost_ctr': '400',          # Hub cost center
  'shipto_code': '150',       # Starlite ship-to code
  'carr_code': 'TT',          # Titan Truck carrier
  'met_of_ship': 'GRD',       # Ground Transport
  'shipphone': '910.735.0000',
  'item_type': 'S',           # Stock item
  'uom': 'EA',                # Each (unit of measure)
  'item_status': 'E'          # Entered status
}

# Auto-approve urgency levels
AUTO_APPROVE_URGENCIES = ('CRITICAL', 'HIGH')


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def test_nexus_connection():
  """
    Tests the nexus database connection.
    Returns True if successful, raises exception otherwise.
    """
  try:
    conn = get_nexus_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 AS test")
    result = cursor.fetchone()
    conn.close()
    print("✓ Nexus connection successful")
    return True
  except Exception as e:
    print(f"✗ Nexus connection failed: {e}")
    raise


def test_anthropic_connection():
  """
    Tests the Anthropic API connection with a minimal request.
    Returns True if successful, raises exception otherwise.
    """
  try:
    client = get_anthropic_client()
    response = client.messages.create(
      model=AI_MODEL,
      max_tokens=10,
      messages=[{"role": "user", "content": "Say 'connected' and nothing else."}]
    )
    print("✓ Anthropic API connection successful")
    return True
  except Exception as e:
    print(f"✗ Anthropic API connection failed: {e}")
    raise