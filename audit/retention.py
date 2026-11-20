from dataclasses import dataclass

from close.calendar import parse_day
from close.revisions import latest_revision
from intake.source_detail import list_source_keys


@dataclass(frozen=True, slots=True)
class CoveredSource:
    batch_id: str
    covering_seal_id: str


def _ordinary_coverage(connection, cutoff):
    coverage_by_day = {}
    for source in list_source_keys(connection, cutoff):
        if source.day not in coverage_by_day:
            coverage_by_day[source.day] = latest_revision(connection, source.day)
        covering = coverage_by_day[source.day]
        if covering is not None and source.sequence <= covering.source_watermark:
            yield CoveredSource(source.batch_id, covering.seal_id)


def _station_coverage(connection, cutoff):
    rows = connection.execute('''
        SELECT b.batch_id, b.day, b.station, b.position
        FROM station_batches AS b
        LEFT JOIN purge_entries AS p ON p.batch_id = b.batch_id
        WHERE b.day < ? AND b.status = 'applied' AND p.batch_id IS NULL
        ORDER BY b.day, b.sequence
    ''', (cutoff,)).fetchall()
    for batch_id, day, station, position in rows:
        covering = connection.execute('''
            SELECT c.seal_id
            FROM station_cuts AS c JOIN revisions AS r ON r.seal_id = c.seal_id
            WHERE c.day = ? AND c.station = ? AND c.position >= ?
            ORDER BY r.sequence, c.seal_id
            LIMIT 1
        ''', (day, station, position)).fetchone()
        if covering is not None:
            yield CoveredSource(batch_id, covering[0])


def eligible_source_batches(connection, cutoff):
    cutoff = parse_day(cutoff)
    candidates = list(_ordinary_coverage(connection, cutoff))
    candidates.extend(_station_coverage(connection, cutoff))
    return tuple(candidates)
