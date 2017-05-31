import pickle
import sys
sys.path.append('.')

import unittest

import requests
import jsonrpcparts

from StringIO import StringIO

from nose import case
from nose.suite import ContextSuite
from nose.plugins.skip import SkipTest
from nose.config import Config
from nose.loader import TestLoader
from nose.result import TextTestResult

from nose_gevented_multiprocess import nose_gevented_multiprocess as gmultiprocess


try:
    # 2.7+
    from unittest.runner import _WritelnDecorator
except ImportError:
    from unittest import _WritelnDecorator


def setup(mod):
    if not gmultiprocess._gevent_patch():
        raise SkipTest("gevented multiprocessing is not available")


class T(unittest.TestCase):
    __test__ = False

    def runTest(self):
        pass

    def runBadTest(self):
        raise Exception('Blahbidy blah!')

    def runFailingTest(self):
        assert False


class GMultiplocessTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        #setup(None)
        super(GMultiplocessTestCase, cls).setUpClass()


class TestingBaseServerAndClientComponents(GMultiplocessTestCase):

    def setUp(self):
        super(TestingBaseServerAndClientComponents, self).setUp()
        self.queue_manager = gmultiprocess.TestsQueueManager()

    def test_queue_manager_get_task(self):
        queue_manager = self.queue_manager

        queue_manager.tasks_queue.put(('a', None))
        queue_manager.tasks_queue.put(('b', None))

        self.assertEqual(
            queue_manager._get_next_task(1),
            ('a', None)
        )
        self.assertEqual(
            queue_manager['get_next_task'](1),
            ('b', None)
        )
        self.assertEqual(
            queue_manager._get_next_task(1),
            None
        )

    def test_queue_manager_store_results(self):
        queue_manager = self.queue_manager

        queue_manager.tasks_queue.put(('a', None))
        queue_manager.tasks_queue.put(('b', None))

        # Storing results depends on task timer having been started here:
        queue_manager._get_next_task(1)
        queue_manager._get_next_task(1)

        queue_manager._store_results(1, 'a')
        queue_manager['store_results'](1, 'b')

        self.assertEqual(
            queue_manager.results_queue.get(),
            (1, 'a')
        )
        self.assertEqual(
            queue_manager.results_queue.get(),
            (1, 'b')
        )
        self.assertEqual(
            queue_manager.results_queue.empty(),
            True
        )


class MockRunnerClient(gmultiprocess.BaseTaskRunner):

    @staticmethod
    def process_task(task_data):
        return task_data


def start_task_runner(process_id, server_port):

    task_runner = MockRunnerClient(process_id, server_port)
    task_runner.run_until_done()


#class TestingQueueServer(GMultiplocessTestCase):
#
#    def test_server_clients_start_and_are_served(self):
#        """
#        At the core of the test runner is the WSGI + JSON-RPC server
#        serving queue elements to clients. This test insures that pings from
#        clents are responded to.
#        """
#
#        queue_manager = gmultiprocess.TasksQueueManager()
#        server = gmultiprocess.WSGIServer(queue_manager)
#
#        queue_manager.tasks_queue.put('a')
#        queue_manager.tasks_queue.put('b')
#        queue_manager.tasks_queue.put('c')
#
#        server_port = server.start()
#
#        gmultiprocess.run_clients(1, server_port)
#
#        self.assertEqual(
#            queue_manager.results_queue.qsize(),
#            3
#        )
#
#        results = [queue_manager.results_queue.next() for _ in range(queue_manager.results_queue.qsize())]
#
#        self.assertEqual(
#            results,
#            [(0, u'a'),(0, u'b'),(0, u'c')]
#        )


class TestingFullCycle(GMultiplocessTestCase):

    @classmethod
    def setUp(self):
        super(TestingFullCycle, self).setUpClass()
        self.tests = ContextSuite(
            tests=[
                case.Test(T('runTest')),
                case.Test(T('runBadTest')),
                case.Test(T('runFailingTest'))
            ]
        )

    #def test_server_clients_full_run(self):
    #    """
    #    At the core of the test runner is the WSGI + JSON-RPC server
    #    serving queue elements to clients. This test insures that pings from
    #    clents are responded to.
    #    """
    #
    #    loaderClass = TestLoader
    #    resultClass = TextTestResult
    #    config = Config()
    #
    #    tests_queue = []
    #    main_test_runner_process = gmultiprocess.GeventedMultiProcessTestRunner(stream=sys.stdout)
    #
    #    main_test_runner_process.collect_tasks(self.tests, tests_queue, [], [], None)
    #
    #    queue_manager = gmultiprocess.TasksQueueManager(tests_queue)
    #    server = gmultiprocess.WSGIServer(queue_manager)
    #    server_port = server.start()
    #
    #    gmultiprocess.run_clients(
    #        1, server_port, loaderClass, resultClass, config
    #    )
    #
    #    self.assertEqual(
    #        queue_manager.results_queue.qsize(),
    #        3
    #    )
    #
    #    results = [queue_manager.results_queue.next() for _ in range(queue_manager.results_queue.qsize())]
    #
    #    test_addresses = set()
    #    for worker_id, response in results:
    #        test_address, serialized_result_obj = response
    #        test_addresses.add(test_address)
    #
    #    self.assertEqual(
    #        test_addresses,
    #        {test_tuple[0] for test_tuple in tests_queue}
    #    )

    def test_plugin_full_run(self):
        """
        At the core of the test runner is the WSGI + JSON-RPC server
        serving queue elements to clients. This test insures that pings from
        clents are responded to.
        """

        runner = gmultiprocess.GeventedMultiProcessTestRunner(stream=StringIO())
        runner.config.options.gevented_processes = 1
        runner.config.options.gevented_timing_file = None
        runner.config.options.gevented_timeout = 60
        result = runner.run(self.tests)

        self.assertEqual(
            result.testsRun,
            3
        )
        self.assertEqual(
            len(result.errors),
            1
        )
        self.assertEqual(
            len(result.failures),
            1
        )


if __name__ == '__main__':
    setup(None)
    unittest.main()
