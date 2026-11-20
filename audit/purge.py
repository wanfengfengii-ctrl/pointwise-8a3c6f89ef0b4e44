from dataclasses import dataclass

from audit.retention import CoveredSource, eligible_source_batches
from close.calendar import parse_day
from intake.source_detail import delete_source_detail
from meter_core import transaction


@dataclass(frozen=True, slots=True)
class PurgeResult:
    cutoff: str
    removed: tuple[CoveredSource, ...]


@dataclass(frozen=True, slots=True)
class PurgeEntry:
    batch_id: str
    covering_seal_id: str
    cutoff: str


def _entries_schema_sql(name):
    return f'''
        CREATE TABLE IF NOT EXISTS {name} (
            batch_id TEXT PRIMARY KEY NOT NULL,
            covering_seal_id TEXT NOT NULL REFERENCES revisions (seal_id),
            cutoff TEXT NOT NULL
        )
    '''


def init_purge_schema(connection):
    existing = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'purge_entries'",
    ).fetchone()
    if existing is not None:
        references_batches = any(
            row[2] == 'batches'
            for row in connection.execute('PRAGMA foreign_key_list(purge_entries)')
        )
        if references_batches:
            connection.execute('ALTER TABLE purge_entries RENAME TO purge_entries_legacy')
            connection.execute(_entries_schema_sql('purge_entries'))
            connection.execute('''
                INSERT INTO purge_entries (batch_id, covering_seal_id, cutoff)
                SELECT batch_id, covering_seal_id, cutoff FROM purge_entries_legacy
            ''')
            connection.execute('DROP TABLE purge_entries_legacy')
            return
    connection.execute(_entries_schema_sql('purge_entries'))


def purge(connection, cutoff):
    cutoff = parse_day(cutoff)
    with transaction(connection, write=True):
        eligible = eligible_source_batches(connection, cutoff)
        for source in eligible:
            delete_source_detail(connection, source.batch_id)
            connection.execute('''
                DELETE FROM station_source_rows WHERE batch_id = ?
            ''', (source.batch_id,))
            connection.execute('INSERT INTO purge_entries VALUES (?, ?, ?)', (
                source.batch_id, source.covering_seal_id, cutoff,
            ))
        return PurgeResult(cutoff, eligible)


def purge_history(connection):
    rows = connection.execute('''
        SELECT p.batch_id, p.covering_seal_id, p.cutoff
        FROM purge_entries AS p
        LEFT JOIN batches AS b ON b.batch_id = p.batch_id
        LEFT JOIN station_batches AS s ON s.batch_id = p.batch_id
        ORDER BY COALESCE(b.day, s.day), COALESCE(b.sequence, s.sequence)
    ''')
    return tuple(PurgeEntry(*row) for row in rows)
