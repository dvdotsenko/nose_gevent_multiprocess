"""
Overview
========

The gevented multiprocess plugin allows you to distribute your tests to be
run among a set of worker processes that run tests in parallel.
This can speed up CPU-bound (as long as the number of work processeses
is around the number of processors or cores available), but is mainly useful
for IO-bound tests that spend most of their time waiting for data to arrive
from someplace else.

This Nose plugin is different from the mainline multiprocess plugin in three
main ways:

1. Mainline multiprocess plugin utilizes multiprocess module's Queue
   objects that are incompatible with Gevent. Removing dependence on these
   allows for running of tests that utilize gevent-based cooperative threads

2. Mainline multiprocess *forks* the main process for each worker.
   This may bring unwelcome data / objects to the worker processes
   and cause contention. This module spawns clean separate python
   processes that load specific tests in isolation from main process.

3. This plugin controls / communicates with the worker processes over
   json-rpc over HTTP. The main process literally starts a light
   web server on a random available port and serves the test payloads
   to workers requesting these over HTTP-based json-rpc requests.
   This approach allows for the workers to talk to master server
   in reliable way (despite socket patching the HTTP is expected to work)
   but introduces exta layers of latency. If you tests are super-short-lived
   it may make sense not to have them in separate case classes, but
   bunched up into larget classes to be sent over as batches. This
   way the milliseconds you gain by parallelizing the test runs are
   not eaten up by wire-time.

How tests are distributed
=========================

The ideal case would be to dispatch each test to a worker process
separately. This ideal is not attainable in all cases, however, because many
test suites depend on context (class, module or package) fixtures.

The plugin can't know (unless you tell it -- see below!) if a context fixture
can be called many times concurrently (is re-entrant), or if it can be shared
among tests running in different processes. Therefore, if a context has
fixtures, the default behavior is to dispatch the entire suite to a worker as
a unit.

Controlling distribution
^^^^^^^^^^^^^^^^^^^^^^^^

There are two context-level variables that you can use to control this default
behavior.

If a context's fixtures are re-entrant, set ``_multiprocess_can_split_ = True``
in the context, and the plugin will dispatch tests in suites bound to that
context as if the context had no fixtures. This means that the fixtures will
execute concurrently and multiple times, typically once per test.

If a context's fixtures can be shared by tests running in different processes
-- such as a package-level fixture that starts an external http server or
initializes a shared database -- then set ``_multiprocess_shared_ = True`` in
the context. These fixtures will then execute in the primary nose process, and
tests in those contexts will be individually dispatched to run in parallel.

How results are collected and reported
======================================

As each test or suite executes in a worker process, results (failures, errors,
and specially handled exceptions like SkipTest) are collected in that
process. When the worker process finishes, it returns results to the main
nose process. There, any progress output is printed (dots!), and the
results from the test run are combined into a consolidated result
set. When results have been received for all dispatched tests, or all
workers have died, the result summary is output as normal.

Beware!
=======

Not all test suites will benefit from, or even operate correctly using, this
plugin. For example, CPU-bound tests will run more slowly if you don't have
multiple processors. There are also some differences in plugin
interactions and behaviors due to the way in which tests are dispatched and
loaded. In general, test loading under this plugin operates as if it were
always in directed mode instead of discovered mode. For instance, doctests
in test modules will always be found when using this plugin with the doctest
plugin.

But the biggest issue you will face is probably concurrency. Unless you
have kept your tests as religiously pure unit tests, with no side-effects, no
ordering issues, and no external dependencies, chances are you will experience
odd, intermittent and unexplainable failures and errors when using this
plugin. This doesn't necessarily mean the plugin is broken; it may mean that
your test suite is not safe for concurrency.

"""
import logging
import os
import sys
import time
import unittest
import pickle
import json
import inspect

sys.path.append('.')

import nose.case
from nose.core import TextTestRunner
from nose import failure
from nose import loader
from nose.plugins.base import Plugin
from nose.pyversion import bytes_
from nose.result import TextTestResult
from nose.suite import ContextSuite
from nose.util import test_address as native_get_test_address

import_errors = []
try:
    import gevent
    import gevent.monkey
    import gevent.pool
    import gevent.server
    import gevent.socket
    import gevent.queue
    import gevent.pywsgi
except ImportError:
    import_errors.append("Unable to import 'gevent' module components.")
try:
    import multiprocessing
except ImportError:
    import_errors.append("Unable to import 'multiprocessing' module.")
try:
    import requests
except ImportError:
    import_errors.append("Unable to import 'requests' module.")
try:
    from jsonrpcparts import WebClient as JSONRPCWebClient
    from jsonrpcparts.wsgiapplication import JSONPRCWSGIApplication
except ImportError:
    import_errors.append("Unable to import 'jsonrpcparts' module.")

from warnings import warn
def _gevent_patch():
    if import_errors:
        warn('\n'.join(import_errors))
        return False
    gevent.monkey.patch_socket()
    gevent.monkey.patch_subprocess()
    return True

try:
    # 2.7+
    from unittest.runner import _WritelnDecorator
except ImportError:
    from unittest import _WritelnDecorator

try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO

# once this plugin is installed with pip/easy_install/setuptools
# it should create this executable script somewhere on the PATH.
CLIENT_RUNNER_FILE_NAME = 'nose_gevented_multiprocess_runner'

# this is a list of plugin classes that will be checked for and created inside
# each worker process
_instantiate_plugins = None

log = logging.getLogger(__name__)

# log.setLevel(logging.INFO)
# console = logging.StreamHandler()
# console.setLevel(logging.INFO)
# log.addHandler(console)

class BaseTaskRunner(object):

    def __init__(self, worker_id, server_port, *args, **kwargs):
        self.worker_id = worker_id
        self.json_rpc_client = JSONRPCWebClient('http://localhost:{}'.format(server_port))

    def setup(self):
        """
        Override this in subclass
        """
        pass

    @staticmethod
    def deserialize_task(serialized_task):
        return serialized_task

    def get_next_task(self):
        answer = self.json_rpc_client.call('get_next_task', self.worker_id)
        if answer:
            return self.deserialize_task(answer)

    @staticmethod
    def serialize_task_result(task_result):
        return task_result

    def send_task_results(self, task_result):
        self.json_rpc_client.notify(
            'store_results',
            self.worker_id,
            self.serialize_task_result(task_result)
        )

    def process_task(self, task, *args, **kwargs):
        raise NotImplemented

    def run_until_done(self):
        log.info("Worker waits for payload: %s" % self.worker_id)

        self.setup()

        task = self.get_next_task()
        while task:
            log.info("Worker received payload: %s #%s" % (self.worker_id, task))
            try:
                task_result = self.process_task(task)
                self.send_task_results(task_result)
            except Exception as ex:
                log.info("Worker caught exception: %s, %s" % (self.worker_id, ex))
            except:
                log.info("Worker caught undefined exception")

            task = self.get_next_task()

        log.info("Worker ending its run: %s" % (self.worker_id))


def serialize_test_case(case):
    base = {
        'shortDescription':case.shortDescription(),
        'repr':str(case)
    }
    try:
        base['id'] = case.id()
    except AttributeError:
        pass
    return base


class TestLet:
    def __init__(self, case_data):
        try:
            self._id = case_data['id']
        except KeyError:
            pass
        self._short_description = case_data['shortDescription']
        self._str = case_data['repr']

    def id(self):
        return self._id

    def shortDescription(self):
        return self._short_description

    def __str__(self):
        return self._str


def unpack_result_object(result):
    """
    Decomposes the test result object into parts that can be serialized
    and sent over wire.
    """

    failures = [(serialize_test_case(c), err) for c, err in result.failures]
    errors = [(serialize_test_case(c), err) for c, err in result.errors]

    errorClasses = {}
    for key, (storage, label, isfail) in result.errorClasses.items():
        errorClasses[key] = (
            [(serialize_test_case(c), err) for c, err in storage],
            label,
            isfail
        )

    return (
        result.stream.getvalue(),
        result.testsRun,
        failures,
        errors,
        errorClasses
    )


def consolidate_batch_results(result, batch_result):
    """
    Merges specific test run results into centralized result instance

    batch_result is a tuple of
        output - string that needs to go to stdout
        testsRun - int of tests run
        failures - list of objects
        errors - list of objects
        errorClasses - object

    :return: batch_result's output string
    """
    # log.debug("batch result is %s" , batch_result)
    try:
        output, testsRun, failures, errors, errorClasses = batch_result
    except ValueError:
        # log.debug("result in unexpected format %s", batch_result)
        failure.Failure(*sys.exc_info())(result)
        return

    result.testsRun += testsRun
    result.failures.extend([(TestLet(c_data), err) for c_data, err in failures])
    result.errors.extend([(TestLet(c_data), err) for c_data, err in errors])

    for key, (storage, label, isfail) in errorClasses.items():
        storage = [(TestLet(c_data), err) for c_data, err in storage]
        entry = result.errorClasses.get(key, ([], label, isfail))
        entry[0].extend(storage)

    return output


# have to inherit KeyboardInterrupt to it will interrupt process properly
class TimedOutException(KeyboardInterrupt):
    def __init__(self, value = "Timed Out"):
        self.value = value
    def __str__(self):
        return repr(self.value)


class NoSharedFixtureContextSuite(ContextSuite):
    """
    Context suite that never fires shared fixtures.

    When a context sets _multiprocess_shared_, fixtures in that context
    are executed by the main process. Using this suite class prevents them
    from executing in the runner process as well.

    """

    def setupContext(self, context):
        if getattr(context, '_multiprocess_shared_', False):
            return
        super(NoSharedFixtureContextSuite, self).setupContext(context)

    def teardownContext(self, context):
        if getattr(context, '_multiprocess_shared_', False):
            return
        super(NoSharedFixtureContextSuite, self).teardownContext(context)


class TestProcessorTaskRunner(BaseTaskRunner):

    def _set_up_base_test_components(self, loader_class, result_class, config):
        self.resultClass = result_class
        self.config = config
        self.prepare_config_plugins(config)
        self.loader = loader_class(config=config)
        #self.loader.suiteClass.suiteClass = ContextSuite

    def setup(self):
        loaderClass_serialized, resultClass_serialized, config_serialized = self.json_rpc_client.call(
            'get_setup_objects', self.worker_id)

        self._set_up_base_test_components(
            pickle.loads(str(loaderClass_serialized)),
            pickle.loads(str(resultClass_serialized)),
            pickle.loads(str(config_serialized))
        )

    @staticmethod
    def prepare_config_plugins(config):
        # we need to process plugins in a worker so they can affect result object
        dummy_parser = config.parserClass()
        if _instantiate_plugins is not None:
            for pluginclass in _instantiate_plugins:
                plugin = pluginclass()
                plugin.addOptions(dummy_parser, {})
                config.plugins.addPlugin(plugin)
        config.plugins.configure(config.options, config)
        config.plugins.begin()

    def create_result_object(self):
        result = self.resultClass(
            _WritelnDecorator(StringIO()),
            descriptions=1,
            verbosity=self.config.verbosity,
            config=self.config
        )
        plug_result = self.config.plugins.prepareTestResult(result)
        if plug_result:
            return plug_result
        return result

    @staticmethod
    def serialize_task_result(task_result):
        """
        :param task_result: A tuple of (test_address, result_object)
        :type task_result: tuple
        :return: tuple of (test_address, pickle_serialized_result_parts_string)
        :rtype: tuple
        """
        test_address, result = task_result
        return test_address, pickle.dumps(unpack_result_object(result))

    def process_task(self, task, *args, **kwargs):
        """
        :param task: A tuple of test address string and tuple of arguments
        :type task: tuple
        :return: a tuple of components from result object
        :rtype: tuple
        """

        test_addr, arg = task

        result = self.create_result_object()
        test = self.loader.loadTestsFromNames([test_addr])

        logging.info("Worker runs test %s '%s'" % (self.worker_id, test_addr))

        try:

            # TODO: figure out what this is about:
            if arg is not None:
                test_addr = test_addr + str(arg)

            test(result)
            return test_addr, result

        #except KeyboardInterrupt as e: #TimedOutException:
        #    timeout = isinstance(e, TimedOutException)
        #
        #    if task_info.pop('test_address',None):
        #        failure.Failure(*sys.exc_info())(result)
        #        if timeout:
        #            msg = 'Worker %s timed out, failing current test %s'
        #        else:
        #            msg = 'Worker %s keyboard interrupt, failing current test %s'
        #        log.exception(msg, worker_id, test_addr)
        #    else:
        #        if timeout:
        #            msg = 'Worker %s test %s timed out'
        #        else:
        #            msg = 'Worker %s test %s keyboard interrupt'
        #        log.debug(msg, worker_id, test_addr)
        #
        #    return (test_addr, unpack_results(result))
        #
        #    if timeout:
        #        keyboardCaught.set()
        #    else:
        #        raise

        #except SystemExit:
        #    task_info.pop('test_address',None)
        #    log.exception('Worker %s system exit',worker_id)
        #    raise

        except:
            failure.Failure(*sys.exc_info())(result)
            return test_addr, result


def start_task_runner(worker_id, server_port, *args, **kwargs):
    """
    Generic task runner starter. This is the entry point a subprocess
    would start with.

    Using it for test runs.
    """
    task_runner = BaseTaskRunner(worker_id, server_port, *args, **kwargs)
    task_runner.run_until_done()

def start_test_processor_task_runner(worker_id, server_port, *args, **kwargs):
    """
    Test processor-specific task runner starter. This is the entry point a subprocess
    would start with.
    """
    if _gevent_patch():
        task_runner = TestProcessorTaskRunner(worker_id, server_port, *args, **kwargs)
        task_runner.run_until_done()


def run_clients(number_of_clients, server_port, *args):
    """
    This function runs on "server" side and starts the needed
    number of test runner processes. It then waits for all
    of these processes to finish and returns.

    :param number_of_clients: Number of subprocesses to run
    :type number_of_clients: int
    :param server_port: Port on which the task server listens for client connections.
    :type server_port: int
    """
    from distutils.spawn import find_executable

    log.info("Starting %s clients\n" % number_of_clients)

    t1 = time.time()

    cwd = os.getcwd()

    # we prefer to run ourselves as client runner.
    runner_script = os.path.abspath(inspect.getfile(inspect.currentframe()))
    if not os.path.exists(runner_script):
        # however when we are installed over setuptools, we
        # end up in an egg and there is no real path to us.
        # for those cases we register us as an executable
        runner_script = find_executable(CLIENT_RUNNER_FILE_NAME)

    assert runner_script

    command_line = '%s "%s" %%s %s' % (sys.executable, runner_script, server_port)

    clients = [
        gevent.subprocess.Popen(command_line % (worker_id + 1), shell=True, cwd=cwd)
        for worker_id in xrange(number_of_clients)
    ]
    for client in clients:
        client.wait()
        log.info("Client %s exiting" % client)
    duration = time.time()-t1
    log.info("%s clients served within %.2f s." % (number_of_clients, duration))


class TasksQueueManager(JSONPRCWSGIApplication):
    """
    This is a "web"(WSGI) application whose methods are exposed as JSON-RPC methods
    """

    def __init__(self, tasks_queue=None, results_queue=None, **kwargs):
        """
        :param tasks_queue: list of task-describing objects to do
        :type tasks_queue: list or tuple
        :param tasks_queue: list of task-describing objects already done
        :type tasks_queue: list or tuple
        """
        super(TasksQueueManager, self).__init__()

        self.tasks_queue = gevent.queue.Queue(items=(tasks_queue or []))
        self.results_queue = gevent.queue.Queue(items=(results_queue or []))

        # registering JSON-RPC handlers
        self['get_next_task'] = self._get_next_task
        self['store_results'] = self._store_results

    def _get_next_task(self, worker_id):
        log.info("Serving next item request for %s" % worker_id)
        log.info("%s items remaining to serve" % self.tasks_queue.qsize())
        if self.tasks_queue.empty():
            return None
        return self.tasks_queue.get()

    def _store_results(self, worker_id, results):
        self.results_queue.put((worker_id, results))
        log.info("Results queue is called by worker %s with %s" % (worker_id, results))


class TestsQueueManager(TasksQueueManager):

    def __init__(self, tasks_queue=None, results_queue=None, loader_class=None, result_class=None, config=None):
        """
        :param tasks_queue: list of task-describing objects to do
        :type tasks_queue: list or tuple
        :param tasks_queue: list of task-describing objects already done
        :type tasks_queue: list or tuple
        """
        super(TestsQueueManager, self).__init__(tasks_queue, results_queue)

        self.loader_class = loader_class
        self.results_class = result_class
        self.config = config

        self['get_setup_objects'] = self._get_nose_set_up_objects

    def _get_nose_set_up_objects(self, worker_id):
        log.info("Serving set up objects to %s" % worker_id)
        return [pickle.dumps(o) for o in (self.loader_class, self.results_class, self.config)]


    def process_test_results(self, remaining_tasks, global_result, output_stream, stop_on_error):

        #completed_tasks = []

        while remaining_tasks:

            sys.stdout.write("Results processor: remaining tasks %s\n" % len(remaining_tasks))

            try:
                worker_id, test_run_results_tuple = self.results_queue.get(timeout=60) # <-- this blocks!!!
            except gevent.queue.Empty:
                break

            test_address, serialized_result_object = test_run_results_tuple
            batch_result = pickle.loads(str(serialized_result_object))

            sys.stdout.write('Results received for worker %d, %s\n' % (worker_id, test_address))

            log.debug('Results received for worker %d, %s', worker_id, test_address)

            try:
                try:
                    remaining_tasks.remove(test_address)
                except ValueError:
                    pass
            except KeyError:
                log.debug("Got result for unknown task? %s", test_address)
                log.debug("current: %s",str(list(remaining_tasks)[0]))
            #else:
            #    completed_tasks.append((test_address, batch_result))

            #remaining_tasks.extend(more_task_addresses)

            output = consolidate_batch_results(global_result, batch_result)
            if output and output_stream:
                output_stream.write(output)

            if (stop_on_error and not global_result.wasSuccessful()):
                # set the stop condition
                # TODO: stop all runs
                break

        #return completed_tasks

    def start_test_results_processor(self, remaining_tasks, global_result, output_stream, stop_on_error):
        """
        Starts test resutls processor worker in the gevent "thread" and returns a "future" one can .join()
        """
        return gevent.spawn(self.process_test_results, remaining_tasks, global_result, output_stream, stop_on_error)


class WSGIServer(object):
    """
    Gevent-based WSGI server in a convenience wrapping that allows to
    start serving on arbitrary port and returns that port number
    when ran.
    """

    def __init__(self, wsgi_application):
        self.wsgi_application = wsgi_application

    def start(self):
        self.server = server = gevent.pywsgi.WSGIServer(
            ('localhost', 0),
            self.wsgi_application,
            log=False
        )
        server.start()
        return server.server_port

    def stop(self):
        self.server.stop()
        self.server.join()

####


def has_shared_fixtures(case):
    context = getattr(case, 'context', None)
    if not context:
        return False
    return getattr(context, '_multiprocess_shared_', False)

def can_split_flag_set(context, fixture):
    """
    Callback that we use to check whether the fixtures found in a
    context or ancestor are ones we care about.

    Contexts can tell us that their fixtures are reentrant by setting
    _multiprocess_can_split_. So if we see that, we return False to
    disregard those fixtures.
    """
    if not fixture:
        return False
    if getattr(context, '_multiprocess_can_split_', False):
        return False
    return True

def get_test_case_address(case):
    """
    Derives a string "address" of the case from its module/file name
    and function name

    :param case: test case object
    :return: String describing the location of the test
    :rtype: str
    """

    if hasattr(case, 'address'):
        file, mod, call = case.address()
    elif hasattr(case, 'context'):
        file, mod, call = native_get_test_address(case.context)
    else:
        raise Exception("Unable to convert %s to address" % case)

    parts = []
    if file is None:
        if mod is None:
            raise Exception("Unaddressable case %s" % case)
        else:
            parts.append(mod)
    else:
        # strip __init__.py(c) from end of file part
        # if present, having it there confuses loader
        dirname, basename = os.path.split(file)
        if basename.startswith('__init__'):
            file = dirname
        parts.append(file)

    if call is not None:
        parts.append(call)

    # do we hate unicode names? TODO: investigate
    return ':'.join(map(str, parts))

def add_task_to_queue(case, tasks_queue, tasks_list):
    """
    Adds a task (by its "address" string) to the global Queue.

    :param case: test case tree object
    :param tasks_queue: task name queue
    :type tasks_queue: list
    :param tasks_list: list of all task "addresses"
    :type tasks_list: list
    :return: String with the test's "address"
    :rtype: str
    """

    arg = None
    if isinstance(case, nose.case.Test) and hasattr(case.test, 'arg'):
        # this removes the top level descriptor and allows real function
        # name to be returned
        case.test.descriptor = None
        arg = case.test.arg

    test_addr = get_test_case_address(case)

    tasks_queue.append((test_addr, arg))

    if arg is not None:
        test_addr += str(arg)
    tasks_list.append(test_addr)

    return test_addr


class GeventedMultiProcess(Plugin):
    """
    Run tests in gevent-aware multiple processes.

    Requires gevent and gipc modules

    --with-gevented-multiprocess option enables this plugin
    --gevented-workers option controls the number of parallel workers
    """

    score = 0
    status = {}
    enabled = False
    name = 'gevented-multiprocess'

    def options(self, parser, env):
        """
        Register command-line options.
        """
        parser.add_option(
            "--gevented-processes",
            action="store",
            default=env.get('NOSE_GEVENTED_PROCESSES', 0),
            dest="gevented_processes",
            metavar="NUM",
            help="Spread test run among this many processes. "
                "Set a number equal to the number of processors "
                "in your machine for best results. "
                "Pass a negative number to have the number of "
                "processes automatically set to the number of "
                "cores. Passing 0 disables parallel "
                "testing. Default is 0 unless NOSE_GEVENTED_PROCESSES is "
                "set. "
                "[NOSE_GEVENTED_PROCESSES]")

    def configure(self, options, config):
        """
        Configure plugin.
        """

        if import_errors:
            self.enabled = False
            return

        try:
            workers = int(options.gevented_processes)
        except (AttributeError, TypeError, ValueError):
            workers = 0

        if workers and _gevent_patch():
            self.enabled = True
        else:
            self.enabled = False
            return

        if workers < 0:
            workers = multiprocessing.cpu_count()

        self.config = config

        options.gevented_processes = workers

    def prepareTestLoader(self, loader):
        """Remember loader class so MultiProcessTestRunner can instantiate
        the right loader.
        """
        self.loaderClass = loader.__class__

    def prepareTestRunner(self, runner):
        """Replace test runner with MultiProcessTestRunner.
        """
        return GeventedMultiProcessTestRunner(
            stream=runner.stream,
            verbosity=self.config.verbosity,
            config=self.config,
            loaderClass=self.loaderClass
        )


class GeventedMultiProcessTestRunner(TextTestRunner):

    def __init__(self, **kw):
        """
        (Part of TextTestRunner API)
        """
        self.loaderClass = kw.pop('loaderClass', loader.defaultTestLoader)
        super(GeventedMultiProcessTestRunner, self).__init__(**kw)

    def collect_tasks(self, test, tasks_queue, tasks_list, to_teardown, result):
        """
        Recursively traverses the test suite tree and either records
        Failure results directly, or recurses into self.collect for
        test suite members that share common fixtures, or adds task
        to the global queue.

        :param test: Test or a collection of test (Test suite)
        :param tasks_queue: List of tuples (task_addr, args)
        :type tasks_queue: list
        :param tasks_list: List of task names task_addr + str(args)
        :type tasks_list: list
        :param to_teardown: List object to be populated with objects to tear down
        :type to_teardown: list
        :param result:
        :type result: TextTestResult
        """

        # Dispatch and collect results
        # It puts indexes only on queue because tests aren't picklable

        self.stream.write("Inspecting test tree for distributable tests...")

        for case in self.get_test_batch(test):
            self.stream.write(".")

            if (isinstance(case, nose.case.Test) and
                isinstance(case.test, failure.Failure)):
                case(result) # run here to capture the failure
                continue

            # handle shared fixtures
            if isinstance(case, ContextSuite) and case.context is failure.Failure:
                case(result) # run here to capture the failure
                continue

            if isinstance(case, ContextSuite) and has_shared_fixtures(case):
                try:
                    case.setUp()
                except (KeyboardInterrupt, SystemExit):
                    raise
                except:
                    result.addError(case, sys.exc_info())
                else:
                    to_teardown.append(case)
                    if case.factory:
                        ancestors = case.factory.context.get(case, [])
                        for ancestor in ancestors[:2]:
                            if getattr(ancestor, '_multiprocess_shared_', False):
                                ancestor._multiprocess_can_split_ = True
                            #ancestor._multiprocess_shared_ = False
                    self.collect_tasks(case, tasks_queue, tasks_list, to_teardown, result)
                continue

            # task_addr is the exact string that was put in tasks_list
            test_addr = add_task_to_queue(case, tasks_queue, tasks_list)
            log.debug("Queued test %s (%s)", len(tasks_list), test_addr)

        self.stream.write(" Found %s test cases\n" % len(tasks_queue))

    def stop_workers(self):
        pass

    def run(self, test):
        """
        This is the entry point for the run of the tests.
        This runner starts the test queue server and spawns the test runner clients.

        (Part of TextTestRunner API)

        Execute the test (which may be a test suite). If the test is a suite,
        distribute it out among as many processes as have been configured, at
        as fine a level as is possible given the context fixtures defined in
        the suite or any sub-suites.
        """
        log.debug("%s.run(%s) (%s)", self, test, os.getpid())

        tasks_queue = []
        remaining_tasks = [] # contains a list of task addresses
        #completed_tasks = [] # list of tuples like [task_address, batch_result]
        to_teardown = []
        #thrownError = None

        # API
        test = self.config.plugins.prepareTest(test) or test
        self.stream = self.config.plugins.setOutputStream(self.stream) or self.stream
        result = self._makeResult()
        start = time.time()

        # populates the queues
        self.collect_tasks(test, tasks_queue, remaining_tasks, to_teardown, result)

        queue_manager = TestsQueueManager(
            tasks_queue,
            loader_class=self.loaderClass,
            result_class=result.__class__,
            config=self.config
        )
        server = WSGIServer(queue_manager)
        server_port = server.start()

        results_processor = queue_manager.start_test_results_processor(remaining_tasks, result, self.stream, self.config.stopOnError)

        number_of_workers = self.config.options.gevented_processes
        run_clients(
            number_of_workers, server_port
        ) # <-- blocks until all are done consuming the queue

        results_processor.join()

        # TODO: not call tests / set ups could have ran. see if we can prune the tearDown collection as result
        for case in to_teardown:
            try:
                case.tearDown()
            except (KeyboardInterrupt, SystemExit):
                raise
            except:
                result.addError(case, sys.exc_info())

        stop = time.time()

        # first write since can freeze on shutting down processes
        result.printErrors()
        result.printSummary(start, stop)
        self.config.plugins.finalize(result)

        #except (KeyboardInterrupt, SystemExit):
        #    if thrownError:
        #        raise thrownError
        #    else:
        #        raise

        return result

    def get_test_batch(self, test):
        """
        Generator that recursively traverses the test case tree
        and yields test batches

        :param test: test or a test case object
        :return: Tests iterable
        :rtype: Iterable
        """

        # allows tests or suites to mark themselves as not safe
        # for multiprocess execution
        if hasattr(test, 'context') and not getattr(test.context, '_multiprocess_', True):
            # self.stream.write("get batch return 1\n")
            return

        if (isinstance(test, ContextSuite) and test.hasFixtures(can_split_flag_set)) \
            or not getattr(test, 'can_split', True) \
            or not isinstance(test, unittest.TestSuite):
            # regular test case, or a suite with context fixtures

            # special case: when run like nosetests path/to/module.py
            # the top-level suite has only one item, and it shares
            # the same context as that item. In that case, we want the
            # item, not the top-level suite
            if isinstance(test, ContextSuite):
                contained = list(test)
                if (
                    len(contained) == 1 and
                    getattr(contained[0], 'context', None) == test.context
                ):
                    test = contained[0]
            yield test
            self.stream.write(".")
            return

        # Suite is without fixtures at this level; but it may have
        # fixtures at any deeper level, so we need to examine it all
        # the way down to the case level
        for case in test:
            for batch in self.get_test_batch(case):
                self.stream.write(".")
                yield batch

def get_client_settings():
    import argparse

    parser = argparse.ArgumentParser(description='Test runner client script for gevent-compatible multiprocessing plugin for Nose.')
    parser.add_argument(
        'worker_id', type=int, help='Identifier for this worker.'
    )
    parser.add_argument(
        'server_port', type=int, help='Test-distributing mother-ship server\'s port number.'
    )

    args = parser.parse_args()
    return args.worker_id, args.server_port

def individual_client_starter():
    worker_id, server_port = get_client_settings()
    print "Starting test runner client #%s (talking to server on port %s)" % (worker_id, server_port)
    start_test_processor_task_runner(worker_id, server_port)

if __name__ == "__main__":
    individual_client_starter()
