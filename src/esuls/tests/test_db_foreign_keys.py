"""Foreign key dichiarate fra due AsyncDB sullo stesso file.

Era il buco: `metadata={"foreign_key": ...}` esisteva, ma ogni AsyncDB
costruiva la propria MetaData, quindi SQLAlchemy non trovava mai la tabella
puntata e la DDL moriva con NoReferencedTableError. Nel layout che la libreria
incoraggia — un AsyncDB per tabella — la funzionalità era indichiarabile.
"""
from dataclasses import dataclass, field
from typing import Optional

import pytest
import sqlalchemy.exc as sa_exc
from sqlalchemy import text

from esuls.db_cli import AsyncDB, TimestampedModel


@dataclass
class Parent(TimestampedModel):
    name: str = field(default=None)


@dataclass
class Child(TimestampedModel):
    parent_id: str = field(default=None, metadata={
        "index": True, "foreign_key": "parent.id", "on_delete": "CASCADE"})
    label: str = field(default=None)


@pytest.fixture
def dbs(tmp_path):
    path = tmp_path / "graph.db"
    # Il figlio è costruito PRIMA del padre di proposito: la FK si risolve alla
    # DDL, non alla costruzione, quindi l'ordine di dichiarazione non conta.
    child = AsyncDB(path, "child", Child)
    parent = AsyncDB(path, "parent", Parent)
    return parent, child


async def test_the_constraint_reaches_the_schema(dbs):
    parent, child = dbs
    await parent.save(Parent(name="p"), skip_errors=False)
    async with child.transaction(read_only=True) as conn:
        ddl = (await conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='child'"
        ))).scalar()
    assert "REFERENCES parent" in ddl
    assert "ON DELETE CASCADE" in ddl


async def test_an_orphan_is_refused(dbs):
    """PRAGMA foreign_keys=ON è sempre stato attivo; ora c'è un vincolo da
    far rispettare."""
    parent, child = dbs
    await parent.save(Parent(name="p"), skip_errors=False)     # crea lo schema
    with pytest.raises(sa_exc.IntegrityError):
        await child.save(Child(parent_id="non-esiste", label="x"), skip_errors=False)


async def test_on_delete_cascade_is_enforced_by_the_database(dbs):
    parent, child = dbs
    p = Parent(name="p")
    await parent.save(p, skip_errors=False)
    await child.save(Child(parent_id=p.id, label="a"), skip_errors=False)
    await child.save(Child(parent_id=p.id, label="b"), skip_errors=False)
    assert await child.count() == 2

    await parent.delete(p.id)
    assert await child.count() == 0     # cancellati da SQLite, non dalla app


async def test_the_parent_table_is_created_even_if_only_the_child_is_used(dbs):
    """create_all gira su tutta la MetaData del file, in ordine di dipendenza:
    senza questo il figlio nascerebbe prima del padre e il primo INSERT
    fallirebbe con 'no such table'."""
    parent, child = dbs
    p = Parent(name="p")
    # si tocca SOLO il figlio: è la sua init a dover creare anche `parent`
    await child.count()
    async with child.transaction(read_only=True) as conn:
        names = {r[0] for r in (await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ))).fetchall()}
    assert {"parent", "child"} <= names


async def test_redeclaring_the_same_table_is_fine(tmp_path):
    """Un modulo importato due volte, o una fixture ricostruita, non deve
    esplodere."""
    path = tmp_path / "again.db"
    a = AsyncDB(path, "parent", Parent)
    b = AsyncDB(path, "parent", Parent)
    assert a._table is b._table
    await a.save(Parent(name="x"), skip_errors=False)
    assert await b.count() == 1


async def test_redeclaring_a_table_with_a_new_schema_replaces_the_old_one(tmp_path):
    """Condividere la MetaData non deve rompere il drift: dentro un processo,
    "la dataclass ha una colonna in più di quando la tabella è stata creata" si
    presenta esattamente così — due classi diverse sullo stesso nome. L'ultima
    dichiarazione vince, ed è quella che il retrofit riconcilia col database."""
    @dataclass
    class ParentV2(TimestampedModel):
        name: str = field(default=None)
        note: Optional[str] = field(default=None)

    path = tmp_path / "drift.db"
    v1 = AsyncDB(path, "parent", Parent)
    await v1.save(Parent(name="p"), skip_errors=False)
    await v1.close()

    v2 = AsyncDB(path, "parent", ParentV2)
    assert "note" in v2._table.columns
    assert v2._table is not v1._table
    row = await v2.find_one(name="p")
    assert row is not None and row.note is None      # colonna aggiunta a caldo


async def test_same_table_name_in_two_files_stays_independent(tmp_path):
    """La MetaData è per FILE: due database possono avere una tabella con lo
    stesso nome e schemi diversi, che è normale."""
    @dataclass
    class Other(TimestampedModel):
        totally_different: Optional[int] = field(default=None)

    a = AsyncDB(tmp_path / "one.db", "parent", Parent)
    b = AsyncDB(tmp_path / "two.db", "parent", Other)
    assert a._table is not b._table
    await a.save(Parent(name="x"), skip_errors=False)
    await b.save(Other(totally_different=1), skip_errors=False)
    assert await a.count() == 1 and await b.count() == 1


async def test_a_foreign_key_to_a_table_nobody_declared_says_so(tmp_path):
    """L'errore di SQLAlchemy da solo non dice cosa fare."""
    @dataclass
    class Dangling(TimestampedModel):
        ghost_id: str = field(default=None, metadata={"foreign_key": "ghost.id"})

    db = AsyncDB(tmp_path / "dangling.db", "dangling", Dangling)
    with pytest.raises(sa_exc.NoReferencedTableError, match="must be imported"):
        await db.count()
