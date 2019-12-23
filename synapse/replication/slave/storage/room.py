# -*- coding: utf-8 -*-
# Copyright 2015, 2016 OpenMarket Ltd
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

from synapse.storage.data_stores.main.room import RoomWorkerStore
from synapse.storage.database import Database

from ._base import BaseSlavedStore
from ._slaved_id_tracker import SlavedIdTracker


class RoomStore(RoomWorkerStore, BaseSlavedStore):
    def __init__(self, database: Database, db_conn, hs):
        super(RoomStore, self).__init__(database, db_conn, hs)
        self._public_room_id_gen = SlavedIdTracker(
            db_conn, "public_room_list_stream", "stream_id"
        )

    def get_current_public_room_stream_id(self):
        return self._public_room_id_gen.get_current_token()

    def stream_positions(self):
        result = super(RoomStore, self).stream_positions()
        result["public_rooms"] = self._public_room_id_gen.get_current_token()
        return result

    def process_replication_rows(self, stream_name, token, rows):
        if stream_name == "public_rooms":
            self._public_room_id_gen.advance(token)

        return super(RoomStore, self).process_replication_rows(stream_name, token, rows)
