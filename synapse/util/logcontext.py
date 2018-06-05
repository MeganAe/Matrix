# Copyright 2014-2016 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

""" Thread-local-alike tracking of log contexts within synapse

This module provides objects and utilities for tracking contexts through
synapse code, so that log lines can include a request identifier, and so that
CPU and database activity can be accounted for against the request that caused
them.

See doc/log_contexts.rst for details on how this works.
"""

from twisted.internet import defer

import threading
import logging

logger = logging.getLogger(__name__)

try:
    import resource

    # Python doesn't ship with a definition of RUSAGE_THREAD but it's defined
    # to be 1 on linux so we hard code it.
    RUSAGE_THREAD = 1

    # If the system doesn't support RUSAGE_THREAD then this should throw an
    # exception.
    resource.getrusage(RUSAGE_THREAD)

    def get_thread_resource_usage():
        return resource.getrusage(RUSAGE_THREAD)
except Exception:
    # If the system doesn't support resource.getrusage(RUSAGE_THREAD) then we
    # won't track resource usage by returning None.
    def get_thread_resource_usage():
        return None


class LoggingContext(object):
    """Additional context for log formatting. Contexts are scoped within a
    "with" block.

    Args:
        name (str): Name for the context for debugging.
    """

    __slots__ = [
        "previous_context", "name", "ru_stime", "ru_utime",
        "db_txn_count", "db_txn_duration_sec", "db_sched_duration_sec",
        "usage_start",
        "main_thread", "alive",
        "request", "tag",
    ]

    thread_local = threading.local()

    class Sentinel(object):
        """Sentinel to represent the root context"""

        __slots__ = []

        def __str__(self):
            return "sentinel"

        def copy_to(self, record):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def add_database_transaction(self, duration_sec):
            pass

        def add_database_scheduled(self, sched_sec):
            pass

        def __nonzero__(self):
            return False
        __bool__ = __nonzero__  # python3

    sentinel = Sentinel()

    def __init__(self, name=None):
        self.previous_context = LoggingContext.current_context()
        self.name = name
        self.ru_stime = 0.
        self.ru_utime = 0.
        self.db_txn_count = 0

        # sec spent waiting for db txns, excluding scheduling time
        self.db_txn_duration_sec = 0

        # sec spent waiting for db txns to be scheduled
        self.db_sched_duration_sec = 0

        # If alive has the thread resource usage when the logcontext last
        # became active.
        self.usage_start = None

        self.main_thread = threading.current_thread()
        self.request = None
        self.tag = ""
        self.alive = True

    def __str__(self):
        return "%s@%x" % (self.name, id(self))

    @classmethod
    def current_context(cls):
        """Get the current logging context from thread local storage

        Returns:
            LoggingContext: the current logging context
        """
        return getattr(cls.thread_local, "current_context", cls.sentinel)

    @classmethod
    def set_current_context(cls, context):
        """Set the current logging context in thread local storage
        Args:
            context(LoggingContext): The context to activate.
        Returns:
            The context that was previously active
        """
        current = cls.current_context()

        if current is not context:
            current.stop()
            cls.thread_local.current_context = context
            context.start()
        return current

    def __enter__(self):
        """Enters this logging context into thread local storage"""
        old_context = self.set_current_context(self)
        if self.previous_context != old_context:
            logger.warn(
                "Expected previous context %r, found %r",
                self.previous_context, old_context
            )
        self.alive = True
        return self

    def __exit__(self, type, value, traceback):
        """Restore the logging context in thread local storage to the state it
        was before this context was entered.
        Returns:
            None to avoid suppressing any exceptions that were thrown.
        """
        current = self.set_current_context(self.previous_context)
        if current is not self:
            if current is self.sentinel:
                logger.warn("Expected logging context %s has been lost", self)
            else:
                logger.warn(
                    "Current logging context %s is not expected context %s",
                    current,
                    self
                )
        self.previous_context = None
        self.alive = False

    def copy_to(self, record):
        """Copy logging fields from this context to a log record or
        another LoggingContext
        """

        # 'request' is the only field we currently use in the logger, so that's
        # all we need to copy
        record.request = self.request

    def start(self):
        if threading.current_thread() is not self.main_thread:
            logger.warning("Started logcontext %s on different thread", self)
            return

        # If we haven't already started record the thread resource usage so
        # far
        if not self.usage_start:
            self.usage_start = get_thread_resource_usage()

    def stop(self):
        if threading.current_thread() is not self.main_thread:
            logger.warning("Stopped logcontext %s on different thread", self)
            return

        # When we stop, let's record the resource used since we started
        if self.usage_start:
            usage_end = get_thread_resource_usage()

            self.ru_utime += usage_end.ru_utime - self.usage_start.ru_utime
            self.ru_stime += usage_end.ru_stime - self.usage_start.ru_stime

            self.usage_start = None
        else:
            logger.warning("Called stop on logcontext %s without calling start", self)

    def get_resource_usage(self):
        """Get CPU time used by this logcontext so far.

        Returns:
            tuple[float, float]: The user and system CPU usage in seconds
        """
        ru_utime = self.ru_utime
        ru_stime = self.ru_stime

        # If we are on the correct thread and we're currently running then we
        # can include resource usage so far.
        is_main_thread = threading.current_thread() is self.main_thread
        if self.alive and self.usage_start and is_main_thread:
            current = get_thread_resource_usage()
            ru_utime += current.ru_utime - self.usage_start.ru_utime
            ru_stime += current.ru_stime - self.usage_start.ru_stime

        return ru_utime, ru_stime

    def add_database_transaction(self, duration_sec):
        self.db_txn_count += 1
        self.db_txn_duration_sec += duration_sec

    def add_database_scheduled(self, sched_sec):
        """Record a use of the database pool

        Args:
            sched_sec (float): number of seconds it took us to get a
                connection
        """
        self.db_sched_duration_sec += sched_sec


class LoggingContextFilter(logging.Filter):
    """Logging filter that adds values from the current logging context to each
    record.
    Args:
        **defaults: Default values to avoid formatters complaining about
            missing fields
    """
    def __init__(self, **defaults):
        self.defaults = defaults

    def filter(self, record):
        """Add each fields from the logging contexts to the record.
        Returns:
            True to include the record in the log output.
        """
        context = LoggingContext.current_context()
        for key, value in self.defaults.items():
            setattr(record, key, value)
        context.copy_to(record)
        return True


class PreserveLoggingContext(object):
    """Captures the current logging context and restores it when the scope is
    exited. Used to restore the context after a function using
    @defer.inlineCallbacks is resumed by a callback from the reactor."""

    __slots__ = ["current_context", "new_context", "has_parent"]

    def __init__(self, new_context=LoggingContext.sentinel):
        self.new_context = new_context

    def __enter__(self):
        """Captures the current logging context"""
        self.current_context = LoggingContext.set_current_context(
            self.new_context
        )

        if self.current_context:
            self.has_parent = self.current_context.previous_context is not None
            if not self.current_context.alive:
                logger.debug(
                    "Entering dead context: %s",
                    self.current_context,
                )

    def __exit__(self, type, value, traceback):
        """Restores the current logging context"""
        context = LoggingContext.set_current_context(self.current_context)

        if context != self.new_context:
            logger.warn(
                "Unexpected logging context: %s is not %s",
                context, self.new_context,
            )

        if self.current_context is not LoggingContext.sentinel:
            if not self.current_context.alive:
                logger.debug(
                    "Restoring dead context: %s",
                    self.current_context,
                )


def preserve_fn(f):
    """Function decorator which wraps the function with run_in_background"""
    def g(*args, **kwargs):
        return run_in_background(f, *args, **kwargs)
    return g


def run_in_background(f, *args, **kwargs):
    """Calls a function, ensuring that the current context is restored after
    return from the function, and that the sentinel context is set once the
    deferred returned by the function completes.

    Useful for wrapping functions that return a deferred which you don't yield
    on (for instance because you want to pass it to deferred.gatherResults()).

    Note that if you completely discard the result, you should make sure that
    `f` doesn't raise any deferred exceptions, otherwise a scary-looking
    CRITICAL error about an unhandled error will be logged without much
    indication about where it came from.
    """
    current = LoggingContext.current_context()
    try:
        res = f(*args, **kwargs)
    except:   # noqa: E722
        # the assumption here is that the caller doesn't want to be disturbed
        # by synchronous exceptions, so let's turn them into Failures.
        return defer.fail()

    if not isinstance(res, defer.Deferred):
        return res

    if res.called and not res.paused:
        # The function should have maintained the logcontext, so we can
        # optimise out the messing about
        return res

    # The function may have reset the context before returning, so
    # we need to restore it now.
    ctx = LoggingContext.set_current_context(current)

    # The original context will be restored when the deferred
    # completes, but there is nothing waiting for it, so it will
    # get leaked into the reactor or some other function which
    # wasn't expecting it. We therefore need to reset the context
    # here.
    #
    # (If this feels asymmetric, consider it this way: we are
    # effectively forking a new thread of execution. We are
    # probably currently within a ``with LoggingContext()`` block,
    # which is supposed to have a single entry and exit point. But
    # by spawning off another deferred, we are effectively
    # adding a new exit point.)
    res.addBoth(_set_context_cb, ctx)
    return res


def make_deferred_yieldable(deferred):
    """Given a deferred, make it follow the Synapse logcontext rules:

    If the deferred has completed (or is not actually a Deferred), essentially
    does nothing (just returns another completed deferred with the
    result/failure).

    If the deferred has not yet completed, resets the logcontext before
    returning a deferred. Then, when the deferred completes, restores the
    current logcontext before running callbacks/errbacks.

    (This is more-or-less the opposite operation to run_in_background.)
    """
    if not isinstance(deferred, defer.Deferred):
        return deferred

    if deferred.called and not deferred.paused:
        # it looks like this deferred is ready to run any callbacks we give it
        # immediately. We may as well optimise out the logcontext faffery.
        return deferred

    # ok, we can't be sure that a yield won't block, so let's reset the
    # logcontext, and add a callback to the deferred to restore it.
    prev_context = LoggingContext.set_current_context(LoggingContext.sentinel)
    deferred.addBoth(_set_context_cb, prev_context)
    return deferred


def _set_context_cb(result, context):
    """A callback function which just sets the logging context"""
    LoggingContext.set_current_context(context)
    return result


# modules to ignore in `logcontext_tracer`
_to_ignore = [
    "synapse.util.logcontext",
    "synapse.http.server",
    "synapse.storage._base",
    "synapse.util.async",
]


def logcontext_tracer(frame, event, arg):
    """A tracer that logs whenever a logcontext "unexpectedly" changes within
    a function. Probably inaccurate.

    Use by calling `sys.settrace(logcontext_tracer)` in the main thread.
    """
    if event == 'call':
        name = frame.f_globals["__name__"]
        if name.startswith("synapse"):
            if name == "synapse.util.logcontext":
                if frame.f_code.co_name in ["__enter__", "__exit__"]:
                    tracer = frame.f_back.f_trace
                    if tracer:
                        tracer.just_changed = True

            tracer = frame.f_trace
            if tracer:
                return tracer

            if not any(name.startswith(ig) for ig in _to_ignore):
                return LineTracer()


class LineTracer(object):
    __slots__ = ["context", "just_changed"]

    def __init__(self):
        self.context = LoggingContext.current_context()
        self.just_changed = False

    def __call__(self, frame, event, arg):
        if event in 'line':
            if self.just_changed:
                self.context = LoggingContext.current_context()
                self.just_changed = False
            else:
                c = LoggingContext.current_context()
                if c != self.context:
                    logger.info(
                        "Context changed! %s -> %s, %s, %s",
                        self.context, c,
                        frame.f_code.co_filename, frame.f_lineno
                    )
                    self.context = c

        return self
