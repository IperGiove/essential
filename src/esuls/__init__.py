"""
esuls - Utility library for async database operations, HTTP requests, and parallel execution
"""

__version__ = "0.1.0"

# Import all utilities
from .utils import run_parallel
from .db_cli import AsyncDB, BaseModel
from .request_cli import AsyncRequest, make_request, make_request_cffi, Response

__all__ = [
    '__version__',
    'run_parallel',
    'AsyncDB',
    'BaseModel',
    'AsyncRequest',
    'make_request',
    'make_request_cffi',
    'Response',
]
