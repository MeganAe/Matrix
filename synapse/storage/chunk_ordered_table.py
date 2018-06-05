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

import math
import logging

from fractions import Fraction

from synapse.storage._base import SQLBaseStore
from synapse.storage.engines import PostgresEngine
from synapse.util.katriel_bodlaender import OrderedListStore

import synapse.metrics

metrics = synapse.metrics.get_metrics_for(__name__)
rebalance_counter = metrics.register_counter("rebalances")


logger = logging.getLogger(__name__)


class ChunkDBOrderedListStore(OrderedListStore):
    """Used as the list store for room chunks, efficiently maintaining them in
    topological order on updates.

    A room chunk is a connected portion of the room events DAG. Chunks are
    constructed so that they have the additional property that for all events in
    the chunk, either all of their prev_events are in that chunk or none of them
    are. This ensures that no event that is subsequently received needs to be
    inserted into the middle of a chunk, since it cannot both reference an event
    in the chunk and be referenced by an event in the chunk (assuming no
    cycles).

    As such the set of chunks in a room inherits a DAG, i.e. if an event in one
    chunk references an event in a second chunk, then we say that the first
    chunk references the second, and thus forming a DAG. (This means that chunks
    start off disconnected until an event is received that connects the two
    chunks.)

    We can therefore end up with multiple chunks in a room when the server
    misses some events, e.g. due to the server being offline for a time.

    The server may only have a subset of all events in a room, in which case
    its possible for the server to have chunks that are unconnected from each
    other. The ordering between unconnected chunks is arbitrary.

    The class is designed for use inside transactions and so takes a
    transaction object in the constructor. This means that it needs to be
    re-instantiated in each transaction, so all state needs to be stored
    in the database.

    Internally the ordering is implemented using a linked list and assigning
    each chunk a fraction. `get_next` and `get_prev` are implemented via linked
    lists, and comparisons implemented using the fractions. When inserting
    chunks fractions are picked such that their denominator is the smallest
    possible. However, if the denominators grow too big then a rebalancing has
    to take place to reduce the denominators; see `_rebalance` for details.

    Note that OrderedListStore orders nodes such that source of an edge
    comes before the target. This is counter intuitive when edges represent
    causality, so for the purposes of ordering algorithm we invert the edge
    directions, i.e. if chunk A has a prev chunk of B then we say that the
    edge is from B to A. This ensures that newer chunks get inserted at the
    end (rather than the start).

    Note: Calls to `add_node` and `add_edge` cannot overlap for the same room,
    and so callers should perform some form of per-room locking when using
    this class.

    Args:
        txn
        room_id (str)
        clock
        database_engine
        rebalance_max_denominator (int): When a rebalance is triggered we
            replace existing orders with those that have a denominator smaller
            or equal to this
        max_denominator (int): A rebalance is triggered when a node has an
            ordering with a denominator greater than this
    """
    def __init__(self,
                 txn, room_id, clock, database_engine,
                 rebalance_max_denominator=100,
                 max_denominator=100000):
        self.txn = txn
        self.room_id = room_id
        self.clock = clock
        self.database_engine = database_engine

        self.rebalance_md = rebalance_max_denominator
        self.max_denominator = max_denominator

    def is_before(self, a, b):
        """Implements OrderedListStore"""
        return self._get_order(a) < self._get_order(b)

    def get_prev(self, node_id):
        """Implements OrderedListStore"""

        sql = """
            SELECT chunk_id FROM chunk_linearized
            WHERE next_chunk_id = ?
        """

        self.txn.execute(sql, (node_id,))

        row = self.txn.fetchone()
        if row:
            return row[0]
        return None

    def get_next(self, node_id):
        """Implements OrderedListStore"""

        sql = """
            SELECT next_chunk_id FROM chunk_linearized
            WHERE chunk_id = ?
        """

        self.txn.execute(sql, (node_id,))

        row = self.txn.fetchone()
        if row:
            return row[0]
        return None

    def _insert_before(self, node_id, target_id):
        """Implements OrderedListStore"""

        rebalance = False  # Set to true if we need to trigger a rebalance

        if target_id:
            before_id = self.get_prev(target_id)
            if before_id:
                new_order = self._insert_between(node_id, before_id, target_id)
            else:
                new_order = self._insert_at_start(node_id, target_id)
        else:
            # If target_id is None then we insert at the end.
            self.txn.execute("""
                SELECT chunk_id
                FROM chunk_linearized
                WHERE room_id = ? AND next_chunk_id is NULL
            """, (self.room_id,))

            row = self.txn.fetchone()
            if row:
                new_order = self._insert_at_end(node_id, row[0])
            else:
                new_order = self._insert_first(node_id)

        rebalance = new_order.denominator > self.max_denominator

        if rebalance:
            self._rebalance(node_id)

    def _insert_after(self, node_id, target_id):
        """Implements OrderedListStore"""

        rebalance = False  # Set to true if we need to trigger a rebalance

        next_chunk_id = None
        if target_id:
            next_chunk_id = self.get_next(target_id)
            if next_chunk_id:
                new_order = self._insert_between(node_id, target_id, next_chunk_id)
            else:
                new_order = self._insert_at_end(node_id, target_id)
        else:
            # If target_id is None then we insert at the start.
            self.txn.execute("""
                SELECT chunk_id
                FROM chunk_linearized
                NATURAL JOIN chunk_linearized_first
                WHERE room_id = ?
            """, (self.room_id,))

            row = self.txn.fetchone()
            if row:
                new_order = self._insert_at_start(node_id, row[0])
            else:
                new_order = self._insert_first(node_id)

        rebalance = new_order.denominator > self.max_denominator

        if rebalance:
            self._rebalance(node_id)

    def _insert_between(self, node_id, left_id, right_id):
        """Inserts node between given existing nodes.
        """

        left_order = self._get_order(left_id)
        right_order = self._get_order(right_id)

        assert left_order < right_order

        new_order = get_fraction_in_range(left_order, right_order)

        SQLBaseStore._simple_update_one_txn(
            self.txn,
            table="chunk_linearized",
            keyvalues={"chunk_id": left_id},
            updatevalues={"next_chunk_id": node_id},
        )

        SQLBaseStore._simple_insert_txn(
            self.txn,
            table="chunk_linearized",
            values={
                "chunk_id": node_id,
                "room_id": self.room_id,
                "next_chunk_id": right_id,
                "numerator": int(new_order.numerator),
                "denominator": int(new_order.denominator),
            }
        )

        return new_order

    def _insert_at_end(self, node_id, last_id):
        """Inserts node at the end using existing last node.
        """

        last_order = self._get_order(last_id)
        new_order = Fraction(int(math.ceil(last_order)) + 1, 1)

        SQLBaseStore._simple_update_one_txn(
            self.txn,
            table="chunk_linearized",
            keyvalues={"chunk_id": last_id},
            updatevalues={"next_chunk_id": node_id},
        )

        SQLBaseStore._simple_insert_txn(
            self.txn,
            table="chunk_linearized",
            values={
                "chunk_id": node_id,
                "room_id": self.room_id,
                "next_chunk_id": None,
                "numerator": int(new_order.numerator),
                "denominator": int(new_order.denominator),
            }
        )

        return new_order

    def _insert_at_start(self, node_id, first_id):
        """Inserts node at the start using existing first node.
        """

        first_order = self._get_order(first_id)
        new_order = get_fraction_in_range(0, first_order)

        SQLBaseStore._simple_update_one_txn(
            self.txn,
            table="chunk_linearized_first",
            keyvalues={"room_id": self.room_id},
            updatevalues={"chunk_id": node_id},
        )

        SQLBaseStore._simple_insert_txn(
            self.txn,
            table="chunk_linearized",
            values={
                "chunk_id": node_id,
                "room_id": self.room_id,
                "next_chunk_id": first_id,
                "numerator": int(new_order.numerator),
                "denominator": int(new_order.denominator),
            }
        )

        return new_order

    def _insert_first(self, node_id):
        """Inserts the first node for this room.
        """

        SQLBaseStore._simple_insert_txn(
            self.txn,
            table="chunk_linearized_first",
            values={
                "room_id": self.room_id,
                "chunk_id": node_id,
            },
        )

        SQLBaseStore._simple_insert_txn(
            self.txn,
            table="chunk_linearized",
            values={
                "chunk_id": node_id,
                "room_id": self.room_id,
                "next_chunk_id": None,
                "numerator": 1,
                "denominator": 1,
            }
        )

        return Fraction(1, 1)

    def get_nodes_with_edges_to(self, node_id):
        """Implements OrderedListStore"""

        # Note that we use the inverse relation here
        sql = """
            SELECT l.chunk_id, l.numerator, l.denominator FROM chunk_graph AS g
            INNER JOIN chunk_linearized AS l ON g.prev_id = l.chunk_id
            WHERE g.chunk_id = ?
        """
        self.txn.execute(sql, (node_id,))
        return [(Fraction(n, d), c) for c, n, d in self.txn]

    def get_nodes_with_edges_from(self, node_id):
        """Implements OrderedListStore"""

        # Note that we use the inverse relation here
        sql = """
            SELECT  l.chunk_id, l.numerator, l.denominator FROM chunk_graph AS g
            INNER JOIN chunk_linearized AS l ON g.chunk_id = l.chunk_id
            WHERE g.prev_id = ?
        """
        self.txn.execute(sql, (node_id,))
        return [(Fraction(n, d), c) for c, n, d in self.txn]

    def _delete_ordering(self, node_id):
        """Implements OrderedListStore"""

        next_chunk_id = SQLBaseStore._simple_select_one_onecol_txn(
            self.txn,
            table="chunk_linearized",
            keyvalues={
                "chunk_id": node_id,
            },
            retcol="next_chunk_id",
        )

        SQLBaseStore._simple_delete_txn(
            self.txn,
            table="chunk_linearized",
            keyvalues={"chunk_id": node_id},
        )

        sql = """
            UPDATE chunk_linearized SET next_chunk_id = ?
            WHERE next_chunk_id = ?
        """

        self.txn.execute(sql, (next_chunk_id, node_id,))

        sql = """
            UPDATE chunk_linearized_first SET chunk_id = ?
            WHERE chunk_id = ?
        """

        self.txn.execute(sql, (next_chunk_id, node_id,))

    def _add_edge_to_graph(self, source_id, target_id):
        """Implements OrderedListStore"""

        # Note that we use the inverse relation
        SQLBaseStore._simple_insert_txn(
            self.txn,
            table="chunk_graph",
            values={"chunk_id": target_id, "prev_id": source_id}
        )

    def _get_order(self, node_id):
        """Get the ordering of the given node.
        """

        row = SQLBaseStore._simple_select_one_txn(
            self.txn,
            table="chunk_linearized",
            keyvalues={"chunk_id": node_id},
            retcols=("numerator", "denominator",),
        )
        return Fraction(row["numerator"], row["denominator"])

    def _rebalance(self, node_id):
        """Rebalances the list around the given node to ensure that the
        ordering denominators aren't too big.

        This is done by starting at the given chunk and generating new orders
        based on a Farey sequence of order `self.rebalance_md` for all
        subsequent chunks that have an order less than that of the ordering
        generated by the Farey sequence.

        For example say we have chunks (and orders): A (23/90),  B (24/91) and
        C (2/3), and we have rebalance_md set to 5, a rebalancing would produce:

            A: 23/90 -> 1/3
            B: 24/91 -> 2/5
            C: 2/3  (no change)

        Since the farey sequence is 1/5, 1/4, 1/3, 2/5, 1/2, ... and 1/3 is the
        smallest term greater than 23/90.

        Note that we've extended Farey Sequence to be infinite by repeating the
        sequence with an added integer. For example sequence with order 3:

            0/1, 1/3, 2/3, 1/1, 4/3, 5/3, 2/1, 7/3, ...
        """

        logger.info("Rebalancing room %s, chunk %s", self.room_id, node_id)

        old_order = self._get_order(node_id)

        a, b, c, d = find_farey_terms(old_order, self.rebalance_md)
        assert old_order < Fraction(a, b)
        assert b + d > self.rebalance_md

        # Since we can easily produce farey sequence terms with an iterative
        # algorithm, we can use WITH RECURSIVE to do so. This is less clear
        # than doing it in python, but saves us being killed by the RTT to the
        # DB if we need to rebalance a large number of nodes.
        with_sql = """
            WITH RECURSIVE chunks (chunk_id, next, n, a, b, c, d) AS (
                    SELECT chunk_id, next_chunk_id, ?, ?, ?, ?, ?
                    FROM chunk_linearized WHERE chunk_id = ?
                UNION ALL
                    SELECT n.chunk_id, n.next_chunk_id, n,
                    c, d, ((n + b) / d) * c - a, ((n + b) / d) * d - b
                    FROM chunks AS c
                    INNER JOIN chunk_linearized AS l ON l.chunk_id = c.chunk_id
                    INNER JOIN chunk_linearized AS n ON n.chunk_id = l.next_chunk_id
                    WHERE c * 1.0 / d > n.numerator * 1.0 / n.denominator
            )
        """

        # Annoyingly, postgres 9.4 doesn't support the standard SQL subquery
        # syntax for updates.
        if isinstance(self.database_engine, PostgresEngine):
            sql = with_sql + """
                UPDATE chunk_linearized AS l
                SET numerator = a, denominator = b
                FROM chunks AS c
                WHERE c.chunk_id = l.chunk_id
            """
        else:
            sql = with_sql + """
                UPDATE chunk_linearized
                SET (numerator, denominator) = (
                    SELECT a, b FROM chunks
                    WHERE chunks.chunk_id = chunk_linearized.chunk_id
                )
                WHERE chunk_id in (SELECT chunk_id FROM chunks)
            """

        self.txn.execute(sql, (
            self.rebalance_md, a, b, c, d, node_id
        ))

        logger.info("Rebalanced %d chunks in room %s", self.txn.rowcount, self.room_id)

        rebalance_counter.inc()


def get_fraction_in_range(min_frac, max_frac):
    """Gets a fraction in between the given numbers.

    Uses Stern-Brocot tree to generate the fraction with the smallest
    denominator.

    See https://en.wikipedia.org/wiki/Stern%E2%80%93Brocot_tree

    Args:
        min_frac (numbers.Rational)
        max_frac (numbers.Rational)

    Returns:
        numbers.Rational
    """

    assert 0 <= min_frac < max_frac

    # If the determinant is 1 then the fraction with smallest numerator and
    # denominator in the range is the mediant, so we don't have to use the
    # stern brocot tree to search for it.
    determinant = (
        min_frac.denominator * max_frac.numerator
        - min_frac.numerator * max_frac.denominator
    )

    if determinant == 1:
        return Fraction(
            min_frac.numerator + max_frac.numerator,
            min_frac.denominator + max_frac.denominator,
        )

    # This works by tracking two fractions a/b and c/d and repeatedly replacing
    # one of them with their mediant, depending on if the mediant is smaller
    # or greater than the specified range.
    a, b, c, d = 0, 1, 1, 0

    while True:
        f = Fraction(a + c, b + d)

        if f <= min_frac:
            a, b, c, d = a + c, b + d, c, d
        elif min_frac < f < max_frac:
            return f
        else:
            a, b, c, d = a, b, a + c, b + d


def find_farey_terms(min_frac, max_denom):
    """Find the smallest pair of fractions that are part of the Farey sequence
    of order `max_denom` (the ordered sequence of all fraction with denominator
    less than or equal to max_denom).

    This is useful as it can be fed into a simple iterative algorithm to
    generate subsequent entries in the sequence.

    A pair of fractions a/b, c/d are neighbours in the sequence of order
    max(b, d) if and only if their determinant is one, i.e. bc - ad = 1. Note
    that the next order sequence is generate by taking the mediants of the
    previous order, so a/b and c/d are neighbours in all sequences with orders
    between max(b, d) and b + d.

    We can therefore use the Stern-Brocot tree to find the closest pair of
    fractions to min_frac such that b + d is strictly greater than max_denom,
    since all neighbouring fractions in Stern-Brocot satisfy the necessary
    determinant property.

    Note that we've extended Farey Sequence to be infinite by repeating the
    sequence with an added integer. For example sequence with order 3:

        0/1, 1/3, 2/3, 1/1, 4/3, 5/3, 2/1, 7/3, ...

    See https://en.wikipedia.org/wiki/Farey_sequence

    Args:
        min_frac (numbers.Rational)
        max_frac (int)

    Returns:
        tuple[int, int, int, int]
    """

    a, b, c, d = 0, 1, 1, 0

    while True:
        cur_frac = Fraction(a + c, b + d)

        if b + d > max_denom:
            break

        if cur_frac <= min_frac:
            a, b, c, d = a + c, b + d, c, d
        elif min_frac < cur_frac:
            a, b, c, d = a, b, a + c, b + d

    # a/b may be smaller than min_frac, so we run the algorithm to generate
    # next Farey sequence terms until a/b is strictly greater than min_frac
    while Fraction(a, b) <= min_frac:
        k = int((max_denom + b) / d)
        a, b, c, d = c, d, k * c - a, k * d - b

    assert min_frac < Fraction(a, b) < Fraction(c, d)
    assert b * c - a * d == 1

    return a, b, c, d
