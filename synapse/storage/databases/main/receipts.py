# Copyright 2014-2016 OpenMarket Ltd
# Copyright 2018 New Vector Ltd
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
from typing import (
    TYPE_CHECKING,
    Any,
    Collection,
    Dict,
    Iterable,
    List,
    Optional,
    Tuple,
    cast,
)

from synapse.api.constants import ReceiptTypes
from synapse.replication.slave.storage._slaved_id_tracker import SlavedIdTracker
from synapse.replication.tcp.streams import ReceiptsStream
from synapse.storage._base import SQLBaseStore, db_to_json, make_in_list_sql_clause
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
)
from synapse.storage.engines import PostgresEngine
from synapse.storage.util.id_generators import (
    AbstractStreamIdTracker,
    MultiWriterIdGenerator,
    StreamIdGenerator,
)
from synapse.types import JsonDict
from synapse.util import json_encoder
from synapse.util.caches.descriptors import cached, cachedList
from synapse.util.caches.stream_change_cache import StreamChangeCache

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ReceiptsWorkerStore(SQLBaseStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        self._instance_name = hs.get_instance_name()
        self._receipts_id_gen: AbstractStreamIdTracker

        if isinstance(database.engine, PostgresEngine):
            self._can_write_to_receipts = (
                self._instance_name in hs.config.worker.writers.receipts
            )

            self._receipts_id_gen = MultiWriterIdGenerator(
                db_conn=db_conn,
                db=database,
                stream_name="receipts",
                instance_name=self._instance_name,
                tables=[("receipts_linearized", "instance_name", "stream_id")],
                sequence_name="receipts_sequence",
                writers=hs.config.worker.writers.receipts,
            )
        else:
            self._can_write_to_receipts = True

            # We shouldn't be running in worker mode with SQLite, but its useful
            # to support it for unit tests.
            #
            # If this process is the writer than we need to use
            # `StreamIdGenerator`, otherwise we use `SlavedIdTracker` which gets
            # updated over replication. (Multiple writers are not supported for
            # SQLite).
            if hs.get_instance_name() in hs.config.worker.writers.receipts:
                self._receipts_id_gen = StreamIdGenerator(
                    db_conn, "receipts_linearized", "stream_id"
                )
            else:
                self._receipts_id_gen = SlavedIdTracker(
                    db_conn, "receipts_linearized", "stream_id"
                )

        super().__init__(database, db_conn, hs)

        max_receipts_stream_id = self.get_max_receipt_stream_id()
        receipts_stream_prefill, min_receipts_stream_id = self.db_pool.get_cache_dict(
            db_conn,
            "receipts_linearized",
            entity_column="room_id",
            stream_column="stream_id",
            max_value=max_receipts_stream_id,
            limit=10000,
        )
        self._receipts_stream_cache = StreamChangeCache(
            "ReceiptsRoomChangeCache",
            min_receipts_stream_id,
            prefilled_cache=receipts_stream_prefill,
        )

    def get_max_receipt_stream_id(self) -> int:
        """Get the current max stream ID for receipts stream"""
        return self._receipts_id_gen.get_current_token()

    async def get_last_receipt_event_id_for_user(
        self, user_id: str, room_id: str, receipt_types: Iterable[str]
    ) -> Optional[str]:
        """
        Fetch the event ID for the latest receipt in a room with one of the given receipt types.

        Args:
            user_id: The user to fetch receipts for.
            room_id: The room ID to fetch the receipt for.
            receipt_type: The receipt types to fetch. Earlier receipt types
                are given priority if multiple receipts point to the same event.

        Returns:
            The latest receipt, if one exists.
        """
        latest_event_id: Optional[str] = None
        latest_stream_ordering = 0
        for receipt_type in receipt_types:
            result = await self._get_last_receipt_event_id_for_user(
                user_id, room_id, receipt_type
            )
            if result is None:
                continue
            event_id, stream_ordering = result

            if latest_event_id is None or latest_stream_ordering < stream_ordering:
                latest_event_id = event_id
                latest_stream_ordering = stream_ordering

        return latest_event_id

    @cached()
    async def _get_last_receipt_event_id_for_user(
        self, user_id: str, room_id: str, receipt_type: str
    ) -> Optional[Tuple[str, int]]:
        """
        Fetch the event ID and stream ordering for the latest receipt.

        Args:
            user_id: The user to fetch receipts for.
            room_id: The room ID to fetch the receipt for.
            receipt_type: The receipt type to fetch.

        Returns:
            The event ID and stream ordering of the latest receipt, if one exists;
            otherwise `None`.
        """
        sql = """
            SELECT event_id, stream_ordering
            FROM receipts_linearized
            INNER JOIN events USING (room_id, event_id)
            WHERE user_id = ?
            AND room_id = ?
            AND receipt_type = ?
        """

        def f(txn: LoggingTransaction) -> Optional[Tuple[str, int]]:
            txn.execute(sql, (user_id, room_id, receipt_type))
            return cast(Optional[Tuple[str, int]], txn.fetchone())

        return await self.db_pool.runInteraction("get_own_receipt_for_user", f)

    async def get_receipts_for_user(
        self, user_id: str, receipt_types: Iterable[str]
    ) -> Dict[str, str]:
        """
        Fetch the event IDs for the latest receipts sent by the given user.

        Args:
            user_id: The user to fetch receipts for.
            receipt_types: The receipt types to check.

        Returns:
            A map of room ID to the event ID of the latest receipt for that room.

            If the user has not sent a receipt to a room then it will not appear
            in the returned dictionary.
        """
        results = await self.get_receipts_for_user_with_orderings(
            user_id, receipt_types
        )

        # Reduce the result to room ID -> event ID.
        return {
            room_id: room_result["event_id"] for room_id, room_result in results.items()
        }

    async def get_receipts_for_user_with_orderings(
        self, user_id: str, receipt_types: Iterable[str]
    ) -> JsonDict:
        """
        Fetch receipts for all rooms that the given user is joined to.

        Args:
            user_id: The user to fetch receipts for.
            receipt_types: The receipt types to fetch. Earlier receipt types
                are given priority if multiple receipts point to the same event.

        Returns:
            A map of room ID to the latest receipt (for the given types).
        """
        results: JsonDict = {}
        for receipt_type in receipt_types:
            partial_result = await self._get_receipts_for_user_with_orderings(
                user_id, receipt_type
            )
            for room_id, room_result in partial_result.items():
                # If the room has not yet been seen, or the receipt is newer,
                # use it.
                if (
                    room_id not in results
                    or results[room_id]["stream_ordering"]
                    < room_result["stream_ordering"]
                ):
                    results[room_id] = room_result

        return results

    @cached()
    async def _get_receipts_for_user_with_orderings(
        self, user_id: str, receipt_type: str
    ) -> JsonDict:
        """
        Fetch receipts for all rooms that the given user is joined to.

        Args:
            user_id: The user to fetch receipts for.
            receipt_type: The receipt type to fetch.

        Returns:
            A map of room ID to the latest receipt information.
        """

        def f(txn: LoggingTransaction) -> List[Tuple[str, str, int, int]]:
            sql = (
                "SELECT rl.room_id, rl.event_id,"
                " e.topological_ordering, e.stream_ordering"
                " FROM receipts_linearized AS rl"
                " INNER JOIN events AS e USING (room_id, event_id)"
                " WHERE rl.room_id = e.room_id"
                " AND rl.event_id = e.event_id"
                " AND user_id = ?"
                " AND receipt_type = ?"
            )
            txn.execute(sql, (user_id, receipt_type))
            return cast(List[Tuple[str, str, int, int]], txn.fetchall())

        rows = await self.db_pool.runInteraction(
            "get_receipts_for_user_with_orderings", f
        )
        return {
            row[0]: {
                "event_id": row[1],
                "topological_ordering": row[2],
                "stream_ordering": row[3],
            }
            for row in rows
        }

    async def get_linearized_receipts_for_rooms(
        self, room_ids: Iterable[str], to_key: int, from_key: Optional[int] = None
    ) -> List[dict]:
        """Get receipts for multiple rooms for sending to clients.

        Args:
            room_id: The room IDs to fetch receipts of.
            to_key: Max stream id to fetch receipts up to.
            from_key: Min stream id to fetch receipts from. None fetches
                from the start.

        Returns:
            A list of receipts.
        """
        room_ids = set(room_ids)

        if from_key is not None:
            # Only ask the database about rooms where there have been new
            # receipts added since `from_key`
            room_ids = self._receipts_stream_cache.get_entities_changed(
                room_ids, from_key
            )

        results = await self._get_linearized_receipts_for_rooms(
            room_ids, to_key, from_key=from_key
        )

        return [ev for res in results.values() for ev in res]

    async def get_linearized_receipts_for_room(
        self, room_id: str, to_key: int, from_key: Optional[int] = None
    ) -> List[dict]:
        """Get receipts for a single room for sending to clients.

        Args:
            room_ids: The room id.
            to_key: Max stream id to fetch receipts up to.
            from_key: Min stream id to fetch receipts from. None fetches
                from the start.

        Returns:
            A list of receipts.
        """
        if from_key is not None:
            # Check the cache first to see if any new receipts have been added
            # since`from_key`. If not we can no-op.
            if not self._receipts_stream_cache.has_entity_changed(room_id, from_key):
                return []

        return await self._get_linearized_receipts_for_room(room_id, to_key, from_key)

    @cached(tree=True)
    async def _get_linearized_receipts_for_room(
        self, room_id: str, to_key: int, from_key: Optional[int] = None
    ) -> List[JsonDict]:
        """See get_linearized_receipts_for_room"""

        def f(txn: LoggingTransaction) -> List[Dict[str, Any]]:
            if from_key:
                sql = (
                    "SELECT * FROM receipts_linearized WHERE"
                    " room_id = ? AND stream_id > ? AND stream_id <= ?"
                )

                txn.execute(sql, (room_id, from_key, to_key))
            else:
                sql = (
                    "SELECT * FROM receipts_linearized WHERE"
                    " room_id = ? AND stream_id <= ?"
                )

                txn.execute(sql, (room_id, to_key))

            rows = self.db_pool.cursor_to_dict(txn)

            return rows

        rows = await self.db_pool.runInteraction("get_linearized_receipts_for_room", f)

        if not rows:
            return []

        content: JsonDict = {}
        for row in rows:
            content.setdefault(row["event_id"], {}).setdefault(row["receipt_type"], {})[
                row["user_id"]
            ] = db_to_json(row["data"])

        return [{"type": "m.receipt", "room_id": room_id, "content": content}]

    @cachedList(
        cached_method_name="_get_linearized_receipts_for_room",
        list_name="room_ids",
        num_args=3,
    )
    async def _get_linearized_receipts_for_rooms(
        self, room_ids: Collection[str], to_key: int, from_key: Optional[int] = None
    ) -> Dict[str, List[JsonDict]]:
        if not room_ids:
            return {}

        def f(txn: LoggingTransaction) -> List[Dict[str, Any]]:
            if from_key:
                sql = """
                    SELECT * FROM receipts_linearized WHERE
                    stream_id > ? AND stream_id <= ? AND
                """
                clause, args = make_in_list_sql_clause(
                    self.database_engine, "room_id", room_ids
                )

                txn.execute(sql + clause, [from_key, to_key] + list(args))
            else:
                sql = """
                    SELECT * FROM receipts_linearized WHERE
                    stream_id <= ? AND
                """

                clause, args = make_in_list_sql_clause(
                    self.database_engine, "room_id", room_ids
                )

                txn.execute(sql + clause, [to_key] + list(args))

            return self.db_pool.cursor_to_dict(txn)

        txn_results = await self.db_pool.runInteraction(
            "_get_linearized_receipts_for_rooms", f
        )

        results: JsonDict = {}
        for row in txn_results:
            # We want a single event per room, since we want to batch the
            # receipts by room, event and type.
            room_event = results.setdefault(
                row["room_id"],
                {"type": "m.receipt", "room_id": row["room_id"], "content": {}},
            )

            # The content is of the form:
            # {"$foo:bar": { "read": { "@user:host": <receipt> }, .. }, .. }
            event_entry = room_event["content"].setdefault(row["event_id"], {})
            receipt_type = event_entry.setdefault(row["receipt_type"], {})

            receipt_type[row["user_id"]] = db_to_json(row["data"])

        results = {
            room_id: [results[room_id]] if room_id in results else []
            for room_id in room_ids
        }
        return results

    @cached(
        num_args=2,
    )
    async def get_linearized_receipts_for_all_rooms(
        self, to_key: int, from_key: Optional[int] = None
    ) -> Dict[str, JsonDict]:
        """Get receipts for all rooms between two stream_ids, up
        to a limit of the latest 100 read receipts.

        Args:
            to_key: Max stream id to fetch receipts up to.
            from_key: Min stream id to fetch receipts from. None fetches
                from the start.

        Returns:
            A dictionary of roomids to a list of receipts.
        """

        def f(txn: LoggingTransaction) -> List[Dict[str, Any]]:
            if from_key:
                sql = """
                    SELECT * FROM receipts_linearized WHERE
                    stream_id > ? AND stream_id <= ?
                    ORDER BY stream_id DESC
                    LIMIT 100
                """
                txn.execute(sql, [from_key, to_key])
            else:
                sql = """
                    SELECT * FROM receipts_linearized WHERE
                    stream_id <= ?
                    ORDER BY stream_id DESC
                    LIMIT 100
                """

                txn.execute(sql, [to_key])

            return self.db_pool.cursor_to_dict(txn)

        txn_results = await self.db_pool.runInteraction(
            "get_linearized_receipts_for_all_rooms", f
        )

        results: JsonDict = {}
        for row in txn_results:
            # We want a single event per room, since we want to batch the
            # receipts by room, event and type.
            room_event = results.setdefault(
                row["room_id"],
                {"type": "m.receipt", "room_id": row["room_id"], "content": {}},
            )

            # The content is of the form:
            # {"$foo:bar": { "read": { "@user:host": <receipt> }, .. }, .. }
            event_entry = room_event["content"].setdefault(row["event_id"], {})
            receipt_type = event_entry.setdefault(row["receipt_type"], {})

            receipt_type[row["user_id"]] = db_to_json(row["data"])

        return results

    async def get_users_sent_receipts_between(
        self, last_id: int, current_id: int
    ) -> List[str]:
        """Get all users who sent receipts between `last_id` exclusive and
        `current_id` inclusive.

        Returns:
            The list of users.
        """

        if last_id == current_id:
            return []

        def _get_users_sent_receipts_between_txn(txn: LoggingTransaction) -> List[str]:
            sql = """
                SELECT DISTINCT user_id FROM receipts_linearized
                WHERE ? < stream_id AND stream_id <= ?
            """
            txn.execute(sql, (last_id, current_id))

            return [r[0] for r in txn]

        return await self.db_pool.runInteraction(
            "get_users_sent_receipts_between", _get_users_sent_receipts_between_txn
        )

    async def get_all_updated_receipts(
        self, instance_name: str, last_id: int, current_id: int, limit: int
    ) -> Tuple[List[Tuple[int, list]], int, bool]:
        """Get updates for receipts replication stream.

        Args:
            instance_name: The writer we want to fetch updates from. Unused
                here since there is only ever one writer.
            last_id: The token to fetch updates from. Exclusive.
            current_id: The token to fetch updates up to. Inclusive.
            limit: The requested limit for the number of rows to return. The
                function may return more or fewer rows.

        Returns:
            A tuple consisting of: the updates, a token to use to fetch
            subsequent updates, and whether we returned fewer rows than exists
            between the requested tokens due to the limit.

            The token returned can be used in a subsequent call to this
            function to get further updatees.

            The updates are a list of 2-tuples of stream ID and the row data
        """

        if last_id == current_id:
            return [], current_id, False

        def get_all_updated_receipts_txn(
            txn: LoggingTransaction,
        ) -> Tuple[List[Tuple[int, list]], int, bool]:
            sql = """
                SELECT stream_id, room_id, receipt_type, user_id, event_id, data
                FROM receipts_linearized
                WHERE ? < stream_id AND stream_id <= ?
                ORDER BY stream_id ASC
                LIMIT ?
            """
            txn.execute(sql, (last_id, current_id, limit))

            updates = cast(
                List[Tuple[int, list]],
                [(r[0], r[1:5] + (db_to_json(r[5]),)) for r in txn],
            )

            limited = False
            upper_bound = current_id

            if len(updates) == limit:
                limited = True
                upper_bound = updates[-1][0]

            return updates, upper_bound, limited

        return await self.db_pool.runInteraction(
            "get_all_updated_receipts", get_all_updated_receipts_txn
        )

    def invalidate_caches_for_receipt(
        self, room_id: str, receipt_type: str, user_id: str
    ) -> None:
        self._get_receipts_for_user_with_orderings.invalidate((user_id, receipt_type))
        self._get_linearized_receipts_for_room.invalidate((room_id,))
        self._get_last_receipt_event_id_for_user.invalidate(
            (user_id, room_id, receipt_type)
        )

    def process_replication_rows(
        self,
        stream_name: str,
        instance_name: str,
        token: int,
        rows: Iterable[Any],
    ) -> None:
        if stream_name == ReceiptsStream.NAME:
            self._receipts_id_gen.advance(instance_name, token)
            for row in rows:
                self.invalidate_caches_for_receipt(
                    row.room_id, row.receipt_type, row.user_id
                )
                self._receipts_stream_cache.entity_has_changed(row.room_id, token)

        return super().process_replication_rows(stream_name, instance_name, token, rows)

    def insert_linearized_receipt_txn(
        self,
        txn: LoggingTransaction,
        room_id: str,
        receipt_type: str,
        user_id: str,
        event_id: str,
        data: JsonDict,
        stream_id: int,
    ) -> Optional[int]:
        """Inserts a receipt into the database if it's newer than the current one.

        Returns:
            None if the receipt is older than the current receipt
            otherwise, the rx timestamp of the event that the receipt corresponds to
                (or 0 if the event is unknown)
        """
        assert self._can_write_to_receipts

        res = self.db_pool.simple_select_one_txn(
            txn,
            table="events",
            retcols=["stream_ordering", "received_ts"],
            keyvalues={"event_id": event_id},
            allow_none=True,
        )

        stream_ordering = int(res["stream_ordering"]) if res else None
        rx_ts = res["received_ts"] if res else 0

        # We don't want to clobber receipts for more recent events, so we
        # have to compare orderings of existing receipts
        if stream_ordering is not None:
            sql = (
                "SELECT stream_ordering, event_id FROM events"
                " INNER JOIN receipts_linearized AS r USING (event_id, room_id)"
                " WHERE r.room_id = ? AND r.receipt_type = ? AND r.user_id = ?"
            )
            txn.execute(sql, (room_id, receipt_type, user_id))

            for so, eid in txn:
                if int(so) >= stream_ordering:
                    logger.debug(
                        "Ignoring new receipt for %s in favour of existing "
                        "one for later event %s",
                        event_id,
                        eid,
                    )
                    return None

        txn.call_after(
            self.invalidate_caches_for_receipt, room_id, receipt_type, user_id
        )

        txn.call_after(
            self._receipts_stream_cache.entity_has_changed, room_id, stream_id
        )

        self.db_pool.simple_upsert_txn(
            txn,
            table="receipts_linearized",
            keyvalues={
                "room_id": room_id,
                "receipt_type": receipt_type,
                "user_id": user_id,
            },
            values={
                "stream_id": stream_id,
                "event_id": event_id,
                "data": json_encoder.encode(data),
            },
            # receipts_linearized has a unique constraint on
            # (user_id, room_id, receipt_type), so no need to lock
            lock=False,
        )

        if (
            receipt_type in (ReceiptTypes.READ, ReceiptTypes.READ_PRIVATE)
            and stream_ordering is not None
        ):
            self._remove_old_push_actions_before_txn(  # type: ignore[attr-defined]
                txn, room_id=room_id, user_id=user_id, stream_ordering=stream_ordering
            )

        return rx_ts

    async def insert_receipt(
        self,
        room_id: str,
        receipt_type: str,
        user_id: str,
        event_ids: List[str],
        data: dict,
    ) -> Optional[Tuple[int, int]]:
        """Insert a receipt, either from local client or remote server.

        Automatically does conversion between linearized and graph
        representations.

        Returns:
            The new receipts stream ID and token, if the receipt is newer than
            what was previously persisted. None, otherwise.
        """
        assert self._can_write_to_receipts

        if not event_ids:
            return None

        if len(event_ids) == 1:
            linearized_event_id = event_ids[0]
        else:
            # we need to points in graph -> linearized form.
            # TODO: Make this better.
            def graph_to_linear(txn: LoggingTransaction) -> str:
                clause, args = make_in_list_sql_clause(
                    self.database_engine, "event_id", event_ids
                )

                sql = """
                    SELECT event_id WHERE room_id = ? AND stream_ordering IN (
                        SELECT max(stream_ordering) WHERE %s
                    )
                """ % (
                    clause,
                )

                txn.execute(sql, [room_id] + list(args))
                rows = txn.fetchall()
                if rows:
                    return rows[0][0]
                else:
                    raise RuntimeError("Unrecognized event_ids: %r" % (event_ids,))

            linearized_event_id = await self.db_pool.runInteraction(
                "insert_receipt_conv", graph_to_linear
            )

        async with self._receipts_id_gen.get_next() as stream_id:  # type: ignore[attr-defined]
            event_ts = await self.db_pool.runInteraction(
                "insert_linearized_receipt",
                self.insert_linearized_receipt_txn,
                room_id,
                receipt_type,
                user_id,
                linearized_event_id,
                data,
                stream_id=stream_id,
            )

        # If the receipt was older than the currently persisted one, nothing to do.
        if event_ts is None:
            return None

        now = self._clock.time_msec()
        logger.debug(
            "RR for event %s in %s (%i ms old)",
            linearized_event_id,
            room_id,
            now - event_ts,
        )

        await self.insert_graph_receipt(room_id, receipt_type, user_id, event_ids, data)

        max_persisted_id = self._receipts_id_gen.get_current_token()

        return stream_id, max_persisted_id

    async def insert_graph_receipt(
        self,
        room_id: str,
        receipt_type: str,
        user_id: str,
        event_ids: List[str],
        data: JsonDict,
    ) -> None:
        assert self._can_write_to_receipts

        await self.db_pool.runInteraction(
            "insert_graph_receipt",
            self.insert_graph_receipt_txn,
            room_id,
            receipt_type,
            user_id,
            event_ids,
            data,
        )

    def insert_graph_receipt_txn(
        self,
        txn: LoggingTransaction,
        room_id: str,
        receipt_type: str,
        user_id: str,
        event_ids: List[str],
        data: JsonDict,
    ) -> None:
        assert self._can_write_to_receipts

        txn.call_after(
            self._get_receipts_for_user_with_orderings.invalidate,
            (user_id, receipt_type),
        )
        # FIXME: This shouldn't invalidate the whole cache
        txn.call_after(self._get_linearized_receipts_for_room.invalidate, (room_id,))

        self.db_pool.simple_delete_txn(
            txn,
            table="receipts_graph",
            keyvalues={
                "room_id": room_id,
                "receipt_type": receipt_type,
                "user_id": user_id,
            },
        )
        self.db_pool.simple_insert_txn(
            txn,
            table="receipts_graph",
            values={
                "room_id": room_id,
                "receipt_type": receipt_type,
                "user_id": user_id,
                "event_ids": json_encoder.encode(event_ids),
                "data": json_encoder.encode(data),
            },
        )


class ReceiptsStore(ReceiptsWorkerStore):
    pass
