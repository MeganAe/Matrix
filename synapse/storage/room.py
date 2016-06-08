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

from twisted.internet import defer

from synapse.api.errors import StoreError

from ._base import SQLBaseStore
from synapse.util.caches.descriptors import cachedInlineCallbacks
from .engines import PostgresEngine, Sqlite3Engine

import collections
import logging
import ujson as json

logger = logging.getLogger(__name__)


OpsLevel = collections.namedtuple(
    "OpsLevel",
    ("ban_level", "kick_level", "redact_level",)
)


class RoomStore(SQLBaseStore):

    @defer.inlineCallbacks
    def store_room(self, room_id, room_creator_user_id, is_public):
        """Stores a room.

        Args:
            room_id (str): The desired room ID, can be None.
            room_creator_user_id (str): The user ID of the room creator.
            is_public (bool): True to indicate that this room should appear in
            public room lists.
        Raises:
            StoreError if the room could not be stored.
        """
        try:
            yield self._simple_insert(
                "rooms",
                {
                    "room_id": room_id,
                    "creator": room_creator_user_id,
                    "is_public": is_public,
                },
                desc="store_room",
            )
        except Exception as e:
            logger.error("store_room with room_id=%s failed: %s", room_id, e)
            raise StoreError(500, "Problem creating room.")

    def get_room(self, room_id):
        """Retrieve a room.

        Args:
            room_id (str): The ID of the room to retrieve.
        Returns:
            A namedtuple containing the room information, or an empty list.
        """
        return self._simple_select_one(
            table="rooms",
            keyvalues={"room_id": room_id},
            retcols=("room_id", "is_public", "creator"),
            desc="get_room",
            allow_none=True,
        )

    def set_room_is_public(self, room_id, is_public):
        return self._simple_update_one(
            table="rooms",
            keyvalues={"room_id": room_id},
            updatevalues={"is_public": is_public},
            desc="set_room_is_public",
        )

    def get_public_room_ids(self):
        return self._simple_select_onecol(
            table="rooms",
            keyvalues={
                "is_public": True,
            },
            retcol="room_id",
            desc="get_public_room_ids",
        )

    def get_room_count(self):
        """Retrieve a list of all rooms
        """

        def f(txn):
            sql = "SELECT count(*)  FROM rooms"
            txn.execute(sql)
            row = txn.fetchone()
            return row[0] or 0

        return self.runInteraction(
            "get_rooms", f
        )

    def _store_room_topic_txn(self, txn, event):
        if hasattr(event, "content") and "topic" in event.content:
            self._simple_insert_txn(
                txn,
                "topics",
                {
                    "event_id": event.event_id,
                    "room_id": event.room_id,
                    "topic": event.content["topic"],
                },
            )

            self._store_event_search_txn(
                txn, event, "content.topic", event.content["topic"]
            )

    def _store_room_name_txn(self, txn, event):
        if hasattr(event, "content") and "name" in event.content:
            self._simple_insert_txn(
                txn,
                "room_names",
                {
                    "event_id": event.event_id,
                    "room_id": event.room_id,
                    "name": event.content["name"],
                }
            )

            self._store_event_search_txn(
                txn, event, "content.name", event.content["name"]
            )

    def _store_room_message_txn(self, txn, event):
        if hasattr(event, "content") and "body" in event.content:
            self._store_event_search_txn(
                txn, event, "content.body", event.content["body"]
            )

    def _store_history_visibility_txn(self, txn, event):
        self._store_content_index_txn(txn, event, "history_visibility")

    def _store_guest_access_txn(self, txn, event):
        self._store_content_index_txn(txn, event, "guest_access")

    def _store_content_index_txn(self, txn, event, key):
        if hasattr(event, "content") and key in event.content:
            sql = (
                "INSERT INTO %(key)s"
                " (event_id, room_id, %(key)s)"
                " VALUES (?, ?, ?)" % {"key": key}
            )
            txn.execute(sql, (
                event.event_id,
                event.room_id,
                event.content[key]
            ))

    def _store_event_search_txn(self, txn, event, key, value):
        if isinstance(self.database_engine, PostgresEngine):
            sql = (
                "INSERT INTO event_search"
                " (event_id, room_id, key, vector, stream_ordering, origin_server_ts)"
                " VALUES (?,?,?,to_tsvector('english', ?),?,?)"
            )
            txn.execute(
                sql,
                (
                    event.event_id, event.room_id, key, value,
                    event.internal_metadata.stream_ordering,
                    event.origin_server_ts,
                )
            )
        elif isinstance(self.database_engine, Sqlite3Engine):
            sql = (
                "INSERT INTO event_search (event_id, room_id, key, value)"
                " VALUES (?,?,?,?)"
            )
            txn.execute(sql, (event.event_id, event.room_id, key, value,))
        else:
            # This should be unreachable.
            raise Exception("Unrecognized database engine")

    @cachedInlineCallbacks()
    def get_room_name_and_aliases(self, room_id):
        def get_room_name(txn):
            sql = (
                "SELECT name FROM room_names"
                " INNER JOIN current_state_events USING (room_id, event_id)"
                " WHERE room_id = ?"
                " LIMIT 1"
            )

            txn.execute(sql, (room_id,))
            rows = txn.fetchall()
            if rows:
                return rows[0][0]
            else:
                return None

            return [row[0] for row in txn.fetchall()]

        def get_room_aliases(txn):
            sql = (
                "SELECT content FROM current_state_events"
                " INNER JOIN events USING (room_id, event_id)"
                " WHERE room_id = ?"
            )
            txn.execute(sql, (room_id,))
            return [row[0] for row in txn.fetchall()]

        name = yield self.runInteraction("get_room_name", get_room_name)
        alias_contents = yield self.runInteraction("get_room_aliases", get_room_aliases)

        aliases = []

        for c in alias_contents:
            try:
                content = json.loads(c)
            except:
                continue

            aliases.extend(content.get('aliases', []))

        defer.returnValue((name, aliases))

    def add_event_report(self, room_id, event_id, user_id, reason, content,
                         received_ts):
        next_id = self._event_reports_id_gen.get_next()
        return self._simple_insert(
            table="event_reports",
            values={
                "id": next_id,
                "received_ts": received_ts,
                "room_id": room_id,
                "event_id": event_id,
                "user_id": user_id,
                "reason": reason,
                "content": json.dumps(content),
            },
            desc="add_event_report"
        )
