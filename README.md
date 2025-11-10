# esuls

A Python utility library for async database operations, HTTP requests, and parallel execution utilities.

## Features

- **AsyncDB** - Type-safe async SQLite with dataclass schemas
- **Async HTTP client** - High-performance HTTP client with retry logic and connection pooling
- **Parallel utilities** - Async parallel execution with concurrency control
- **CloudFlare bypass** - curl-cffi integration for bypassing protections

## Installation

```bash
# With pip
pip install esuls

# With uv
uv pip install esuls
```

## Usage

### Parallel Execution

```python
import asyncio
from esuls import run_parallel

async def fetch_data(id):
    await asyncio.sleep(1)
    return f"Data {id}"

async def main():
    # Run multiple async functions in parallel with concurrency limit
    results = await run_parallel(
        lambda: fetch_data(1),
        lambda: fetch_data(2),
        lambda: fetch_data(3),
        limit=20  # Max concurrent tasks
    )
    print(results)

asyncio.run(main())
```

### Database Client (AsyncDB)

```python
import asyncio
from dataclasses import dataclass, field
from esuls import AsyncDB, BaseModel

# Define your schema
@dataclass
class User(BaseModel):
    name: str = field(metadata={"index": True})
    email: str = field(metadata={"unique": True})
    age: int = 0

async def main():
    # Initialize database
    db = AsyncDB(db_path="users.db", table_name="users", schema_class=User)

    # Save data
    user = User(name="Alice", email="alice@example.com", age=30)
    await db.save(user)

    # Save multiple items
    users = [
        User(name="Bob", email="bob@example.com", age=25),
        User(name="Charlie", email="charlie@example.com", age=35)
    ]
    await db.save_batch(users)

    # Query data
    results = await db.find(name="Alice")
    print(results)

    # Query with filters
    adults = await db.find(age__gte=18, order_by="-age")

    # Count
    count = await db.count(age__gte=18)

    # Get by ID
    user = await db.get_by_id(user_id)

    # Delete
    await db.delete(user_id)

asyncio.run(main())
```

**Query Operators:**
- `field__eq` - Equal (default)
- `field__gt` - Greater than
- `field__gte` - Greater than or equal
- `field__lt` - Less than
- `field__lte` - Less than or equal
- `field__neq` - Not equal
- `field__like` - SQL LIKE
- `field__in` - IN operator (pass a list)

### HTTP Request Client

```python
import asyncio
from esuls import AsyncRequest, make_request

# Using context manager (recommended for multiple requests)
async def example1():
    async with AsyncRequest() as client:
        response = await client.request(
            url="https://api.example.com/data",
            method="GET",
            add_user_agent=True,
            max_attempt=3,
            timeout_request=30
        )
        if response:
            data = response.json()
            print(data)

# Using standalone function (uses shared connection pool)
async def example2():
    response = await make_request(
        url="https://api.example.com/users",
        method="POST",
        json_data={"name": "Alice", "email": "alice@example.com"},
        headers={"Authorization": "Bearer token"},
        max_attempt=5,
        force_response=True  # Return response even on error
    )
    if response:
        print(response.status_code)
        print(response.text)

asyncio.run(example1())
```

**Request Parameters:**
- `url` - Request URL
- `method` - HTTP method (GET, POST, PUT, DELETE, etc.)
- `headers` - Request headers
- `cookies` - Cookies dict
- `params` - URL parameters
- `json_data` - JSON body
- `files` - Multipart file upload
- `proxy` - Proxy URL
- `timeout_request` - Timeout in seconds (default: 60)
- `max_attempt` - Max retry attempts (default: 10)
- `force_response` - Return response even on error (default: False)
- `json_response` - Validate JSON response (default: False)
- `json_response_check` - Check for key in JSON response
- `skip_response` - Skip if text contains pattern(s)
- `exception_sleep` - Delay between retries in seconds (default: 10)
- `add_user_agent` - Add random User-Agent header (default: False)

### CloudFlare Bypass

```python
import asyncio
from esuls import make_request_cffi

async def fetch_protected_page():
    html = await make_request_cffi("https://protected-site.com")
    if html:
        print(html)

asyncio.run(fetch_protected_page())
```

## Development

### Project Structure

```
utils/
├── pyproject.toml
├── README.md
├── LICENSE
└── src/
    └── esuls/
        ├── __init__.py
        ├── utils.py          # Parallel execution utilities
        ├── db_cli.py         # AsyncDB with dataclass schemas
        └── request_cli.py    # Async HTTP client
```

### Local Development Installation

```bash
# Navigate to the project
cd utils

# Install in editable mode with uv
uv pip install -e .

# Or with pip
pip install -e .
```

### Building and Publishing

```bash
# With uv
uv build
twine upload dist/*

# Or with traditional tools
pip install build twine
python -m build
twine upload dist/*
```

## Advanced Features

### AsyncDB Schema Definition

```python
from dataclasses import dataclass, field
from esuls import BaseModel
from datetime import datetime
from typing import Optional, List
import enum

class Status(enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"

@dataclass
class User(BaseModel):
    # BaseModel provides: id, created_at, updated_at

    # Indexed field
    email: str = field(metadata={"index": True, "unique": True})

    # Simple fields
    name: str = ""
    age: int = 0

    # Enum support
    status: Status = Status.ACTIVE

    # JSON-serialized complex types
    tags: List[str] = field(default_factory=list)

    # Optional fields
    phone: Optional[str] = None

    # Table constraints (optional)
    __table_constraints__ = [
        "CHECK (age >= 0)"
    ]
```

### Connection Pooling & Performance

The HTTP client uses:
- Shared connection pool (prevents "too many open files" errors)
- Automatic retry with exponential backoff
- SSL optimization
- Random User-Agent rotation
- Cookie and header persistence

## License

MIT License
