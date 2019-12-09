# -*- coding: utf-8 -*-
# Copyright 2016 OpenMarket Ltd
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

import inspect
import logging
from functools import wraps

from prometheus_client import Counter

from twisted.internet import defer

from synapse.logging.context import LoggingContext
from synapse.metrics import InFlightGauge

logger = logging.getLogger(__name__)

block_counter = Counter("synapse_util_metrics_block_count", "", ["block_name"])

block_timer = Counter("synapse_util_metrics_block_time_seconds", "", ["block_name"])

block_ru_utime = Counter(
    "synapse_util_metrics_block_ru_utime_seconds", "", ["block_name"]
)

block_ru_stime = Counter(
    "synapse_util_metrics_block_ru_stime_seconds", "", ["block_name"]
)

block_db_txn_count = Counter(
    "synapse_util_metrics_block_db_txn_count", "", ["block_name"]
)

# seconds spent waiting for db txns, excluding scheduling time, in this block
block_db_txn_duration = Counter(
    "synapse_util_metrics_block_db_txn_duration_seconds", "", ["block_name"]
)

# seconds spent waiting for a db connection, in this block
block_db_sched_duration = Counter(
    "synapse_util_metrics_block_db_sched_duration_seconds", "", ["block_name"]
)

# Tracks the number of blocks currently active
in_flight = InFlightGauge(
    "synapse_util_metrics_block_in_flight",
    "",
    labels=["block_name"],
    sub_metrics=["real_time_max", "real_time_sum"],
)


def measure_func(name=None):
    def wrapper(func):
        block_name = func.__name__ if name is None else name

        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def measured_func(self, *args, **kwargs):
                with Measure(self.clock, block_name):
                    r = await func(self, *args, **kwargs)
                return r

        else:

            @wraps(func)
            @defer.inlineCallbacks
            def measured_func(self, *args, **kwargs):
                with Measure(self.clock, block_name):
                    r = yield func(self, *args, **kwargs)
                return r

        return measured_func

    return wrapper


class Measure(object):
    __slots__ = [
        "clock",
        "name",
        "_logging_context",
        "start",
    ]

    def __init__(self, clock, name):
        self.clock = clock
        self.name = name
        self._logging_context = None
        self.start = None

    def __enter__(self):
        if self._logging_context:
            raise RuntimeError("Measure() objects cannot be re-used")

        self.start = self.clock.time()
        parent_context = LoggingContext.current_context()
        self._logging_context = LoggingContext(
            "Measure[%s]" % (self.name,), parent_context
        )
        self._logging_context.__enter__()
        in_flight.register((self.name,), self._update_in_flight)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self._logging_context:
            raise RuntimeError("Measure() block exited without being entered")

        duration = self.clock.time() - self.start
        usage = self._logging_context.get_resource_usage()

        in_flight.unregister((self.name,), self._update_in_flight)
        self._logging_context.__exit__(exc_type, exc_val, exc_tb)

        try:
            block_counter.labels(self.name).inc()
            block_timer.labels(self.name).inc(duration)
            block_ru_utime.labels(self.name).inc(usage.ru_utime)
            block_ru_stime.labels(self.name).inc(usage.ru_stime)
            block_db_txn_count.labels(self.name).inc(usage.db_txn_count)
            block_db_txn_duration.labels(self.name).inc(usage.db_txn_duration_sec)
            block_db_sched_duration.labels(self.name).inc(usage.db_sched_duration_sec)
        except ValueError:
            logger.warning("Failed to save metrics! Usage: %s", usage)

    def _update_in_flight(self, metrics):
        """Gets called when processing in flight metrics
        """
        duration = self.clock.time() - self.start

        metrics.real_time_max = max(metrics.real_time_max, duration)
        metrics.real_time_sum += duration

        # TODO: Add other in flight metrics.
