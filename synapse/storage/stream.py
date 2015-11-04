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

""" This module is responsible for getting events from the DB for pagination
and event streaming.

The order it returns events in depend on whether we are streaming forwards or
are paginating backwards. We do this because we want to handle out of order
messages nicely, while still returning them in the correct order when we
paginate bacwards.

This is implemented by keeping two ordering columns: stream_ordering and
topological_ordering. Stream ordering is basically insertion/received order
(except for events from backfill requests). The topological_ordering is a
weak ordering of events based on the pdu graph.

This means that we have to have two different types of tokens, depending on
what sort order was used:
    - stream tokens are of the form: "s%d", which maps directly to the column
    - topological tokems: "t%d-%d", where the integers map to the topological
      and stream ordering columns respectively.
"""

from twisted.internet import defer

from ._base import SQLBaseStore
from synapse.util.caches.descriptors import cachedInlineCallbacks
from synapse.api.constants import EventTypes
from synapse.types import RoomStreamToken
from synapse.util.logutils import log_function

import logging


logger = logging.getLogger(__name__)


MAX_STREAM_SIZE = 1000


_STREAM_TOKEN = "stream"
_TOPOLOGICAL_TOKEN = "topological"


def lower_bound(token):
    if token.topological is None:
        return "(%d < %s)" % (token.stream, "stream_ordering")
    else:
        return "(%d < %s OR (%d = %s AND %d < %s))" % (
            token.topological, "topological_ordering",
            token.topological, "topological_ordering",
            token.stream, "stream_ordering",
        )


def upper_bound(token):
    if token.topological is None:
        return "(%d >= %s)" % (token.stream, "stream_ordering")
    else:
        return "(%d > %s OR (%d = %s AND %d >= %s))" % (
            token.topological, "topological_ordering",
            token.topological, "topological_ordering",
            token.stream, "stream_ordering",
        )


class StreamStore(SQLBaseStore):

    @defer.inlineCallbacks
    def get_appservice_room_stream(self, service, from_key, to_key, limit=0):
        # NB this lives here instead of appservice.py so we can reuse the
        # 'private' StreamToken class in this file.
        if limit:
            limit = max(limit, MAX_STREAM_SIZE)
        else:
            limit = MAX_STREAM_SIZE

        # From and to keys should be integers from ordering.
        from_id = RoomStreamToken.parse_stream_token(from_key)
        to_id = RoomStreamToken.parse_stream_token(to_key)

        if from_key == to_key:
            defer.returnValue(([], to_key))
            return

        # select all the events between from/to with a sensible limit
        sql = (
            "SELECT e.event_id, e.room_id, e.type, s.state_key, "
            "e.stream_ordering FROM events AS e "
            "LEFT JOIN state_events as s ON "
            "e.event_id = s.event_id "
            "WHERE e.stream_ordering > ? AND e.stream_ordering <= ? "
            "ORDER BY stream_ordering ASC LIMIT %(limit)d "
        ) % {
            "limit": limit
        }

        def f(txn):
            # pull out all the events between the tokens
            txn.execute(sql, (from_id.stream, to_id.stream,))
            rows = self.cursor_to_dict(txn)

            # Logic:
            #  - We want ALL events which match the AS room_id regex
            #  - We want ALL events which match the rooms represented by the AS
            #    room_alias regex
            #  - We want ALL events for rooms that AS users have joined.
            # This is currently supported via get_app_service_rooms (which is
            # used for the Notifier listener rooms). We can't reasonably make a
            # SQL query for these room IDs, so we'll pull all the events between
            # from/to and filter in python.
            rooms_for_as = self._get_app_service_rooms_txn(txn, service)
            room_ids_for_as = [r.room_id for r in rooms_for_as]

            def app_service_interested(row):
                if row["room_id"] in room_ids_for_as:
                    return True

                if row["type"] == EventTypes.Member:
                    if service.is_interested_in_user(row.get("state_key")):
                        return True
                return False

            ret = self._get_events_txn(
                txn,
                # apply the filter on the room id list
                [
                    r["event_id"] for r in rows
                    if app_service_interested(r)
                ],
                get_prev_content=True
            )

            self._set_before_and_after(ret, rows)

            if rows:
                key = "s%d" % max(r["stream_ordering"] for r in rows)
            else:
                # Assume we didn't get anything because there was nothing to
                # get.
                key = to_key

            return ret, key

        results = yield self.runInteraction("get_appservice_room_stream", f)
        defer.returnValue(results)

    @log_function
    def get_room_events_stream(
        self,
        user_id,
        from_key,
        to_key,
        limit=0,
        is_guest=False,
        room_ids=None
    ):
        room_ids = room_ids or []
        room_ids = [r for r in room_ids]
        if is_guest:
            current_room_membership_sql = (
                "SELECT c.room_id FROM history_visibility AS h"
                " INNER JOIN current_state_events AS c"
                " ON h.event_id = c.event_id"
                " WHERE c.room_id IN (%s) AND h.history_visibility = 'world_readable'" % (
                    ",".join(map(lambda _: "?", room_ids))
                )
            )
            current_room_membership_args = room_ids
        else:
            current_room_membership_sql = (
                "SELECT m.room_id FROM room_memberships as m "
                " INNER JOIN current_state_events as c"
                " ON m.event_id = c.event_id AND c.state_key = m.user_id"
                " WHERE m.user_id = ? AND m.membership = 'join'"
            )
            current_room_membership_args = [user_id]
            if room_ids:
                current_room_membership_sql += " AND m.room_id in (%s)" % (
                    ",".join(map(lambda _: "?", room_ids))
                )
                current_room_membership_args = [user_id] + room_ids

        # We also want to get any membership events about that user, e.g.
        # invites or leave notifications.
        membership_sql = (
            "SELECT m.event_id FROM room_memberships as m "
            "INNER JOIN current_state_events as c ON m.event_id = c.event_id "
            "WHERE m.user_id = ? "
        )
        membership_args = [user_id]

        if limit:
            limit = max(limit, MAX_STREAM_SIZE)
        else:
            limit = MAX_STREAM_SIZE

        # From and to keys should be integers from ordering.
        from_id = RoomStreamToken.parse_stream_token(from_key)
        to_id = RoomStreamToken.parse_stream_token(to_key)

        if from_key == to_key:
            return defer.succeed(([], to_key))

        sql = (
            "SELECT e.event_id, e.stream_ordering FROM events AS e WHERE "
            "(e.outlier = ? AND (room_id IN (%(current)s)) OR "
            "(event_id IN (%(invites)s))) "
            "AND e.stream_ordering > ? AND e.stream_ordering <= ? "
            "ORDER BY stream_ordering ASC LIMIT %(limit)d "
        ) % {
            "current": current_room_membership_sql,
            "invites": membership_sql,
            "limit": limit
        }

        def f(txn):
            args = ([False] + current_room_membership_args + membership_args +
                    [from_id.stream, to_id.stream])
            txn.execute(sql, args)

            rows = self.cursor_to_dict(txn)

            ret = self._get_events_txn(
                txn,
                [r["event_id"] for r in rows],
                get_prev_content=True
            )

            self._set_before_and_after(ret, rows)

            if rows:
                key = "s%d" % max(r["stream_ordering"] for r in rows)
            else:
                # Assume we didn't get anything because there was nothing to
                # get.
                key = to_key

            return ret, key

        return self.runInteraction("get_room_events_stream", f)

    @defer.inlineCallbacks
    def paginate_room_events(self, room_id, from_key, to_key=None,
                             direction='b', limit=-1):
        # Tokens really represent positions between elements, but we use
        # the convention of pointing to the event before the gap. Hence
        # we have a bit of asymmetry when it comes to equalities.
        args = [False, room_id]
        if direction == 'b':
            order = "DESC"
            bounds = upper_bound(RoomStreamToken.parse(from_key))
            if to_key:
                bounds = "%s AND %s" % (
                    bounds, lower_bound(RoomStreamToken.parse(to_key))
                )
        else:
            order = "ASC"
            bounds = lower_bound(RoomStreamToken.parse(from_key))
            if to_key:
                bounds = "%s AND %s" % (
                    bounds, upper_bound(RoomStreamToken.parse(to_key))
                )

        if int(limit) > 0:
            args.append(int(limit))
            limit_str = " LIMIT ?"
        else:
            limit_str = ""

        sql = (
            "SELECT * FROM events"
            " WHERE outlier = ? AND room_id = ? AND %(bounds)s"
            " ORDER BY topological_ordering %(order)s,"
            " stream_ordering %(order)s %(limit)s"
        ) % {
            "bounds": bounds,
            "order": order,
            "limit": limit_str
        }

        def f(txn):
            txn.execute(sql, args)

            rows = self.cursor_to_dict(txn)

            if rows:
                topo = rows[-1]["topological_ordering"]
                toke = rows[-1]["stream_ordering"]
                if direction == 'b':
                    # Tokens are positions between events.
                    # This token points *after* the last event in the chunk.
                    # We need it to point to the event before it in the chunk
                    # when we are going backwards so we subtract one from the
                    # stream part.
                    toke -= 1
                next_token = str(RoomStreamToken(topo, toke))
            else:
                # TODO (erikj): We should work out what to do here instead.
                next_token = to_key if to_key else from_key

            return rows, next_token,

        rows, token = yield self.runInteraction("paginate_room_events", f)

        events = yield self._get_events(
            [r["event_id"] for r in rows],
            get_prev_content=True
        )

        self._set_before_and_after(events, rows)

        defer.returnValue((events, token))

    @cachedInlineCallbacks(num_args=4)
    def get_recent_events_for_room(self, room_id, limit, end_token, from_token=None):

        end_token = RoomStreamToken.parse_stream_token(end_token)

        if from_token is None:
            sql = (
                "SELECT stream_ordering, topological_ordering, event_id"
                " FROM events"
                " WHERE room_id = ? AND stream_ordering <= ? AND outlier = ?"
                " ORDER BY topological_ordering DESC, stream_ordering DESC"
                " LIMIT ?"
            )
        else:
            from_token = RoomStreamToken.parse_stream_token(from_token)
            sql = (
                "SELECT stream_ordering, topological_ordering, event_id"
                " FROM events"
                " WHERE room_id = ? AND stream_ordering > ?"
                " AND stream_ordering <= ? AND outlier = ?"
                " ORDER BY topological_ordering DESC, stream_ordering DESC"
                " LIMIT ?"
            )

        def get_recent_events_for_room_txn(txn):
            if from_token is None:
                txn.execute(sql, (room_id, end_token.stream, False, limit,))
            else:
                txn.execute(sql, (
                    room_id, from_token.stream, end_token.stream, False, limit
                ))

            rows = self.cursor_to_dict(txn)

            rows.reverse()  # As we selected with reverse ordering

            if rows:
                # Tokens are positions between events.
                # This token points *after* the last event in the chunk.
                # We need it to point to the event before it in the chunk
                # since we are going backwards so we subtract one from the
                # stream part.
                topo = rows[0]["topological_ordering"]
                toke = rows[0]["stream_ordering"] - 1
                start_token = str(RoomStreamToken(topo, toke))

                token = (start_token, str(end_token))
            else:
                token = (str(end_token), str(end_token))

            return rows, token

        rows, token = yield self.runInteraction(
            "get_recent_events_for_room", get_recent_events_for_room_txn
        )

        logger.debug("stream before")
        events = yield self._get_events(
            [r["event_id"] for r in rows],
            get_prev_content=True
        )
        logger.debug("stream after")

        self._set_before_and_after(events, rows)

        defer.returnValue((events, token))

    @defer.inlineCallbacks
    def get_room_events_max_id(self, direction='f'):
        token = yield self._stream_id_gen.get_max_token(self)
        if direction != 'b':
            defer.returnValue("s%d" % (token,))
        else:
            topo = yield self.runInteraction(
                "_get_max_topological_txn", self._get_max_topological_txn
            )
            defer.returnValue("t%d-%d" % (topo, token))

    def get_stream_token_for_event(self, event_id):
        """The stream token for an event
        Args:
            event_id(str): The id of the event to look up a stream token for.
        Raises:
            StoreError if the event wasn't in the database.
        Returns:
            A deferred "s%d" stream token.
        """
        return self._simple_select_one_onecol(
            table="events",
            keyvalues={"event_id": event_id},
            retcol="stream_ordering",
        ).addCallback(lambda row: "s%d" % (row,))

    def get_topological_token_for_event(self, event_id):
        """The stream token for an event
        Args:
            event_id(str): The id of the event to look up a stream token for.
        Raises:
            StoreError if the event wasn't in the database.
        Returns:
            A deferred "t%d-%d" topological token.
        """
        return self._simple_select_one(
            table="events",
            keyvalues={"event_id": event_id},
            retcols=("stream_ordering", "topological_ordering"),
        ).addCallback(lambda row: "t%d-%d" % (
            row["topological_ordering"], row["stream_ordering"],)
        )

    def _get_max_topological_txn(self, txn):
        txn.execute(
            "SELECT MAX(topological_ordering) FROM events"
            " WHERE outlier = ?",
            (False,)
        )

        rows = txn.fetchall()
        return rows[0][0] if rows else 0

    @defer.inlineCallbacks
    def _get_min_token(self):
        row = yield self._execute(
            "_get_min_token", None, "SELECT MIN(stream_ordering) FROM events"
        )

        self.min_token = row[0][0] if row and row[0] and row[0][0] else -1
        self.min_token = min(self.min_token, -1)

        logger.debug("min_token is: %s", self.min_token)

        defer.returnValue(self.min_token)

    @staticmethod
    def _set_before_and_after(events, rows):
        for event, row in zip(events, rows):
            stream = row["stream_ordering"]
            topo = event.depth
            internal = event.internal_metadata
            internal.before = str(RoomStreamToken(topo, stream - 1))
            internal.after = str(RoomStreamToken(topo, stream))

    @defer.inlineCallbacks
    def get_events_around(self, room_id, event_id, before_limit, after_limit):
        """Retrieve events and pagination tokens around a given event in a
        room.

        Args:
            room_id (str)
            event_id (str)
            before_limit (int)
            after_limit (int)

        Returns:
            dict
        """

        results = yield self.runInteraction(
            "get_events_around", self._get_events_around_txn,
            room_id, event_id, before_limit, after_limit
        )

        events_before = yield self._get_events(
            [e for e in results["before"]["event_ids"]],
            get_prev_content=True
        )

        events_after = yield self._get_events(
            [e for e in results["after"]["event_ids"]],
            get_prev_content=True
        )

        defer.returnValue({
            "events_before": events_before,
            "events_after": events_after,
            "start": results["before"]["token"],
            "end": results["after"]["token"],
        })

    def _get_events_around_txn(self, txn, room_id, event_id, before_limit, after_limit):
        """Retrieves event_ids and pagination tokens around a given event in a
        room.

        Args:
            room_id (str)
            event_id (str)
            before_limit (int)
            after_limit (int)

        Returns:
            dict
        """

        results = self._simple_select_one_txn(
            txn,
            "events",
            keyvalues={
                "event_id": event_id,
                "room_id": room_id,
            },
            retcols=["stream_ordering", "topological_ordering"],
        )

        stream_ordering = results["stream_ordering"]
        topological_ordering = results["topological_ordering"]

        query_before = (
            "SELECT topological_ordering, stream_ordering, event_id FROM events"
            " WHERE room_id = ? AND (topological_ordering < ?"
            " OR (topological_ordering = ? AND stream_ordering < ?))"
            " ORDER BY topological_ordering DESC, stream_ordering DESC"
            " LIMIT ?"
        )

        query_after = (
            "SELECT topological_ordering, stream_ordering, event_id FROM events"
            " WHERE room_id = ? AND (topological_ordering > ?"
            " OR (topological_ordering = ? AND stream_ordering > ?))"
            " ORDER BY topological_ordering ASC, stream_ordering ASC"
            " LIMIT ?"
        )

        txn.execute(
            query_before,
            (
                room_id, topological_ordering, topological_ordering,
                stream_ordering, before_limit,
            )
        )

        rows = self.cursor_to_dict(txn)
        events_before = [r["event_id"] for r in rows]

        if rows:
            start_token = str(RoomStreamToken(
                rows[0]["topological_ordering"],
                rows[0]["stream_ordering"] - 1,
            ))
        else:
            start_token = str(RoomStreamToken(
                topological_ordering,
                stream_ordering - 1,
            ))

        txn.execute(
            query_after,
            (
                room_id, topological_ordering, topological_ordering,
                stream_ordering, after_limit,
            )
        )

        rows = self.cursor_to_dict(txn)
        events_after = [r["event_id"] for r in rows]

        if rows:
            end_token = str(RoomStreamToken(
                rows[-1]["topological_ordering"],
                rows[-1]["stream_ordering"],
            ))
        else:
            end_token = str(RoomStreamToken(
                topological_ordering,
                stream_ordering,
            ))

        return {
            "before": {
                "event_ids": events_before,
                "token": start_token,
            },
            "after": {
                "event_ids": events_after,
                "token": end_token,
            },
        }
