# -*- coding: utf-8 -*-
# Copyright 2015 OpenMarket Ltd
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


from tests import unittest
from twisted.internet import defer

from synapse.storage._base import Cache, cached


class CacheTestCase(unittest.TestCase):

    def setUp(self):
        self.cache = Cache("test")

    def test_empty(self):
        failed = False
        try:
            self.cache.get("foo")
        except KeyError:
            failed = True

        self.assertTrue(failed)

    def test_hit(self):
        self.cache.prefill("foo", 123)

        self.assertEquals(self.cache.get("foo"), 123)

    def test_invalidate(self):
        self.cache.prefill("foo", 123)
        self.cache.invalidate("foo")

        failed = False
        try:
            self.cache.get("foo")
        except KeyError:
            failed = True

        self.assertTrue(failed)

    def test_eviction(self):
        cache = Cache("test", max_entries=2)

        cache.prefill(1, "one")
        cache.prefill(2, "two")
        cache.prefill(3, "three")  # 1 will be evicted

        failed = False
        try:
            cache.get(1)
        except KeyError:
            failed = True

        self.assertTrue(failed)

        cache.get(2)
        cache.get(3)

    def test_eviction_lru(self):
        cache = Cache("test", max_entries=2, lru=True)

        cache.prefill(1, "one")
        cache.prefill(2, "two")

        # Now access 1 again, thus causing 2 to be least-recently used
        cache.get(1)

        cache.prefill(3, "three")

        failed = False
        try:
            cache.get(2)
        except KeyError:
            failed = True

        self.assertTrue(failed)

        cache.get(1)
        cache.get(3)


class CacheDecoratorTestCase(unittest.TestCase):

    @defer.inlineCallbacks
    def test_passthrough(self):
        @cached()
        def func(self, key):
            return key

        self.assertEquals((yield func(self, "foo")), "foo")
        self.assertEquals((yield func(self, "bar")), "bar")

    @defer.inlineCallbacks
    def test_hit(self):
        callcount = [0]

        @cached()
        def func(self, key):
            callcount[0] += 1
            return key

        yield func(self, "foo")

        self.assertEquals(callcount[0], 1)

        self.assertEquals((yield func(self, "foo")), "foo")
        self.assertEquals(callcount[0], 1)

    @defer.inlineCallbacks
    def test_invalidate(self):
        callcount = [0]

        @cached()
        def func(self, key):
            callcount[0] += 1
            return key

        yield func(self, "foo")

        self.assertEquals(callcount[0], 1)

        func.invalidate("foo")

        yield func(self, "foo")

        self.assertEquals(callcount[0], 2)

    def test_invalidate_missing(self):
        @cached()
        def func(self, key):
            return key

        func.invalidate("what")

    @defer.inlineCallbacks
    def test_max_entries(self):
        callcount = [0]

        @cached(max_entries=10)
        def func(self, key):
            callcount[0] += 1
            return key

        for k in range(0,12):
            yield func(self, k)

        self.assertEquals(callcount[0], 12)

        # There must have been at least 2 evictions, meaning if we calculate
        # all 12 values again, we must get called at least 2 more times
        for k in range(0,12):
            yield func(self, k)

        self.assertTrue(callcount[0] >= 14,
            msg="Expected callcount >= 14, got %d" % (callcount[0]))

    @defer.inlineCallbacks
    def test_prefill(self):
        callcount = [0]

        @cached()
        def func(self, key):
            callcount[0] += 1
            return key

        func.prefill("foo", 123)

        self.assertEquals((yield func(self, "foo")), 123)
        self.assertEquals(callcount[0], 0)
