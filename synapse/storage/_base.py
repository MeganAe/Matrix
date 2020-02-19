# -*- coding: utf-8 -*-
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
import random
from abc import ABCMeta
from typing import Any, Optional

from six import PY2
from six.moves import builtins

from canonicaljson import json

from synapse.storage.database import LoggingTransaction  # noqa: F401
from synapse.storage.database import make_in_list_sql_clause  # noqa: F401
from synapse.storage.database import Database
from synapse.types import Collection, get_domain_from_id

logger = logging.getLogger(__name__)


# some of our subclasses have abstract methods, so we use the ABCMeta metaclass.
class SQLBaseStore(metaclass=ABCMeta):
    """Base class for data stores that holds helper functions.

    Note that multiple instances of this class will exist as there will be one
    per data store (and not one per physical database).
    """

    def __init__(self, database: Database, db_conn, hs):
        self.hs = hs
        self._clock = hs.get_clock()
        self.database_engine = database.engine
        self.db = database
        self.rand = random.SystemRandom()

    def _invalidate_state_caches(self, room_id, members_changed):
        """Invalidates caches that are based on the current state, but does
        not stream invalidations down replication.

        Args:
            room_id (str): Room where state changed
            members_changed (iterable[str]): The user_ids of members that have
                changed
        """
        for host in {get_domain_from_id(u) for u in members_changed}:
            self._attempt_to_invalidate_cache("is_host_joined", (room_id, host))
            self._attempt_to_invalidate_cache("was_host_joined", (room_id, host))

        self._attempt_to_invalidate_cache("get_users_in_room", (room_id,))
        self._attempt_to_invalidate_cache("get_room_summary", (room_id,))
        self._attempt_to_invalidate_cache("get_current_state_ids", (room_id,))

    def _attempt_to_invalidate_cache(
        self, cache_name: str, key: Optional[Collection[Any]]
    ):
        """Attempts to invalidate the cache of the given name, ignoring if the
        cache doesn't exist. Mainly used for invalidating caches on workers,
        where they may not have the cache.

        Args:
            cache_name
            key: Entry to invalidate. If None then invalidates the entire
                cache.
        """

        try:
            if key is None:
                getattr(self, cache_name).invalidate_all()
            else:
                getattr(self, cache_name).invalidate(tuple(key))
        except AttributeError:
            # We probably haven't pulled in the cache in this worker,
            # which is fine.
            pass


def db_to_json(db_content):
    """
    Take some data from a database row and return a JSON-decoded object.

    Args:
        db_content (memoryview|buffer|bytes|bytearray|unicode)
    """
    # psycopg2 on Python 3 returns memoryview objects, which we need to
    # cast to bytes to decode
    if isinstance(db_content, memoryview):
        db_content = db_content.tobytes()

    # psycopg2 on Python 2 returns buffer objects, which we need to cast to
    # bytes to decode
    if PY2 and isinstance(db_content, builtins.buffer):
        db_content = bytes(db_content)

    # Decode it to a Unicode string before feeding it to json.loads, so we
    # consistenty get a Unicode-containing object out.
    if isinstance(db_content, (bytes, bytearray)):
        db_content = db_content.decode("utf8")

    try:
        return json.loads(db_content)
    except Exception:
        logging.warning("Tried to decode '%r' as JSON and failed", db_content)
        raise
