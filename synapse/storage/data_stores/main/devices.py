# -*- coding: utf-8 -*-
# Copyright 2016 OpenMarket Ltd
# Copyright 2019 New Vector Ltd
# Copyright 2019 The Matrix.org Foundation C.I.C.
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

from six import iteritems

from canonicaljson import json

from twisted.internet import defer

from synapse.api.errors import Codes, StoreError
from synapse.logging.opentracing import (
    get_active_span_text_map,
    set_tag,
    trace,
    whitelisted_homeserver,
)
from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.storage._base import SQLBaseStore, db_to_json, make_in_list_sql_clause
from synapse.storage.database import Database
from synapse.types import get_verify_key_from_cross_signing_key
from synapse.util.caches.descriptors import (
    Cache,
    cached,
    cachedInlineCallbacks,
    cachedList,
)
from synapse.util.iterutils import batch_iter

logger = logging.getLogger(__name__)

DROP_DEVICE_LIST_STREAMS_NON_UNIQUE_INDEXES = (
    "drop_device_list_streams_non_unique_indexes"
)


class DeviceWorkerStore(SQLBaseStore):
    def get_device(self, user_id, device_id):
        """Retrieve a device. Only returns devices that are not marked as
        hidden.

        Args:
            user_id (str): The ID of the user which owns the device
            device_id (str): The ID of the device to retrieve
        Returns:
            defer.Deferred for a dict containing the device information
        Raises:
            StoreError: if the device is not found
        """
        return self.db.simple_select_one(
            table="devices",
            keyvalues={"user_id": user_id, "device_id": device_id, "hidden": False},
            retcols=("user_id", "device_id", "display_name"),
            desc="get_device",
        )

    @defer.inlineCallbacks
    def get_devices_by_user(self, user_id):
        """Retrieve all of a user's registered devices. Only returns devices
        that are not marked as hidden.

        Args:
            user_id (str):
        Returns:
            defer.Deferred: resolves to a dict from device_id to a dict
            containing "device_id", "user_id" and "display_name" for each
            device.
        """
        devices = yield self.db.simple_select_list(
            table="devices",
            keyvalues={"user_id": user_id, "hidden": False},
            retcols=("user_id", "device_id", "display_name"),
            desc="get_devices_by_user",
        )

        return {d["device_id"]: d for d in devices}

    @trace
    @defer.inlineCallbacks
    def get_device_updates_by_remote(self, destination, from_stream_id, limit):
        """Get a stream of device updates to send to the given remote server.

        Args:
            destination (str): The host the device updates are intended for
            from_stream_id (int): The minimum stream_id to filter updates by, exclusive
            limit (int): Maximum number of device updates to return
        Returns:
            Deferred[tuple[int, list[tuple[string,dict]]]]:
                current stream id (ie, the stream id of the last update included in the
                response), and the list of updates, where each update is a pair of EDU
                type and EDU contents
        """
        now_stream_id = self._device_list_id_gen.get_current_token()

        has_changed = self._device_list_federation_stream_cache.has_entity_changed(
            destination, int(from_stream_id)
        )
        if not has_changed:
            return now_stream_id, []

        # We retrieve n+1 devices from the list of outbound pokes where n is
        # our outbound device update limit. We then check if the very last
        # device has the same stream_id as the second-to-last device. If so,
        # then we ignore all devices with that stream_id and only send the
        # devices with a lower stream_id.
        #
        # If when culling the list we end up with no devices afterwards, we
        # consider the device update to be too large, and simply skip the
        # stream_id; the rationale being that such a large device list update
        # is likely an error.
        updates = yield self.db.runInteraction(
            "get_device_updates_by_remote",
            self._get_device_updates_by_remote_txn,
            destination,
            from_stream_id,
            now_stream_id,
            limit + 1,
        )

        # Return an empty list if there are no updates
        if not updates:
            return now_stream_id, []

        # get the cross-signing keys of the users in the list, so that we can
        # determine which of the device changes were cross-signing keys
        users = set(r[0] for r in updates)
        master_key_by_user = {}
        self_signing_key_by_user = {}
        for user in users:
            cross_signing_key = yield self.get_e2e_cross_signing_key(user, "master")
            if cross_signing_key:
                key_id, verify_key = get_verify_key_from_cross_signing_key(
                    cross_signing_key
                )
                # verify_key is a VerifyKey from signedjson, which uses
                # .version to denote the portion of the key ID after the
                # algorithm and colon, which is the device ID
                master_key_by_user[user] = {
                    "key_info": cross_signing_key,
                    "device_id": verify_key.version,
                }

            cross_signing_key = yield self.get_e2e_cross_signing_key(
                user, "self_signing"
            )
            if cross_signing_key:
                key_id, verify_key = get_verify_key_from_cross_signing_key(
                    cross_signing_key
                )
                self_signing_key_by_user[user] = {
                    "key_info": cross_signing_key,
                    "device_id": verify_key.version,
                }

        # if we have exceeded the limit, we need to exclude any results with the
        # same stream_id as the last row.
        if len(updates) > limit:
            stream_id_cutoff = updates[-1][2]
            now_stream_id = stream_id_cutoff - 1
        else:
            stream_id_cutoff = None

        # Perform the equivalent of a GROUP BY
        #
        # Iterate through the updates list and copy non-duplicate
        # (user_id, device_id) entries into a map, with the value being
        # the max stream_id across each set of duplicate entries
        #
        # maps (user_id, device_id) -> (stream_id, opentracing_context)
        # as long as their stream_id does not match that of the last row
        #
        # opentracing_context contains the opentracing metadata for the request
        # that created the poke
        #
        # The most recent request's opentracing_context is used as the
        # context which created the Edu.

        query_map = {}
        cross_signing_keys_by_user = {}
        for user_id, device_id, update_stream_id, update_context in updates:
            if stream_id_cutoff is not None and update_stream_id >= stream_id_cutoff:
                # Stop processing updates
                break

            if (
                user_id in master_key_by_user
                and device_id == master_key_by_user[user_id]["device_id"]
            ):
                result = cross_signing_keys_by_user.setdefault(user_id, {})
                result["master_key"] = master_key_by_user[user_id]["key_info"]
            elif (
                user_id in self_signing_key_by_user
                and device_id == self_signing_key_by_user[user_id]["device_id"]
            ):
                result = cross_signing_keys_by_user.setdefault(user_id, {})
                result["self_signing_key"] = self_signing_key_by_user[user_id][
                    "key_info"
                ]
            else:
                key = (user_id, device_id)

                previous_update_stream_id, _ = query_map.get(key, (0, None))

                if update_stream_id > previous_update_stream_id:
                    query_map[key] = (update_stream_id, update_context)

        # If we didn't find any updates with a stream_id lower than the cutoff, it
        # means that there are more than limit updates all of which have the same
        # steam_id.

        # That should only happen if a client is spamming the server with new
        # devices, in which case E2E isn't going to work well anyway. We'll just
        # skip that stream_id and return an empty list, and continue with the next
        # stream_id next time.
        if not query_map and not cross_signing_keys_by_user:
            return stream_id_cutoff, []

        results = yield self._get_device_update_edus_by_remote(
            destination, from_stream_id, query_map
        )

        # add the updated cross-signing keys to the results list
        for user_id, result in iteritems(cross_signing_keys_by_user):
            result["user_id"] = user_id
            # FIXME: switch to m.signing_key_update when MSC1756 is merged into the spec
            results.append(("org.matrix.signing_key_update", result))

        return now_stream_id, results

    def _get_device_updates_by_remote_txn(
        self, txn, destination, from_stream_id, now_stream_id, limit
    ):
        """Return device update information for a given remote destination

        Args:
            txn (LoggingTransaction): The transaction to execute
            destination (str): The host the device updates are intended for
            from_stream_id (int): The minimum stream_id to filter updates by, exclusive
            now_stream_id (int): The maximum stream_id to filter updates by, inclusive
            limit (int): Maximum number of device updates to return

        Returns:
            List: List of device updates
        """
        # get the list of device updates that need to be sent
        sql = """
            SELECT user_id, device_id, stream_id, opentracing_context FROM device_lists_outbound_pokes
            WHERE destination = ? AND ? < stream_id AND stream_id <= ? AND sent = ?
            ORDER BY stream_id
            LIMIT ?
        """
        txn.execute(sql, (destination, from_stream_id, now_stream_id, False, limit))

        return list(txn)

    @defer.inlineCallbacks
    def _get_device_update_edus_by_remote(self, destination, from_stream_id, query_map):
        """Returns a list of device update EDUs as well as E2EE keys

        Args:
            destination (str): The host the device updates are intended for
            from_stream_id (int): The minimum stream_id to filter updates by, exclusive
            query_map (Dict[(str, str): (int, str|None)]): Dictionary mapping
                user_id/device_id to update stream_id and the relevent json-encoded
                opentracing context

        Returns:
            List[Dict]: List of objects representing an device update EDU

        """
        devices = (
            yield self.db.runInteraction(
                "_get_e2e_device_keys_txn",
                self._get_e2e_device_keys_txn,
                query_map.keys(),
                include_all_devices=True,
                include_deleted_devices=True,
            )
            if query_map
            else {}
        )

        results = []
        for user_id, user_devices in iteritems(devices):
            # The prev_id for the first row is always the last row before
            # `from_stream_id`
            prev_id = yield self._get_last_device_update_for_remote_user(
                destination, user_id, from_stream_id
            )
            for device_id, device in iteritems(user_devices):
                stream_id, opentracing_context = query_map[(user_id, device_id)]
                result = {
                    "user_id": user_id,
                    "device_id": device_id,
                    "prev_id": [prev_id] if prev_id else [],
                    "stream_id": stream_id,
                    "org.matrix.opentracing_context": opentracing_context,
                }

                prev_id = stream_id

                if device is not None:
                    key_json = device.get("key_json", None)
                    if key_json:
                        result["keys"] = db_to_json(key_json)
                    device_display_name = device.get("device_display_name", None)
                    if device_display_name:
                        result["device_display_name"] = device_display_name
                else:
                    result["deleted"] = True

                results.append(("m.device_list_update", result))

        return results

    def _get_last_device_update_for_remote_user(
        self, destination, user_id, from_stream_id
    ):
        def f(txn):
            prev_sent_id_sql = """
                SELECT coalesce(max(stream_id), 0) as stream_id
                FROM device_lists_outbound_last_success
                WHERE destination = ? AND user_id = ? AND stream_id <= ?
            """
            txn.execute(prev_sent_id_sql, (destination, user_id, from_stream_id))
            rows = txn.fetchall()
            return rows[0][0]

        return self.db.runInteraction("get_last_device_update_for_remote_user", f)

    def mark_as_sent_devices_by_remote(self, destination, stream_id):
        """Mark that updates have successfully been sent to the destination.
        """
        return self.db.runInteraction(
            "mark_as_sent_devices_by_remote",
            self._mark_as_sent_devices_by_remote_txn,
            destination,
            stream_id,
        )

    def _mark_as_sent_devices_by_remote_txn(self, txn, destination, stream_id):
        # We update the device_lists_outbound_last_success with the successfully
        # poked users. We do the join to see which users need to be inserted and
        # which updated.
        sql = """
            SELECT user_id, coalesce(max(o.stream_id), 0), (max(s.stream_id) IS NOT NULL)
            FROM device_lists_outbound_pokes as o
            LEFT JOIN device_lists_outbound_last_success as s
                USING (destination, user_id)
            WHERE destination = ? AND o.stream_id <= ?
            GROUP BY user_id
        """
        txn.execute(sql, (destination, stream_id))
        rows = txn.fetchall()

        sql = """
            UPDATE device_lists_outbound_last_success
            SET stream_id = ?
            WHERE destination = ? AND user_id = ?
        """
        txn.executemany(sql, ((row[1], destination, row[0]) for row in rows if row[2]))

        sql = """
            INSERT INTO device_lists_outbound_last_success
            (destination, user_id, stream_id) VALUES (?, ?, ?)
        """
        txn.executemany(
            sql, ((destination, row[0], row[1]) for row in rows if not row[2])
        )

        # Delete all sent outbound pokes
        sql = """
            DELETE FROM device_lists_outbound_pokes
            WHERE destination = ? AND stream_id <= ?
        """
        txn.execute(sql, (destination, stream_id))

    @defer.inlineCallbacks
    def add_user_signature_change_to_streams(self, from_user_id, user_ids):
        """Persist that a user has made new signatures

        Args:
            from_user_id (str): the user who made the signatures
            user_ids (list[str]): the users who were signed
        """

        with self._device_list_id_gen.get_next() as stream_id:
            yield self.db.runInteraction(
                "add_user_sig_change_to_streams",
                self._add_user_signature_change_txn,
                from_user_id,
                user_ids,
                stream_id,
            )
        return stream_id

    def _add_user_signature_change_txn(self, txn, from_user_id, user_ids, stream_id):
        txn.call_after(
            self._user_signature_stream_cache.entity_has_changed,
            from_user_id,
            stream_id,
        )
        self.db.simple_insert_txn(
            txn,
            "user_signature_stream",
            values={
                "stream_id": stream_id,
                "from_user_id": from_user_id,
                "user_ids": json.dumps(user_ids),
            },
        )

    def get_device_stream_token(self):
        return self._device_list_id_gen.get_current_token()

    @trace
    @defer.inlineCallbacks
    def get_user_devices_from_cache(self, query_list):
        """Get the devices (and keys if any) for remote users from the cache.

        Args:
            query_list(list): List of (user_id, device_ids), if device_ids is
                falsey then return all device ids for that user.

        Returns:
            (user_ids_not_in_cache, results_map), where user_ids_not_in_cache is
            a set of user_ids and results_map is a mapping of
            user_id -> device_id -> device_info
        """
        user_ids = set(user_id for user_id, _ in query_list)
        user_map = yield self.get_device_list_last_stream_id_for_remotes(list(user_ids))
        user_ids_in_cache = set(
            user_id for user_id, stream_id in user_map.items() if stream_id
        )
        user_ids_not_in_cache = user_ids - user_ids_in_cache

        results = {}
        for user_id, device_id in query_list:
            if user_id not in user_ids_in_cache:
                continue

            if device_id:
                device = yield self._get_cached_user_device(user_id, device_id)
                results.setdefault(user_id, {})[device_id] = device
            else:
                results[user_id] = yield self._get_cached_devices_for_user(user_id)

        set_tag("in_cache", results)
        set_tag("not_in_cache", user_ids_not_in_cache)

        return user_ids_not_in_cache, results

    @cachedInlineCallbacks(num_args=2, tree=True)
    def _get_cached_user_device(self, user_id, device_id):
        content = yield self.db.simple_select_one_onecol(
            table="device_lists_remote_cache",
            keyvalues={"user_id": user_id, "device_id": device_id},
            retcol="content",
            desc="_get_cached_user_device",
        )
        return db_to_json(content)

    @cachedInlineCallbacks()
    def _get_cached_devices_for_user(self, user_id):
        devices = yield self.db.simple_select_list(
            table="device_lists_remote_cache",
            keyvalues={"user_id": user_id},
            retcols=("device_id", "content"),
            desc="_get_cached_devices_for_user",
        )
        return {
            device["device_id"]: db_to_json(device["content"]) for device in devices
        }

    def get_devices_with_keys_by_user(self, user_id):
        """Get all devices (with any device keys) for a user

        Returns:
            (stream_id, devices)
        """
        return self.db.runInteraction(
            "get_devices_with_keys_by_user",
            self._get_devices_with_keys_by_user_txn,
            user_id,
        )

    def _get_devices_with_keys_by_user_txn(self, txn, user_id):
        now_stream_id = self._device_list_id_gen.get_current_token()

        devices = self._get_e2e_device_keys_txn(
            txn, [(user_id, None)], include_all_devices=True
        )

        if devices:
            user_devices = devices[user_id]
            results = []
            for device_id, device in iteritems(user_devices):
                result = {"device_id": device_id}

                key_json = device.get("key_json", None)
                if key_json:
                    result["keys"] = db_to_json(key_json)
                device_display_name = device.get("device_display_name", None)
                if device_display_name:
                    result["device_display_name"] = device_display_name

                results.append(result)

            return now_stream_id, results

        return now_stream_id, []

    def get_users_whose_devices_changed(self, from_key, user_ids):
        """Get set of users whose devices have changed since `from_key` that
        are in the given list of user_ids.

        Args:
            from_key (str): The device lists stream token
            user_ids (Iterable[str])

        Returns:
            Deferred[set[str]]: The set of user_ids whose devices have changed
            since `from_key`
        """
        from_key = int(from_key)

        # Get set of users who *may* have changed. Users not in the returned
        # list have definitely not changed.
        to_check = list(
            self._device_list_stream_cache.get_entities_changed(user_ids, from_key)
        )

        if not to_check:
            return defer.succeed(set())

        def _get_users_whose_devices_changed_txn(txn):
            changes = set()

            sql = """
                SELECT DISTINCT user_id FROM device_lists_stream
                WHERE stream_id > ?
                AND
            """

            for chunk in batch_iter(to_check, 100):
                clause, args = make_in_list_sql_clause(
                    txn.database_engine, "user_id", chunk
                )
                txn.execute(sql + clause, (from_key,) + tuple(args))
                changes.update(user_id for user_id, in txn)

            return changes

        return self.db.runInteraction(
            "get_users_whose_devices_changed", _get_users_whose_devices_changed_txn
        )

    @defer.inlineCallbacks
    def get_users_whose_signatures_changed(self, user_id, from_key):
        """Get the users who have new cross-signing signatures made by `user_id` since
        `from_key`.

        Args:
            user_id (str): the user who made the signatures
            from_key (str): The device lists stream token
        """
        from_key = int(from_key)
        if self._user_signature_stream_cache.has_entity_changed(user_id, from_key):
            sql = """
                SELECT DISTINCT user_ids FROM user_signature_stream
                WHERE from_user_id = ? AND stream_id > ?
            """
            rows = yield self.db.execute(
                "get_users_whose_signatures_changed", None, sql, user_id, from_key
            )
            return set(user for row in rows for user in json.loads(row[0]))
        else:
            return set()

    def get_all_device_list_changes_for_remotes(self, from_key, to_key):
        """Return a list of `(stream_id, user_id, destination)` which is the
        combined list of changes to devices, and which destinations need to be
        poked. `destination` may be None if no destinations need to be poked.
        """
        # We do a group by here as there can be a large number of duplicate
        # entries, since we throw away device IDs.
        sql = """
            SELECT MAX(stream_id) AS stream_id, user_id, destination
            FROM device_lists_stream
            LEFT JOIN device_lists_outbound_pokes USING (stream_id, user_id, device_id)
            WHERE ? < stream_id AND stream_id <= ?
            GROUP BY user_id, destination
        """
        return self.db.execute(
            "get_all_device_list_changes_for_remotes", None, sql, from_key, to_key
        )

    @cached(max_entries=10000)
    def get_device_list_last_stream_id_for_remote(self, user_id):
        """Get the last stream_id we got for a user. May be None if we haven't
        got any information for them.
        """
        return self.db.simple_select_one_onecol(
            table="device_lists_remote_extremeties",
            keyvalues={"user_id": user_id},
            retcol="stream_id",
            desc="get_device_list_last_stream_id_for_remote",
            allow_none=True,
        )

    @cachedList(
        cached_method_name="get_device_list_last_stream_id_for_remote",
        list_name="user_ids",
        inlineCallbacks=True,
    )
    def get_device_list_last_stream_id_for_remotes(self, user_ids):
        rows = yield self.db.simple_select_many_batch(
            table="device_lists_remote_extremeties",
            column="user_id",
            iterable=user_ids,
            retcols=("user_id", "stream_id"),
            desc="get_device_list_last_stream_id_for_remotes",
        )

        results = {user_id: None for user_id in user_ids}
        results.update({row["user_id"]: row["stream_id"] for row in rows})

        return results


class DeviceBackgroundUpdateStore(SQLBaseStore):
    def __init__(self, database: Database, db_conn, hs):
        super(DeviceBackgroundUpdateStore, self).__init__(database, db_conn, hs)

        self.db.updates.register_background_index_update(
            "device_lists_stream_idx",
            index_name="device_lists_stream_user_id",
            table="device_lists_stream",
            columns=["user_id", "device_id"],
        )

        # create a unique index on device_lists_remote_cache
        self.db.updates.register_background_index_update(
            "device_lists_remote_cache_unique_idx",
            index_name="device_lists_remote_cache_unique_id",
            table="device_lists_remote_cache",
            columns=["user_id", "device_id"],
            unique=True,
        )

        # And one on device_lists_remote_extremeties
        self.db.updates.register_background_index_update(
            "device_lists_remote_extremeties_unique_idx",
            index_name="device_lists_remote_extremeties_unique_idx",
            table="device_lists_remote_extremeties",
            columns=["user_id"],
            unique=True,
        )

        # once they complete, we can remove the old non-unique indexes.
        self.db.updates.register_background_update_handler(
            DROP_DEVICE_LIST_STREAMS_NON_UNIQUE_INDEXES,
            self._drop_device_list_streams_non_unique_indexes,
        )

    @defer.inlineCallbacks
    def _drop_device_list_streams_non_unique_indexes(self, progress, batch_size):
        def f(conn):
            txn = conn.cursor()
            txn.execute("DROP INDEX IF EXISTS device_lists_remote_cache_id")
            txn.execute("DROP INDEX IF EXISTS device_lists_remote_extremeties_id")
            txn.close()

        yield self.db.runWithConnection(f)
        yield self.db.updates._end_background_update(
            DROP_DEVICE_LIST_STREAMS_NON_UNIQUE_INDEXES
        )
        return 1


class DeviceStore(DeviceWorkerStore, DeviceBackgroundUpdateStore):
    def __init__(self, database: Database, db_conn, hs):
        super(DeviceStore, self).__init__(database, db_conn, hs)

        # Map of (user_id, device_id) -> bool. If there is an entry that implies
        # the device exists.
        self.device_id_exists_cache = Cache(
            name="device_id_exists", keylen=2, max_entries=10000
        )

        self._clock.looping_call(self._prune_old_outbound_device_pokes, 60 * 60 * 1000)

    @defer.inlineCallbacks
    def store_device(self, user_id, device_id, initial_device_display_name):
        """Ensure the given device is known; add it to the store if not

        Args:
            user_id (str): id of user associated with the device
            device_id (str): id of device
            initial_device_display_name (str): initial displayname of the
               device. Ignored if device exists.
        Returns:
            defer.Deferred: boolean whether the device was inserted or an
                existing device existed with that ID.
        Raises:
            StoreError: if the device is already in use
        """
        key = (user_id, device_id)
        if self.device_id_exists_cache.get(key, None):
            return False

        try:
            inserted = yield self.db.simple_insert(
                "devices",
                values={
                    "user_id": user_id,
                    "device_id": device_id,
                    "display_name": initial_device_display_name,
                    "hidden": False,
                },
                desc="store_device",
                or_ignore=True,
            )
            if not inserted:
                # if the device already exists, check if it's a real device, or
                # if the device ID is reserved by something else
                hidden = yield self.db.simple_select_one_onecol(
                    "devices",
                    keyvalues={"user_id": user_id, "device_id": device_id},
                    retcol="hidden",
                )
                if hidden:
                    raise StoreError(400, "The device ID is in use", Codes.FORBIDDEN)
            self.device_id_exists_cache.prefill(key, True)
            return inserted
        except StoreError:
            raise
        except Exception as e:
            logger.error(
                "store_device with device_id=%s(%r) user_id=%s(%r)"
                " display_name=%s(%r) failed: %s",
                type(device_id).__name__,
                device_id,
                type(user_id).__name__,
                user_id,
                type(initial_device_display_name).__name__,
                initial_device_display_name,
                e,
            )
            raise StoreError(500, "Problem storing device.")

    @defer.inlineCallbacks
    def delete_device(self, user_id, device_id):
        """Delete a device.

        Args:
            user_id (str): The ID of the user which owns the device
            device_id (str): The ID of the device to delete
        Returns:
            defer.Deferred
        """
        yield self.db.simple_delete_one(
            table="devices",
            keyvalues={"user_id": user_id, "device_id": device_id, "hidden": False},
            desc="delete_device",
        )

        self.device_id_exists_cache.invalidate((user_id, device_id))

    @defer.inlineCallbacks
    def delete_devices(self, user_id, device_ids):
        """Deletes several devices.

        Args:
            user_id (str): The ID of the user which owns the devices
            device_ids (list): The IDs of the devices to delete
        Returns:
            defer.Deferred
        """
        yield self.db.simple_delete_many(
            table="devices",
            column="device_id",
            iterable=device_ids,
            keyvalues={"user_id": user_id, "hidden": False},
            desc="delete_devices",
        )
        for device_id in device_ids:
            self.device_id_exists_cache.invalidate((user_id, device_id))

    def update_device(self, user_id, device_id, new_display_name=None):
        """Update a device. Only updates the device if it is not marked as
        hidden.

        Args:
            user_id (str): The ID of the user which owns the device
            device_id (str): The ID of the device to update
            new_display_name (str|None): new displayname for device; None
               to leave unchanged
        Raises:
            StoreError: if the device is not found
        Returns:
            defer.Deferred
        """
        updates = {}
        if new_display_name is not None:
            updates["display_name"] = new_display_name
        if not updates:
            return defer.succeed(None)
        return self.db.simple_update_one(
            table="devices",
            keyvalues={"user_id": user_id, "device_id": device_id, "hidden": False},
            updatevalues=updates,
            desc="update_device",
        )

    @defer.inlineCallbacks
    def mark_remote_user_device_list_as_unsubscribed(self, user_id):
        """Mark that we no longer track device lists for remote user.
        """
        yield self.db.simple_delete(
            table="device_lists_remote_extremeties",
            keyvalues={"user_id": user_id},
            desc="mark_remote_user_device_list_as_unsubscribed",
        )
        self.get_device_list_last_stream_id_for_remote.invalidate((user_id,))

    def update_remote_device_list_cache_entry(
        self, user_id, device_id, content, stream_id
    ):
        """Updates a single device in the cache of a remote user's devicelist.

        Note: assumes that we are the only thread that can be updating this user's
        device list.

        Args:
            user_id (str): User to update device list for
            device_id (str): ID of decivice being updated
            content (dict): new data on this device
            stream_id (int): the version of the device list

        Returns:
            Deferred[None]
        """
        return self.db.runInteraction(
            "update_remote_device_list_cache_entry",
            self._update_remote_device_list_cache_entry_txn,
            user_id,
            device_id,
            content,
            stream_id,
        )

    def _update_remote_device_list_cache_entry_txn(
        self, txn, user_id, device_id, content, stream_id
    ):
        if content.get("deleted"):
            self.db.simple_delete_txn(
                txn,
                table="device_lists_remote_cache",
                keyvalues={"user_id": user_id, "device_id": device_id},
            )

            txn.call_after(self.device_id_exists_cache.invalidate, (user_id, device_id))
        else:
            self.db.simple_upsert_txn(
                txn,
                table="device_lists_remote_cache",
                keyvalues={"user_id": user_id, "device_id": device_id},
                values={"content": json.dumps(content)},
                # we don't need to lock, because we assume we are the only thread
                # updating this user's devices.
                lock=False,
            )

        txn.call_after(self._get_cached_user_device.invalidate, (user_id, device_id))
        txn.call_after(self._get_cached_devices_for_user.invalidate, (user_id,))
        txn.call_after(
            self.get_device_list_last_stream_id_for_remote.invalidate, (user_id,)
        )

        self.db.simple_upsert_txn(
            txn,
            table="device_lists_remote_extremeties",
            keyvalues={"user_id": user_id},
            values={"stream_id": stream_id},
            # again, we can assume we are the only thread updating this user's
            # extremity.
            lock=False,
        )

    def update_remote_device_list_cache(self, user_id, devices, stream_id):
        """Replace the entire cache of the remote user's devices.

        Note: assumes that we are the only thread that can be updating this user's
        device list.

        Args:
            user_id (str): User to update device list for
            devices (list[dict]): list of device objects supplied over federation
            stream_id (int): the version of the device list

        Returns:
            Deferred[None]
        """
        return self.db.runInteraction(
            "update_remote_device_list_cache",
            self._update_remote_device_list_cache_txn,
            user_id,
            devices,
            stream_id,
        )

    def _update_remote_device_list_cache_txn(self, txn, user_id, devices, stream_id):
        self.db.simple_delete_txn(
            txn, table="device_lists_remote_cache", keyvalues={"user_id": user_id}
        )

        self.db.simple_insert_many_txn(
            txn,
            table="device_lists_remote_cache",
            values=[
                {
                    "user_id": user_id,
                    "device_id": content["device_id"],
                    "content": json.dumps(content),
                }
                for content in devices
            ],
        )

        txn.call_after(self._get_cached_devices_for_user.invalidate, (user_id,))
        txn.call_after(self._get_cached_user_device.invalidate_many, (user_id,))
        txn.call_after(
            self.get_device_list_last_stream_id_for_remote.invalidate, (user_id,)
        )

        self.db.simple_upsert_txn(
            txn,
            table="device_lists_remote_extremeties",
            keyvalues={"user_id": user_id},
            values={"stream_id": stream_id},
            # we don't need to lock, because we can assume we are the only thread
            # updating this user's extremity.
            lock=False,
        )

    @defer.inlineCallbacks
    def add_device_change_to_streams(self, user_id, device_ids, hosts):
        """Persist that a user's devices have been updated, and which hosts
        (if any) should be poked.
        """
        with self._device_list_id_gen.get_next() as stream_id:
            yield self.db.runInteraction(
                "add_device_change_to_streams",
                self._add_device_change_txn,
                user_id,
                device_ids,
                hosts,
                stream_id,
            )
        return stream_id

    def _add_device_change_txn(self, txn, user_id, device_ids, hosts, stream_id):
        now = self._clock.time_msec()

        txn.call_after(
            self._device_list_stream_cache.entity_has_changed, user_id, stream_id
        )
        for host in hosts:
            txn.call_after(
                self._device_list_federation_stream_cache.entity_has_changed,
                host,
                stream_id,
            )

        # Delete older entries in the table, as we really only care about
        # when the latest change happened.
        txn.executemany(
            """
            DELETE FROM device_lists_stream
            WHERE user_id = ? AND device_id = ? AND stream_id < ?
            """,
            [(user_id, device_id, stream_id) for device_id in device_ids],
        )

        self.db.simple_insert_many_txn(
            txn,
            table="device_lists_stream",
            values=[
                {"stream_id": stream_id, "user_id": user_id, "device_id": device_id}
                for device_id in device_ids
            ],
        )

        context = get_active_span_text_map()

        self.db.simple_insert_many_txn(
            txn,
            table="device_lists_outbound_pokes",
            values=[
                {
                    "destination": destination,
                    "stream_id": stream_id,
                    "user_id": user_id,
                    "device_id": device_id,
                    "sent": False,
                    "ts": now,
                    "opentracing_context": json.dumps(context)
                    if whitelisted_homeserver(destination)
                    else "{}",
                }
                for destination in hosts
                for device_id in device_ids
            ],
        )

    def _prune_old_outbound_device_pokes(self):
        """Delete old entries out of the device_lists_outbound_pokes to ensure
        that we don't fill up due to dead servers. We keep one entry per
        (destination, user_id) tuple to ensure that the prev_ids remain correct
        if the server does come back.
        """
        yesterday = self._clock.time_msec() - 24 * 60 * 60 * 1000

        def _prune_txn(txn):
            select_sql = """
                SELECT destination, user_id, max(stream_id) as stream_id
                FROM device_lists_outbound_pokes
                GROUP BY destination, user_id
                HAVING min(ts) < ? AND count(*) > 1
            """

            txn.execute(select_sql, (yesterday,))
            rows = txn.fetchall()

            if not rows:
                return

            delete_sql = """
                DELETE FROM device_lists_outbound_pokes
                WHERE ts < ? AND destination = ? AND user_id = ? AND stream_id < ?
            """

            txn.executemany(
                delete_sql, ((yesterday, row[0], row[1], row[2]) for row in rows)
            )

            # Since we've deleted unsent deltas, we need to remove the entry
            # of last successful sent so that the prev_ids are correctly set.
            sql = """
                DELETE FROM device_lists_outbound_last_success
                WHERE destination = ? AND user_id = ?
            """
            txn.executemany(sql, ((row[0], row[1]) for row in rows))

            logger.info("Pruned %d device list outbound pokes", txn.rowcount)

        return run_as_background_process(
            "prune_old_outbound_device_pokes",
            self.db.runInteraction,
            "_prune_old_outbound_device_pokes",
            _prune_txn,
        )
