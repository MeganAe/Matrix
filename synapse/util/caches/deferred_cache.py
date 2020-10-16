# -*- coding: utf-8 -*-
# Copyright 2015, 2016 OpenMarket Ltd
# Copyright 2018 New Vector Ltd
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

import enum
import threading
from typing import Callable, Generic, Iterable, MutableMapping, Optional, TypeVar, cast

from prometheus_client import Gauge

from twisted.internet import defer

from synapse.util.async_helpers import ObservableDeferred
from synapse.util.caches.lrucache import LruCache
from synapse.util.caches.treecache import TreeCache, iterate_tree_cache_entry

cache_pending_metric = Gauge(
    "synapse_util_caches_cache_pending",
    "Number of lookups currently pending for this cache",
    ["name"],
)


KT = TypeVar("KT")
VT = TypeVar("VT")


class _Sentinel(enum.Enum):
    # defining a sentinel in this way allows mypy to correctly handle the
    # type of a dictionary lookup.
    sentinel = object()


class DeferredCache(Generic[KT, VT]):
    """Wraps an LruCache, adding support for Deferred results.

    It expects that each entry added with set() will be a Deferred; likewise get()
    may return an ObservableDeferred.
    """

    __slots__ = (
        "cache",
        "thread",
        "_pending_deferred_cache",
    )

    def __init__(
        self,
        name: str,
        max_entries: int = 1000,
        keylen: int = 1,
        tree: bool = False,
        iterable: bool = False,
        apply_cache_factor_from_config: bool = True,
    ):
        """
        Args:
            name: The name of the cache
            max_entries: Maximum amount of entries that the cache will hold
            keylen: The length of the tuple used as the cache key. Ignored unless
               `tree` is True.
            tree: Use a TreeCache instead of a dict as the underlying cache type
            iterable: If True, count each item in the cached object as an entry,
                rather than each cached object
            apply_cache_factor_from_config: Whether cache factors specified in the
                config file affect `max_entries`
        """
        cache_type = TreeCache if tree else dict

        # _pending_deferred_cache maps from the key value to a `CacheEntry` object.
        self._pending_deferred_cache = (
            cache_type()
        )  # type: MutableMapping[KT, CacheEntry]

        def metrics_cb():
            cache_pending_metric.labels(name).set(len(self._pending_deferred_cache))

        # cache is used for completed results and maps to the result itself, rather than
        # a Deferred.
        self.cache = LruCache(
            max_size=max_entries,
            keylen=keylen,
            cache_name=name,
            cache_type=cache_type,
            size_callback=(lambda d: len(d)) if iterable else None,
            metrics_collection_callback=metrics_cb,
            apply_cache_factor_from_config=apply_cache_factor_from_config,
        )

        self.thread = None  # type: Optional[threading.Thread]

    @property
    def max_entries(self):
        return self.cache.max_size

    def check_thread(self):
        expected_thread = self.thread
        if expected_thread is None:
            self.thread = threading.current_thread()
        else:
            if expected_thread is not threading.current_thread():
                raise ValueError(
                    "Cache objects can only be accessed from the main thread"
                )

    def get(
        self,
        key: KT,
        default=_Sentinel.sentinel,
        callback: Optional[Callable[[], None]] = None,
        update_metrics: bool = True,
    ):
        """Looks the key up in the caches.

        Args:
            key(tuple)
            default: What is returned if key is not in the caches. If not
                specified then function throws KeyError instead
            callback(fn): Gets called when the entry in the cache is invalidated
            update_metrics (bool): whether to update the cache hit rate metrics

        Returns:
            Either an ObservableDeferred or the result itself
        """
        callbacks = [callback] if callback else []
        val = self._pending_deferred_cache.get(key, _Sentinel.sentinel)
        if val is not _Sentinel.sentinel:
            val.callbacks.update(callbacks)
            if update_metrics:
                m = self.cache.metrics
                assert m  # we always have a name, so should always have metrics
                m.inc_hits()
            return val.deferred

        val = self.cache.get(
            key, default, callbacks=callbacks, update_metrics=update_metrics
        )
        if val is _Sentinel.sentinel:
            raise KeyError()
        else:
            return val

    def set(
        self,
        key: KT,
        value: defer.Deferred,
        callback: Optional[Callable[[], None]] = None,
    ) -> ObservableDeferred:
        if not isinstance(value, defer.Deferred):
            raise TypeError("not a Deferred")

        callbacks = [callback] if callback else []
        self.check_thread()
        observable = ObservableDeferred(value, consumeErrors=True)
        observer = observable.observe()
        entry = CacheEntry(deferred=observable, callbacks=callbacks)

        existing_entry = self._pending_deferred_cache.pop(key, None)
        if existing_entry:
            existing_entry.invalidate()

        self._pending_deferred_cache[key] = entry

        def compare_and_pop():
            """Check if our entry is still the one in _pending_deferred_cache, and
            if so, pop it.

            Returns true if the entries matched.
            """
            existing_entry = self._pending_deferred_cache.pop(key, None)
            if existing_entry is entry:
                return True

            # oops, the _pending_deferred_cache has been updated since
            # we started our query, so we are out of date.
            #
            # Better put back whatever we took out. (We do it this way
            # round, rather than peeking into the _pending_deferred_cache
            # and then removing on a match, to make the common case faster)
            if existing_entry is not None:
                self._pending_deferred_cache[key] = existing_entry

            return False

        def cb(result):
            if compare_and_pop():
                self.cache.set(key, result, entry.callbacks)
            else:
                # we're not going to put this entry into the cache, so need
                # to make sure that the invalidation callbacks are called.
                # That was probably done when _pending_deferred_cache was
                # updated, but it's possible that `set` was called without
                # `invalidate` being previously called, in which case it may
                # not have been. Either way, let's double-check now.
                entry.invalidate()

        def eb(_fail):
            compare_and_pop()
            entry.invalidate()

        # once the deferred completes, we can move the entry from the
        # _pending_deferred_cache to the real cache.
        #
        observer.addCallbacks(cb, eb)
        return observable

    def prefill(self, key: KT, value: VT, callback: Callable[[], None] = None):
        callbacks = [callback] if callback else []
        self.cache.set(key, value, callbacks=callbacks)

    def invalidate(self, key):
        self.check_thread()
        self.cache.pop(key, None)

        # if we have a pending lookup for this key, remove it from the
        # _pending_deferred_cache, which will (a) stop it being returned
        # for future queries and (b) stop it being persisted as a proper entry
        # in self.cache.
        entry = self._pending_deferred_cache.pop(key, None)

        # run the invalidation callbacks now, rather than waiting for the
        # deferred to resolve.
        if entry:
            entry.invalidate()

    def invalidate_many(self, key: KT):
        self.check_thread()
        if not isinstance(key, tuple):
            raise TypeError("The cache key must be a tuple not %r" % (type(key),))
        self.cache.del_multi(key)

        # if we have a pending lookup for this key, remove it from the
        # _pending_deferred_cache, as above
        entry_dict = self._pending_deferred_cache.pop(cast(KT, key), None)
        if entry_dict is not None:
            for entry in iterate_tree_cache_entry(entry_dict):
                entry.invalidate()

    def invalidate_all(self):
        self.check_thread()
        self.cache.clear()
        for entry in self._pending_deferred_cache.values():
            entry.invalidate()
        self._pending_deferred_cache.clear()


class CacheEntry:
    __slots__ = ["deferred", "callbacks", "invalidated"]

    def __init__(
        self, deferred: ObservableDeferred, callbacks: Iterable[Callable[[], None]]
    ):
        self.deferred = deferred
        self.callbacks = set(callbacks)
        self.invalidated = False

    def invalidate(self):
        if not self.invalidated:
            self.invalidated = True
            for callback in self.callbacks:
                callback()
            self.callbacks.clear()
