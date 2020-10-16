# -*- coding: utf-8 -*-
# Copyright 2020 The Matrix.org Foundation C.I.C.
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

import unittest
from functools import partial

from twisted.internet import defer

from synapse.util.caches.deferred_cache import DeferredCache


class DeferredCacheTestCase(unittest.TestCase):
    def test_empty(self):
        cache = DeferredCache("test")
        failed = False
        try:
            cache.get("foo")
        except KeyError:
            failed = True

        self.assertTrue(failed)

    def test_hit(self):
        cache = DeferredCache("test")
        cache.prefill("foo", 123)

        self.assertEquals(cache.get("foo"), 123)

    def test_invalidate(self):
        cache = DeferredCache("test")
        cache.prefill(("foo",), 123)
        cache.invalidate(("foo",))

        failed = False
        try:
            cache.get(("foo",))
        except KeyError:
            failed = True

        self.assertTrue(failed)

    def test_invalidate_all(self):
        cache = DeferredCache("testcache")

        callback_record = [False, False]

        def record_callback(idx):
            callback_record[idx] = True

        # add a couple of pending entries
        d1 = defer.Deferred()
        cache.set("key1", d1, partial(record_callback, 0))

        d2 = defer.Deferred()
        cache.set("key2", d2, partial(record_callback, 1))

        # lookup should return observable deferreds
        self.assertFalse(cache.get("key1").has_called())
        self.assertFalse(cache.get("key2").has_called())

        # let one of the lookups complete
        d2.callback("result2")

        # for now at least, the cache will return real results rather than an
        # observabledeferred
        self.assertEqual(cache.get("key2"), "result2")

        # now do the invalidation
        cache.invalidate_all()

        # lookup should return none
        self.assertIsNone(cache.get("key1", None))
        self.assertIsNone(cache.get("key2", None))

        # both callbacks should have been callbacked
        self.assertTrue(callback_record[0], "Invalidation callback for key1 not called")
        self.assertTrue(callback_record[1], "Invalidation callback for key2 not called")

        # letting the other lookup complete should do nothing
        d1.callback("result1")
        self.assertIsNone(cache.get("key1", None))

    def test_eviction(self):
        cache = DeferredCache(
            "test", max_entries=2, apply_cache_factor_from_config=False
        )

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
        cache = DeferredCache(
            "test", max_entries=2, apply_cache_factor_from_config=False
        )

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
