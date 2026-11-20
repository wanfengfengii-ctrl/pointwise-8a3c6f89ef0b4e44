from dataclasses import dataclass

from close.calendar import parse_day
from close.revisions import Revision, append_revision, get_revision
from intake.station_batches import station_day_group, station_position_total
from meter_core import ConflictError, InputError, require_name, transaction


@dataclass(frozen=True, slots=True)
class StationCut:
    revision: Revision
    positions: dict


def init_station_cut_schema(connection):
    connection.execute('''
        CREATE TABLE IF NOT EXISTS station_cuts (
            seal_id TEXT NOT NULL REFERENCES revisions (seal_id) ON DELETE CASCADE,
            day TEXT NOT NULL,
            station TEXT NOT NULL,
            position INTEGER NOT NULL CHECK (position >= 0),
            PRIMARY KEY (seal_id, station)
        ) WITHOUT ROWID
    ''')


def _validated_positions(positions, group):
    if not isinstance(positions, dict):
        raise InputError('positions must be a dictionary of station to position')
    if set(positions) != set(group):
        raise InputError('positions must cover exactly the registered station group')
    validated = {}
    for station, position in positions.items():
        require_name(station, 'station')
        if type(position) is not int or position < 0:
            raise InputError('position must be a non-negative integer')
        validated[station] = position
    return validated


def seal_station_cut(connection, day, seal_id, positions):
    day = parse_day(day)
    seal_id = require_name(seal_id, 'seal_id')
    with transaction(connection, write=True):
        group = station_day_group(connection, day)
        if group is None:
            raise InputError(f'day is not registered as a station day: {day!r}')
        validated = _validated_positions(positions, group)
        total = 0
        for station in group:
            total += station_position_total(connection, day, station, validated[station])
        revision = append_revision(connection, day, seal_id, total, 0)
        connection.executemany(
            'INSERT INTO station_cuts VALUES (?, ?, ?, ?)',
            ((seal_id, day, station, validated[station]) for station in group),
        )
        return StationCut(revision, dict(validated))


def station_cut_for(connection, seal_id):
    rows = connection.execute(
        'SELECT station, position FROM station_cuts WHERE seal_id = ? ORDER BY station',
        (seal_id,),
    ).fetchall()
    if not rows:
        return None
    revision = get_revision(connection, seal_id)
    if revision is None:
        return None
    return StationCut(revision, {station: position for station, position in rows})


def lookup_station_cut(connection, seal_id):
    seal_id = require_name(seal_id, 'seal_id')
    with transaction(connection):
        return station_cut_for(connection, seal_id)


def require_ordinary_day(connection, day):
    group = station_day_group(connection, day)
    if group is not None:
        raise ConflictError(f'day is a station-cut day: {day!r}')
