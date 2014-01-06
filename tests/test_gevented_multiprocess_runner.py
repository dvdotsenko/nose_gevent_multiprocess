import unittest
import imp
import os
import sys
import pickle
sys.path.append('.')

from nose.loader import TestLoader
from nose.suite import ContextSuite

from nose import case
from nose.result import TextTestResult
from nose.config import Config

from nose_gevented_multiprocess import nose_gevented_multiprocess as gmultiprocess

class T_fixt:

    @classmethod
    def setupClass(cls):
        pass

    def test_a(self):
        pass
    def test_b(self):
        pass


class T:
    def test_a(self):
        pass
    def test_b(self):
        pass


class TC(unittest.TestCase):
    __test__ = False

    @classmethod
    def tearDownClass(cls):
        super(TC, cls).tearDownClass()

    def tearDown(self):
        super(TC, self).tearDown()

    def runTest(self):
        pass

    def runBadTest(self):
        raise Exception('Blahbidy blah!')

    def runFailingTest(self):
        assert False

class TestMultiProcessTestRunner(unittest.TestCase):

    def setUp(self):
        self.runner = gmultiprocess.GeventedMultiProcessTestRunner(
            #stream=_WritelnDecorator(sys.stdout),
            verbosity=10,
            loaderClass=TestLoader,
            config=Config()
        )
        self.loader = TestLoader()

    def test_next_batch_with_classes(self):
        tests = list(self.runner.get_test_batch(
            ContextSuite(
                tests=[
                    self.loader.makeTest(T_fixt),
                    self.loader.makeTest(T)
                ]
            )
        ))
        print tests
        self.assertEqual(len(tests), 3)

    def test_next_batch_with_module_fixt(self):
        mod_with_fixt = imp.new_module('mod_with_fixt')
        sys.modules['mod_with_fixt'] = mod_with_fixt

        def teardown():
            pass

        class Test(T):
            pass

        mod_with_fixt.Test = Test
        mod_with_fixt.teardown = teardown
        Test.__module__ = 'mod_with_fixt'

        tests = list(self.runner.get_test_batch(
            self.loader.loadTestsFromModule(mod_with_fixt)
        ))
        print tests
        self.assertEqual(len(tests), 1)

    def test_next_batch_with_module(self):
        mod_no_fixt = imp.new_module('mod_no_fixt')
        sys.modules['mod_no_fixt'] = mod_no_fixt

        class Test2(T):
            pass

        class Test_fixt(T_fixt):
            pass

        mod_no_fixt.Test = Test2
        Test2.__module__ = 'mod_no_fixt'
        mod_no_fixt.Test_fixt = Test_fixt
        Test_fixt.__module__ = 'mod_no_fixt'

        tests = list(self.runner.get_test_batch(
            self.loader.loadTestsFromModule(mod_no_fixt)
        ))
        print tests
        self.assertEqual(len(tests), 3)

    def test_next_batch_with_generator_method(self):
        class Tg:
            def test_gen(self):
                for i in range(0, 3):
                    yield self.check, i
            def check(self, val):
                pass
        tests = list(self.runner.get_test_batch(self.loader.makeTest(Tg)))
        print tests
        print [gmultiprocess.get_test_case_address(t) for t in tests]
        self.assertEqual(len(tests), 1)

    def test_next_batch_can_split_set(self):

        mod_with_fixt2 = imp.new_module('mod_with_fixt2')
        sys.modules['mod_with_fixt2'] = mod_with_fixt2

        def setup():
            pass

        class Test(T):
            pass

        class Test_fixt(T_fixt):
            pass

        mod_with_fixt2.Test = Test
        mod_with_fixt2.Test_fixt = Test_fixt
        mod_with_fixt2.setup = setup
        mod_with_fixt2._multiprocess_can_split_ = True
        Test.__module__ = 'mod_with_fixt2'
        Test_fixt.__module__ = 'mod_with_fixt2'

        tests = list(self.runner.get_test_batch(
            self.loader.loadTestsFromModule(mod_with_fixt2)
        ))
        print tests
        self.assertEqual(len(tests), 3)

    def test_runner_collect_tests(self):

        test = ContextSuite(
            tests=[
                self.loader.makeTest(T_fixt),
                self.loader.makeTest(T),
                case.Test(TC('runTest'))
            ]
        )

        kw = dict(
            tasks_queue = [],
            tasks_list = [],
            to_teardown = [],
            result = None
        )

        self.runner.collect_tasks(test, **kw)

        # Tasks Queue

        should_be_tasks = [
            'T_fixt',
            'T.test_a',
            'T.test_b',
            'TC.runTest'
        ]
        tasks = [
            addr.split(':')[-1]
            for addr, args in kw['tasks_queue']
        ]
        self.assertEqual(
            tasks,
            should_be_tasks
        )

        # Tasks List

        should_be_tasks = [
            'T_fixt',
            'T.test_a()', # args is a tuple. appended as string to base addr
            'T.test_b()', # args is a tuple. appended as string to base addr
            'TC.runTest'
        ]
        tasks = [
            addr.split(':')[-1]
            for addr in kw['tasks_list']
        ]
        self.assertEqual(
            tasks,
            should_be_tasks
        )

        # TODO: add test for tearDown capture and for results being filled with failures

class TestingClientTestRunner(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        super(TestingClientTestRunner, cls).setUpClass()

        loaderClass = TestLoader
        resultClass = TextTestResult
        config = Config()

        cls.task_runner = gmultiprocess.TestProcessorTaskRunner(
            "worker_id",
            "server_port"
        )
        cls.task_runner._set_up_base_test_components(loaderClass, resultClass, config)

    def test_process_task(self):

        test_address =  gmultiprocess.get_test_case_address(case.Test(TC('runTest')))
        test_address, result = self.task_runner.process_task((test_address, None))

        self.assertEqual(
            result.testsRun,
            1
        )
        self.assertEqual(
            len(result.errors),
            0
        )
        self.assertEqual(
            len(result.failures),
            0
        )

        test_address =  gmultiprocess.get_test_case_address(case.Test(TC('runBadTest')))
        test_address, result = self.task_runner.process_task((test_address, None))

        self.assertEqual(
            result.testsRun,
            1
        )
        self.assertEqual(
            len(result.errors),
            1
        )

        test_address =  gmultiprocess.get_test_case_address(case.Test(TC('runFailingTest')))
        test_address, result = self.task_runner.process_task((test_address, None))

        self.assertEqual(
            result.testsRun,
            1
        )
        self.assertEqual(
            len(result.failures),
            1
        )

        #result_parts = gmultiprocess.unpack_result_object(result)
        #serialized_result_parts = pickle.dumps(result_parts)


class TestingHelperFunctions(unittest.TestCase):

    def test_get_test_case_address(self):
        test = case.Test(TC('runTest'))
        address = gmultiprocess.get_test_case_address(test)

        filename = os.path.abspath(os.path.join('.', __file__))
        should_be_address = filename + ':' + TC.__name__ + '.' + TC.runTest.__name__

        self.assertEqual(
            address,
            should_be_address
        )

    def test_add_task_to_queue(self):
        test = case.Test(TC('runTest'))
        should_be_address = gmultiprocess.get_test_case_address(test)

        tasks_queue = []
        tasks_list = []
        address = gmultiprocess.add_task_to_queue(test, tasks_queue, tasks_list)

        self.assertEqual(
            address,
            should_be_address
        )

        self.assertEqual(
            tasks_queue,
            [(address, None)]
        )

        self.assertEqual(
            tasks_list,
            [address]
        )


if __name__ == '__main__':
    unittest.main()
