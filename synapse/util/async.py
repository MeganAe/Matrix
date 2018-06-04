# -*- coding: utf-8 -*-
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


from twisted.internet import defer, reactor
from twisted.internet.defer import CancelledError
from twisted.python import failure

from .logcontext import (
    PreserveLoggingContext, make_deferred_yieldable, run_in_background
)
from synapse.util import logcontext, unwrapFirstError

from contextlib import contextmanager

import logging

from six.moves import range

logger = logging.getLogger(__name__)


@defer.inlineCallbacks
def sleep(seconds):
    d = defer.Deferred()
    with PreserveLoggingContext():
        reactor.callLater(seconds, d.callback, seconds)
        res = yield d
    defer.returnValue(res)


def run_on_reactor():
    """ This will cause the rest of the function to be invoked upon the next
    iteration of the main loop
    """
    return sleep(0)


class ObservableDeferred(object):
    """Wraps a deferred object so that we can add observer deferreds. These
    observer deferreds do not affect the callback chain of the original
    deferred.

    If consumeErrors is true errors will be captured from the origin deferred.

    Cancelling or otherwise resolving an observer will not affect the original
    ObservableDeferred.

    NB that it does not attempt to do anything with logcontexts; in general
    you should probably make_deferred_yieldable the deferreds
    returned by `observe`, and ensure that the original deferred runs its
    callbacks in the sentinel logcontext.
    """

    __slots__ = ["_deferred", "_observers", "_result"]

    def __init__(self, deferred, consumeErrors=False):
        object.__setattr__(self, "_deferred", deferred)
        object.__setattr__(self, "_result", None)
        object.__setattr__(self, "_observers", set())

        def callback(r):
            object.__setattr__(self, "_result", (True, r))
            while self._observers:
                try:
                    # TODO: Handle errors here.
                    self._observers.pop().callback(r)
                except Exception:
                    pass
            return r

        def errback(f):
            object.__setattr__(self, "_result", (False, f))
            while self._observers:
                try:
                    # TODO: Handle errors here.
                    self._observers.pop().errback(f)
                except Exception:
                    pass

            if consumeErrors:
                return None
            else:
                return f

        deferred.addCallbacks(callback, errback)

    def observe(self):
        """Observe the underlying deferred.

        Can return either a deferred if the underlying deferred is still pending
        (or has failed), or the actual value. Callers may need to use maybeDeferred.
        """
        if not self._result:
            d = defer.Deferred()

            def remove(r):
                self._observers.discard(d)
                return r
            d.addBoth(remove)

            self._observers.add(d)
            return d
        else:
            success, res = self._result
            return res if success else defer.fail(res)

    def observers(self):
        return self._observers

    def has_called(self):
        return self._result is not None

    def has_succeeded(self):
        return self._result is not None and self._result[0] is True

    def get_result(self):
        return self._result[1]

    def __getattr__(self, name):
        return getattr(self._deferred, name)

    def __setattr__(self, name, value):
        setattr(self._deferred, name, value)

    def __repr__(self):
        return "<ObservableDeferred object at %s, result=%r, _deferred=%r>" % (
            id(self), self._result, self._deferred,
        )


def concurrently_execute(func, args, limit):
    """Executes the function with each argument conncurrently while limiting
    the number of concurrent executions.

    Args:
        func (func): Function to execute, should return a deferred.
        args (list): List of arguments to pass to func, each invocation of func
            gets a signle argument.
        limit (int): Maximum number of conccurent executions.

    Returns:
        deferred: Resolved when all function invocations have finished.
    """
    it = iter(args)

    @defer.inlineCallbacks
    def _concurrently_execute_inner():
        try:
            while True:
                yield func(next(it))
        except StopIteration:
            pass

    return logcontext.make_deferred_yieldable(defer.gatherResults([
        run_in_background(_concurrently_execute_inner)
        for _ in range(limit)
    ], consumeErrors=True)).addErrback(unwrapFirstError)


class Linearizer(object):
    """Linearizes access to resources based on a key. Useful to ensure only one
    thing is happening at a time on a given resource.

    Example:

        with (yield linearizer.queue("test_key")):
            # do some work.

    """
    def __init__(self, name=None):
        if name is None:
            self.name = id(self)
        else:
            self.name = name
        self.key_to_defer = {}

    @defer.inlineCallbacks
    def queue(self, key):
        # If there is already a deferred in the queue, we pull it out so that
        # we can wait on it later.
        # Then we replace it with a deferred that we resolve *after* the
        # context manager has exited.
        # We only return the context manager after the previous deferred has
        # resolved.
        # This all has the net effect of creating a chain of deferreds that
        # wait for the previous deferred before starting their work.
        current_defer = self.key_to_defer.get(key)

        new_defer = defer.Deferred()
        self.key_to_defer[key] = new_defer

        if current_defer:
            logger.debug(
                "Waiting to acquire linearizer lock %r for key %r", self.name, key
            )
            try:
                with PreserveLoggingContext():
                    yield current_defer
            except Exception:
                logger.exception("Unexpected exception in Linearizer")

            logger.debug("Acquired linearizer lock %r for key %r", self.name, key)

            # if the code holding the lock completes synchronously, then it
            # will recursively run the next claimant on the list. That can
            # relatively rapidly lead to stack exhaustion. This is essentially
            # the same problem as http://twistedmatrix.com/trac/ticket/9304.
            #
            # In order to break the cycle, we add a cheeky sleep(0) here to
            # ensure that we fall back to the reactor between each iteration.
            #
            # (There's no particular need for it to happen before we return
            # the context manager, but it needs to happen while we hold the
            # lock, and the context manager's exit code must be synchronous,
            # so actually this is the only sensible place.
            yield run_on_reactor()

        else:
            logger.debug("Acquired uncontended linearizer lock %r for key %r",
                         self.name, key)

        @contextmanager
        def _ctx_manager():
            try:
                yield
            finally:
                logger.debug("Releasing linearizer lock %r for key %r", self.name, key)
                with PreserveLoggingContext():
                    new_defer.callback(None)
                current_d = self.key_to_defer.get(key)
                if current_d is new_defer:
                    self.key_to_defer.pop(key, None)

        defer.returnValue(_ctx_manager())


class Limiter(object):
    """Limits concurrent access to resources based on a key. Useful to ensure
    only a few thing happen at a time on a given resource.

    Example:

        with (yield limiter.queue("test_key")):
            # do some work.

    """
    def __init__(self, max_count):
        """
        Args:
            max_count(int): The maximum number of concurrent access
        """
        self.max_count = max_count

        # key_to_defer is a map from the key to a 2 element list where
        # the first element is the number of things executing
        # the second element is a list of deferreds for the things blocked from
        # executing.
        self.key_to_defer = {}

    @defer.inlineCallbacks
    def queue(self, key):
        entry = self.key_to_defer.setdefault(key, [0, []])

        # If the number of things executing is greater than the maximum
        # then add a deferred to the list of blocked items
        # When on of the things currently executing finishes it will callback
        # this item so that it can continue executing.
        if entry[0] >= self.max_count:
            new_defer = defer.Deferred()
            entry[1].append(new_defer)

            logger.debug("Waiting to acquire limiter lock for key %r", key)
            with PreserveLoggingContext():
                yield new_defer
            logger.debug("Acquired limiter lock for key %r", key)
        else:
            logger.debug("Acquired uncontended limiter lock for key %r", key)

        entry[0] += 1

        @contextmanager
        def _ctx_manager():
            try:
                yield
            finally:
                logger.debug("Releasing limiter lock for key %r", key)

                # We've finished executing so check if there are any things
                # blocked waiting to execute and start one of them
                entry[0] -= 1

                if entry[1]:
                    next_def = entry[1].pop(0)

                    with PreserveLoggingContext():
                        next_def.callback(None)
                elif entry[0] == 0:
                    # We were the last thing for this key: remove it from the
                    # map.
                    del self.key_to_defer[key]

        defer.returnValue(_ctx_manager())


class ReadWriteLock(object):
    """A deferred style read write lock.

    Example:

        with (yield read_write_lock.read("test_key")):
            # do some work
    """

    # IMPLEMENTATION NOTES
    #
    # We track the most recent queued reader and writer deferreds (which get
    # resolved when they release the lock).
    #
    # Read: We know its safe to acquire a read lock when the latest writer has
    # been resolved. The new reader is appeneded to the list of latest readers.
    #
    # Write: We know its safe to acquire the write lock when both the latest
    # writers and readers have been resolved. The new writer replaces the latest
    # writer.

    def __init__(self):
        # Latest readers queued
        self.key_to_current_readers = {}

        # Latest writer queued
        self.key_to_current_writer = {}

    @defer.inlineCallbacks
    def read(self, key):
        new_defer = defer.Deferred()

        curr_readers = self.key_to_current_readers.setdefault(key, set())
        curr_writer = self.key_to_current_writer.get(key, None)

        curr_readers.add(new_defer)

        # We wait for the latest writer to finish writing. We can safely ignore
        # any existing readers... as they're readers.
        yield make_deferred_yieldable(curr_writer)

        @contextmanager
        def _ctx_manager():
            try:
                yield
            finally:
                new_defer.callback(None)
                self.key_to_current_readers.get(key, set()).discard(new_defer)

        defer.returnValue(_ctx_manager())

    @defer.inlineCallbacks
    def write(self, key):
        new_defer = defer.Deferred()

        curr_readers = self.key_to_current_readers.get(key, set())
        curr_writer = self.key_to_current_writer.get(key, None)

        # We wait on all latest readers and writer.
        to_wait_on = list(curr_readers)
        if curr_writer:
            to_wait_on.append(curr_writer)

        # We can clear the list of current readers since the new writer waits
        # for them to finish.
        curr_readers.clear()
        self.key_to_current_writer[key] = new_defer

        yield make_deferred_yieldable(defer.gatherResults(to_wait_on))

        @contextmanager
        def _ctx_manager():
            try:
                yield
            finally:
                new_defer.callback(None)
                if self.key_to_current_writer[key] == new_defer:
                    self.key_to_current_writer.pop(key)

        defer.returnValue(_ctx_manager())


class DeferredTimeoutError(Exception):
    """
    This error is raised by default when a L{Deferred} times out.
    """


def add_timeout_to_deferred(deferred, timeout, on_timeout_cancel=None):
    """
    Add a timeout to a deferred by scheduling it to be cancelled after
    timeout seconds.

    This is essentially a backport of deferred.addTimeout, which was introduced
    in twisted 16.5.

    If the deferred gets timed out, it errbacks with a DeferredTimeoutError,
    unless a cancelable function was passed to its initialization or unless
    a different on_timeout_cancel callable is provided.

    Args:
        deferred (defer.Deferred): deferred to be timed out
        timeout (Number): seconds to time out after

        on_timeout_cancel (callable): A callable which is called immediately
            after the deferred times out, and not if this deferred is
            otherwise cancelled before the timeout.

            It takes an arbitrary value, which is the value of the deferred at
            that exact point in time (probably a CancelledError Failure), and
            the timeout.

            The default callable (if none is provided) will translate a
            CancelledError Failure into a DeferredTimeoutError.
    """
    timed_out = [False]

    def time_it_out():
        timed_out[0] = True
        deferred.cancel()

    delayed_call = reactor.callLater(timeout, time_it_out)

    def convert_cancelled(value):
        if timed_out[0]:
            to_call = on_timeout_cancel or _cancelled_to_timed_out_error
            return to_call(value, timeout)
        return value

    deferred.addBoth(convert_cancelled)

    def cancel_timeout(result):
        # stop the pending call to cancel the deferred if it's been fired
        if delayed_call.active():
            delayed_call.cancel()
        return result

    deferred.addBoth(cancel_timeout)


def _cancelled_to_timed_out_error(value, timeout):
    if isinstance(value, failure.Failure):
        value.trap(CancelledError)
        raise DeferredTimeoutError(timeout, "Deferred")
    return value
