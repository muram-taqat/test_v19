from odoo.tests import tagged, TransactionCase

from ..helper.multi_threading import pick_rebalanced_group, Threading


@tagged('post_install', '-at_install')
class TestRebalancedScheduling(TransactionCase):
    """The work-stealing scheduler behind `Threading(rebalance=True)`.

    Real threads are disabled in Odoo's test mode, so the pure picking
    function IS the behaviour under test: it fully determines which group
    every freed worker serves next.
    """

    def test_serves_home_group_first(self):
        remaining = {'s1': 5, 's2': 30, 's3': 5, 's4': 5}
        in_flight = {'s1': 0, 's2': 0, 's3': 0, 's4': 0}
        # even though s2 has the most work, a worker with work at home stays home
        self.assertEqual(pick_rebalanced_group(remaining, in_flight, 's1', cap=4), 's1')

    def test_drained_home_helps_the_busiest_group(self):
        # s1's worker drained its queue: it must go help s2, the busiest
        remaining = {'s1': 0, 's2': 30, 's3': 5, 's4': 5}
        in_flight = {'s1': 1, 's2': 1, 's3': 1, 's4': 1}
        self.assertEqual(pick_rebalanced_group(remaining, in_flight, 's1', cap=4), 's2')

    def test_last_group_standing_takes_every_thread_up_to_cap(self):
        remaining = {'s1': 0, 's2': 10, 's3': 0, 's4': 0}
        in_flight = {'s1': 0, 's2': 3, 's3': 0, 's4': 0}
        # the 4th worker joins too: 4 concurrent on the last busy server
        self.assertEqual(pick_rebalanced_group(remaining, in_flight, 's4', cap=4), 's2')
        # but never a 5th
        in_flight['s2'] = 4
        self.assertIsNone(pick_rebalanced_group(remaining, in_flight, 's4', cap=4))

    def test_everything_drained_returns_none(self):
        remaining = {'s1': 0, 's2': 0}
        in_flight = {'s1': 0, 's2': 0}
        self.assertIsNone(pick_rebalanced_group(remaining, in_flight, 's1', cap=4))

    def test_tie_prefers_more_idle_capacity(self):
        remaining = {'s1': 0, 's2': 7, 's3': 7}
        in_flight = {'s1': 0, 's2': 2, 's3': 1}
        self.assertEqual(pick_rebalanced_group(remaining, in_flight, 's1', cap=4), 's3')

    def test_full_drain_simulation_matches_the_described_shape(self):
        """Simulate a whole night: 4 servers, one very loaded; workers pick via
        the real function. The invariants of the requested design must hold:
        start at one worker per server, never exceed the cap on any server, and
        the busiest server ends up served by every worker."""
        remaining = {'s1': 40, 's2': 6, 's3': 6, 's4': 6}
        cap = 4
        workers = {'w1': 's1', 'w2': 's2', 'w3': 's3', 'w4': 's4'}  # home per worker
        in_flight = dict.fromkeys(remaining, 0)
        running = {}   # worker -> group currently served
        max_seen = dict.fromkeys(remaining, 0)
        finished_order = []

        for _tick in range(200):
            # complete everything in flight (1 tick per batch)
            for worker, group in list(running.items()):
                in_flight[group] -= 1
                del running[worker]
            # every idle worker picks its next batch
            for worker, home in workers.items():
                if worker in running:
                    continue
                group = pick_rebalanced_group(remaining, in_flight, home, cap)
                if group is None:
                    continue
                remaining[group] -= 1
                in_flight[group] += 1
                running[worker] = group
                max_seen[group] = max(max_seen[group], in_flight[group])
            for group in list(remaining):
                if remaining[group] == 0 and in_flight[group] == 0 and group not in finished_order:
                    finished_order.append(group)
            if not any(remaining.values()) and not running:
                break

        self.assertFalse(any(remaining.values()), "the pool must drain everything")
        # small servers drain first; the loaded one finishes last
        self.assertEqual(finished_order[-1], 's1')
        # the loaded server ended up served by all four workers...
        self.assertEqual(max_seen['s1'], cap)
        # ...and no server ever exceeded the cap
        self.assertTrue(all(seen <= cap for seen in max_seen.values()))

    def test_decorator_accepts_rebalance_flag(self):
        decorator = Threading(threaded_by='company_id', max_threads_per_threaded=4, rebalance=True)
        self.assertTrue(decorator.rebalance)
        self.assertFalse(Threading().rebalance)
