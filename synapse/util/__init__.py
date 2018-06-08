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

from synapse.util.logcontext import PreserveLoggingContext

from twisted.internet import defer, task

import logging

from itertools import islice

logger = logging.getLogger(__name__)


def unwrapFirstError(failure):
    # defer.gatherResults and DeferredLists wrap failures.
    failure.trap(defer.FirstError)
    return failure.value.subFailure


class Clock(object):
    """A small utility that obtains current time-of-day so that time may be
    mocked during unit-tests.

    TODO(paul): Also move the sleep() functionality into it
    """

    def __init__(self, reactor=None):
        if not reactor:
            from twisted.internet import reactor
        self._reactor = reactor

    def time(self):
        """Returns the current system time in seconds since epoch."""
        return self._reactor.seconds()

    def time_msec(self):
        """Returns the current system time in miliseconds since epoch."""
        return int(self.time() * 1000)

    def looping_call(self, f, msec):
        """Call a function repeatedly.

         Waits `msec` initially before calling `f` for the first time.

        Args:
            f(function): The function to call repeatedly.
            msec(float): How long to wait between calls in milliseconds.
        """
        call = task.LoopingCall(f)
        call.clock = self._reactor
        call.start(msec / 1000.0, now=False)
        return call

    def call_later(self, delay, callback, *args, **kwargs):
        """Call something later

        Args:
            delay(float): How long to wait in seconds.
            callback(function): Function to call
            *args: Postional arguments to pass to function.
            **kwargs: Key arguments to pass to function.
        """
        def wrapped_callback(*args, **kwargs):
            with PreserveLoggingContext():
                callback(*args, **kwargs)

        with PreserveLoggingContext():
            return self._reactor.callLater(delay, wrapped_callback, *args, **kwargs)

    def cancel_call_later(self, timer, ignore_errs=False):
        try:
            timer.cancel()
        except Exception:
            if not ignore_errs:
                raise


def batch_iter(iterable, size):
    """batch an iterable up into tuples with a maximum size

    Args:
        iterable (iterable): the iterable to slice
        size (int): the maximum batch size

    Returns:
        an iterator over the chunks
    """
    # make sure we can deal with iterables like lists too
    sourceiter = iter(iterable)
    # call islice until it returns an empty tuple
    return iter(lambda: tuple(islice(sourceiter, size)), ())
