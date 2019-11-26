# -*- coding: utf-8 -*-
# Copyright 2019 New Vector Ltd
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

import attr

from synapse.api.constants import RelationTypes
from synapse.storage._base import SQLBaseStore
from synapse.storage.data_stores.main.stream import generate_pagination_where_clause
from synapse.storage.relations import (
    AggregationPaginationToken,
    PaginationChunk,
    RelationPaginationToken,
)
from synapse.util.caches.descriptors import cached, cachedInlineCallbacks

logger = logging.getLogger(__name__)


class RelationsWorkerStore(SQLBaseStore):
    @cached(tree=True)
    def get_relations_for_event(
        self,
        event_id,
        relation_type=None,
        event_type=None,
        aggregation_key=None,
        limit=5,
        direction="b",
        from_token=None,
        to_token=None,
    ):
        """Get a list of relations for an event, ordered by topological ordering.

        Args:
            event_id (str): Fetch events that relate to this event ID.
            relation_type (str|None): Only fetch events with this relation
                type, if given.
            event_type (str|None): Only fetch events with this event type, if
                given.
            aggregation_key (str|None): Only fetch events with this aggregation
                key, if given.
            limit (int): Only fetch the most recent `limit` events.
            direction (str): Whether to fetch the most recent first (`"b"`) or
                the oldest first (`"f"`).
            from_token (RelationPaginationToken|None): Fetch rows from the given
                token, or from the start if None.
            to_token (RelationPaginationToken|None): Fetch rows up to the given
                token, or up to the end if None.

        Returns:
            Deferred[PaginationChunk]: List of event IDs that match relations
            requested. The rows are of the form `{"event_id": "..."}`.
        """

        where_clause = ["relates_to_id = ?"]
        where_args = [event_id]

        if relation_type is not None:
            where_clause.append("relation_type = ?")
            where_args.append(relation_type)

        if event_type is not None:
            where_clause.append("type = ?")
            where_args.append(event_type)

        if aggregation_key:
            where_clause.append("aggregation_key = ?")
            where_args.append(aggregation_key)

        pagination_clause = generate_pagination_where_clause(
            direction=direction,
            column_names=("topological_ordering", "stream_ordering"),
            from_token=attr.astuple(from_token) if from_token else None,
            to_token=attr.astuple(to_token) if to_token else None,
            engine=self.database_engine,
        )

        if pagination_clause:
            where_clause.append(pagination_clause)

        if direction == "b":
            order = "DESC"
        else:
            order = "ASC"

        sql = """
            SELECT event_id, topological_ordering, stream_ordering
            FROM event_relations
            INNER JOIN events USING (event_id)
            WHERE %s
            ORDER BY topological_ordering %s, stream_ordering %s
            LIMIT ?
        """ % (
            " AND ".join(where_clause),
            order,
            order,
        )

        def _get_recent_references_for_event_txn(txn):
            txn.execute(sql, where_args + [limit + 1])

            last_topo_id = None
            last_stream_id = None
            events = []
            for row in txn:
                events.append({"event_id": row[0]})
                last_topo_id = row[1]
                last_stream_id = row[2]

            next_batch = None
            if len(events) > limit and last_topo_id and last_stream_id:
                next_batch = RelationPaginationToken(last_topo_id, last_stream_id)

            return PaginationChunk(
                chunk=list(events[:limit]), next_batch=next_batch, prev_batch=from_token
            )

        return self.runInteraction(
            "get_recent_references_for_event", _get_recent_references_for_event_txn
        )

    @cached(tree=True)
    def get_aggregation_groups_for_event(
        self,
        event_id,
        event_type=None,
        limit=5,
        direction="b",
        from_token=None,
        to_token=None,
    ):
        """Get a list of annotations on the event, grouped by event type and
        aggregation key, sorted by count.

        This is used e.g. to get the what and how many reactions have happend
        on an event.

        Args:
            event_id (str): Fetch events that relate to this event ID.
            event_type (str|None): Only fetch events with this event type, if
                given.
            limit (int): Only fetch the `limit` groups.
            direction (str): Whether to fetch the highest count first (`"b"`) or
                the lowest count first (`"f"`).
            from_token (AggregationPaginationToken|None): Fetch rows from the
                given token, or from the start if None.
            to_token (AggregationPaginationToken|None): Fetch rows up to the
                given token, or up to the end if None.


        Returns:
            Deferred[PaginationChunk]: List of groups of annotations that
            match. Each row is a dict with `type`, `key` and `count` fields.
        """

        where_clause = ["relates_to_id = ?", "relation_type = ?"]
        where_args = [event_id, RelationTypes.ANNOTATION]

        if event_type:
            where_clause.append("type = ?")
            where_args.append(event_type)

        having_clause = generate_pagination_where_clause(
            direction=direction,
            column_names=("COUNT(*)", "MAX(stream_ordering)"),
            from_token=attr.astuple(from_token) if from_token else None,
            to_token=attr.astuple(to_token) if to_token else None,
            engine=self.database_engine,
        )

        if direction == "b":
            order = "DESC"
        else:
            order = "ASC"

        if having_clause:
            having_clause = "HAVING " + having_clause
        else:
            having_clause = ""

        sql = """
            SELECT type, aggregation_key, COUNT(DISTINCT sender), MAX(stream_ordering)
            FROM event_relations
            INNER JOIN events USING (event_id)
            WHERE {where_clause}
            GROUP BY relation_type, type, aggregation_key
            {having_clause}
            ORDER BY COUNT(*) {order}, MAX(stream_ordering) {order}
            LIMIT ?
        """.format(
            where_clause=" AND ".join(where_clause),
            order=order,
            having_clause=having_clause,
        )

        def _get_aggregation_groups_for_event_txn(txn):
            txn.execute(sql, where_args + [limit + 1])

            next_batch = None
            events = []
            for row in txn:
                events.append({"type": row[0], "key": row[1], "count": row[2]})
                next_batch = AggregationPaginationToken(row[2], row[3])

            if len(events) <= limit:
                next_batch = None

            return PaginationChunk(
                chunk=list(events[:limit]), next_batch=next_batch, prev_batch=from_token
            )

        return self.runInteraction(
            "get_aggregation_groups_for_event", _get_aggregation_groups_for_event_txn
        )

    @cachedInlineCallbacks()
    def get_applicable_edit(self, event_id):
        """Get the most recent edit (if any) that has happened for the given
        event.

        Correctly handles checking whether edits were allowed to happen.

        Args:
            event_id (str): The original event ID

        Returns:
            Deferred[EventBase|None]: Returns the most recent edit, if any.
        """

        # We only allow edits for `m.room.message` events that have the same sender
        # and event type. We can't assert these things during regular event auth so
        # we have to do the checks post hoc.

        # Fetches latest edit that has the same type and sender as the
        # original, and is an `m.room.message`.
        sql = """
            SELECT edit.event_id FROM events AS edit
            INNER JOIN event_relations USING (event_id)
            INNER JOIN events AS original ON
                original.event_id = relates_to_id
                AND edit.type = original.type
                AND edit.sender = original.sender
            WHERE
                relates_to_id = ?
                AND relation_type = ?
                AND edit.type = 'm.room.message'
            ORDER by edit.origin_server_ts DESC, edit.event_id DESC
            LIMIT 1
        """

        def _get_applicable_edit_txn(txn):
            txn.execute(sql, (event_id, RelationTypes.REPLACE))
            row = txn.fetchone()
            if row:
                return row[0]

        edit_id = yield self.runInteraction(
            "get_applicable_edit", _get_applicable_edit_txn
        )

        if not edit_id:
            return

        edit_event = yield self.get_event(edit_id, allow_none=True)
        return edit_event

    def has_user_annotated_event(self, parent_id, event_type, aggregation_key, sender):
        """Check if a user has already annotated an event with the same key
        (e.g. already liked an event).

        Args:
            parent_id (str): The event being annotated
            event_type (str): The event type of the annotation
            aggregation_key (str): The aggregation key of the annotation
            sender (str): The sender of the annotation

        Returns:
            Deferred[bool]
        """

        sql = """
            SELECT 1 FROM event_relations
            INNER JOIN events USING (event_id)
            WHERE
                relates_to_id = ?
                AND relation_type = ?
                AND type = ?
                AND sender = ?
                AND aggregation_key = ?
            LIMIT 1;
        """

        def _get_if_user_has_annotated_event(txn):
            txn.execute(
                sql,
                (
                    parent_id,
                    RelationTypes.ANNOTATION,
                    event_type,
                    sender,
                    aggregation_key,
                ),
            )

            return bool(txn.fetchone())

        return self.runInteraction(
            "get_if_user_has_annotated_event", _get_if_user_has_annotated_event
        )


class RelationsStore(RelationsWorkerStore):
    def _handle_event_relations(self, txn, event):
        """Handles inserting relation data during peristence of events

        Args:
            txn
            event (EventBase)
        """
        relation = event.content.get("m.relates_to")
        if not relation:
            # No relations
            return

        rel_type = relation.get("rel_type")
        if rel_type not in (
            RelationTypes.ANNOTATION,
            RelationTypes.REFERENCE,
            RelationTypes.REPLACE,
        ):
            # Unknown relation type
            return

        parent_id = relation.get("event_id")
        if not parent_id:
            # Invalid relation
            return

        aggregation_key = relation.get("key")

        self._simple_insert_txn(
            txn,
            table="event_relations",
            values={
                "event_id": event.event_id,
                "relates_to_id": parent_id,
                "relation_type": rel_type,
                "aggregation_key": aggregation_key,
            },
        )

        txn.call_after(self.get_relations_for_event.invalidate_many, (parent_id,))
        txn.call_after(
            self.get_aggregation_groups_for_event.invalidate_many, (parent_id,)
        )

        if rel_type == RelationTypes.REPLACE:
            txn.call_after(self.get_applicable_edit.invalidate, (parent_id,))

    def _handle_redaction(self, txn, redacted_event_id):
        """Handles receiving a redaction and checking whether we need to remove
        any redacted relations from the database.

        Args:
            txn
            redacted_event_id (str): The event that was redacted.
        """

        self._simple_delete_txn(
            txn, table="event_relations", keyvalues={"event_id": redacted_event_id}
        )
