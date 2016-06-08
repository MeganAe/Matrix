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

from ._base import SQLBaseStore
from twisted.internet import defer
from synapse.util.caches.descriptors import cachedInlineCallbacks

import logging
import ujson as json

logger = logging.getLogger(__name__)


class EventPushActionsStore(SQLBaseStore):
    def __init__(self, hs):
        self.stream_ordering_month_ago = None
        super(EventPushActionsStore, self).__init__(hs)

    def _set_push_actions_for_event_and_users_txn(self, txn, event, tuples):
        """
        Args:
            event: the event set actions for
            tuples: list of tuples of (user_id, actions)
        """
        values = []
        for uid, actions in tuples:
            values.append({
                'room_id': event.room_id,
                'event_id': event.event_id,
                'user_id': uid,
                'actions': json.dumps(actions),
                'stream_ordering': event.internal_metadata.stream_ordering,
                'topological_ordering': event.depth,
                'notif': 1,
                'highlight': 1 if _action_has_highlight(actions) else 0,
            })

        for uid, __ in tuples:
            txn.call_after(
                self.get_unread_event_push_actions_by_room_for_user.invalidate_many,
                (event.room_id, uid)
            )
        self._simple_insert_many_txn(txn, "event_push_actions", values)

    @cachedInlineCallbacks(num_args=3, lru=True, tree=True, max_entries=5000)
    def get_unread_event_push_actions_by_room_for_user(
            self, room_id, user_id, last_read_event_id
    ):
        def _get_unread_event_push_actions_by_room(txn):
            sql = (
                "SELECT stream_ordering, topological_ordering"
                " FROM events"
                " WHERE room_id = ? AND event_id = ?"
            )
            txn.execute(
                sql, (room_id, last_read_event_id)
            )
            results = txn.fetchall()
            if len(results) == 0:
                return {"notify_count": 0, "highlight_count": 0}

            stream_ordering = results[0][0]
            topological_ordering = results[0][1]

            sql = (
                "SELECT sum(notif), sum(highlight)"
                " FROM event_push_actions ea"
                " WHERE"
                " user_id = ?"
                " AND room_id = ?"
                " AND ("
                "       topological_ordering > ?"
                "       OR (topological_ordering = ? AND stream_ordering > ?)"
                ")"
            )
            txn.execute(sql, (
                user_id, room_id,
                topological_ordering, topological_ordering, stream_ordering
            ))
            row = txn.fetchone()
            if row:
                return {
                    "notify_count": row[0] or 0,
                    "highlight_count": row[1] or 0,
                }
            else:
                return {"notify_count": 0, "highlight_count": 0}

        ret = yield self.runInteraction(
            "get_unread_event_push_actions_by_room",
            _get_unread_event_push_actions_by_room
        )
        defer.returnValue(ret)

    @defer.inlineCallbacks
    def get_push_action_users_in_range(self, min_stream_ordering, max_stream_ordering):
        def f(txn):
            sql = (
                "SELECT DISTINCT(user_id) FROM event_push_actions WHERE"
                " stream_ordering >= ? AND stream_ordering <= ?"
            )
            txn.execute(sql, (min_stream_ordering, max_stream_ordering))
            return [r[0] for r in txn.fetchall()]
        ret = yield self.runInteraction("get_push_action_users_in_range", f)
        defer.returnValue(ret)

    @defer.inlineCallbacks
    def get_unread_push_actions_for_user_in_range(self, user_id,
                                                  min_stream_ordering,
                                                  max_stream_ordering=None,
                                                  limit=20):
        def get_after_receipt(txn):
            sql = (
                "SELECT ep.event_id, ep.room_id, ep.stream_ordering, ep.actions, "
                "e.received_ts "
                "FROM ("
                "   SELECT room_id, user_id, "
                "       max(topological_ordering) as topological_ordering, "
                "       max(stream_ordering) as stream_ordering "
                "       FROM events"
                "   NATURAL JOIN receipts_linearized WHERE receipt_type = 'm.read'"
                "   GROUP BY room_id, user_id"
                ") AS rl,"
                " event_push_actions AS ep"
                " INNER JOIN events AS e USING (room_id, event_id)"
                " WHERE"
                "   ep.room_id = rl.room_id"
                "   AND ("
                "       ep.topological_ordering > rl.topological_ordering"
                "       OR ("
                "           ep.topological_ordering = rl.topological_ordering"
                "           AND ep.stream_ordering > rl.stream_ordering"
                "       )"
                "   )"
                "   AND ep.stream_ordering > ?"
                "   AND ep.user_id = ?"
                "   AND ep.user_id = rl.user_id"
            )
            args = [min_stream_ordering, user_id]
            if max_stream_ordering is not None:
                sql += " AND ep.stream_ordering <= ?"
                args.append(max_stream_ordering)
            sql += " ORDER BY ep.stream_ordering ASC LIMIT ?"
            args.append(limit)
            txn.execute(sql, args)
            return txn.fetchall()
        after_read_receipt = yield self.runInteraction(
            "get_unread_push_actions_for_user_in_range", get_after_receipt
        )

        def get_no_receipt(txn):
            sql = (
                "SELECT ep.event_id, ep.room_id, ep.stream_ordering, ep.actions,"
                " e.received_ts"
                " FROM event_push_actions AS ep"
                " JOIN events e ON ep.room_id = e.room_id AND ep.event_id = e.event_id"
                " WHERE ep.room_id not in ("
                "   SELECT room_id FROM events NATURAL JOIN receipts_linearized"
                "   WHERE receipt_type = 'm.read' AND user_id = ?"
                "   GROUP BY room_id"
                ") AND ep.user_id = ? AND ep.stream_ordering > ?"
            )
            args = [user_id, user_id, min_stream_ordering]
            if max_stream_ordering is not None:
                sql += " AND ep.stream_ordering <= ?"
                args.append(max_stream_ordering)
            sql += " ORDER BY ep.stream_ordering ASC"
            txn.execute(sql, args)
            return txn.fetchall()
        no_read_receipt = yield self.runInteraction(
            "get_unread_push_actions_for_user_in_range", get_no_receipt
        )

        defer.returnValue([
            {
                "event_id": row[0],
                "room_id": row[1],
                "stream_ordering": row[2],
                "actions": json.loads(row[3]),
                "received_ts": row[4],
            } for row in after_read_receipt + no_read_receipt
        ])

    @defer.inlineCallbacks
    def get_time_of_last_push_action_before(self, stream_ordering):
        def f(txn):
            sql = (
                "SELECT e.received_ts"
                " FROM event_push_actions AS ep"
                " JOIN events e ON ep.room_id = e.room_id AND ep.event_id = e.event_id"
                " WHERE ep.stream_ordering > ?"
                " ORDER BY ep.stream_ordering ASC"
                " LIMIT 1"
            )
            txn.execute(sql, (stream_ordering,))
            return txn.fetchone()
        result = yield self.runInteraction("get_time_of_last_push_action_before", f)
        defer.returnValue(result[0] if result else None)

    @defer.inlineCallbacks
    def get_latest_push_action_stream_ordering(self):
        def f(txn):
            txn.execute("SELECT MAX(stream_ordering) FROM event_push_actions")
            return txn.fetchone()
        result = yield self.runInteraction(
            "get_latest_push_action_stream_ordering", f
        )
        defer.returnValue(result[0] or 0)

    def _remove_push_actions_for_event_id_txn(self, txn, room_id, event_id):
        # Sad that we have to blow away the cache for the whole room here
        txn.call_after(
            self.get_unread_event_push_actions_by_room_for_user.invalidate_many,
            (room_id,)
        )
        txn.execute(
            "DELETE FROM event_push_actions WHERE room_id = ? AND event_id = ?",
            (room_id, event_id)
        )

    def _remove_old_push_actions_before_txn(self, txn, room_id, user_id,
                                            topological_ordering):
        """
        Purges old, stale push actions for a user and room before a given
        topological_ordering
        Args:
            txn: The transcation
            room_id: Room ID to delete from
            user_id: user ID to delete for
            topological_ordering: The lowest topological ordering which will
                                  not be deleted.
        """
        txn.call_after(
            self.get_unread_event_push_actions_by_room_for_user.invalidate_many,
            (room_id, user_id, )
        )

        # We need to join on the events table to get the received_ts for
        # event_push_actions and sqlite won't let us use a join in a delete so
        # we can't just delete where received_ts < x. Furthermore we can
        # only identify event_push_actions by a tuple of room_id, event_id
        # we we can't use a subquery.
        # Instead, we look up the stream ordering for the last event in that
        # room received before the threshold time and delete event_push_actions
        # in the room with a stream_odering before that.
        txn.execute(
            "DELETE FROM event_push_actions "
            " WHERE user_id = ? AND room_id = ? AND "
            " topological_ordering < ? AND stream_ordering < ?",
            (user_id, room_id, topological_ordering, self.stream_ordering_month_ago)
        )

    @defer.inlineCallbacks
    def _find_stream_orderings_for_times(self):
        yield self.runInteraction(
            "_find_stream_orderings_for_times",
            self._find_stream_orderings_for_times_txn
        )

    def _find_stream_orderings_for_times_txn(self, txn):
        logger.info("Searching for stream ordering 1 month ago")
        self.stream_ordering_month_ago = self._find_first_stream_ordering_after_ts_txn(
            txn, self._clock.time_msec() - 30 * 24 * 60 * 60 * 1000
        )
        logger.info(
            "Found stream ordering 1 month ago: it's %d",
            self.stream_ordering_month_ago
        )

    def _find_first_stream_ordering_after_ts_txn(self, txn, ts):
        """
        Find the stream_ordering of the first event that was received after
        a given timestamp. This is relatively slow as there is no index on
        received_ts but we can then use this to delete push actions before
        this.

        received_ts must necessarily be in the same order as stream_ordering
        and stream_ordering is indexed, so we manually binary search using
        stream_ordering
        """
        txn.execute("SELECT MAX(stream_ordering) FROM events")
        max_stream_ordering = txn.fetchone()[0]

        if max_stream_ordering is None:
            return 0

        range_start = 0
        range_end = max_stream_ordering

        sql = (
            "SELECT received_ts FROM events"
            " WHERE stream_ordering > ?"
            " ORDER BY stream_ordering"
            " LIMIT 1"
        )

        while range_end - range_start > 1:
            middle = int((range_end + range_start) / 2)
            txn.execute(sql, (middle,))
            middle_ts = txn.fetchone()[0]
            if ts > middle_ts:
                range_start = middle
            else:
                range_end = middle

        return range_end


def _action_has_highlight(actions):
    for action in actions:
        try:
            if action.get("set_tweak", None) == "highlight":
                return action.get("value", True)
        except AttributeError:
            pass

    return False
