# -*- coding: utf-8 -*-
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

from synapse.storage._base import SQLBaseStore, LoggingTransaction
from synapse.storage.prepare_database import get_statements

SQL = """

ALTER TABLE events ADD COLUMN chunk_id BIGINT;

-- FIXME: Add index on contains_url

INSERT INTO background_updates (update_name, progress_json) VALUES
    ('events_chunk_index', '{}');

-- Stores how chunks of graph relate to each other
CREATE TABLE chunk_graph (
    chunk_id BIGINT NOT NULL,
    prev_id BIGINT NOT NULL
);

CREATE UNIQUE INDEX chunk_graph_id ON chunk_graph (chunk_id, prev_id);
CREATE INDEX chunk_graph_prev_id ON chunk_graph (prev_id);

-- The extremities in each chunk. Note that these are pointing to events that
-- we don't have, rather than boundary between chunks.
CREATE TABLE chunk_backwards_extremities (
    chunk_id BIGINT NOT NULL,
    event_id TEXT NOT NULL
);

CREATE INDEX chunk_backwards_extremities_id ON chunk_backwards_extremities(
    chunk_id, event_id
);
CREATE INDEX chunk_backwards_extremities_event_id ON chunk_backwards_extremities(
    event_id
);

-- Maintains an absolute ordering of chunks. Gets updated when we see new
-- edges between chunks.
CREATE TABLE chunk_linearized (
    chunk_id BIGINT NOT NULL,
    room_id TEXT NOT NULL,
    next_chunk_id BIGINT,  -- The chunk directly after this chunk, or NULL if last chunk
    numerator BIGINT NOT NULL,
    denominator BIGINT NOT NULL
);

CREATE UNIQUE INDEX chunk_linearized_id ON chunk_linearized (chunk_id);
CREATE UNIQUE INDEX chunk_linearized_next_id ON chunk_linearized (
    next_chunk_id, room_id
);

-- Records the first chunk in a room.
CREATE TABLE chunk_linearized_first (
    chunk_id BIGINT NOT NULL,
    room_id TEXT NOT NULL
);

CREATE UNIQUE INDEX chunk_linearized_first_id ON chunk_linearized_first (room_id);

INSERT into background_updates (update_name, progress_json)
    VALUES ('event_fields_chunk_id', '{}');

"""


def run_create(cur, database_engine, *args, **kwargs):
    for statement in get_statements(SQL.splitlines()):
        cur.execute(statement)

    txn = LoggingTransaction(
        cur, "schema_update", database_engine, [], [],
    )

    rows = SQLBaseStore._simple_select_list_txn(
        txn,
        table="event_forward_extremities",
        keyvalues={},
        retcols=("event_id", "room_id",),
    )

    next_chunk_id = 1
    room_to_next_order = {}
    prev_chunks_by_room = {}

    for row in rows:
        chunk_id = next_chunk_id
        next_chunk_id += 1

        room_id = row["room_id"]
        event_id = row["event_id"]

        SQLBaseStore._simple_update_txn(
            txn,
            table="events",
            keyvalues={"room_id": room_id, "event_id": event_id},
            updatevalues={"chunk_id": chunk_id},
        )

        ordering = room_to_next_order.get(room_id, 1)
        room_to_next_order[room_id] = ordering + 1

        prev_chunks = prev_chunks_by_room.setdefault(room_id, [])

        SQLBaseStore._simple_insert_txn(
            txn,
            table="chunk_linearized",
            values={
                "chunk_id": chunk_id,
                "room_id": row["room_id"],
                "numerator": ordering,
                "denominator": 1,
            },
        )

        if prev_chunks:
            SQLBaseStore._simple_update_one_txn(
                txn,
                table="chunk_linearized",
                keyvalues={"chunk_id": prev_chunks[-1]},
                updatevalues={"next_chunk_id": chunk_id},
            )
        else:
            SQLBaseStore._simple_insert_txn(
                txn,
                table="chunk_linearized_first",
                values={
                    "chunk_id": chunk_id,
                    "room_id": row["room_id"],
                },
            )

        prev_chunks.append(chunk_id)


def run_upgrade(*args, **kwargs):
    pass
