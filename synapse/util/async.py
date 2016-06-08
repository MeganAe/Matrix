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

from .logcontext import (
    PreserveLoggingContext, preserve_fn, preserve_context_over_deferred,
)
from synapse.util import unwrapFirstError

from contextlib import contextmanager


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
                except:
                    pass
            return r

        def errback(f):
            object.__setattr__(self, "_result", (False, f))
            while self._observers:
                try:
                    # TODO: Handle errors here.
                    self._observers.pop().errback(f)
                except:
                    pass

            if consumeErrors:
                return None
            else:
                return f

        deferred.addCallbacks(callback, errback)

    def observe(self):
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
            return defer.succeed(res) if success else defer.fail(res)

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
                yield func(it.next())
        except StopIteration:
            pass

    return defer.gatherResults([
        preserve_fn(_concurrently_execute_inner)()
        for _ in xrange(limit)
    ], consumeErrors=True).addErrback(unwrapFirstError)


class Linearizer(object):
    """Linearizes access to resources based on a key. Useful to ensure only one
    thing is happening at a time on a given resource.

    Example:

        with (yield linearizer.queue("test_key")):
            # do some work.

    """
    def __init__(self):
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
            yield preserve_context_over_deferred(current_defer)

        @contextmanager
        def _ctx_manager():
            try:
                yield
            finally:
                new_defer.callback(None)
                current_d = self.key_to_defer.get(key)
                if current_d is new_defer:
                    self.key_to_defer.pop(key, None)

        defer.returnValue(_ctx_manager())
