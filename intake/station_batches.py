import hashlib
import json
from dataclasses import dataclass

from close.calendar import parse_day
from intake.observations import (
    current_amount, insert_observation, iter_station_amounts, replace_current_amount,
)
from intake.validate import validate_batch
from meter_core import (
    ConflictError, InputError, decode_litres, encode_litres, require_name, transaction,
)


@dataclass(frozen=True, slots=True)
class StationBatchResult:
    batch_id: str
    state: str
    applied_through: int
    blocked_position: int
    blocked_by: str


def init_station_batch_schema(connection):
    connection.execute('''
        CREATE TABLE IF NOT EXISTS station_days (
            day TEXT PRIMARY KEY NOT NULL
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS station_group (
            day TEXT NOT NULL REFERENCES station_days (day) ON DELETE CASCADE,
            station TEXT NOT NULL,
            PRIMARY KEY (day, station)
        ) WITHOUT ROWID
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS station_batches (
            batch_id TEXT PRIMARY KEY NOT NULL,
            day TEXT NOT NULL,
            station TEXT NOT NULL,
            position INTEGER NOT NULL CHECK (position > 0),
            sequence INTEGER NOT NULL CHECK (sequence > 0),
            kind TEXT NOT NULL CHECK (kind IN ('initial', 'correction')),
            fingerprint TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'applied', 'invalid')),
            applied_total TEXT,
            UNIQUE (day, station, position),
            UNIQUE (day, sequence)
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS station_source_rows (
            batch_id TEXT NOT NULL REFERENCES station_batches (batch_id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            station TEXT NOT NULL,
            name TEXT NOT NULL,
            litres TEXT NOT NULL,
            PRIMARY KEY (batch_id, ordinal)
        ) WITHOUT ROWID
    ''')


def station_day_group(connection, day):
    registered = connection.execute(
        'SELECT 1 FROM station_days WHERE day = ?', (day,),
    ).fetchone()
    if registered is None:
        return None
    rows = connection.execute(
        'SELECT station FROM station_group WHERE day = ? ORDER BY station', (day,),
    )
    return tuple(station for (station,) in rows)


def is_station_day(connection, day):
    return connection.execute(
        'SELECT 1 FROM station_days WHERE day = ?', (day,),
    ).fetchone() is not None


def station_batch_exists(connection, batch_id):
    return connection.execute(
        'SELECT 1 FROM station_batches WHERE batch_id = ?', (batch_id,),
    ).fetchone() is not None


def register_stations(connection, day, stations):
    day = parse_day(day)
    if not isinstance(stations, (list, tuple)) or not stations:
        raise InputError('stations must be a non-empty list or tuple of station names')
    names = [require_name(station, 'station') for station in stations]
    if len(set(names)) != len(names):
        raise InputError('stations must not repeat a station name')
    requested = frozenset(names)
    with transaction(connection, write=True):
        registered = station_day_group(connection, day)
        if registered is not None:
            if frozenset(registered) != requested:
                raise ConflictError(f'station group is fixed for {day!r}')
            return None
        if connection.execute(
            'SELECT 1 FROM batches WHERE day = ? LIMIT 1', (day,),
        ).fetchone() is not None:
            raise ConflictError(f'day already has ordinary batches: {day!r}')
        if connection.execute(
            'SELECT 1 FROM revisions WHERE day = ? LIMIT 1', (day,),
        ).fetchone() is not None:
            raise ConflictError(f'day already has finalizations: {day!r}')
        connection.execute('INSERT INTO station_days (day) VALUES (?)', (day,))
        connection.executemany(
            'INSERT INTO station_group (day, station) VALUES (?, ?)',
            ((day, station) for station in sorted(requested)),
        )
    return None


def _fingerprint(day, batch_id, station, position, kind, rows):
    encoded = [(row_station, name, encode_litres(value)) for row_station, name, value in rows]
    canonical = json.dumps(
        [day, batch_id, station, position, kind, encoded],
        ensure_ascii=True, separators=(',', ':'),
    )
    return hashlib.sha256(canonical.encode('ascii')).hexdigest()


def _station_total(connection, day, station):
    return sum(iter_station_amounts(connection, day, station), start=0)


def _applied_through(connection, day, station):
    (through,) = connection.execute(
        '''
        SELECT COALESCE(MAX(position), 0) FROM station_batches
        WHERE day = ? AND station = ? AND status = 'applied'
        ''',
        (day, station),
    ).fetchone()
    return through


def _frontier(connection, day, station):
    applied_through = _applied_through(connection, day, station)
    blocked_position = applied_through + 1
    row = connection.execute(
        '''
        SELECT status FROM station_batches
        WHERE day = ? AND station = ? AND position = ?
        ''',
        (day, station, blocked_position),
    ).fetchone()
    blocked_by = 'invalid' if row is not None and row[0] == 'invalid' else 'missing'
    return applied_through, blocked_position, blocked_by


def _source_rows(connection, batch_id):
    rows = connection.execute(
        'SELECT name, litres FROM station_source_rows WHERE batch_id = ? ORDER BY ordinal',
        (batch_id,),
    ).fetchall()
    return tuple((name, decode_litres(litres)) for name, litres in rows)


def _batch_invalid(connection, day, station, kind, rows):
    for name, _ in rows:
        present = current_amount(connection, day, station, name) is not None
        if kind == 'initial':
            if present:
                return True
        elif not present:
            return True
    return False


def _apply_rows(connection, day, station, kind, rows):
    for name, litres in rows:
        if kind == 'initial':
            insert_observation(connection, day, station, name, litres)
        else:
            replace_current_amount(connection, day, station, name, litres)


def _advance_station(connection, day, station):
    while True:
        applied_through = _applied_through(connection, day, station)
        position = applied_through + 1
        row = connection.execute(
            '''
            SELECT batch_id, kind, status FROM station_batches
            WHERE day = ? AND station = ? AND position = ?
            ''',
            (day, station, position),
        ).fetchone()
        if row is None:
            return
        batch_id, kind, status = row
        if status != 'pending':
            return
        rows = _source_rows(connection, batch_id)
        if _batch_invalid(connection, day, station, kind, rows):
            connection.execute(
                'UPDATE station_batches SET status = ? WHERE batch_id = ?',
                ('invalid', batch_id),
            )
            return
        _apply_rows(connection, day, station, kind, rows)
        total = _station_total(connection, day, station)
        connection.execute(
            'UPDATE station_batches SET status = ?, applied_total = ? WHERE batch_id = ?',
            ('applied', encode_litres(total), batch_id),
        )


def _check_validation(day, station, position, batch_id, kind, rows):
    station = require_name(station, 'station')
    if type(position) is not int or position <= 0:
        raise InputError('position must be a positive integer')
    batch = validate_batch(day, batch_id, kind, rows)
    for row_station, _, _ in batch.rows:
        if row_station != station:
            raise InputError('every row station must equal the receiving station')
    return batch, station


def _receive_result(connection, day, station, batch_id):
    (status,) = connection.execute(
        'SELECT status FROM station_batches WHERE batch_id = ?', (batch_id,),
    ).fetchone()
    applied_through, blocked_position, blocked_by = _frontier(connection, day, station)
    return StationBatchResult(batch_id, status, applied_through, blocked_position, blocked_by)


def receive_station_batch(connection, day, station, position, batch_id, kind, rows):
    batch, station = _check_validation(day, station, position, batch_id, kind, rows)
    fingerprint = _fingerprint(
        batch.day, batch.batch_id, station, position, batch.kind, batch.rows,
    )
    with transaction(connection, write=True):
        group = station_day_group(connection, batch.day)
        if group is None:
            raise InputError(f'day is not registered as a station day: {batch.day!r}')
        if station not in group:
            raise InputError(f'station is not in the registered group: {station!r}')
        stored = connection.execute(
            '''
            SELECT day, station, position, kind, fingerprint FROM station_batches
            WHERE batch_id = ?
            ''',
            (batch.batch_id,),
        ).fetchone()
        if stored is not None:
            if stored != (batch.day, station, position, batch.kind, fingerprint):
                raise ConflictError(
                    f'batch identity has different parameters: {batch.batch_id!r}',
                )
            if connection.execute(
                'SELECT 1 FROM batches WHERE batch_id = ?', (batch.batch_id,),
            ).fetchone() is not None:
                raise ConflictError(
                    f'batch identity has different parameters: {batch.batch_id!r}',
                )
            return _receive_result(connection, batch.day, station, batch.batch_id)

        if connection.execute(
            'SELECT 1 FROM batches WHERE batch_id = ?', (batch.batch_id,),
        ).fetchone() is not None:
            raise ConflictError(
                f'batch identity has different parameters: {batch.batch_id!r}',
            )
        occupant = connection.execute(
            '''
            SELECT batch_id FROM station_batches
            WHERE day = ? AND station = ? AND position = ?
            ''',
            (batch.day, station, position),
        ).fetchone()
        if occupant is not None:
            raise ConflictError(
                f'position already received for {batch.day!r} {station!r}: {position}',
            )
        (sequence,) = connection.execute(
            'SELECT COALESCE(MAX(sequence), 0) FROM station_batches WHERE day = ?',
            (batch.day,),
        ).fetchone()
        connection.execute(
            '''
            INSERT INTO station_batches
                (batch_id, day, station, position, sequence, kind, fingerprint, status,
                 applied_total)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL)
            ''',
            (
                batch.batch_id, batch.day, station, position, sequence + 1,
                batch.kind, fingerprint,
            ),
        )
        connection.executemany(
            'INSERT INTO station_source_rows VALUES (?, ?, ?, ?, ?)',
            (
                (batch.batch_id, ordinal, row_station, name, encode_litres(litres))
                for ordinal, (row_station, name, litres) in enumerate(batch.rows, start=1)
            ),
        )
        _advance_station(connection, batch.day, station)
        return _receive_result(connection, batch.day, station, batch.batch_id)


def station_position_total(connection, day, station, position):
    if position == 0:
        return 0
    row = connection.execute(
        '''
        SELECT applied_total FROM station_batches
        WHERE day = ? AND station = ? AND position = ? AND status = 'applied'
        ''',
        (day, station, position),
    ).fetchone()
    if row is None or row[0] is None:
        raise ConflictError(
            f'station position is not fully applied: {(day, station, position)!r}',
        )
    return decode_litres(row[0])
