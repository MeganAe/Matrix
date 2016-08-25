# -*- coding: utf-8 -*-
# Copyright 2016 OpenMarket Ltd
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
import ujson

from twisted.internet import defer

from ._base import SQLBaseStore


logger = logging.getLogger(__name__)


class DeviceInboxStore(SQLBaseStore):

    @defer.inlineCallbacks
    def add_messages_to_device_inbox(self, messages_by_user_then_device):
        """
        Args:
            messages_by_user_and_device(dict):
                Dictionary of user_id to device_id to message.
        Returns:
            A deferred that resolves when the messages have been inserted.
        """

        def select_devices_txn(txn, user_id, devices):
            if not devices:
                return []
            sql = (
                "SELECT user_id, device_id FROM devices"
                " WHERE user_id = ? AND device_id IN ("
                + ",".join("?" * len(devices))
                + ")"
            )
            # TODO: Maybe this needs to be done in batches if there are
            # too many local devices for a given user.
            args = [user_id] + devices
            txn.execute(sql, args)
            return [tuple(row) for row in txn.fetchall()]

        def add_messages_to_device_inbox_txn(txn, stream_id):
            local_users_and_devices = set()
            for user_id, messages_by_device in messages_by_user_then_device.items():
                local_users_and_devices.update(
                    select_devices_txn(txn, user_id, messages_by_device.keys())
                )

            sql = (
                "INSERT INTO device_inbox"
                " (user_id, device_id, stream_id, message_json)"
                " VALUES (?,?,?,?)"
            )
            rows = []
            for user_id, messages_by_device in messages_by_user_then_device.items():
                for device_id, message in messages_by_device.items():
                    message_json = ujson.dumps(message)
                    # Only insert into the local inbox if the device exists on
                    # this server
                    if (user_id, device_id) in local_users_and_devices:
                        rows.append((user_id, device_id, stream_id, message_json))

            txn.executemany(sql, rows)

        with self._device_inbox_id_gen.get_next() as stream_id:
            yield self.runInteraction(
                "add_messages_to_device_inbox",
                add_messages_to_device_inbox_txn,
                stream_id
            )

    def get_new_messages_for_device(
        self, user_id, device_id, current_stream_id, limit=100
    ):
        """
        Args:
            user_id(str): The recipient user_id.
            device_id(str): The recipient device_id.
            current_stream_id(int): The current position of the to device
                message stream.
        Returns:
            Deferred ([dict], int): List of messages for the device and where
                in the stream the messages got to.
        """
        def get_new_messages_for_device_txn(txn):
            sql = (
                "SELECT stream_id, message_json FROM device_inbox"
                " WHERE user_id = ? AND device_id = ?"
                " AND stream_id <= ?"
                " ORDER BY stream_id ASC"
                " LIMIT ?"
            )
            txn.execute(sql, (user_id, device_id, current_stream_id, limit))
            messages = []
            for row in txn.fetchall():
                stream_pos = row[0]
                messages.append(ujson.loads(row[1]))
            if len(messages) < limit:
                stream_pos = current_stream_id
            return (messages, stream_pos)

        return self.runInteraction(
            "get_new_messages_for_device", get_new_messages_for_device_txn,
        )

    def delete_messages_for_device(self, user_id, device_id, up_to_stream_id):
        """
        Args:
            user_id(str): The recipient user_id.
            device_id(str): The recipient device_id.
            up_to_stream_id(int): Where to delete messages up to.
        Returns:
            A deferred that resolves when the messages have been deleted.
        """
        def delete_messages_for_device_txn(txn):
            sql = (
                "DELETE FROM device_inbox"
                " WHERE user_id = ? AND device_id = ?"
                " AND stream_id <= ?"
            )
            txn.execute(sql, (user_id, device_id, up_to_stream_id))

        return self.runInteraction(
            "delete_messages_for_device", delete_messages_for_device_txn
        )

    def get_to_device_stream_token(self):
        return self._device_inbox_id_gen.get_current_token()
