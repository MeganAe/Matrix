# -*- coding: utf-8 -*-
# Copyright 2015, 2016 OpenMarket Ltd
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


from itertools import chain


# TODO(paul): I can't believe Python doesn't have one of these
def map_concat(func, items):
    # flatten a list-of-lists
    return list(chain.from_iterable(map(func, items)))


class BaseMetric(object):

    def __init__(self, name, labels=[]):
        self.name = name
        self.labels = labels  # OK not to clone as we never write it

    def dimension(self):
        return len(self.labels)

    def is_scalar(self):
        return not len(self.labels)

    def _render_labelvalue(self, value):
        # TODO: some kind of value escape
        return '"%s"' % (value)

    def _render_key(self, values):
        if self.is_scalar():
            return ""
        return "{%s}" % (
            ",".join(["%s=%s" % (k, self._render_labelvalue(v))
                      for k, v in zip(self.labels, values)])
        )


class CounterMetric(BaseMetric):
    """The simplest kind of metric; one that stores a monotonically-increasing
    integer that counts events."""

    def __init__(self, *args, **kwargs):
        super(CounterMetric, self).__init__(*args, **kwargs)

        self.counts = {}

        # Scalar metrics are never empty
        if self.is_scalar():
            self.counts[()] = 0

    def inc_by(self, incr, *values):
        if len(values) != self.dimension():
            raise ValueError(
                "Expected as many values to inc() as labels (%d)" % (self.dimension())
            )

        # TODO: should assert that the tag values are all strings

        if values not in self.counts:
            self.counts[values] = incr
        else:
            self.counts[values] += incr

    def inc(self, *values):
        self.inc_by(1, *values)

    def render_item(self, k):
        return ["%s%s %d" % (self.name, self._render_key(k), self.counts[k])]

    def render(self):
        return map_concat(self.render_item, sorted(self.counts.keys()))


class CallbackMetric(BaseMetric):
    """A metric that returns the numeric value returned by a callback whenever
    it is rendered. Typically this is used to implement gauges that yield the
    size or other state of some in-memory object by actively querying it."""

    def __init__(self, name, callback, labels=[]):
        super(CallbackMetric, self).__init__(name, labels=labels)

        self.callback = callback

    def render(self):
        value = self.callback()

        if self.is_scalar():
            return ["%s %d" % (self.name, value)]

        return ["%s%s %d" % (self.name, self._render_key(k), value[k])
                for k in sorted(value.keys())]


class DistributionMetric(object):
    """A combination of an event counter and an accumulator, which counts
    both the number of events and accumulates the total value. Typically this
    could be used to keep track of method-running times, or other distributions
    of values that occur in discrete occurances.

    TODO(paul): Try to export some heatmap-style stats?
    """

    def __init__(self, name, *args, **kwargs):
        self.counts = CounterMetric(name + ":count", **kwargs)
        self.totals = CounterMetric(name + ":total", **kwargs)

    def inc_by(self, inc, *values):
        self.counts.inc(*values)
        self.totals.inc_by(inc, *values)

    def render(self):
        return self.counts.render() + self.totals.render()


class CacheMetric(object):
    __slots__ = ("name", "cache_name", "hits", "misses", "size_callback")

    def __init__(self, name, size_callback, cache_name):
        self.name = name
        self.cache_name = cache_name

        self.hits = 0
        self.misses = 0

        self.size_callback = size_callback

    def inc_hits(self):
        self.hits += 1

    def inc_misses(self):
        self.misses += 1

    def render(self):
        size = self.size_callback()
        hits = self.hits
        total = self.misses + self.hits

        return [
            """%s:hits{name="%s"} %d""" % (self.name, self.cache_name, hits),
            """%s:total{name="%s"} %d""" % (self.name, self.cache_name, total),
            """%s:size{name="%s"} %d""" % (self.name, self.cache_name, size),
        ]
