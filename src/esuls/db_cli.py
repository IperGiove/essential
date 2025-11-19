import asyncio
import aiosqlite
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TypeVar, Generic, Type, get_type_hints, Union, Tuple
from dataclasses import dataclass, asdict, fields, is_dataclass, field
from functools import lru_cache
import uuid
import contextlib
import enum
from loguru import logger

T = TypeVar('T')
SchemaType = TypeVar('SchemaType', bound='BaseModel')

@dataclass
class BaseModel:
    id: str = field(default_factory=lambda: str(uuid.uuid4()), metadata={"primary_key": True})
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


class AsyncDB(Generic[SchemaType]):
    """High-performance async SQLite with dataclass schema and reliable connection handling."""
    
    OPERATOR_MAP = {
        'gt': '>', 'lt': '<', 'gte': '>=', 'lte': '<=', 
        'neq': '!=', 'like': 'LIKE', 'in': 'IN', 'eq': '='
    }
    
    def __init__(self, db_path: Union[str, Path], table_name: str, schema_class: Type[SchemaType]):
        """Initialize AsyncDB with a path and schema dataclass."""
        if not is_dataclass(schema_class):
            raise TypeError(f"Schema must be a dataclass, got {schema_class}")
            
        self.db_path = Path(db_path).resolve()
        self.schema_class = schema_class
        self.table_name = table_name
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Make schema initialization unique per instance
        self._db_key = f"{str(self.db_path)}:{self.table_name}:{self.schema_class.__name__}"
        self._write_lock = asyncio.Lock()
        self._type_hints = get_type_hints(schema_class)
        
        # Use a class-level set to track initialized schemas
        if not hasattr(AsyncDB, '_initialized_schemas'):
            AsyncDB._initialized_schemas = set()
    
    async def _get_connection(self) -> aiosqlite.Connection:
        """Create a new optimized connection."""
        db = await aiosqlite.connect(self.db_path)
        # Fast WAL mode with minimal sync
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA cache_size=10000")
        
        # Initialize schema if needed (check per unique schema)
        if self._db_key not in AsyncDB._initialized_schemas:
            await self._init_schema(db)
            AsyncDB._initialized_schemas.add(self._db_key)
            
        return db
    
    async def _init_schema(self, db: aiosqlite.Connection) -> None:
        """Generate schema from dataclass structure with support for field additions."""
        logger.debug(f"Initializing schema for {self.schema_class.__name__} in table {self.table_name}")
        
        field_defs = []
        indexes = []
        
        # First check if table exists
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (self.table_name,)
        )
        table_exists = await cursor.fetchone() is not None
        
        existing_columns = set()
        if table_exists:
            # Get existing columns if table exists
            cursor = await db.execute(f"PRAGMA table_info({self.table_name})")
            columns = await cursor.fetchall()
            existing_columns = {col[1] for col in columns}  # col[1] is the column name
        
        # Process all fields in the dataclass - ONLY THIS SCHEMA CLASS
        schema_fields = fields(self.schema_class)
        logger.debug(f"Processing {len(schema_fields)} fields for {self.schema_class.__name__}")
        
        for f in schema_fields:
            field_name = f.name
            field_type = self._type_hints.get(field_name)
            logger.debug(f"  Field: {field_name} -> {field_type}")
            
            # Map Python types to SQLite types
            if field_type in (int, bool):
                sql_type = "INTEGER"
            elif field_type in (float,):
                sql_type = "REAL"
            elif field_type in (str, enum.EnumType):
                sql_type = "TEXT"
            elif field_type in (datetime,):
                sql_type = "TIMESTAMP"
            elif field_type == List[str]:
                sql_type = "TEXT"  # Stored as JSON
            else:
                sql_type = "TEXT"  # Default to TEXT/JSON for complex types
                
            # Handle special field metadata
            constraints = []
            if f.metadata.get('primary_key'):
                constraints.append("PRIMARY KEY")
            if f.metadata.get('unique'):
                constraints.append("UNIQUE")
            if not f.default and not f.default_factory and f.metadata.get('required', True):
                constraints.append("NOT NULL")
                
            field_def = f"{field_name} {sql_type} {' '.join(constraints)}"
            
            if not table_exists:
                # Add field definition for new table creation
                field_defs.append(field_def)
            elif field_name not in existing_columns:
                # Alter table to add the new column without NOT NULL constraint
                alter_sql = f"ALTER TABLE {self.table_name} ADD COLUMN {field_name} {sql_type}"
                logger.debug(f"  Adding new column: {alter_sql}")
                await db.execute(alter_sql)
                await db.commit()
                
            # Handle indexes
            if f.metadata.get('index'):
                index_name = f"idx_{self.table_name}_{field_name}"
                index_sql = f"CREATE INDEX IF NOT EXISTS {index_name} ON {self.table_name}({field_name})"
                indexes.append(index_sql)
        
        # Create table if it doesn't exist
        if not table_exists:
            # Check for table constraints
            table_constraints = getattr(self.schema_class, '__table_constraints__', [])

            constraints_sql = ""
            if table_constraints:
                constraints_sql = ", " + ", ".join(table_constraints)

            create_sql = f"""
                CREATE TABLE IF NOT EXISTS {self.table_name} (
                    {', '.join(field_defs)}{constraints_sql}
                )
            """
            logger.debug(f"Creating table: {create_sql}")
            await db.execute(create_sql)
        
        # Create indexes
        for idx_stmt in indexes:
            await db.execute(idx_stmt)
            
        await db.commit()
        logger.debug(f"Schema initialization complete for {self.schema_class.__name__}")
    
    @contextlib.asynccontextmanager
    async def transaction(self):
        """Run operations in a transaction with reliable cleanup."""
        db = await self._get_connection()
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()
    
    # @lru_cache(maxsize=128)
    def _serialize_value(self, value: Any) -> Any:
        """Fast value serialization with type-based optimization."""
        if value is None or isinstance(value, (int, float, bool, str)):
            return value
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, enum.Enum):
            return value.value
        if isinstance(value, (list, dict, tuple)):
            return json.dumps(value)
        return str(value)
    
    def _deserialize_value(self, field_name: str, value: Any) -> Any:
        """Deserialize values based on field type."""
        if value is None:
            return value

        field_type = self._type_hints.get(field_name)

        # Handle string fields - ensure phone numbers are strings
        if field_type is str or (hasattr(field_type, '__origin__') and field_type.__origin__ is Union and str in getattr(field_type, '__args__', ())):
            return str(value)

        if field_type is datetime and isinstance(value, str):
            return datetime.fromisoformat(value)

        # Handle enum types
        if hasattr(field_type, '__origin__') and field_type.__origin__ is Union:
            # Handle Optional[EnumType] case
            args = getattr(field_type, '__args__', ())
            for arg in args:
                if arg is not type(None) and hasattr(arg, '__bases__') and enum.Enum in arg.__bases__:
                    try:
                        return arg(value)
                    except (ValueError, TypeError):
                        pass
        elif hasattr(field_type, '__bases__') and enum.Enum in field_type.__bases__:
            # Handle direct enum types
            try:
                return field_type(value)
            except (ValueError, TypeError):
                pass

        if isinstance(value, str):
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                pass

        return value
    
    @lru_cache(maxsize=64)
    def _generate_save_sql(self, field_names: Tuple[str, ...]) -> str:
        """Generate efficient SQL for upsert with proper conflict handling."""
        columns = ','.join(field_names)
        placeholders = ','.join('?' for _ in field_names)

        return f"""
            INSERT OR REPLACE INTO {self.table_name} ({columns},id)
            VALUES ({placeholders},?)
        """
    
    async def save_batch(self, items: List[SchemaType], skip_errors: bool = True) -> int:
        """Save multiple items in a single transaction for better performance.

        Args:
            items: List of schema objects to save
            skip_errors: If True, skip items that cause errors

        Returns:
            Number of items successfully saved
        """
        if not items:
            return 0

        saved_count = 0

        async with self._write_lock:
            async with self.transaction() as db:
                for item in items:
                    try:
                        if not isinstance(item, self.schema_class):
                            if not skip_errors:
                                raise TypeError(f"Expected {self.schema_class.__name__}, got {type(item).__name__}")
                            continue

                        # Extract and process data
                        data = asdict(item)
                        item_id = data.pop('id', None) or str(uuid.uuid4())

                        # Ensure created_at and updated_at are set
                        now = datetime.now()
                        if not data.get('created_at'):
                            data['created_at'] = now
                        data['updated_at'] = now

                        # Prepare SQL and values
                        field_names = tuple(sorted(data.keys()))
                        sql = self._generate_save_sql(field_names)
                        values = [self._serialize_value(data[name]) for name in field_names]
                        values.append(item_id)

                        # Execute save
                        await db.execute(sql, values)
                        saved_count += 1

                    except Exception as e:
                        if skip_errors:
                            logger.warning(f"Save error (skipped): {e}")
                            continue
                        raise

        return saved_count

    async def save(self, item: SchemaType, skip_errors: bool = True) -> bool:
        """Store a schema object with upsert functionality and error handling.

        Args:
            item: The schema object to save
            skip_errors: If True, silently skip errors and return False. If False, raise errors.

        Returns:
            True if save was successful, False if error occurred and skip_errors=True
        """
        try:
            if not isinstance(item, self.schema_class):
                if skip_errors:
                    return False
                raise TypeError(f"Expected {self.schema_class.__name__}, got {type(item).__name__}")

            # Extract and process data
            data = asdict(item)
            item_id = data.pop('id', None) or str(uuid.uuid4())

            # Ensure created_at and updated_at are set
            now = datetime.now()
            if not data.get('created_at'):
                data['created_at'] = now
            data['updated_at'] = now

            # Prepare SQL and values
            field_names = tuple(sorted(data.keys()))
            sql = self._generate_save_sql(field_names)
            values = [self._serialize_value(data[name]) for name in field_names]
            values.append(item_id)

            # Perform save with reliable transaction
            async with self._write_lock:
                async with self.transaction() as db:
                    await db.execute(sql, values)

            return True

        except Exception as e:
            if skip_errors:
                logger.warning(f"Save error (skipped): {e}")
                return False
            raise
    
    async def get_by_id(self, id: str) -> Optional[SchemaType]:
        """Fetch an item by ID with reliable connection handling."""
        async with self.transaction() as db:
            cursor = await db.execute(f"SELECT * FROM {self.table_name} WHERE id = ?", (id,))
            row = await cursor.fetchone()
            
            if not row:
                return None
                
            # Get column names and build data dictionary
            columns = [desc[0] for desc in cursor.description]
            return self.schema_class(**{
                col: self._deserialize_value(col, row[i]) 
                for i, col in enumerate(columns)
            })
    
    def _build_where_clause(self, filters: Dict[str, Any]) -> Tuple[str, List[Any]]:
        """Build optimized WHERE clause for queries."""
        if not filters:
            return "", []
            
        conditions = []
        values = []
        
        for key, value in filters.items():
            # Handle special values
            if value == 'now':
                value = datetime.now()
                
            # Parse field and operator
            parts = key.split('__', 1)
            field = parts[0]
            
            if len(parts) > 1 and parts[1] in self.OPERATOR_MAP:
                op_str = self.OPERATOR_MAP[parts[1]]
                
                # Handle IN operator specially
                if op_str == 'IN' and isinstance(value, (list, tuple)):
                    placeholders = ','.join(['?'] * len(value))
                    conditions.append(f"{field} IN ({placeholders})")
                    values.extend(value)
                else:
                    conditions.append(f"{field} {op_str} ?")
                    values.append(value)
            else:
                # Default to equality
                conditions.append(f"{field} = ?")
                values.append(value)
                
        return f"WHERE {' AND '.join(conditions)}", values
    
    async def find(self, order_by=None, **filters) -> List[SchemaType]:
        """Query items with reliable connection handling."""
        where_clause, values = self._build_where_clause(filters)
        
        # Build query
        query = f"SELECT * FROM {self.table_name} {where_clause}"
        
        # Add ORDER BY clause if specified
        if order_by:
            order_fields = [order_by] if isinstance(order_by, str) else order_by
            order_clauses = [
                f"{field[1:]} DESC" if field.startswith('-') else f"{field} ASC" 
                for field in order_fields
            ]
            query += f" ORDER BY {', '.join(order_clauses)}"
        
        # Execute query with reliable transaction
        async with self.transaction() as db:
            cursor = await db.execute(query, values)
            rows = await cursor.fetchall()
            
            if not rows:
                return []
                
            # Process results
            columns = [desc[0] for desc in cursor.description]
            return [
                self.schema_class(**{
                    col: self._deserialize_value(col, row[i])
                    for i, col in enumerate(columns)
                })
                for row in rows
            ]
    
    async def count(self, **filters) -> int:
        """Count items matching filters with reliable connection handling."""
        where_clause, values = self._build_where_clause(filters)
        query = f"SELECT COUNT(*) FROM {self.table_name} {where_clause}"
        
        async with self.transaction() as db:
            cursor = await db.execute(query, values)
            result = await cursor.fetchone()
            return result[0] if result else 0
    
    async def fetch_all(self) -> List[SchemaType]:
        """Retrieve all items."""
        return await self.find()
    
    async def delete(self, id: str) -> bool:
        """Delete an item by ID with reliable transaction handling."""
        async with self._write_lock:
            async with self.transaction() as db:
                cursor = await db.execute(f"DELETE FROM {self.table_name} WHERE id = ?", (id,))
                return cursor.rowcount > 0