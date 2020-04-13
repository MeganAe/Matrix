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
from synapse.replication.tcp.streams._base import ReceiptsStream

from tests.replication.tcp.streams._base import BaseStreamTestCase

USER_ID = "@feeling:blue"


class ReceiptsStreamTestCase(BaseStreamTestCase):
    def test_receipt(self):
        self.reconnect()

        # make the client subscribe to the receipts stream
        self.test_handler.streams.add("receipts")

        # tell the master to send a new receipt
        self.get_success(
            self.hs.get_datastore().insert_receipt(
                "!room:blue", "m.read", USER_ID, ["$event:blue"], {"a": 1}
            )
        )
        self.replicate()

        # there should be one RDATA command
        self.test_handler.on_rdata.assert_called_once()
        stream_name, token, rdata_rows = self.test_handler.on_rdata.call_args[0]
        self.assertEqual(stream_name, "receipts")
        self.assertEqual(1, len(rdata_rows))
        row = rdata_rows[0]  # type: ReceiptsStream.ReceiptsStreamRow
        self.assertEqual("!room:blue", row.room_id)
        self.assertEqual("m.read", row.receipt_type)
        self.assertEqual(USER_ID, row.user_id)
        self.assertEqual("$event:blue", row.event_id)
        self.assertEqual({"a": 1}, row.data)

        # Now let's disconnect and insert some data.
        self.disconnect()

        self.test_handler.on_rdata.reset_mock()

        self.get_success(
            self.hs.get_datastore().insert_receipt(
                "!room2:blue", "m.read", USER_ID, ["$event2:foo"], {"a": 2}
            )
        )
        self.replicate()

        # Nothing should have happened as we are disconnected
        self.test_handler.on_rdata.assert_not_called()

        self.reconnect()
        self.pump(0.1)

        # We should now have caught up and get the missing data
        self.test_handler.on_rdata.assert_called_once()
        stream_name, token, rdata_rows = self.test_handler.on_rdata.call_args[0]
        self.assertEqual(stream_name, "receipts")
        self.assertEqual(token, 3)
        self.assertEqual(1, len(rdata_rows))

        row = rdata_rows[0]  # type: ReceiptsStream.ReceiptsStreamRow
        self.assertEqual("!room2:blue", row.room_id)
        self.assertEqual("m.read", row.receipt_type)
        self.assertEqual(USER_ID, row.user_id)
        self.assertEqual("$event2:foo", row.event_id)
        self.assertEqual({"a": 2}, row.data)
