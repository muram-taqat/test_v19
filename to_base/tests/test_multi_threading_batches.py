from odoo.tests import tagged, TransactionCase

from ..helper.multi_threading import Threading


@tagged('post_install', '-at_install')
class TestThreadingBatches(TransactionCase):
    """`max_batch_size` is a promise made to the decorated method, not a way of
    dividing work between threads.

    A method decorated with `max_batch_size=1` is written for a single record
    and calls ensure_one(). It gets one thread whenever the records all live on
    the same server, or whenever the thread budget - which shrinks with the free
    db connections - comes out at one; and it gets none at all in test mode.
    None of that changes what the method was written to accept.
    """

    def setUp(self):
        super(TestThreadingBatches, self).setUp()
        self.records = self.env['res.partner'].create([
            {'name': 'Threading Batch %s' % i} for i in range(5)
        ])
        self.seen = []

    def _one_at_a_time(self, **kwargs):
        seen = self.seen

        @Threading(max_batch_size=1, **kwargs)
        def method(records):
            records.ensure_one()
            seen.append(records.id)
            return records
        return method

    def test_01_a_single_record_method_is_given_one_record(self):
        """Reported from production as `Expected singleton: odoo.instance(88803,
        88670, ...)` out of the SSL certificate cron, which is decorated exactly
        this way."""
        self._one_at_a_time()(self.records)
        self.assertEqual(self.seen, self.records.ids)

    def test_02_the_records_come_back_to_the_caller(self):
        result = self._one_at_a_time()(self.records)
        self.assertEqual(result, self.records,
                         'callers read the result of the whole run, not of the last batch')

    def test_03_a_method_returning_nothing_gets_its_recordset_back(self):
        @Threading(max_batch_size=2)
        def method(records):
            return None
        self.assertEqual(method(self.records), self.records)

    def test_04_a_batch_size_larger_than_the_work_changes_nothing(self):
        """The one case that already worked must keep behaving to the letter,
        including what a method returns when it is not a recordset."""
        calls = []

        @Threading(max_batch_size=10)
        def method(records):
            calls.append(len(records))
            return 'a plain result'
        self.assertEqual(method(self.records), 'a plain result')
        self.assertEqual(calls, [5], 'one call, with everything in it')

    def test_05_batches_are_bounded_by_the_size_asked_for(self):
        sizes = []

        @Threading(max_batch_size=2)
        def method(records):
            sizes.append(len(records))
        method(self.records)
        self.assertEqual(sum(sizes), 5)
        self.assertTrue(all(size <= 2 for size in sizes), sizes)

    def test_06_an_empty_recordset_is_still_one_call(self):
        calls = []

        @Threading(max_batch_size=1)
        def method(records):
            calls.append(records)
        method(self.env['res.partner'])
        self.assertEqual(len(calls), 1, 'a method may rely on being called at all')
