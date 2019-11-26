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

from canonicaljson import json

from twisted.internet import defer

from synapse.logging.opentracing import log_kv, set_tag, trace
from synapse.storage._base import SQLBaseStore, make_in_list_sql_clause
from synapse.storage.background_updates import BackgroundUpdateStore
from synapse.util.caches.expiringcache import ExpiringCache

logger = logging.getLogger(__name__)


class DeviceInboxWorkerStore(SQLBaseStore):
    def get_to_device_stream_token(self):
        return self._device_inbox_id_gen.get_current_token()

    def get_new_messages_for_device(
        self, user_id, device_id, last_stream_id, current_stream_id, limit=100
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
        has_changed = self._device_inbox_stream_cache.has_entity_changed(
            user_id, last_stream_id
        )
        if not has_changed:
            return defer.succeed(([], current_stream_id))

        def get_new_messages_for_device_txn(txn):
            sql = (
                "SELECT stream_id, message_json FROM device_inbox"
                " WHERE user_id = ? AND device_id = ?"
                " AND ? < stream_id AND stream_id <= ?"
                " ORDER BY stream_id ASC"
                " LIMIT ?"
            )
            txn.execute(
                sql, (user_id, device_id, last_stream_id, current_stream_id, limit)
            )
            messages = []
            for row in txn:
                stream_pos = row[0]
                messages.append(json.loads(row[1]))
            if len(messages) < limit:
                stream_pos = current_stream_id
            return messages, stream_pos

        return self.runInteraction(
            "get_new_messages_for_device", get_new_messages_for_device_txn
        )

    @trace
    @defer.inlineCallbacks
    def delete_messages_for_device(self, user_id, device_id, up_to_stream_id):
        """
        Args:
            user_id(str): The recipient user_id.
            device_id(str): The recipient device_id.
            up_to_stream_id(int): Where to delete messages up to.
        Returns:
            A deferred that resolves to the number of messages deleted.
        """
        # If we have cached the last stream id we've deleted up to, we can
        # check if there is likely to be anything that needs deleting
        last_deleted_stream_id = self._last_device_delete_cache.get(
            (user_id, device_id), None
        )

        set_tag("last_deleted_stream_id", last_deleted_stream_id)

        if last_deleted_stream_id:
            has_changed = self._device_inbox_stream_cache.has_entity_changed(
                user_id, last_deleted_stream_id
            )
            if not has_changed:
                log_kv({"message": "No changes in cache since last check"})
                return 0

        def delete_messages_for_device_txn(txn):
            sql = (
                "DELETE FROM device_inbox"
                " WHERE user_id = ? AND device_id = ?"
                " AND stream_id <= ?"
            )
            txn.execute(sql, (user_id, device_id, up_to_stream_id))
            return txn.rowcount

        count = yield self.runInteraction(
            "delete_messages_for_device", delete_messages_for_device_txn
        )

        log_kv(
            {"message": "deleted {} messages for device".format(count), "count": count}
        )

        # Update the cache, ensuring that we only ever increase the value
        last_deleted_stream_id = self._last_device_delete_cache.get(
            (user_id, device_id), 0
        )
        self._last_device_delete_cache[(user_id, device_id)] = max(
            last_deleted_stream_id, up_to_stream_id
        )

        return count

    @trace
    def get_new_device_msgs_for_remote(
        self, destination, last_stream_id, current_stream_id, limit
    ):
        """
        Args:
            destination(str): The name of the remote server.
            last_stream_id(int|long): The last position of the device message stream
                that the server sent up to.
            current_stream_id(int|long): The current position of the device
                message stream.
        Returns:
            Deferred ([dict], int|long): List of messages for the device and where
                in the stream the messages got to.
        """

        set_tag("destination", destination)
        set_tag("last_stream_id", last_stream_id)
        set_tag("current_stream_id", current_stream_id)
        set_tag("limit", limit)

        has_changed = self._device_federation_outbox_stream_cache.has_entity_changed(
            destination, last_stream_id
        )
        if not has_changed or last_stream_id == current_stream_id:
            log_kv({"message": "No new messages in stream"})
            return defer.succeed(([], current_stream_id))

        if limit <= 0:
            # This can happen if we run out of room for EDUs in the transaction.
            return defer.succeed(([], last_stream_id))

        @trace
        def get_new_messages_for_remote_destination_txn(txn):
            sql = (
                "SELECT stream_id, messages_json FROM device_federation_outbox"
                " WHERE destination = ?"
                " AND ? < stream_id AND stream_id <= ?"
                " ORDER BY stream_id ASC"
                " LIMIT ?"
            )
            txn.execute(sql, (destination, last_stream_id, current_stream_id, limit))
            messages = []
            for row in txn:
                stream_pos = row[0]
                messages.append(json.loads(row[1]))
            if len(messages) < limit:
                log_kv({"message": "Set stream position to current position"})
                stream_pos = current_stream_id
            return messages, stream_pos

        return self.runInteraction(
            "get_new_device_msgs_for_remote",
            get_new_messages_for_remote_destination_txn,
        )

    @trace
    def delete_device_msgs_for_remote(self, destination, up_to_stream_id):
        """Used to delete messages when the remote destination acknowledges
        their receipt.

        Args:
            destination(str): The destination server_name
            up_to_stream_id(int): Where to delete messages up to.
        Returns:
            A deferred that resolves when the messages have been deleted.
        """

        def delete_messages_for_remote_destination_txn(txn):
            sql = (
                "DELETE FROM device_federation_outbox"
                " WHERE destination = ?"
                " AND stream_id <= ?"
            )
            txn.execute(sql, (destination, up_to_stream_id))

        return self.runInteraction(
            "delete_device_msgs_for_remote", delete_messages_for_remote_destination_txn
        )


class DeviceInboxBackgroundUpdateStore(BackgroundUpdateStore):
    DEVICE_INBOX_STREAM_ID = "device_inbox_stream_drop"

    def __init__(self, db_conn, hs):
        super(DeviceInboxBackgroundUpdateStore, self).__init__(db_conn, hs)

        self.register_background_index_update(
            "device_inbox_stream_index",
            index_name="device_inbox_stream_id_user_id",
            table="device_inbox",
            columns=["stream_id", "user_id"],
        )

        self.register_background_update_handler(
            self.DEVICE_INBOX_STREAM_ID, self._background_drop_index_device_inbox
        )

    @defer.inlineCallbacks
    def _background_drop_index_device_inbox(self, progress, batch_size):
        def reindex_txn(conn):
            txn = conn.cursor()
            txn.execute("DROP INDEX IF EXISTS device_inbox_stream_id")
            txn.close()

        yield self.runWithConnection(reindex_txn)

        yield self._end_background_update(self.DEVICE_INBOX_STREAM_ID)

        return 1


class DeviceInboxStore(DeviceInboxWorkerStore, DeviceInboxBackgroundUpdateStore):
    DEVICE_INBOX_STREAM_ID = "device_inbox_stream_drop"

    def __init__(self, db_conn, hs):
        super(DeviceInboxStore, self).__init__(db_conn, hs)

        # Map of (user_id, device_id) to the last stream_id that has been
        # deleted up to. This is so that we can no op deletions.
        self._last_device_delete_cache = ExpiringCache(
            cache_name="last_device_delete_cache",
            clock=self._clock,
            max_len=10000,
            expiry_ms=30 * 60 * 1000,
        )

    @trace
    @defer.inlineCallbacks
    def add_messages_to_device_inbox(
        self, local_messages_by_user_then_device, remote_messages_by_destination
    ):
        """Used to send messages from this server.

        Args:
            sender_user_id(str): The ID of the user sending these messages.
            local_messages_by_user_and_device(dict):
                Dictionary of user_id to device_id to message.
            remote_messages_by_destination(dict):
                Dictionary of destination server_name to the EDU JSON to send.
        Returns:
            A deferred stream_id that resolves when the messages have been
            inserted.
        """

        def add_messages_txn(txn, now_ms, stream_id):
            # Add the local messages directly to the local inbox.
            self._add_messages_to_local_device_inbox_txn(
                txn, stream_id, local_messages_by_user_then_device
            )

            # Add the remote messages to the federation outbox.
            # We'll send them to a remote server when we next send a
            # federation transaction to that destination.
            sql = (
                "INSERT INTO device_federation_outbox"
                " (destination, stream_id, queued_ts, messages_json)"
                " VALUES (?,?,?,?)"
            )
            rows = []
            for destination, edu in remote_messages_by_destination.items():
                edu_json = json.dumps(edu)
                rows.append((destination, stream_id, now_ms, edu_json))
            txn.executemany(sql, rows)

        with self._device_inbox_id_gen.get_next() as stream_id:
            now_ms = self.clock.time_msec()
            yield self.runInteraction(
                "add_messages_to_device_inbox", add_messages_txn, now_ms, stream_id
            )
            for user_id in local_messages_by_user_then_device.keys():
                self._device_inbox_stream_cache.entity_has_changed(user_id, stream_id)
            for destination in remote_messages_by_destination.keys():
                self._device_federation_outbox_stream_cache.entity_has_changed(
                    destination, stream_id
                )

        return self._device_inbox_id_gen.get_current_token()

    @defer.inlineCallbacks
    def add_messages_from_remote_to_device_inbox(
        self, origin, message_id, local_messages_by_user_then_device
    ):
        def add_messages_txn(txn, now_ms, stream_id):
            # Check if we've already inserted a matching message_id for that
            # origin. This can happen if the origin doesn't receive our
            # acknowledgement from the first time we received the message.
            already_inserted = self._simple_select_one_txn(
                txn,
                table="device_federation_inbox",
                keyvalues={"origin": origin, "message_id": message_id},
                retcols=("message_id",),
                allow_none=True,
            )
            if already_inserted is not None:
                return

            # Add an entry for this message_id so that we know we've processed
            # it.
            self._simple_insert_txn(
                txn,
                table="device_federation_inbox",
                values={
                    "origin": origin,
                    "message_id": message_id,
                    "received_ts": now_ms,
                },
            )

            # Add the messages to the approriate local device inboxes so that
            # they'll be sent to the devices when they next sync.
            self._add_messages_to_local_device_inbox_txn(
                txn, stream_id, local_messages_by_user_then_device
            )

        with self._device_inbox_id_gen.get_next() as stream_id:
            now_ms = self.clock.time_msec()
            yield self.runInteraction(
                "add_messages_from_remote_to_device_inbox",
                add_messages_txn,
                now_ms,
                stream_id,
            )
            for user_id in local_messages_by_user_then_device.keys():
                self._device_inbox_stream_cache.entity_has_changed(user_id, stream_id)

        return stream_id

    def _add_messages_to_local_device_inbox_txn(
        self, txn, stream_id, messages_by_user_then_device
    ):
        # Compatible method of performing an upsert
        sql = "SELECT stream_id FROM device_max_stream_id"

        txn.execute(sql)
        rows = txn.fetchone()
        if rows:
            db_stream_id = rows[0]
            if db_stream_id < stream_id:
                # Insert the new stream_id
                sql = "UPDATE device_max_stream_id SET stream_id = ?"
        else:
            # No rows, perform an insert
            sql = "INSERT INTO device_max_stream_id (stream_id) VALUES (?)"

        txn.execute(sql, (stream_id,))

        local_by_user_then_device = {}
        for user_id, messages_by_device in messages_by_user_then_device.items():
            messages_json_for_user = {}
            devices = list(messages_by_device.keys())
            if len(devices) == 1 and devices[0] == "*":
                # Handle wildcard device_ids.
                sql = "SELECT device_id FROM devices WHERE user_id = ?"
                txn.execute(sql, (user_id,))
                message_json = json.dumps(messages_by_device["*"])
                for row in txn:
                    # Add the message for all devices for this user on this
                    # server.
                    device = row[0]
                    messages_json_for_user[device] = message_json
            else:
                if not devices:
                    continue

                clause, args = make_in_list_sql_clause(
                    txn.database_engine, "device_id", devices
                )
                sql = "SELECT device_id FROM devices WHERE user_id = ? AND " + clause

                # TODO: Maybe this needs to be done in batches if there are
                # too many local devices for a given user.
                txn.execute(sql, [user_id] + list(args))
                for row in txn:
                    # Only insert into the local inbox if the device exists on
                    # this server
                    device = row[0]
                    message_json = json.dumps(messages_by_device[device])
                    messages_json_for_user[device] = message_json

            if messages_json_for_user:
                local_by_user_then_device[user_id] = messages_json_for_user

        if not local_by_user_then_device:
            return

        sql = (
            "INSERT INTO device_inbox"
            " (user_id, device_id, stream_id, message_json)"
            " VALUES (?,?,?,?)"
        )
        rows = []
        for user_id, messages_by_device in local_by_user_then_device.items():
            for device_id, message_json in messages_by_device.items():
                rows.append((user_id, device_id, stream_id, message_json))

        txn.executemany(sql, rows)

    def get_all_new_device_messages(self, last_pos, current_pos, limit):
        """
        Args:
            last_pos(int):
            current_pos(int):
            limit(int):
        Returns:
            A deferred list of rows from the device inbox
        """
        if last_pos == current_pos:
            return defer.succeed([])

        def get_all_new_device_messages_txn(txn):
            # We limit like this as we might have multiple rows per stream_id, and
            # we want to make sure we always get all entries for any stream_id
            # we return.
            upper_pos = min(current_pos, last_pos + limit)
            sql = (
                "SELECT max(stream_id), user_id"
                " FROM device_inbox"
                " WHERE ? < stream_id AND stream_id <= ?"
                " GROUP BY user_id"
            )
            txn.execute(sql, (last_pos, upper_pos))
            rows = txn.fetchall()

            sql = (
                "SELECT max(stream_id), destination"
                " FROM device_federation_outbox"
                " WHERE ? < stream_id AND stream_id <= ?"
                " GROUP BY destination"
            )
            txn.execute(sql, (last_pos, upper_pos))
            rows.extend(txn)

            # Order by ascending stream ordering
            rows.sort()

            return rows

        return self.runInteraction(
            "get_all_new_device_messages", get_all_new_device_messages_txn
        )
