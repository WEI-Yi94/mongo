#!/usr/bin/env python
"""
Resmoke Test Suite Generator.

Analyze the evergreen history for tests run under the given task and create new evergreen tasks
to attempt to keep the task runtime under a specified amount.
"""

from __future__ import absolute_import

import argparse
import datetime
import logging
import os
import sys
from collections import defaultdict
from collections import namedtuple

import requests
import yaml

from shrub.config import Configuration
from shrub.command import CommandDefinition
from shrub.task import TaskDependency
from shrub.variant import DisplayTaskDefinition
from shrub.variant import TaskSpec

# Get relative imports to work when the package is not installed on the PYTHONPATH.
if __name__ == "__main__" and __package__ is None:
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import buildscripts.client.evergreen as evergreen  # pylint: disable=wrong-import-position
import buildscripts.resmokelib.suitesconfig as suitesconfig  # pylint: disable=wrong-import-position
import buildscripts.util.read_config as read_config  # pylint: disable=wrong-import-position
import buildscripts.util.taskname as taskname  # pylint: disable=wrong-import-position
import buildscripts.util.testname as testname  # pylint: disable=wrong-import-position

LOGGER = logging.getLogger(__name__)

TEST_SUITE_DIR = os.path.join("buildscripts", "resmokeconfig", "suites")
CONFIG_DIR = "generated_resmoke_config"

HEADER_TEMPLATE = """# DO NOT EDIT THIS FILE. All manual edits will be lost.
# This file was generated by {file} from
# {suite_file}.
"""

ConfigOptions = namedtuple("ConfigOptions", [
    "fallback_num_sub_suites",
    "max_sub_suites",
    "project",
    "resmoke_args",
    "resmoke_jobs_max",
    "run_multiple_jobs",
    "suite",
    "task",
    "variant",
])


def enable_logging():
    """Enable verbose logging for execution."""

    logging.basicConfig(
        format='[%(asctime)s - %(name)s - %(levelname)s] %(message)s',
        level=logging.DEBUG,
        stream=sys.stdout,
    )


def get_config_options(cmd_line_options, config_file):
    """
    Get the configuration to use for generated tests.

    Command line options override config file options.

    :param cmd_line_options: Command line options specified.
    :param config_file: config file to use.
    :return: ConfigOptions to use.
    """
    config_file_data = read_config.read_config_file(config_file)

    fallback_num_sub_suites = read_config.get_config_value("fallback_num_sub_suites",
                                                           cmd_line_options, config_file_data,
                                                           required=True)
    max_sub_suites = read_config.get_config_value("max_sub_suites", cmd_line_options,
                                                  config_file_data)
    project = read_config.get_config_value("project", cmd_line_options, config_file_data,
                                           required=True)
    resmoke_args = read_config.get_config_value("resmoke_args", cmd_line_options, config_file_data,
                                                default="")
    resmoke_jobs_max = read_config.get_config_value("resmoke_jobs_max", cmd_line_options,
                                                    config_file_data)
    run_multiple_jobs = read_config.get_config_value("run_multiple_jobs", cmd_line_options,
                                                     config_file_data, default="true")
    task = read_config.get_config_value("task", cmd_line_options, config_file_data, required=True)
    suite = read_config.get_config_value("suite", cmd_line_options, config_file_data, default=task)
    variant = read_config.get_config_value("build_variant", cmd_line_options, config_file_data,
                                           required=True)

    return ConfigOptions(fallback_num_sub_suites, max_sub_suites, project, resmoke_args,
                         resmoke_jobs_max, run_multiple_jobs, suite, task, variant)


def divide_remaining_tests_among_suites(remaining_tests_runtimes, suites):
    """Divide the list of tests given among the suites given."""
    suite_idx = 0
    for test_file, runtime in remaining_tests_runtimes:
        current_suite = suites[suite_idx]
        current_suite.add_test(test_file, runtime)
        suite_idx += 1
        if suite_idx >= len(suites):
            suite_idx = 0


def divide_tests_into_suites(tests_runtimes, max_time_seconds, max_suites=None):
    """
    Divide the given tests into suites.

    Each suite should be able to execute in less than the max time specified.
    """
    suites = []
    current_suite = Suite()
    last_test_processed = len(tests_runtimes)
    LOGGER.debug("Determines suites for runtime: %ds", max_time_seconds)
    for idx, (test_file, runtime) in enumerate(tests_runtimes):
        if current_suite.get_runtime() + runtime > max_time_seconds:
            LOGGER.debug("Runtime(%d) + new test(%d) > max(%d)", current_suite.get_runtime(),
                         runtime, max_time_seconds)
            if current_suite.get_test_count() > 0:
                suites.append(current_suite)
                current_suite = Suite()
                if max_suites and len(suites) >= max_suites:
                    last_test_processed = idx
                    break

        current_suite.add_test(test_file, runtime)

    if current_suite.get_test_count() > 0:
        suites.append(current_suite)

    if max_suites and last_test_processed < len(tests_runtimes):
        # We must have hit the max suite limit, just randomly add the remaining tests to suites.
        divide_remaining_tests_among_suites(tests_runtimes[last_test_processed:], suites)

    return suites


def generate_subsuite_file(source_suite_name, target_suite_name, roots=None, excludes=None):
    """
    Read and evaluate the yaml suite file.

    Override selector.roots and selector.excludes with the provided values. Write the results to
    target_suite_name.
    """
    source_file = os.path.join(TEST_SUITE_DIR, source_suite_name + ".yml")
    with open(source_file, "r") as fstream:
        suite_config = yaml.load(fstream)

    with open(os.path.join(CONFIG_DIR, target_suite_name + ".yml"), 'w') as out:
        out.write(HEADER_TEMPLATE.format(file=__file__, suite_file=source_file))
        if roots:
            suite_config['selector']['roots'] = roots
        if excludes:
            suite_config['selector']['exclude_files'] = excludes
        out.write(yaml.dump(suite_config, default_flow_style=False, Dumper=yaml.SafeDumper))


def render_suite(suites, suite_name):
    """Render the given suites into yml files that can be used by resmoke.py."""
    for idx, suite in enumerate(suites):
        suite.name = taskname.name_generated_task(suite_name, idx, len(suites))
        generate_subsuite_file(suite_name, suite.name, roots=suite.tests)


def render_misc_suite(test_list, suite_name):
    """Render a misc suite to run any tests that might be added to the directory."""
    subsuite_name = "{0}_{1}".format(suite_name, "misc")
    generate_subsuite_file(suite_name, subsuite_name, excludes=test_list)


def prepare_directory_for_suite(directory):
    """Ensure that dir exists."""
    if not os.path.exists(directory):
        os.makedirs(directory)


def generate_evg_config(suites, options):
    """Generate evergreen configuration for the given suites."""
    evg_config = Configuration()

    task_names = []
    task_specs = []

    def generate_task(sub_suite_name, sub_task_name):
        """Generate evergreen config for a resmoke task."""
        task_names.append(sub_task_name)
        task_specs.append(TaskSpec(sub_task_name))
        task = evg_config.task(sub_task_name)

        target_suite_file = os.path.join(CONFIG_DIR, sub_suite_name)

        run_tests_vars = {
            "resmoke_args": "--suites={0} {1}".format(target_suite_file, options.resmoke_args),
            "run_multiple_jobs": options.run_multiple_jobs,
        }

        if options.resmoke_jobs_max:
            run_tests_vars["resmoke_jobs_max"] = options.resmoke_jobs_max

        commands = [
            CommandDefinition().function("do setup"),
            CommandDefinition().function("run tests").vars(run_tests_vars)
        ]
        task.dependency(TaskDependency("compile")).commands(commands)

    for idx, suite in enumerate(suites):
        sub_task_name = taskname.name_generated_task(options.task, idx, len(suites),
                                                     options.variant)
        generate_task(suite.name, sub_task_name)

    # Add the misc suite
    misc_suite_name = "{0}_misc".format(options.suite)
    generate_task(misc_suite_name, "{0}_misc_{1}".format(options.task, options.variant))

    dt = DisplayTaskDefinition(options.task).execution_tasks(task_names) \
        .execution_task("{0}_gen".format(options.task))
    evg_config.variant(options.variant).tasks(task_specs).display_task(dt)

    return evg_config


class TestStats(object):
    """Represent the test statistics for the task that is being analyzed."""

    def __init__(self, evg_test_stats_results):
        """Initialize the TestStats with raw results from the Evergreen API."""
        # Mapping from test_file to {"num_run": X, "duration": Y} for tests
        self._runtime_by_test = defaultdict(dict)
        # Mapping from test_name to {"num_run": X, "duration": Y} for hooks
        self._hook_runtime_by_test = defaultdict(dict)

        for doc in evg_test_stats_results:
            self._add_stats(doc)

    def _add_stats(self, test_stats):
        """Add the statistics found in a document returned by the Evergreen test_stats/ endpoint."""
        test_file = testname.normalize_test_file(test_stats["test_file"])
        duration = test_stats["avg_duration_pass"]
        num_run = test_stats["num_pass"]
        is_hook = testname.is_resmoke_hook(test_file)
        if is_hook:
            self._add_test_hook_stats(test_file, duration, num_run)
        else:
            self._add_test_stats(test_file, duration, num_run)

    def _add_test_stats(self, test_file, duration, num_run):
        """Add the statistics for a test."""
        self._add_runtime_info(self._runtime_by_test, test_file, duration, num_run)

    def _add_test_hook_stats(self, test_file, duration, num_run):
        """Add the statistics for a hook."""
        test_name = testname.split_test_hook_name(test_file)[0]
        self._add_runtime_info(self._hook_runtime_by_test, test_name, duration, num_run)

    @staticmethod
    def _add_runtime_info(runtime_dict, test_name, duration, num_run):
        runtime_info = runtime_dict[test_name]
        if not runtime_info:
            runtime_info["duration"] = duration
            runtime_info["num_run"] = num_run
        else:
            runtime_info["duration"] = TestStats._average(
                runtime_info["duration"], runtime_info["num_run"], duration, num_run)
            runtime_info["num_run"] += num_run

    @staticmethod
    def _average(value_a, num_a, value_b, num_b):
        """Compute a weighted average of 2 values with associated numbers."""
        return float(value_a * num_a + value_b * num_b) / (num_a + num_b)

    def get_tests_runtimes(self):
        """Return the list of (test_file, runtime_in_secs) tuples ordered by decreasing runtime."""
        tests = []
        for test_file, runtime_info in self._runtime_by_test.items():
            duration = runtime_info["duration"]
            test_name = testname.get_short_name_from_test_file(test_file)
            hook_runtime_info = self._hook_runtime_by_test[test_name]
            if hook_runtime_info:
                duration += hook_runtime_info["duration"]
            tests.append((test_file, duration))
        return sorted(tests, key=lambda x: x[1], reverse=True)


class Suite(object):
    """A suite of tests that can be run by evergreen."""

    def __init__(self):
        """Initialize the object."""
        self.tests = []
        self.total_runtime = 0

    def add_test(self, test_file, runtime):
        """Add the given test to this suite."""

        self.tests.append(test_file)
        self.total_runtime += runtime

    def get_runtime(self):
        """Get the current average runtime of all the tests currently in this suite."""

        return self.total_runtime

    def get_test_count(self):
        """Get the number of tests currently in this suite."""

        return len(self.tests)


class Main(object):
    """Orchestrate the execution of generate_resmoke_suites."""

    def __init__(self, evergreen_api):
        """Initialize the object."""
        self.evergreen_api = evergreen_api
        self.options = None
        self.config_options = None
        self.test_list = []

    def parse_commandline(self):
        """Parse the command line options and return the parsed data."""
        parser = argparse.ArgumentParser(description=self.main.__doc__)

        parser.add_argument("--expansion-file", dest="expansion_file", type=str,
                            help="Location of expansions file generated by evergreen.")
        parser.add_argument("--analysis-duration", dest="duration_days", default=14,
                            help="Number of days to analyze.", type=int)
        parser.add_argument("--execution-time", dest="execution_time_minutes", default=60, type=int,
                            help="Target execution time (in minutes).")
        parser.add_argument("--max-sub-suites", dest="max_sub_suites", type=int,
                            help="Max number of suites to divide into.")
        parser.add_argument("--fallback-num-sub-suites", dest="fallback_num_sub_suites", type=int,
                            help="The number of suites to divide into if the Evergreen test "
                            "statistics are not available.")
        parser.add_argument("--project", dest="project", help="The Evergreen project to analyse.")
        parser.add_argument("--resmoke-args", dest="resmoke_args",
                            help="Arguments to pass to resmoke calls.")
        parser.add_argument("--resmoke-jobs_max", dest="resmoke_jobs_max",
                            help="Number of resmoke jobs to invoke.")
        parser.add_argument("--suite", dest="suite",
                            help="Name of suite being split(defaults to task_name)")
        parser.add_argument("--run-multiple-jobs", dest="run_multiple_jobs",
                            help="Should resmoke run multiple jobs")
        parser.add_argument("--task-name", dest="task", help="Name of task to split.")
        parser.add_argument("--variant", dest="build_variant",
                            help="Build variant being run against.")
        parser.add_argument("--verbose", dest="verbose", action="store_true", default=False,
                            help="Enable verbose logging.")

        options = parser.parse_args()

        self.config_options = get_config_options(options, options.expansion_file)
        return options

    def calculate_suites(self, start_date, end_date):
        """Divide tests into suites based on statistics for the provided period."""
        try:
            evg_stats = self.get_evg_stats(self.config_options.project, start_date, end_date,
                                           self.config_options.task, self.config_options.variant)
            return self.calculate_suites_from_evg_stats(evg_stats,
                                                        self.options.execution_time_minutes * 60)
        except requests.HTTPError as err:
            if err.response.status_code == requests.codes.SERVICE_UNAVAILABLE:
                # Evergreen may return a 503 when the service is degraded.
                # We fall back to splitting the tests into a fixed number of suites.
                LOGGER.warning("Received 503 from Evergreen, "
                               "dividing the tests evenly among suites")
                return self.calculate_fallback_suites()
            else:
                raise

    def get_evg_stats(self, project, start_date, end_date, task, variant):
        """Collect test execution statistics data from Evergreen."""
        # pylint: disable=too-many-arguments

        days = (end_date - start_date).days
        return self.evergreen_api.test_stats(project, after_date=start_date.strftime("%Y-%m-%d"),
                                             before_date=end_date.strftime("%Y-%m-%d"),
                                             tasks=[task], variants=[variant], group_by="test",
                                             group_num_days=days)

    def calculate_suites_from_evg_stats(self, data, execution_time_secs):
        """Divide tests into suites that can be run in less than the specified execution time."""
        test_stats = TestStats(data)
        tests_runtimes = test_stats.get_tests_runtimes()
        self.test_list = [info[0] for info in tests_runtimes]
        return divide_tests_into_suites(tests_runtimes, execution_time_secs,
                                        self.options.max_sub_suites)

    def calculate_fallback_suites(self):
        """Divide tests into a fixed number of suites."""
        num_suites = self.config_options.fallback_num_sub_suites
        tests = self.list_tests()
        suites = [Suite() for _ in range(num_suites)]
        for idx, test_file in enumerate(tests):
            suites[idx % num_suites].add_test(test_file, 0)
        return suites

    def list_tests(self):
        """List the test files that are part of the suite being split."""
        return suitesconfig.get_suite(self.config_options.suite).tests

    def write_evergreen_configuration(self, suites, task):
        """Generate the evergreen configuration for the new suite and write it to disk."""
        evg_config = generate_evg_config(suites, self.config_options)

        with open(os.path.join(CONFIG_DIR, task + ".json"), "w") as file_handle:
            file_handle.write(evg_config.to_json())

    def main(self):
        """Generate resmoke suites that run within a specified target execution time."""

        options = self.parse_commandline()
        self.options = options

        if options.verbose:
            enable_logging()

        LOGGER.debug("Starting execution for options %s", options)
        end_date = datetime.datetime.utcnow().replace(microsecond=0)
        start_date = end_date - datetime.timedelta(days=options.duration_days)

        prepare_directory_for_suite(CONFIG_DIR)

        suites = self.calculate_suites(start_date, end_date)

        LOGGER.debug("Creating %d suites for %s", len(suites), self.config_options.task)

        render_suite(suites, self.config_options.suite)
        render_misc_suite(self.test_list, self.config_options.suite)

        self.write_evergreen_configuration(suites, self.config_options.task)


if __name__ == "__main__":
    Main(evergreen.get_evergreen_apiv2()).main()
