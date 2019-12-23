# -*- coding: utf-8 -*-
# Copyright 2018 Vector Creations Ltd
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

from synapse.storage._base import SQLBaseStore

logger = logging.getLogger(__name__)


class StateDeltasStore(SQLBaseStore):
    def get_current_state_deltas(self, prev_stream_id: int, max_stream_id: int):
        """Fetch a list of room state changes since the given stream id

        Each entry in the result contains the following fields:
            - stream_id (int)
            - room_id (str)
            - type (str): event type
            - state_key (str):
            - event_id (str|None): new event_id for this state key. None if the
                state has been deleted.
            - prev_event_id (str|None): previous event_id for this state key. None
                if it's new state.

        Args:
            prev_stream_id (int): point to get changes since (exclusive)
            max_stream_id (int): the point that we know has been correctly persisted
               - ie, an upper limit to return changes from.

        Returns:
            Deferred[tuple[int, list[dict]]: A tuple consisting of:
               - the stream id which these results go up to
               - list of current_state_delta_stream rows. If it is empty, we are
                 up to date.
        """
        prev_stream_id = int(prev_stream_id)

        # check we're not going backwards
        assert prev_stream_id <= max_stream_id

        if not self._curr_state_delta_stream_cache.has_any_entity_changed(
            prev_stream_id
        ):
            # if the CSDs haven't changed between prev_stream_id and now, we
            # know for certain that they haven't changed between prev_stream_id and
            # max_stream_id.
            return max_stream_id, []

        def get_current_state_deltas_txn(txn):
            # First we calculate the max stream id that will give us less than
            # N results.
            # We arbitarily limit to 100 stream_id entries to ensure we don't
            # select toooo many.
            sql = """
                SELECT stream_id, count(*)
                FROM current_state_delta_stream
                WHERE stream_id > ? AND stream_id <= ?
                GROUP BY stream_id
                ORDER BY stream_id ASC
                LIMIT 100
            """
            txn.execute(sql, (prev_stream_id, max_stream_id))

            total = 0

            for stream_id, count in txn:
                total += count
                if total > 100:
                    # We arbitarily limit to 100 entries to ensure we don't
                    # select toooo many.
                    logger.debug(
                        "Clipping current_state_delta_stream rows to stream_id %i",
                        stream_id,
                    )
                    clipped_stream_id = stream_id
                    break
            else:
                # if there's no problem, we may as well go right up to the max_stream_id
                clipped_stream_id = max_stream_id

            # Now actually get the deltas
            sql = """
                SELECT stream_id, room_id, type, state_key, event_id, prev_event_id
                FROM current_state_delta_stream
                WHERE ? < stream_id AND stream_id <= ?
                ORDER BY stream_id ASC
            """
            txn.execute(sql, (prev_stream_id, clipped_stream_id))
            return clipped_stream_id, self.db.cursor_to_dict(txn)

        return self.db.runInteraction(
            "get_current_state_deltas", get_current_state_deltas_txn
        )

    def _get_max_stream_id_in_current_state_deltas_txn(self, txn):
        return self.db.simple_select_one_onecol_txn(
            txn,
            table="current_state_delta_stream",
            keyvalues={},
            retcol="COALESCE(MAX(stream_id), -1)",
        )

    def get_max_stream_id_in_current_state_deltas(self):
        return self.db.runInteraction(
            "get_max_stream_id_in_current_state_deltas",
            self._get_max_stream_id_in_current_state_deltas_txn,
        )
