# -*- coding: utf-8 -*-
# Copyright 2014, 2015 OpenMarket Ltd
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

import collections
import logging

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
                RoomsTable.table_name,
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
            table=RoomsTable.table_name,
            keyvalues={"room_id": room_id},
            retcols=RoomsTable.fields,
            desc="get_room",
        )

    @defer.inlineCallbacks
    def get_rooms(self, is_public):
        """Retrieve a list of all public rooms.

        Args:
            is_public (bool): True if the rooms returned should be public.
        Returns:
            A list of room dicts containing at least a "room_id" key, a
            "topic" key if one is set, and a "name" key if one is set
        """

        def f(txn):
            topic_subquery = (
                "SELECT topics.event_id as event_id, "
                "topics.room_id as room_id, topic "
                "FROM topics "
                "INNER JOIN current_state_events as c "
                "ON c.event_id = topics.event_id "
            )

            name_subquery = (
                "SELECT room_names.event_id as event_id, "
                "room_names.room_id as room_id, name "
                "FROM room_names "
                "INNER JOIN current_state_events as c "
                "ON c.event_id = room_names.event_id "
            )

            # We use non printing ascii character US () as a seperator
            sql = (
                "SELECT r.room_id, n.name, t.topic, "
                "group_concat(a.room_alias, '') "
                "FROM rooms AS r "
                "LEFT JOIN (%(topic)s) AS t ON t.room_id = r.room_id "
                "LEFT JOIN (%(name)s) AS n ON n.room_id = r.room_id "
                "INNER JOIN room_aliases AS a ON a.room_id = r.room_id "
                "WHERE r.is_public = ? "
                "GROUP BY r.room_id "
            ) % {
                "topic": topic_subquery,
                "name": name_subquery,
            }

            c = txn.execute(sql, (is_public,))

            return c.fetchall()

        rows = yield self.runInteraction(
            "get_rooms", f
        )

        ret = [
            {
                "room_id": r[0],
                "name": r[1],
                "topic": r[2],
                "aliases": r[3].split(""),
            }
            for r in rows
        ]

        defer.returnValue(ret)

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

    @defer.inlineCallbacks
    def get_room_name_and_aliases(self, room_id):
        del_sql = (
            "SELECT event_id FROM redactions WHERE redacts = e.event_id "
            "LIMIT 1"
        )

        sql = (
            "SELECT e.*, (%(redacted)s) AS redacted FROM events as e "
            "INNER JOIN current_state_events as c ON e.event_id = c.event_id "
            "INNER JOIN state_events as s ON e.event_id = s.event_id "
            "WHERE c.room_id = ? "
        ) % {
            "redacted": del_sql,
        }

        sql += " AND ((s.type = 'm.room.name' AND s.state_key = '')"
        sql += " OR s.type = 'm.room.aliases')"
        args = (room_id,)

        results = yield self._execute_and_decode("get_current_state", sql, *args)

        events = yield self._parse_events(results)

        name = None
        aliases = []

        for e in events:
            if e.type == 'm.room.name':
                if 'name' in e.content:
                    name = e.content['name']
            elif e.type == 'm.room.aliases':
                if 'aliases' in e.content:
                    aliases.extend(e.content['aliases'])

        defer.returnValue((name, aliases))


class RoomsTable(object):
    table_name = "rooms"

    fields = [
        "room_id",
        "is_public",
        "creator"
    ]
