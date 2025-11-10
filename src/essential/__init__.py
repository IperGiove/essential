"""
Essential - Modular utility library
"""

__version__ = "0.1.0"

# Import utils (always available, no extra dependencies)
from .utils import *

# Conditional imports with helpful error messages
def _optional_import(module_name, extra_name):
    """Helper function to provide clear error messages for optional dependencies."""
    def _import_error(*args, **kwargs):
        raise ImportError(
            f"{module_name} requires additional dependencies. "
            f"Install with: pip install essential[{extra_name}]"
        )
    return _import_error

# Try to import db_cli if database extras are installed
try:
    from .db_cli import *
except ImportError:
    # Create a dummy that will raise a helpful error
    db_cli = type('Module', (), {'__getattr__': _optional_import('db_cli', 'database')})()

# Try to import request_cli if requests extras are installed
try:
    from .request_cli import *
except ImportError:
    # Create a dummy that will raise a helpful error
    request_cli = type('Module', (), {'__getattr__': _optional_import('request_cli', 'requests')})()

__all__ = ['__version__']
