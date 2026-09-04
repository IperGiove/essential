"""Scritture da PIÙ PROCESSI sullo stesso file SQLite.

Il test che manca quando si sviluppa con un processo solo: dentro un processo
il writer è uno StaticPool di una connessione, quindi le scritture fanno già la
fila in Python e nessun conflitto emerge. Il problema nasce quando un SECONDO
processo apre lo stesso file — N worker uvicorn, un cron, uno script di
manutenzione lanciato mentre il sito gira.

Con un `BEGIN` differito, una transazione che LEGGE e poi SCRIVE deve promuovere
il lock condiviso a esclusivo, e SQLite rifiuta quella promozione con
SQLITE_BUSY SUBITO, senza consultare `busy_timeout`: aspettare significherebbe
un abbraccio mortale, perché entrambe le connessioni terrebbero un lock di
lettura in attesa che l'altra lo molli.

Con `BEGIN IMMEDIATE` il lock si prende all'inizio, non c'è niente da
promuovere, e `busy_timeout` torna a fare il suo mestiere: le scritture si
mettono in fila invece di fallire.
"""
import multiprocessing as mp
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from esuls import AsyncDB, TimestampedModel


@dataclass
class Riga(TimestampedModel):
    valore: str = field(default=None)


def _scrittore(db_path: str, quante: int, coda) -> None:
    """Un processo che legge-e-scrive, che è la forma della quasi totalità
    delle transazioni vere (`save`, `update_fields`, un upsert)."""
    import asyncio

    async def lavora():
        db = AsyncDB(db_path, "riga", Riga)
        ok = busy = 0
        for i in range(quante):
            try:
                async with db.transaction() as conn:
                    from sqlalchemy import text
                    await conn.execute(text("SELECT COUNT(*) FROM riga"))
                    await conn.execute(
                        text("INSERT INTO riga (id, valore) VALUES (:i, :v)"),
                        {"i": f"{os.getpid()}-{i}", "v": "x"})
                ok += 1
            except Exception as e:            # noqa: BLE001 — si conta, non si distingue
                if "locked" in str(e).lower():
                    busy += 1
                else:
                    raise
        await db.close()
        return ok, busy

    coda.put(asyncio.run(lavora()))


@pytest.mark.skipif(not hasattr(os, "fork"), reason="serve fork")
def test_six_processes_writing_at_once_lose_nothing():
    """Sei processi, cento scritture a testa. Nessuna deve andare persa.

    Con il `BEGIN` differito che c'era prima della 0.10 questo test falliva con
    oltre metà delle scritture rifiutate — misurato su un sito vero: 56% di
    HTTP 500 e 246.910 righe di "database is locked" in settanta secondi.
    """
    percorso = str(Path(tempfile.mkdtemp()) / "multi.db")
    # Il primo AsyncDB crea la tabella: i figli devono trovarla già lì.
    import asyncio

    async def prepara():
        db = AsyncDB(percorso, "riga", Riga)
        await db.save(Riga(valore="seme"), skip_errors=False)
        await db.close()

    asyncio.run(prepara())

    ctx = mp.get_context("fork")
    coda = ctx.Queue()
    processi = [ctx.Process(target=_scrittore, args=(percorso, 100, coda))
                for _ in range(6)]
    for p in processi:
        p.start()
    for p in processi:
        p.join(timeout=120)

    scritte = rifiutate = 0
    while not coda.empty():
        a, b = coda.get()
        scritte += a
        rifiutate += b

    righe = sqlite3.connect(percorso).execute("SELECT COUNT(*) FROM riga").fetchone()[0]
    assert rifiutate == 0, f"{rifiutate} scritture rifiutate con 'database is locked'"
    assert scritte == 600, f"solo {scritte} scritture completate su 600"
    assert righe == 601, f"nel file ce ne sono {righe} invece di 601 (600 + il seme)"


def test_the_writer_begins_immediately_and_the_reader_does_not():
    """La differenza fra i due motori è deliberata e va nella direzione giusta.

    Si guarda l'SQL EFFETTIVAMENTE emesso, non una proprietà che gli somiglia:
    la prima stesura di questo test controllava `isolation_level`, che è None su
    entrambi — quindi passava anche senza la correzione, cioè non controllava
    niente.
    """
    from sqlalchemy import event, text

    from esuls.db_cli import _get_engines

    percorso = Path(tempfile.mkdtemp()) / "begin.db"
    writer, reader = _get_engines(percorso)

    emesso: dict[str, list[str]] = {"writer": [], "reader": []}

    def spia(nome):
        def _cb(conn, cursor, statement, *a):
            if statement.upper().startswith("BEGIN"):
                emesso[nome].append(statement.upper())
        return _cb

    event.listen(writer, "before_cursor_execute", spia("writer"))
    event.listen(reader, "before_cursor_execute", spia("reader"))

    with writer.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS t (i INTEGER)"))
    with reader.begin() as conn:
        conn.execute(text("SELECT 1"))

    assert emesso["writer"] == ["BEGIN IMMEDIATE"], emesso["writer"]
    assert emesso["reader"] == ["BEGIN"], emesso["reader"]
