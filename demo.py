import json
from pathlib import Path
from tempfile import TemporaryDirectory

from audit.lookup import lookup
from audit.purge import purge, purge_history
from audit.reconcile import reconcile_posting
from audit.workspace import initialize_workspace, open_workspace
from close.finalize import lookup_station_cut, seal, seal_station_cut
from dispatch.submit import ReplyLost, advance
from intake.batches import apply, receive_station_batch, register_stations
from meter_core import ConflictError
from receiver.balances import balance
from receiver.receipts import receipt_for_request


def lose_reply(receipt):
    raise ReplyLost(f'reply for {receipt.request_id} lost after receiver commit')


def demonstrate(root, sample):
    day = sample['day']
    other_day = sample['other_day']
    initialize_workspace(root)
    with open_workspace(root) as work:
        apply(work.local, day, 'east-initial', 'initial', sample['initial'])
        first = seal(work.local, day, 'east-1')
        advance(work.local, work.external, day)
        apply(work.local, day, 'east-correction-1', 'correction', [sample['corrections'][0]])
        second = seal(work.local, day, 'east-2')
        lost = advance(work.local, work.external, day, after_post=lose_reply)
        pending = lookup(work.local, second.seal_id)
        received = balance(work.external, day)
        original = receipt_for_request(work.external, received.postings[-1].request_id)
        apply(work.local, day, 'east-correction-2', 'correction', [sample['corrections'][1]])
        third = seal(work.local, day, 'east-3')
        removed = purge(work.local, sample['cutoff']).removed

    with open_workspace(root) as work:
        attempts = [advance(work.local, work.external, day) for _ in range(2)]
        reviewed = [lookup(work.local, revision.seal_id) for revision in (first, second, third)]
        account = balance(work.external, day)
        expected = {
            'sealed': [5, 12, 9], 'pending_status': 'pending', 'pending_delta': None,
            'pending_external_balance': 12, 'attempt_order': ['east-2', 'east-3'],
            'confirmed_deltas': [5, 7, -3], 'same_original_receipt': True,
        }
        actual = {
            'sealed': [item.revision.total if item else None for item in reviewed],
            'pending_status': pending.state, 'pending_delta': pending.delta,
            'pending_external_balance': received.total,
            'attempt_order': [attempt.seal_id for attempt in attempts],
            'confirmed_deltas': [item.delta if item else None for item in reviewed],
            'same_original_receipt': original is not None and attempts[0].receipt == original,
        }
        downstream_expected = {'balance': 9, 'posting_deltas': [5, 7, -3], 'purged_batches': 3}
        downstream_actual = {
            'balance': account.total, 'posting_deltas': [entry.delta for entry in account.postings],
            'purged_batches': len(removed),
        }
        if actual != expected or downstream_actual != downstream_expected:
            raise AssertionError(
                f'DAILY_DELTA_MISMATCH actual={actual!r} downstream={downstream_actual!r} '
                f'expected={expected!r} expected_downstream={downstream_expected!r}'
            )
        if lost.state != 'pending' or not reconcile_posting(work.external, reviewed[-1]).balance_matches:
            raise AssertionError('pending transition or final reconciliation failed')

        unchanged = seal(work.local, day, 'east-4')
        zero = advance(work.local, work.external, day)
        apply(work.local, other_day, 'west-initial', 'initial', sample['other_initial'])
        seal(work.local, other_day, 'west-1')
        west = advance(work.local, work.external, other_day)
        east_account = balance(work.external, day)
        west_account = balance(work.external, other_day)
        secondary_actual = {
            'zero_seal': zero.seal_id, 'zero_delta': zero.receipt.delta,
            'distinct_zero_receipt': zero.receipt != attempts[-1].receipt,
            'last_total': unchanged.total, 'east_balance': east_account.total,
            'east_deltas': [posting.delta for posting in east_account.postings],
            'west_balance': west_account.total, 'west_delta': west.receipt.delta,
        }
        secondary_expected = {
            'zero_seal': 'east-4', 'zero_delta': 0, 'distinct_zero_receipt': True,
            'last_total': 9, 'east_balance': 9, 'east_deltas': [5, 7, -3, 0],
            'west_balance': 4, 'west_delta': 4,
        }
        if secondary_actual != secondary_expected:
            raise AssertionError(f'closing ledger actual={secondary_actual!r} expected={secondary_expected!r}')

    evidence = {
        'case_id': 'v5.lost_reply_with_later_revision', 'input': sample,
        'expected': expected, 'actual': actual,
        'downstream_expected': downstream_expected, 'downstream_actual': downstream_actual,
        'code_paths': [
            'intake/batches.py', 'close/finalize.py', 'dispatch/submit.py',
            'dispatch/confirm.py', 'receiver/requests.py', 'receiver/balances.py',
            'audit/lookup.py', 'audit/purge.py',
        ],
        'oracle': '2+3=5; 2+10=12; 2+7=9; adjacent differences are 5, 7, -3.',
    }
    print('POINTWISE_SCENARIO ' + json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    print(json.dumps(secondary_actual, ensure_ascii=False, sort_keys=True))


def _receive_station(work, sample, batch_id):
    batch = sample['batches'][batch_id]
    return receive_station_batch(
        work.local, sample['day'], batch['station'], batch['position'], batch_id,
        batch['kind'], batch['rows'],
    )


def _seal_station(work, sample, sealed):
    return seal_station_cut(
        work.local, sample['day'], sealed['seal_id'], sealed['positions'],
    )


def demonstrate_station_cuts(root, sample):
    day = sample['day']
    initialize_workspace(root)
    with open_workspace(root) as work:
        register_stations(work.local, day, sample['stations'])
        arrivals = [_receive_station(work, sample, batch_id)
                    for batch_id in sample['arrival_before_gap_fill']]
        gap = _receive_station(work, sample, 'E3')
        blocked = sample['blocked_cut']
        try:
            _seal_station(work, sample, blocked)
        except ConflictError:
            blocked_rejected = True
        else:
            blocked_rejected = False
        first = _seal_station(work, sample, sample['first_seal'])
        first_purge = purge(work.local, sample['cutoff']).removed

    with open_workspace(root) as work:
        filled = _receive_station(work, sample, sample['gap_fill'])
        replayed = _receive_station(work, sample, 'E3')
        seals = [_seal_station(work, sample, more) for more in sample['more_seals']]
        second_purge = purge(work.local, sample['cutoff']).removed
        after_purge = _seal_station(work, sample, sample['seal_after_purge'])
        deltas = []
        while True:
            result = advance(work.local, work.external, day)
            if result.state == 'idle':
                break
            deltas.append(result.receipt.delta)
        invalid = sample['after_invalid']
        invalid_states = {}
        for batch_id in invalid['arrival']:
            outcome = _receive_station(work, sample, batch_id)
            invalid_states[batch_id] = {
                'state': outcome.state, 'applied_through': outcome.applied_through,
                'blocked_position': outcome.blocked_position, 'blocked_by': outcome.blocked_by,
            }
        valid_cut = _seal_station(work, sample, invalid['valid_cut'])
        try:
            _seal_station(work, sample, invalid['invalid_cut'])
        except ConflictError:
            invalid_cut_rejected = True
        else:
            invalid_cut_rejected = False
        history = purge_history(work.local)
        old_cut = lookup_station_cut(work.local, sample['first_seal']['seal_id'])
        total_balance = balance(work.external, day).total

    expected = {
        'arrival_states': [item.state for item in arrivals],
        'gap_e3': [gap.state, gap.applied_through, gap.blocked_by],
        'blocked_rejected': True,
        'first_cut_total': sample['first_seal']['total'],
        'first_purge': sorted((item.batch_id, item.covering_seal_id) for item in first_purge),
        'gap_fill': [filled.state, filled.applied_through],
        'replayed_e3': [replayed.state, replayed.applied_through],
        'seal_totals': [more['total'] for more in sample['more_seals']],
        'second_purge': sorted((item.batch_id, item.covering_seal_id) for item in second_purge),
        'after_purge_total': sample['seal_after_purge']['total'],
        'posting_deltas': deltas,
        'invalid_states': invalid_states,
        'valid_cut_total': valid_cut.revision.total,
        'invalid_cut_rejected': True,
        'old_cut_positions': old_cut.positions if old_cut else None,
        'purged_batches': len(history),
        'balance': total_balance,
    }
    actual = {
        'arrival_states': [item.state for item in arrivals],
        'gap_e3': [gap.state, gap.applied_through, gap.blocked_by],
        'blocked_rejected': blocked_rejected,
        'first_cut_total': first.revision.total,
        'first_purge': sorted((item.batch_id, item.covering_seal_id) for item in first_purge),
        'gap_fill': [filled.state, filled.applied_through],
        'replayed_e3': [replayed.state, replayed.applied_through],
        'seal_totals': [item.revision.total for item in seals],
        'second_purge': sorted((item.batch_id, item.covering_seal_id) for item in second_purge),
        'after_purge_total': after_purge.revision.total,
        'posting_deltas': deltas,
        'invalid_states': invalid_states,
        'valid_cut_total': valid_cut.revision.total,
        'invalid_cut_rejected': invalid_cut_rejected,
        'old_cut_positions': old_cut.positions if old_cut else None,
        'purged_batches': len(history),
        'balance': total_balance,
    }
    if actual != expected:
        raise AssertionError(
            f'STATION_CUT_MISMATCH actual={actual!r} expected={expected!r}'
        )
    evidence = {
        'case_id': 'v6.station_cuts_gap_fill_and_purge', 'input': sample,
        'expected': expected, 'actual': actual,
        'code_paths': [
            'intake/station_batches.py', 'close/station_cuts.py', 'audit/retention.py',
            'audit/purge.py', 'dispatch/submit.py',
        ],
        'oracle': 'east a: 2,7,8; west a: 4,6; gap-fill E2 then E3; cuts totals 8,9,14,8,11,14.',
    }
    print('POINTWISE_STATION_CUT ' + json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    print(json.dumps(
        {key: actual[key] for key in (
            'gap_e3', 'gap_fill', 'replayed_e3', 'seal_totals', 'posting_deltas',
            'invalid_states', 'balance',
        )},
        ensure_ascii=False, sort_keys=True,
    ))


def main():
    sample_path = Path(__file__).resolve().parent / 'samples' / 'shift.json'
    sample = json.loads(sample_path.read_text(encoding='utf-8'))
    with TemporaryDirectory(prefix='estuary-demo-') as root:
        demonstrate(root, sample)
    cut_sample_path = Path(__file__).resolve().parent / 'samples' / 'station_cuts.json'
    cut_sample = json.loads(cut_sample_path.read_text(encoding='utf-8'))
    with TemporaryDirectory(prefix='estuary-cut-demo-') as root:
        demonstrate_station_cuts(root, cut_sample)


if __name__ == '__main__':
    main()
