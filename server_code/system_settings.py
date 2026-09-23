import pymssql
import anvil.secrets

# =============================================================================
# GLOBAL PARAMETERS MANAGEMENT
# =============================================================================

@anvil.server.callable
def get_global_parameters():
  """
    Get all global parameters for the system admin form.
    Returns a dictionary of parameter_name: parameter_value
    """
  from .db_config import get_nexus_connection

  conn = get_nexus_connection()
  cursor = conn.cursor()

  cursor.execute("""
        SELECT parameter_name, parameter_value, description
        FROM dbo.global_parameters
    """)

  params = {}
  for row in cursor.fetchall():
    params[row['parameter_name']] = {
      'value': float(row['parameter_value']) if row['parameter_value'] else 0,
      'description': row['description']
    }

  conn.close()
  return params


@anvil.server.callable
def update_global_parameter(parameter_name, new_value):
  """
    Update a single global parameter value.
    
    Args:
        parameter_name: The parameter name in the database
        new_value: The new value (will be converted to decimal)
    
    Returns:
        dict with success status and message
    """
  from .db_config import get_nexus_connection

  try:
    # Validate the value is numeric
    numeric_value = float(new_value)

    conn = get_nexus_connection()
    cursor = conn.cursor()

    # Check if parameter exists
    cursor.execute("""
            SELECT parameter_name FROM dbo.global_parameters
            WHERE parameter_name = %s
        """, (parameter_name,))

    if not cursor.fetchone():
      conn.close()
      return {'success': False, 'error': f'Parameter "{parameter_name}" not found'}

      # Update the parameter
    cursor.execute("""
            UPDATE dbo.global_parameters
            SET parameter_value = %s,
                last_updated = GETDATE()
            WHERE parameter_name = %s
        """, (numeric_value, parameter_name))

    conn.commit()
    conn.close()

    return {'success': True, 'message': f'{parameter_name} updated to {numeric_value}'}

  except ValueError:
    return {'success': False, 'error': 'Invalid numeric value'}
  except Exception as e:
    return {'success': False, 'error': str(e)}


# =============================================================================
# GLOBAL PARAMETERS (TEXT) MANAGEMENT
# =============================================================================
# Sibling of global_parameters for string-valued configuration (URLs, email
# addresses, template overrides, etc.). See sql/create_global_parameters_text.sql.

def get_text_parameter(parameter_name, default=None):
  """
    Server-side helper (not Anvil-callable) to read a single text parameter.

    Use this from other server modules when you need a string setting, e.g.
        from . import system_settings
        url = system_settings.get_text_parameter('dashboard_url',
                                                 'https://titan-hub.anvil.app/')

    Returns the stored value, or `default` if the row is missing or on error.
    Errors are logged but never raised — callers always get a usable value.
    """
  from .db_config import get_nexus_connection

  try:
    conn = get_nexus_connection()
    cursor = conn.cursor()
    cursor.execute("""
            SELECT parameter_value
            FROM dbo.global_parameters_text
            WHERE parameter_name = %s
        """, (parameter_name,))
    row = cursor.fetchone()
    conn.close()
    if row and row['parameter_value']:
      return row['parameter_value']
    return default
  except Exception as e:
    print(f"[system_settings] get_text_parameter('{parameter_name}') failed: {e}")
    return default


@anvil.server.callable
def get_global_parameters_text():
  """
    Get all global text parameters for the system admin form.
    Returns a dict of parameter_name -> {value, description}.
    Shape mirrors get_global_parameters() so the admin UI can treat both
    tables the same way.
    """
  from .db_config import get_nexus_connection

  conn = get_nexus_connection()
  cursor = conn.cursor()

  cursor.execute("""
        SELECT parameter_name, parameter_value, description
        FROM dbo.global_parameters_text
    """)

  params = {}
  for row in cursor.fetchall():
    params[row['parameter_name']] = {
      'value': row['parameter_value'] or '',
      'description': row['description']
    }

  conn.close()
  return params


@anvil.server.callable
def update_global_parameter_text(parameter_name, new_value):
  """
    Update a single text parameter value.

    Args:
        parameter_name: The parameter name in the database
        new_value: The new string value

    Returns:
        dict with success status and message
    """
  from .db_config import get_nexus_connection

  try:
    if new_value is None:
      return {'success': False, 'error': 'Value cannot be null'}

    # Coerce to str and trim; enforce the schema's 500-char ceiling
    new_value = str(new_value).strip()
    if not new_value:
      return {'success': False, 'error': 'Value cannot be empty'}
    if len(new_value) > 500:
      return {'success': False, 'error': 'Value exceeds 500-character limit'}

    conn = get_nexus_connection()
    cursor = conn.cursor()

    # Check if parameter exists
    cursor.execute("""
            SELECT parameter_name FROM dbo.global_parameters_text
            WHERE parameter_name = %s
        """, (parameter_name,))

    if not cursor.fetchone():
      conn.close()
      return {'success': False, 'error': f'Parameter "{parameter_name}" not found'}

    cursor.execute("""
            UPDATE dbo.global_parameters_text
            SET parameter_value = %s,
                last_updated = GETDATE()
            WHERE parameter_name = %s
        """, (new_value, parameter_name))

    conn.commit()
    conn.close()

    return {'success': True, 'message': f'{parameter_name} updated'}

  except Exception as e:
    return {'success': False, 'error': str(e)}
