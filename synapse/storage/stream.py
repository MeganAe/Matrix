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

from synapse.storage._base import SQLBaseStore
from synapse.storage.events import EventsWorkerStore

from synapse.types import RoomStreamToken
from synapse.util.caches.stream_change_cache import StreamChangeCache
from synapse.util.logcontext import make_deferred_yieldable, run_in_background
from synapse.storage.chunk_ordered_table import ChunkDBOrderedListStore
from synapse.storage.engines import PostgresEngine

import abc
import logging

from six.moves import range
from collections import namedtuple


logger = logging.getLogger(__name__)


MAX_STREAM_SIZE = 1000


_STREAM_TOKEN = "stream"
_TOPOLOGICAL_TOKEN = "topological"


# Used as return values for pagination APIs
_EventDictReturn = namedtuple("_EventDictReturn", (
    "event_id", "chunk_id", "topological_ordering", "stream_ordering",
))


def lower_bound(token, engine, inclusive=False):
    inclusive = "=" if inclusive else ""
    if token.chunk is None:
        return "(%d <%s %s)" % (token.stream, inclusive, "stream_ordering")
    else:
        if isinstance(engine, PostgresEngine):
            # Postgres doesn't optimise ``(x < a) OR (x=a AND y<b)`` as well
            # as it optimises ``(x,y) < (a,b)`` on multicolumn indexes. So we
            # use the later form when running against postgres.
            return "(chunk_id = %d AND (%d,%d) <%s (%s,%s))" % (
                token.chunk, token.topological, token.stream, inclusive,
                "topological_ordering", "stream_ordering",
            )
        return "(chunk_id = %d AND (%d < %s OR (%d = %s AND %d <%s %s)))" % (
            token.chunk,
            token.topological, "topological_ordering",
            token.topological, "topological_ordering",
            token.stream, inclusive, "stream_ordering",
        )


def upper_bound(token, engine, inclusive=True):
    inclusive = "=" if inclusive else ""
    if token.chunk is None:
        return "(%d >%s %s)" % (token.stream, inclusive, "stream_ordering")
    else:
        if isinstance(engine, PostgresEngine):
            # Postgres doesn't optimise ``(x > a) OR (x=a AND y>b)`` as well
            # as it optimises ``(x,y) > (a,b)`` on multicolumn indexes. So we
            # use the later form when running against postgres.
            return "(chunk_id = %d AND (%d,%d) >%s (%s,%s))" % (
                token.chunk, token.topological, token.stream, inclusive,
                "topological_ordering", "stream_ordering",
            )
        return "(chunk_id = %d AND (%d > %s OR (%d = %s AND %d >%s %s)))" % (
            token.chunk,
            token.topological, "topological_ordering",
            token.topological, "topological_ordering",
            token.stream, inclusive, "stream_ordering",
        )


def filter_to_clause(event_filter):
    # NB: This may create SQL clauses that don't optimise well (and we don't
    # have indices on all possible clauses). E.g. it may create
    # "room_id == X AND room_id != X", which postgres doesn't optimise.

    if not event_filter:
        return "", []

    clauses = []
    args = []

    if event_filter.types:
        clauses.append(
            "(%s)" % " OR ".join("type = ?" for _ in event_filter.types)
        )
        args.extend(event_filter.types)

    for typ in event_filter.not_types:
        clauses.append("type != ?")
        args.append(typ)

    if event_filter.senders:
        clauses.append(
            "(%s)" % " OR ".join("sender = ?" for _ in event_filter.senders)
        )
        args.extend(event_filter.senders)

    for sender in event_filter.not_senders:
        clauses.append("sender != ?")
        args.append(sender)

    if event_filter.rooms:
        clauses.append(
            "(%s)" % " OR ".join("room_id = ?" for _ in event_filter.rooms)
        )
        args.extend(event_filter.rooms)

    for room_id in event_filter.not_rooms:
        clauses.append("room_id != ?")
        args.append(room_id)

    if event_filter.contains_url:
        clauses.append("contains_url = ?")
        args.append(event_filter.contains_url)

    return " AND ".join(clauses), args


class StreamWorkerStore(EventsWorkerStore, SQLBaseStore):
    """This is an abstract base class where subclasses must implement
    `get_room_max_stream_ordering` and `get_room_min_stream_ordering`
    which can be called in the initializer.
    """

    __metaclass__ = abc.ABCMeta

    def __init__(self, db_conn, hs):
        super(StreamWorkerStore, self).__init__(db_conn, hs)

        events_max = self.get_room_max_stream_ordering()
        event_cache_prefill, min_event_val = self._get_cache_dict(
            db_conn, "events",
            entity_column="room_id",
            stream_column="stream_ordering",
            max_value=events_max,
        )
        self._events_stream_cache = StreamChangeCache(
            "EventsRoomStreamChangeCache", min_event_val,
            prefilled_cache=event_cache_prefill,
        )
        self._membership_stream_cache = StreamChangeCache(
            "MembershipStreamChangeCache", events_max,
        )

        self._stream_order_on_start = self.get_room_max_stream_ordering()

    @abc.abstractmethod
    def get_room_max_stream_ordering(self):
        raise NotImplementedError()

    @abc.abstractmethod
    def get_room_min_stream_ordering(self):
        raise NotImplementedError()

    @defer.inlineCallbacks
    def get_room_events_stream_for_rooms(self, room_ids, from_key, to_key, limit=0,
                                         order='DESC'):
        from_id = RoomStreamToken.parse_stream_token(from_key).stream

        room_ids = yield self._events_stream_cache.get_entities_changed(
            room_ids, from_id
        )

        if not room_ids:
            defer.returnValue({})

        results = {}
        room_ids = list(room_ids)
        for rm_ids in (room_ids[i:i + 20] for i in range(0, len(room_ids), 20)):
            res = yield make_deferred_yieldable(defer.gatherResults([
                run_in_background(
                    self.get_room_events_stream_for_room,
                    room_id, from_key, to_key, limit, order=order,
                )
                for room_id in rm_ids
            ], consumeErrors=True))
            results.update(dict(zip(rm_ids, res)))

        defer.returnValue(results)

    def get_rooms_that_changed(self, room_ids, from_key):
        """Given a list of rooms and a token, return rooms where there may have
        been changes.

        Args:
            room_ids (list)
            from_key (str): The room_key portion of a StreamToken
        """
        from_key = RoomStreamToken.parse_stream_token(from_key).stream
        return set(
            room_id for room_id in room_ids
            if self._events_stream_cache.has_entity_changed(room_id, from_key)
        )

    @defer.inlineCallbacks
    def get_room_events_stream_for_room(self, room_id, from_key, to_key, limit=0,
                                        order='DESC'):

        """Get new room events in stream ordering since `from_key`.

        Args:
            room_id (str)
            from_key (str): Token from which no events are returned before
            to_key (str): Token from which no events are returned after. (This
                is typically the current stream token)
            limit (int): Maximum number of events to return
            order (str): Either "DESC" or "ASC". Determines which events are
                returned when the result is limited. If "DESC" then the most
                recent `limit` events are returned, otherwise returns the
                oldest `limit` events.

        Returns:
            Deferred[tuple[list[FrozenEvent], str]]: Returns the list of
            events (in ascending order) and the token from the start of
            the chunk of events returned.
        """
        if from_key == to_key:
            defer.returnValue(([], from_key))

        from_id = RoomStreamToken.parse_stream_token(from_key).stream
        to_id = RoomStreamToken.parse_stream_token(to_key).stream

        has_changed = yield self._events_stream_cache.has_entity_changed(
            room_id, from_id
        )

        if not has_changed:
            defer.returnValue(([], from_key))

        def f(txn):
            sql = (
                "SELECT event_id, stream_ordering FROM events WHERE"
                " room_id = ?"
                " AND not outlier"
                " AND stream_ordering > ? AND stream_ordering <= ?"
                " ORDER BY stream_ordering %s LIMIT ?"
            ) % (order,)
            txn.execute(sql, (room_id, from_id, to_id, limit))

            rows = [_EventDictReturn(row[0], None, None, row[1]) for row in txn]
            return rows

        rows = yield self.runInteraction("get_room_events_stream_for_room", f)

        ret = yield self._get_events(
            [r.event_id for r in rows],
            get_prev_content=True
        )

        self._set_before_and_after(ret, rows, topo_order=from_id is None)

        if order.lower() == "desc":
            ret.reverse()

        if rows:
            key = "s%d" % min(r.stream_ordering for r in rows)
        else:
            # Assume we didn't get anything because there was nothing to
            # get.
            key = from_key

        defer.returnValue((ret, key))

    @defer.inlineCallbacks
    def get_membership_changes_for_user(self, user_id, from_key, to_key):
        from_id = RoomStreamToken.parse_stream_token(from_key).stream
        to_id = RoomStreamToken.parse_stream_token(to_key).stream

        if from_key == to_key:
            defer.returnValue([])

        if from_id:
            has_changed = self._membership_stream_cache.has_entity_changed(
                user_id, int(from_id)
            )
            if not has_changed:
                defer.returnValue([])

        def f(txn):
            sql = (
                "SELECT m.event_id, stream_ordering FROM events AS e,"
                " room_memberships AS m"
                " WHERE e.event_id = m.event_id"
                " AND m.user_id = ?"
                " AND e.stream_ordering > ? AND e.stream_ordering <= ?"
                " ORDER BY e.stream_ordering ASC"
            )
            txn.execute(sql, (user_id, from_id, to_id,))

            rows = [_EventDictReturn(row[0], None, None, row[1]) for row in txn]

            return rows

        rows = yield self.runInteraction("get_membership_changes_for_user", f)

        ret = yield self._get_events(
            [r.event_id for r in rows],
            get_prev_content=True
        )

        self._set_before_and_after(ret, rows, topo_order=False)

        defer.returnValue(ret)

    @defer.inlineCallbacks
    def get_recent_events_for_room(self, room_id, limit, end_token):
        """Get the most recent events in the room in topological ordering.

        Args:
            room_id (str)
            limit (int)
            end_token (str): The stream token representing now.

        Returns:
            Deferred[tuple[list[FrozenEvent],  str]]: Returns a list of
            events and a token pointing to the start of the returned
            events.
            The events returned are in ascending order.
        """

        rows, token = yield self.get_recent_event_ids_for_room(
            room_id, limit, end_token,
        )

        logger.debug("stream before")
        events = yield self._get_events(
            [r.event_id for r in rows],
            get_prev_content=True
        )
        logger.debug("stream after")

        self._set_before_and_after(events, rows)

        defer.returnValue((events, token))

    @defer.inlineCallbacks
    def get_recent_event_ids_for_room(self, room_id, limit, end_token):
        """Get the most recent events in the room in topological ordering.

        Args:
            room_id (str)
            limit (int)
            end_token (str): The stream token representing now.

        Returns:
            Deferred[tuple[list[_EventDictReturn],  str]]: Returns a list of
            _EventDictReturn and a token pointing to the start of the returned
            events.
            The events returned are in ascending order.
        """
        # Allow a zero limit here, and no-op.
        if limit == 0:
            defer.returnValue(([], end_token))

        end_token = RoomStreamToken.parse(end_token)

        rows, token, _ = yield self.runInteraction(
            "get_recent_event_ids_for_room", self._paginate_room_events_txn,
            room_id, from_token=end_token, limit=limit,
        )

        # We want to return the results in ascending order.
        rows.reverse()

        defer.returnValue((rows, token))

    def get_room_event_after_stream_ordering(self, room_id, stream_ordering):
        """Gets details of the first event in a room at or after a stream ordering

        Args:
            room_id (str):
            stream_ordering (int):

        Returns:
            Deferred[(int, int, str)]:
                (stream ordering, topological ordering, event_id)
        """
        def _f(txn):
            sql = (
                "SELECT stream_ordering, topological_ordering, event_id"
                " FROM events"
                " WHERE room_id = ? AND stream_ordering >= ?"
                " AND NOT outlier"
                " ORDER BY stream_ordering"
                " LIMIT 1"
            )
            txn.execute(sql, (room_id, stream_ordering, ))
            return txn.fetchone()

        return self.runInteraction(
            "get_room_event_after_stream_ordering", _f,
        )

    @defer.inlineCallbacks
    def get_room_events_max_id(self, room_id=None):
        """Returns the current token for rooms stream.

        By default, it returns the current global stream token. Specifying a
        `room_id` causes it to return the current room specific topological
        token.
        """
        if room_id is None:
            token = yield self.get_room_max_stream_ordering()
            defer.returnValue(str(RoomStreamToken(None, None, token)))
        else:
            token = yield self.runInteraction(
                "get_room_events_max_id", self._get_topological_token_for_room_txn,
                room_id,
            )
            if not token:
                raise Exception("Server not in room")
            defer.returnValue(str(token))

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
        ).addCallback(lambda row: str(RoomStreamToken(None, None, row)))

    def get_topological_token_for_event(self, event_id):
        """The stream token for an event
        Args:
            event_id(str): The id of the event to look up a stream token for.
        Raises:
            StoreError if the event wasn't in the database.
        Returns:
            A deferred topological token.
        """
        return self._simple_select_one(
            table="events",
            keyvalues={"event_id": event_id},
            retcols=("stream_ordering", "topological_ordering", "chunk_id"),
            desc="get_topological_token_for_event",
        ).addCallback(lambda row: str(RoomStreamToken(
            row["chunk_id"],
            row["topological_ordering"],
            row["stream_ordering"],
        )))

    def _get_topological_token_for_room_txn(self, txn, room_id):
        sql = """
            SELECT chunk_id, topological_ordering, stream_ordering
            FROM events
            NATURAL JOIN event_forward_extremities
            WHERE room_id = ?
            ORDER BY stream_ordering DESC
            LIMIT 1
        """
        txn.execute(sql, (room_id,))
        row = txn.fetchone()
        if row:
            c, t, s = row
            return RoomStreamToken(c, t, s)
        return None

    def get_max_topological_token(self, room_id, stream_key):
        sql = (
            "SELECT max(topological_ordering) FROM events"
            " WHERE room_id = ? AND stream_ordering < ?"
        )
        return self._execute(
            "get_max_topological_token", None,
            sql, room_id, stream_key,
        ).addCallback(
            lambda r: r[0][0] if r else 0
        )

    def _get_max_topological_txn(self, txn, room_id):
        txn.execute(
            "SELECT MAX(topological_ordering) FROM events"
            " WHERE room_id = ?",
            (room_id,)
        )

        rows = txn.fetchall()
        return rows[0][0] if rows else 0

    @staticmethod
    def _set_before_and_after(events, rows, topo_order=True):
        """Inserts ordering information to events' internal metadata from
        the DB rows.

        Args:
            events (list[FrozenEvent])
            rows (list[_EventDictReturn])
            topo_order (bool): Whether the events were ordered topologically
                or by stream ordering. If true then all rows should have a non
                null topological_ordering.
        """
        for event, row in zip(events, rows):
            chunk = row.chunk_id
            topo = row.topological_ordering
            stream = row.stream_ordering

            internal = event.internal_metadata

            internal.stream_ordering = stream

            if topo_order:
                internal.before = str(RoomStreamToken(chunk, topo, stream - 1))
                internal.after = str(RoomStreamToken(chunk, topo, stream))
            else:
                internal.before = str(RoomStreamToken(None, None, stream - 1))
                internal.after = str(RoomStreamToken(None, None, stream))

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
            retcols=["stream_ordering", "topological_ordering", "chunk_id"],
        )

        # Paginating backwards includes the event at the token, but paginating
        # forward doesn't.
        before_token = RoomStreamToken(
            results["chunk_id"],
            results["topological_ordering"],
            results["stream_ordering"] - 1,
        )

        after_token = RoomStreamToken(
            results["chunk_id"],
            results["topological_ordering"],
            results["stream_ordering"],
        )

        rows, start_token, _ = self._paginate_room_events_txn(
            txn, room_id, before_token, direction='b', limit=before_limit,
        )
        events_before = [r.event_id for r in rows]

        rows, end_token, _ = self._paginate_room_events_txn(
            txn, room_id, after_token, direction='f', limit=after_limit,
        )
        events_after = [r.event_id for r in rows]

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

    @defer.inlineCallbacks
    def get_all_new_events_stream(self, from_id, current_id, limit):
        """Get all new events"""

        def get_all_new_events_stream_txn(txn):
            sql = (
                "SELECT e.stream_ordering, e.event_id"
                " FROM events AS e"
                " WHERE"
                " ? < e.stream_ordering AND e.stream_ordering <= ?"
                " ORDER BY e.stream_ordering ASC"
                " LIMIT ?"
            )

            txn.execute(sql, (from_id, current_id, limit))
            rows = txn.fetchall()

            upper_bound = current_id
            if len(rows) == limit:
                upper_bound = rows[-1][0]

            return upper_bound, [row[1] for row in rows]

        upper_bound, event_ids = yield self.runInteraction(
            "get_all_new_events_stream", get_all_new_events_stream_txn,
        )

        events = yield self._get_events(event_ids)

        defer.returnValue((upper_bound, events))

    def get_federation_out_pos(self, typ):
        return self._simple_select_one_onecol(
            table="federation_stream_position",
            retcol="stream_id",
            keyvalues={"type": typ},
            desc="get_federation_out_pos"
        )

    def update_federation_out_pos(self, typ, stream_id):
        return self._simple_update_one(
            table="federation_stream_position",
            keyvalues={"type": typ},
            updatevalues={"stream_id": stream_id},
            desc="update_federation_out_pos",
        )

    def has_room_changed_since(self, room_id, stream_id):
        return self._events_stream_cache.has_entity_changed(room_id, stream_id)

    def _paginate_room_events_txn(self, txn, room_id, from_token, to_token=None,
                                  direction='b', limit=-1, event_filter=None):
        """Returns list of events before or after a given token.

        Args:
            txn
            room_id (str)
            from_token (RoomStreamToken): The token used to stream from
            to_token (RoomStreamToken|None): A token which if given limits the
                results to only those before
            direction(char): Either 'b' or 'f' to indicate whether we are
                paginating forwards or backwards from `from_key`.
            limit (int): The maximum number of events to return.
            event_filter (Filter|None): If provided filters the events to
                those that match the filter.

        Returns:
            Deferred[tuple[list[_EventDictReturn], str, list[int]]: Returns
            the results as a list of _EventDictReturn, a token that points to
            the end of the result set, and a list of chunks iterated over.
        """

        limit = int(limit)  # Sometimes we are passed a string from somewhere
        assert limit >= 0

        # There are two modes of fetching events: by stream order or by
        # topological order. This is determined by whether the from_token is a
        # stream or topological token. If stream then we can simply do a select
        # ordered by stream_ordering column. If topological, then we need to
        # fetch events from one chunk at a time until we hit the limit.

        # For backwards compatibility we need to check if the token has a
        # topological part but no chunk part. If that's the case we can use the
        # stream part to generate an appropriate topological token.
        if from_token.chunk is None and from_token.topological is not None:
            res = self._simple_select_one_txn(
                txn,
                table="events",
                keyvalues={
                    "stream_ordering": from_token.stream,
                },
                retcols=(
                    "chunk_id",
                    "topological_ordering",
                    "stream_ordering",
                ),
                allow_none=True,
            )
            if res and res["chunk_id"] is not None:
                from_token = RoomStreamToken(
                    res["chunk_id"],
                    res["topological_ordering"],
                    res["stream_ordering"],
                )

        # Tokens really represent positions between elements, but we use
        # the convention of pointing to the event before the gap. Hence
        # we have a bit of asymmetry when it comes to equalities.
        args = [False, room_id]
        if direction == 'b':
            order = "DESC"
            bounds = upper_bound(
                from_token, self.database_engine
            )
            if to_token:
                bounds = "%s AND %s" % (bounds, lower_bound(
                    to_token, self.database_engine
                ))
        else:
            order = "ASC"
            bounds = lower_bound(
                from_token, self.database_engine
            )
            if to_token:
                bounds = "%s AND %s" % (bounds, upper_bound(
                    to_token, self.database_engine
                ))

        filter_clause, filter_args = filter_to_clause(event_filter)

        if filter_clause:
            bounds += " AND " + filter_clause
            args.extend(filter_args)

        args.append(limit)

        sql = (
            "SELECT event_id, chunk_id, topological_ordering, stream_ordering"
            " FROM events"
            " WHERE outlier = ? AND room_id = ? AND %(bounds)s"
            " ORDER BY topological_ordering %(order)s,"
            " stream_ordering %(order)s LIMIT ?"
        ) % {
            "bounds": bounds,
            "order": order,
        }

        txn.execute(sql, args)

        rows = [_EventDictReturn(*row) for row in txn]

        # If we are paginating topologically and we haven't hit the limit on
        # number of events then we need to fetch events from the previous or
        # next chunk.

        iterated_chunks = []

        chunk_id = None
        if rows:
            chunk_id = rows[-1].chunk_id
            iterated_chunks = [r.chunk_id for r in rows]
        elif from_token.chunk:
            chunk_id = from_token.chunk
            iterated_chunks = [chunk_id]

        table = ChunkDBOrderedListStore(
            txn, room_id, self.clock,
        )

        if filter_clause:
            filter_clause = "AND " + filter_clause

        sql = (
            "SELECT event_id, chunk_id, topological_ordering, stream_ordering"
            " FROM events"
            " WHERE outlier = ? AND room_id = ? %(filter_clause)s"
            " ORDER BY topological_ordering %(order)s,"
            " stream_ordering %(order)s LIMIT ?"
        ) % {
            "filter_clause": filter_clause,
            "order": order,
        }

        args = [False, room_id] + filter_args + [limit]

        while chunk_id and (limit <= 0 or len(rows) < limit):
            if chunk_id not in iterated_chunks:
                iterated_chunks.append(chunk_id)

            if direction == 'b':
                chunk_id = table.get_prev(chunk_id)
            else:
                chunk_id = table.get_next(chunk_id)

            if chunk_id is None:
                break

            txn.execute(sql, args)
            new_rows = [_EventDictReturn(*row) for row in txn]

            rows.extend(new_rows)

        # We may have inserted more rows than necessary in the loop above
        rows = rows[:limit]

        if rows:
            chunk = rows[-1].chunk_id
            topo = rows[-1].topological_ordering
            toke = rows[-1].stream_ordering
            if direction == 'b':
                # Tokens are positions between events.
                # This token points *after* the last event in the chunk.
                # We need it to point to the event before it in the chunk
                # when we are going backwards so we subtract one from the
                # stream part.
                toke -= 1
            next_token = RoomStreamToken(chunk, topo, toke)
        else:
            # TODO (erikj): We should work out what to do here instead.
            next_token = to_token if to_token else from_token

        return rows, str(next_token), iterated_chunks,

    @defer.inlineCallbacks
    def paginate_room_events(self, room_id, from_key, to_key=None,
                             direction='b', limit=-1, event_filter=None):
        """Returns list of events before or after a given token.

        Args:
            room_id (str)
            from_key (str): The token used to stream from
            to_key (str|None): A token which if given limits the results to
                only those before
            direction(char): Either 'b' or 'f' to indicate whether we are
                paginating forwards or backwards from `from_key`.
            limit (int): The maximum number of events to return. Zero or less
                means no limit.
            event_filter (Filter|None): If provided filters the events to
                those that match the filter.

        Returns:
            tuple[list[dict], str, list[str]]: Returns the results as a list of
            dicts, a token that points to the end of the result set, and a list
            of backwards extremities. The dicts have the keys "event_id",
            "topological_ordering" and "stream_ordering".
        """

        from_key = RoomStreamToken.parse(from_key)
        if to_key:
            to_key = RoomStreamToken.parse(to_key)

        def _do_paginate_room_events(txn):
            rows, token, chunks = self._paginate_room_events_txn(
                txn, room_id, from_key, to_key, direction, limit, event_filter,
            )

            # We now fetch the extremities by fetching the extremities for
            # each chunk we iterated over.
            extremities = []
            seen = set()
            for chunk_id in chunks:
                if chunk_id in seen:
                    continue
                seen.add(chunk_id)

                event_ids = self._simple_select_onecol_txn(
                    txn,
                    table="chunk_backwards_extremities",
                    keyvalues={"chunk_id": chunk_id},
                    retcol="event_id"
                )

                extremities.extend(e for e in event_ids if e not in extremities)

            return rows, token, extremities

        rows, token, extremities = yield self.runInteraction(
            "paginate_room_events", _do_paginate_room_events,
        )

        events = yield self._get_events(
            [r.event_id for r in rows],
            get_prev_content=True
        )

        self._set_before_and_after(events, rows)

        defer.returnValue((events, token, extremities))


class StreamStore(StreamWorkerStore):
    def get_room_max_stream_ordering(self):
        return self._stream_id_gen.get_current_token()

    def get_room_min_stream_ordering(self):
        return self._backfill_id_gen.get_current_token()
