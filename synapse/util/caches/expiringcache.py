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

import logging
from collections import OrderedDict

from synapse.config import cache as cache_config
from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.util.caches import register_cache

logger = logging.getLogger(__name__)


SENTINEL = object()


class ExpiringCache:
    def __init__(
        self,
        cache_name,
        clock,
        max_len=0,
        expiry_ms=0,
        reset_expiry_on_get=False,
        iterable=False,
    ):
        """
        Args:
            cache_name (str): Name of this cache, used for logging.
            clock (Clock)
            max_len (int): Max size of dict. If the dict grows larger than this
                then the oldest items get automatically evicted. Default is 0,
                which indicates there is no max limit.
            expiry_ms (int): How long before an item is evicted from the cache
                in milliseconds. Default is 0, indicating items never get
                evicted based on time.
            reset_expiry_on_get (bool): If true, will reset the expiry time for
                an item on access. Defaults to False.
            iterable (bool): If true, the size is calculated by summing the
                sizes of all entries, rather than the number of entries.
        """
        self._cache_name = cache_name

        self._original_max_size = max_len

        self._max_size = int(max_len * cache_config.properties.default_factor_size)

        self._clock = clock

        self._expiry_ms = expiry_ms
        self._reset_expiry_on_get = reset_expiry_on_get

        self._cache = OrderedDict()

        self.iterable = iterable

        self.metrics = register_cache("expiring", cache_name, self)

        if not self._expiry_ms:
            # Don't bother starting the loop if things never expire
            return

        def f():
            return run_as_background_process(
                "prune_cache_%s" % self._cache_name, self._prune_cache
            )

        self._clock.looping_call(f, self._expiry_ms / 2)

    def __setitem__(self, key, value):
        now = self._clock.time_msec()
        self._cache[key] = _CacheEntry(now, value)
        self.evict()

    def evict(self):
        # Evict if there are now too many items
        while self._max_size and len(self) > self._max_size:
            _key, value = self._cache.popitem(last=False)
            if self.iterable:
                self.metrics.inc_evictions(len(value.value))
            else:
                self.metrics.inc_evictions()

    def __getitem__(self, key):
        try:
            entry = self._cache[key]
            self.metrics.inc_hits()
        except KeyError:
            self.metrics.inc_misses()
            raise

        if self._reset_expiry_on_get:
            entry.time = self._clock.time_msec()

        return entry.value

    def pop(self, key, default=SENTINEL):
        """Removes and returns the value with the given key from the cache.

        If the key isn't in the cache then `default` will be returned if
        specified, otherwise `KeyError` will get raised.

        Identical functionality to `dict.pop(..)`.
        """

        value = self._cache.pop(key, default)
        if value is SENTINEL:
            raise KeyError(key)

        return value

    def __contains__(self, key):
        return key in self._cache

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def setdefault(self, key, value):
        try:
            return self[key]
        except KeyError:
            self[key] = value
            return value

    def _prune_cache(self):
        if not self._expiry_ms:
            # zero expiry time means don't expire. This should never get called
            # since we have this check in start too.
            return
        begin_length = len(self)

        now = self._clock.time_msec()

        keys_to_delete = set()

        for key, cache_entry in self._cache.items():
            if now - cache_entry.time > self._expiry_ms:
                keys_to_delete.add(key)

        for k in keys_to_delete:
            value = self._cache.pop(k)
            if self.iterable:
                self.metrics.inc_evictions(len(value.value))
            else:
                self.metrics.inc_evictions()

        logger.debug(
            "[%s] _prune_cache before: %d, after len: %d",
            self._cache_name,
            begin_length,
            len(self),
        )

    def __len__(self):
        if self.iterable:
            return sum(len(entry.value) for entry in self._cache.values())
        else:
            return len(self._cache)

    def set_cache_factor(self, factor: float) -> bool:
        """
        Set the cache factor for this individual cache.

        This will trigger a resize if it changes, which may require evicting
        items from the cache.

        Returns:
            bool: Whether the cache changed size or not.
        """
        new_size = int(self._original_max_size * factor)
        if new_size != self._max_size:
            self._max_size = new_size
            self.evict()
            return True
        return False


class _CacheEntry:
    __slots__ = ["time", "value"]

    def __init__(self, time, value):
        self.time = time
        self.value = value
