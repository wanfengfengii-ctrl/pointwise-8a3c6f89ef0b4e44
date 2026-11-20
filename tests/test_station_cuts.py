import json
import sqlite3
from pathlib import Path

from audit.lookup import lookup
from audit.purge import purge, purge_history
from audit.workspace import initialize_workspace, open_workspace
from close.finalize import lookup_station_cut, seal, seal_station_cut
from dispatch.submit import advance
from intake.batches import (
    apply, receive_station_batch, register_stations, station_day_group,
)
from intake.station_batches import station_batch_exists
from meter_core import ConflictError, InputError
from receiver.balances import balance
from tests.support import DAY, NEXT, WorkspaceTestCase, rejected_commit

SAMPLE = json.loads(
    (Path(__file__).resolve().parent.parent / 'samples' / 'station_cuts.json')
    .read_text(encoding='utf-8')
)


def receive(connection, batch_id):
    batch = SAMPLE['batches'][batch_id]
    return receive_station_batch(
        connection, SAMPLE['day'], batch['station'], batch['position'], batch_id,
        batch['kind'], batch['rows'],
    )


class StationCutTests(WorkspaceTestCase):
    def register(self, connection):
        self.assertIsNone(register_stations(connection, SAMPLE['day'], SAMPLE['stations']))

    def test_sample_joint_scenario(self):
        day = SAMPLE['day']
        with self.open() as work:
            self.register(work.local)
            for batch_id in SAMPLE['arrival_before_gap_fill']:
                receive(work.local, batch_id)
            gap = receive(work.local, 'E3')
            self.assertEqual(
                (gap.state, gap.applied_through, gap.blocked_position, gap.blocked_by),
                ('pending', 1, 2, 'missing'),
            )
            blocked = SAMPLE['blocked_cut']
            with self.assertRaises(ConflictError):
                seal_station_cut(work.local, day, blocked['seal_id'], blocked['positions'])

            first = SAMPLE['first_seal']
            cut = seal_station_cut(work.local, day, first['seal_id'], first['positions'])
            self.assertEqual(cut.revision.total, first['total'])
            self.assertEqual(cut.positions, first['positions'])
            self.assertEqual(lookup_station_cut(work.local, first['seal_id']).revision, cut.revision)
            self.assertEqual(lookup_station_cut(work.local, first['seal_id']).positions, cut.positions)
            self.assertIsNone(lookup_station_cut(work.local, 'no-such-cut'))

            removed = purge(work.local, SAMPLE['cutoff']).removed
            expected = SAMPLE['first_purge']
            self.assertEqual(
                [(item.batch_id, item.covering_seal_id) for item in removed],
                [(batch_id, seals[0]) for batch_id, seals in expected.items()],
            )
        with self.open() as work:
            filled = receive(work.local, SAMPLE['gap_fill'])
            self.assertEqual((filled.state, filled.applied_through), ('applied', 3))
            self.assertEqual(receive(work.local, 'E3').state, 'applied')
            self.assertEqual(receive(work.local, 'E3').applied_through, 3)
            for more in SAMPLE['more_seals']:
                revision = seal_station_cut(
                    work.local, day, more['seal_id'], more['positions'],
                ).revision
                self.assertEqual(revision.total, more['total'])
            removed = purge(work.local, SAMPLE['cutoff']).removed
            actual = {}
            for item in removed:
                actual.setdefault(item.batch_id, []).append(item.covering_seal_id)
            self.assertEqual(set(actual), set(SAMPLE['second_purge']))
            for batch_id, seals in SAMPLE['second_purge'].items():
                self.assertIn(actual[batch_id][0], seals)

            after = SAMPLE['seal_after_purge']
            cut = seal_station_cut(work.local, day, after['seal_id'], after['positions'])
            self.assertEqual(cut.revision.total, after['total'])
            deltas = []
            while True:
                result = advance(work.local, work.external, day)
                if result.state == 'idle':
                    break
                deltas.append(result.receipt.delta)
            self.assertEqual(deltas, SAMPLE['posting_deltas'])
            self.assertEqual(lookup(work.local, after['seal_id']).revision, cut.revision)

            invalid = SAMPLE['after_invalid']
            for batch_id in invalid['arrival']:
                result = receive(work.local, batch_id)
                expected = invalid['states'][batch_id]
                self.assertEqual(
                    (result.state, result.applied_through,
                     result.blocked_position, result.blocked_by),
                    (expected['state'], expected['applied_through'],
                     expected['blocked_position'], expected['blocked_by']),
                )
            valid = invalid['valid_cut']
            valid_revision = seal_station_cut(
                work.local, day, valid['seal_id'], valid['positions'],
            ).revision
            self.assertEqual(valid_revision.total, valid['total'])
            with self.assertRaises(ConflictError):
                seal_station_cut(
                    work.local, day, invalid['invalid_cut']['seal_id'],
                    invalid['invalid_cut']['positions'],
                )
            self.assertIsNone(lookup_station_cut(work.local, invalid['invalid_cut']['seal_id']))

    def test_register_rules(self):
        with self.open() as work:
            for bad in ([], (), 'east', ['east', 'east'], ['east', '']):
                with self.subTest(stations=bad), self.assertRaises(InputError):
                    register_stations(work.local, DAY, bad)
            self.assertIsNone(register_stations(work.local, DAY, ['east', 'west']))
            self.assertIsNone(register_stations(work.local, DAY, ('west', 'east')))
            self.assertEqual(station_day_group(work.local, DAY), ('east', 'west'))
            with self.assertRaises(ConflictError):
                register_stations(work.local, DAY, ['east'])
            with self.assertRaises(InputError):
                register_stations(work.local, '20250301', ['east'])

            apply(work.local, NEXT, 'ordinary', 'initial', [('east', 'a', 1)])
            with self.assertRaises(ConflictError):
                register_stations(work.local, NEXT, ['east'])
            seal(work.local, NEXT, 'ordinary-seal')
            apply_other = '2025-03-03'
            apply(work.local, apply_other, 'ordinary-2', 'initial', [('east', 'a', 1)])
            with self.assertRaises(ConflictError):
                register_stations(work.local, apply_other, ['east'])

    def test_receive_validation_and_identity(self):
        with self.open() as work:
            with self.assertRaises(InputError):
                receive_station_batch(
                    work.local, DAY, 'east', 1, 'X', 'initial', [('east', 'a', 1)],
                )
            self.register(work.local)
            for station, position, rows in (
                ('north', 1, [('north', 'a', 1)]),
                ('east', 0, [('east', 'a', 1)]),
                ('east', True, [('east', 'a', 1)]),
                ('east', 1.0, [('east', 'a', 1)]),
                ('east', 1, [('west', 'a', 1)]),
                ('east', 1, [('east', 'a', True)]),
            ):
                with self.subTest(station=station, position=position, rows=rows), \
                        self.assertRaises(InputError):
                    receive_station_batch(
                        work.local, DAY, station, position, 'X', 'initial', rows,
                    )
            apply_day = '2025-03-05'
            apply(work.local, apply_day, 'ordinary', 'initial', [('east', 'a', 1)])
            with self.assertRaises(ConflictError):
                apply(work.local, DAY, 'ordinary', 'initial', [('east', 'a', 1)])
            with self.assertRaises(ConflictError):
                apply(work.local, apply_day, 'E1', 'initial', [('east', 'a', 1)])

            first = receive_station_batch(
                work.local, DAY, 'east', 1, 'A', 'initial', [('east', 'a', 5)],
            )
            self.assertEqual((first.state, first.applied_through), ('applied', 1))
            self.assertEqual(
                receive_station_batch(
                    work.local, DAY, 'east', 1, 'A', 'initial', [('east', 'a', 5)],
                ),
                first,
            )
            with self.assertRaises(ConflictError):
                receive_station_batch(
                    work.local, DAY, 'east', 1, 'A', 'initial', [('east', 'a', 6)],
                )
            with self.assertRaises(ConflictError):
                receive_station_batch(
                    work.local, DAY, 'west', 1, 'A', 'initial', [('west', 'a', 1)],
                )
            receive_station_batch(
                work.local, DAY, 'east', 2, 'B', 'correction', [('east', 'a', 9)],
            )
            with self.assertRaises(ConflictError):
                receive_station_batch(
                    work.local, DAY, 'east', 2, 'C', 'correction', [('east', 'a', 1)],
                )
            with self.assertRaises(ConflictError):
                receive_station_batch(
                    work.local, DAY, 'east', 1, 'B', 'correction', [('east', 'a', 9)],
                )

    def test_invalid_batch_blocks_and_others_continue(self):
        with self.open() as work:
            self.register(work.local)
            receive_station_batch(
                work.local, DAY, 'east', 1, 'A', 'initial', [('east', 'a', 5)],
            )
            invalid = receive_station_batch(
                work.local, DAY, 'east', 2, 'B', 'correction', [('east', 'absent', 1)],
            )
            self.assertEqual(
                (invalid.state, invalid.applied_through, invalid.blocked_by),
                ('invalid', 1, 'invalid'),
            )
            waiting = receive_station_batch(
                work.local, DAY, 'east', 3, 'C', 'correction', [('east', 'a', 9)],
            )
            self.assertEqual((waiting.state, waiting.applied_through), ('pending', 1))
            west = receive_station_batch(
                work.local, DAY, 'west', 1, 'W1', 'initial', [('west', 'a', 4)],
            )
            self.assertEqual((west.state, west.applied_through), ('applied', 1))
            with self.assertRaises(ConflictError):
                seal_station_cut(work.local, DAY, 'cut-invalid', {'east': 3, 'west': 1})
            cut = seal_station_cut(work.local, DAY, 'cut-ok', {'east': 1, 'west': 1})
            self.assertEqual(cut.revision.total, 9)
            # An invalid result persists on identical replay.
            self.assertEqual(
                receive_station_batch(
                    work.local, DAY, 'east', 2, 'B', 'correction', [('east', 'absent', 1)],
                ),
                invalid,
            )
            # Invalid positions keep their original identity.
            with self.assertRaises(ConflictError):
                receive_station_batch(
                    work.local, DAY, 'east', 2, 'D', 'correction', [('east', 'a', 1)],
                )
            # Only the covered prefix is purged; the invalid position stays.
            removed = {item.batch_id for item in purge(work.local, NEXT).removed}
            self.assertEqual(removed, {'A', 'W1'})
            self.assertTrue(station_batch_exists(work.local, 'B'))
            self.assertEqual(
                work.local.execute(
                    "SELECT COUNT(*) FROM station_source_rows WHERE batch_id = 'B'",
                ).fetchone(),
                (1,),
            )

    def test_empty_and_same_value_batches_advance(self):
        with self.open() as work:
            self.register(work.local)
            first = receive_station_batch(
                work.local, DAY, 'east', 1, 'A', 'initial', [('east', 'a', 5)],
            )
            empty = receive_station_batch(
                work.local, DAY, 'east', 2, 'B', 'correction', [],
            )
            same = receive_station_batch(
                work.local, DAY, 'east', 3, 'C', 'correction', [('east', 'a', 5)],
            )
            self.assertEqual((first.state, empty.state, same.state), ('applied',) * 3)
            self.assertEqual(same.applied_through, 3)
            cut = seal_station_cut(work.local, DAY, 'cut', {'east': 0, 'west': 0})
            self.assertEqual(cut.revision.total, 0)
            cut = seal_station_cut(work.local, DAY, 'cut2', {'east': 3, 'west': 0})
            self.assertEqual(cut.revision.total, 5)

    def test_cut_vector_selection_and_validation(self):
        with self.open() as work:
            self.register(work.local)
            for position, value in ((1, 2), (2, 7), (3, 9)):
                batch_id = f'E{position}'
                receive_station_batch(
                    work.local, DAY, 'east', position, batch_id,
                    'initial' if position == 1 else 'correction', [('east', 'a', value)],
                )
            receive_station_batch(
                work.local, DAY, 'west', 1, 'W1', 'initial', [('west', 'a', 4)],
            )
            early = seal_station_cut(work.local, DAY, 'S1', {'east': 1, 'west': 1})
            later = seal_station_cut(work.local, DAY, 'S2', {'east': 3, 'west': 1})
            self.assertEqual((early.revision.total, later.revision.total), (6, 13))
            self.assertEqual((early.revision.sequence, later.revision.sequence), (1, 2))
            self.assertEqual(early.revision.source_watermark, 0)
            older = seal_station_cut(work.local, DAY, 'S3', {'east': 2, 'west': 0})
            self.assertEqual((older.revision.total, older.revision.sequence), (7, 3))
            self.assertEqual(lookup_station_cut(work.local, 'S1').positions, {'east': 1, 'west': 1})
            self.assertEqual(lookup_station_cut(work.local, 'S3').positions, {'east': 2, 'west': 0})

            for seal_id, positions in (
                ('bad-extra', {'east': 1, 'west': 1, 'north': 0}),
                ('bad-missing', {'east': 1}),
                ('bad-bool', {'east': True, 'west': 0}),
                ('bad-negative', {'east': -1, 'west': 0}),
                ('bad-type', {'east': 1, 'west': '0'}),
                ('bad-unapplied', {'east': 4, 'west': 0}),
            ):
                with self.subTest(seal_id=seal_id), self.assertRaises(InputError if seal_id != 'bad-unapplied' else ConflictError):
                    seal_station_cut(work.local, DAY, seal_id, positions)
            with self.assertRaises(InputError):
                seal_station_cut(work.local, DAY, 'bad-shape', [('east', 1)])
            with self.assertRaises(InputError):
                seal_station_cut(work.local, NEXT, 'unregistered', {'east': 0, 'west': 0})
            with self.assertRaises(ConflictError):
                seal_station_cut(work.local, DAY, 'S1', {'east': 1, 'west': 1})
            self.assertEqual(lookup_station_cut(work.local, 'S1').revision, early.revision)

    def test_cut_uses_historical_values_at_each_position(self):
        with self.open() as work:
            self.register(work.local)
            receive_station_batch(
                work.local, DAY, 'east', 1, 'A', 'initial', [('east', 'a', 2), ('east', 'b', 3)],
            )
            receive_station_batch(
                work.local, DAY, 'east', 2, 'B', 'correction', [('east', 'a', 10)],
            )
            receive_station_batch(
                work.local, DAY, 'east', 3, 'C', 'correction', [('east', 'a', 7)],
            )
            # The current total is 10; earlier vectors must keep their own snapshots.
            first = seal_station_cut(work.local, DAY, 'S1', {'east': 1, 'west': 0})
            second = seal_station_cut(work.local, DAY, 'S2', {'east': 2, 'west': 0})
            third = seal_station_cut(work.local, DAY, 'S3', {'east': 3, 'west': 0})
            self.assertEqual(
                [item.revision.total for item in (first, second, third)], [5, 13, 10],
            )
            # Purging does not lose the snapshots needed by existing cuts.
            purge(work.local, NEXT)
            self.assertEqual(
                [lookup_station_cut(work.local, seal_id).revision.total
                 for seal_id in ('S1', 'S2', 'S3')],
                [5, 13, 10],
            )

    def test_cut_rejects_ordinary_revision_lookup(self):
        with self.open() as work:
            apply(work.local, DAY, 'ordinary', 'initial', [('east', 'a', 5)])
            revision = seal(work.local, DAY, 'ordinary-seal')
            self.assertIsNone(lookup_station_cut(work.local, revision.seal_id))
            self.assertIsNotNone(lookup(work.local, revision.seal_id))

    def test_purge_station_batches_and_replay(self):
        with self.open() as work:
            self.register(work.local)
            rows = [('east', 'a', 5)]
            receive_station_batch(work.local, DAY, 'east', 1, 'A', 'initial', rows)
            received = receive_station_batch(
                work.local, DAY, 'east', 2, 'B', 'correction', [('east', 'a', 8)],
            )
            pending = receive_station_batch(
                work.local, DAY, 'east', 3, 'C', 'correction', [('east', 'a', 11)],
            )
            self.assertEqual(pending.state, 'applied')
            # No coverage yet.
            self.assertEqual(purge(work.local, NEXT).removed, ())
            cut = seal_station_cut(work.local, DAY, 'S1', {'east': 1, 'west': 0})
            removed = purge(work.local, NEXT).removed
            self.assertEqual([(item.batch_id, item.covering_seal_id) for item in removed],
                             [('A', 'S1')])
            self.assertEqual(
                work.local.execute(
                    'SELECT COUNT(*) FROM station_source_rows WHERE batch_id = ?', ('A',),
                ).fetchone(),
                (0,),
            )
            self.assertTrue(station_batch_exists(work.local, 'A'))
            # Replay still matches the original parameters after the payload is gone.
            replay = receive_station_batch(work.local, DAY, 'east', 1, 'A', 'initial', rows)
            self.assertEqual((replay.state, replay.applied_through), ('applied', 3))
            with self.assertRaises(ConflictError):
                receive_station_batch(work.local, DAY, 'east', 1, 'A', 'initial', [('east', 'a', 6)])
            # Later seal can select an earlier position; earlier coverage stands.
            seal_station_cut(work.local, DAY, 'S2', {'east': 3, 'west': 0})
            seal_station_cut(work.local, DAY, 'S3', {'east': 1, 'west': 0})
            removed = purge(work.local, NEXT).removed
            self.assertEqual([(item.batch_id, item.covering_seal_id) for item in removed],
                             [('B', 'S2'), ('C', 'S2')])
            self.assertEqual([entry.batch_id for entry in purge_history(work.local)],
                             ['A', 'B', 'C'])
            # Pending, blocked and never-covered batches stay.
            seal_station_cut(work.local, DAY, 'S4', {'east': 1, 'west': 0})
            self.assertEqual(purge(work.local, NEXT).removed, ())
            self.assertEqual(
                work.local.execute('SELECT COUNT(*) FROM station_source_rows').fetchone(),
                (0,),
            )

    def test_purge_station_rollback_and_reopen(self):
        day = SAMPLE['day']
        with self.open() as work:
            self.register(work.local)
            for batch_id in SAMPLE['arrival_before_gap_fill']:
                receive(work.local, batch_id)
            first = SAMPLE['first_seal']
            seal_station_cut(work.local, day, first['seal_id'], first['positions'])
            receive(work.local, SAMPLE['gap_fill'])
            seal_station_cut(work.local, day, 'S2', {'east': 3, 'west': 2})
            before = work.local.execute(
                'SELECT batch_id FROM station_source_rows ORDER BY batch_id, ordinal',
            ).fetchall()
            with rejected_commit(work.local), self.assertRaises(sqlite3.DatabaseError):
                purge(work.local, SAMPLE['cutoff'])
            after = work.local.execute(
                'SELECT batch_id FROM station_source_rows ORDER BY batch_id, ordinal',
            ).fetchall()
            self.assertEqual(before, after)
            self.assertEqual(purge_history(work.local), ())

        initialize_workspace(self.root)
        with self.open() as work:
            # First cut may be signed after reopening on an old vector.
            earlier = seal_station_cut(work.local, day, 'S3', {'east': 1, 'west': 1})
            self.assertEqual(earlier.revision.total, 6)
            removed = purge(work.local, SAMPLE['cutoff']).removed
            self.assertTrue(removed)
            self.assertTrue(all(item.covering_seal_id for item in removed))
            self.assertEqual([entry.batch_id for entry in purge_history(work.local)],
                             [item.batch_id for item in removed])

        initialize_workspace(self.root)
        with self.open() as work:
            # Sealed cuts and receipts stay retrievable by number after the purge.
            stored = lookup_station_cut(work.local, 'S3')
            self.assertEqual((stored.revision, stored.positions), (earlier.revision, {'east': 1, 'west': 1}))
            reviews = [lookup(work.local, seal_id) for seal_id in ('S1', 'S2', 'S3')]
            self.assertTrue(all(review is not None for review in reviews))
            self.assertEqual(
                [review.revision.total for review in reviews], [8, 14, 6],
            )

    def test_receive_commit_failure_leaves_nothing(self):
        with self.open() as work:
            self.register(work.local)
            with rejected_commit(work.local), self.assertRaises(sqlite3.DatabaseError):
                receive_station_batch(
                    work.local, DAY, 'east', 1, 'A', 'initial', [('east', 'a', 5)],
                )
            self.assertFalse(station_batch_exists(work.local, 'A'))
            self.assertEqual(
                work.local.execute('SELECT COUNT(*) FROM station_source_rows').fetchone(),
                (0,),
            )
            result = receive_station_batch(
                work.local, DAY, 'east', 1, 'A', 'initial', [('east', 'a', 5)],
            )
            self.assertEqual((result.state, result.applied_through), ('applied', 1))
            with rejected_commit(work.local), self.assertRaises(sqlite3.DatabaseError):
                seal_station_cut(work.local, DAY, 'S1', {'east': 1, 'west': 0})
            self.assertIsNone(lookup_station_cut(work.local, 'S1'))
            self.assertEqual(
                seal_station_cut(work.local, DAY, 'S1', {'east': 1, 'west': 0}).revision.total,
                5,
            )

    def test_station_day_requires_station_entry_points(self):
        with self.open() as work:
            self.register(work.local)
            with self.assertRaises(ConflictError):
                apply(work.local, DAY, 'ordinary', 'initial', [('east', 'a', 1)])
            receive_station_batch(
                work.local, DAY, 'east', 1, 'A', 'initial', [('east', 'a', 1)],
            )
            with self.assertRaises(ConflictError):
                seal(work.local, DAY, 'ordinary-seal')
            self.assertIsNone(lookup(work.local, 'ordinary-seal'))
            with self.assertRaises(InputError):
                receive_station_batch(
                    work.local, NEXT, 'east', 1, 'N1', 'initial', [('east', 'a', 1)],
                )
            with self.assertRaises(InputError):
                seal_station_cut(work.local, NEXT, 'N2', {'east': 0})
