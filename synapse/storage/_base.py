# Copyright 2014-2016 OpenMarket Ltd
# Copyright 2017-2018 New Vector Ltd
# Copyright 2019 The Matrix.org Foundation C.I.C.
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
from abc import ABCMeta
from typing import TYPE_CHECKING, Any, Collection, Iterable, Optional, Union

from synapse.storage.database import make_in_list_sql_clause  # noqa: F401; noqa: F401
from synapse.storage.database import DatabasePool, LoggingDatabaseConnection
from synapse.types import get_domain_from_id
from synapse.util import json_decoder

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


# some of our subclasses have abstract methods, so we use the ABCMeta metaclass.
class SQLBaseStore(metaclass=ABCMeta):
    """Base class for data stores that holds helper functions.

    Note that multiple instances of this class will exist as there will be one
    per data store (and not one per physical database).
    """

    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        self.hs = hs
        self._clock = hs.get_clock()
        self.database_engine = database.engine
        self.db_pool = database
        # Beeper: setup the external caches
        self._enable_external_caches()

    def process_replication_rows(
        self,
        stream_name: str,
        instance_name: str,
        token: int,
        rows: Iterable[Any],
    ) -> None:
        pass

    def _invalidate_state_caches(
        self, room_id: str, members_changed: Collection[str]
    ) -> None:
        """Invalidates caches that are based on the current state, but does
        not stream invalidations down replication.

        Args:
            room_id: Room where state changed
            members_changed: The user_ids of members that have changed
        """
        # If there were any membership changes, purge the appropriate caches.
        for host in {get_domain_from_id(u) for u in members_changed}:
            self._attempt_to_invalidate_cache("is_host_joined", (room_id, host))
        if members_changed:
            self._attempt_to_invalidate_cache("get_users_in_room", (room_id,))
            self._attempt_to_invalidate_cache("get_current_hosts_in_room", (room_id,))
            self._attempt_to_invalidate_cache(
                "get_users_in_room_with_profiles", (room_id,)
            )
            self._attempt_to_invalidate_cache(
                "get_number_joined_users_in_room", (room_id,)
            )
            self._attempt_to_invalidate_cache("get_local_users_in_room", (room_id,))

            # There's no easy way of invalidating this cache for just the users
            # that have changed, so we just clear the entire thing.
            self._attempt_to_invalidate_cache("does_pair_of_users_share_a_room", None)

        for user_id in members_changed:
            self._attempt_to_invalidate_cache(
                "get_user_in_room_with_profile", (room_id, user_id)
            )
            self._attempt_to_invalidate_cache(
                "get_rooms_for_user_with_stream_ordering", (user_id,)
            )
            self._attempt_to_invalidate_cache("get_rooms_for_user", (user_id,))

        # Purge other caches based on room state.
        self._attempt_to_invalidate_cache("get_room_summary", (room_id,))
        self._attempt_to_invalidate_cache("get_partial_current_state_ids", (room_id,))

    def _attempt_to_invalidate_cache(
        self, cache_name: str, key: Optional[Collection[Any]]
    ) -> None:
        """Attempts to invalidate the cache of the given name, ignoring if the
        cache doesn't exist. Mainly used for invalidating caches on workers,
        where they may not have the cache.

        Note that this function does not invalidate any remote caches, only the
        local in-memory ones. Any remote invalidation must be performed before
        calling this.

        Args:
            cache_name
            key: Entry to invalidate. If None then invalidates the entire
                cache.
        """

        try:
            cache = getattr(self, cache_name)
        except AttributeError:
            # We probably haven't pulled in the cache in this worker,
            # which is fine.
            return

        if key is None:
            cache.invalidate_all()
        else:
            # Prefer any local-only invalidation method. Invalidating any non-local
            # cache must be be done before this.
            invalidate_method = getattr(cache, "invalidate_local", cache.invalidate)
            invalidate_method(tuple(key))

    # Beeper: externalised caches in Redis
    # It is *critical* that all cache invalidations happe here, something we'll have to keep
    # an eye on when merging upstream changes (until we upstream it!).

    # Currently we're only doing a subset of the state caches, aiming for the ones that cause
    # most pain and avoiding tree caches (not so easily supported in Redis). This list
    # should be a subset of the non-async invalidation method above.

    def _enable_external_caches(self) -> None:
        external_cache = self.hs.get_external_sharded_cache()
        if external_cache.is_enabled():
            self._attempt_to_enable_redis_cache(
                "_get_user_ids_from_membership_event_ids"
            )
            self._attempt_to_enable_redis_cache("get_users_in_room")
            self._attempt_to_enable_redis_cache("get_current_hosts_in_room")
            self._attempt_to_enable_redis_cache("get_local_users_in_room")
            self._attempt_to_enable_redis_cache("get_rooms_for_user")
            self._attempt_to_enable_redis_cache(  # @cachedList of above
                "get_rooms_for_users"
            )
            self._attempt_to_enable_redis_cache(
                "get_rooms_for_user_with_stream_ordering"
            )

    def _attempt_to_enable_redis_cache(self, cache_name: str) -> None:
        external_cache = self.hs.get_external_sharded_cache()
        if not external_cache.is_enabled():
            return

        try:
            cache_method = getattr(self, cache_name)
        except AttributeError:
            return

        cache_method.enable_redis_cache(external_cache)

    async def _invalidate_external_state_caches(
        self, room_id: str, members_changed: Collection[str]
    ) -> None:
        external_cache = self.hs.get_external_sharded_cache()
        if not external_cache.is_enabled():
            return

        if members_changed:
            await self._attempt_to_invalidate_external_cache(
                "get_users_in_room", (room_id,)
            )
            await self._attempt_to_invalidate_external_cache(
                "get_current_hosts_in_room", (room_id,)
            )
            await self._attempt_to_invalidate_external_cache(
                "get_local_users_in_room", (room_id,)
            )

        for user_id in members_changed:
            await self._attempt_to_invalidate_external_cache(
                "get_rooms_for_user", (user_id,)
            )
            await self._attempt_to_invalidate_external_cache(
                "get_rooms_for_user_with_stream_ordering", (user_id,)
            )

    async def _attempt_to_invalidate_external_cache(
        self,
        cache_name: str,
        key: Collection[Any],
    ) -> None:
        try:
            cache = getattr(self, cache_name)
        except AttributeError:
            return

        await cache.invalidate_external(tuple(key))


def db_to_json(db_content: Union[memoryview, bytes, bytearray, str]) -> Any:
    """
    Take some data from a database row and return a JSON-decoded object.

    Args:
        db_content: The JSON-encoded contents from the database.

    Returns:
        The object decoded from JSON.
    """
    # psycopg2 on Python 3 returns memoryview objects, which we need to
    # cast to bytes to decode
    if isinstance(db_content, memoryview):
        db_content = db_content.tobytes()

    # Decode it to a Unicode string before feeding it to the JSON decoder, since
    # it only supports handling strings
    if isinstance(db_content, (bytes, bytearray)):
        db_content = db_content.decode("utf8")

    try:
        return json_decoder.decode(db_content)
    except Exception:
        logging.warning("Tried to decode '%r' as JSON and failed", db_content)
        raise
