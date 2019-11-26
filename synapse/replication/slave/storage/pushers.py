# -*- coding: utf-8 -*-
# Copyright 2016 OpenMarket Ltd
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

from synapse.storage.data_stores.main.pusher import PusherWorkerStore

from ._base import BaseSlavedStore
from ._slaved_id_tracker import SlavedIdTracker


class SlavedPusherStore(PusherWorkerStore, BaseSlavedStore):
    def __init__(self, db_conn, hs):
        super(SlavedPusherStore, self).__init__(db_conn, hs)
        self._pushers_id_gen = SlavedIdTracker(
            db_conn, "pushers", "id", extra_tables=[("deleted_pushers", "stream_id")]
        )

    def stream_positions(self):
        result = super(SlavedPusherStore, self).stream_positions()
        result["pushers"] = self._pushers_id_gen.get_current_token()
        return result

    def process_replication_rows(self, stream_name, token, rows):
        if stream_name == "pushers":
            self._pushers_id_gen.advance(token)
        return super(SlavedPusherStore, self).process_replication_rows(
            stream_name, token, rows
        )
