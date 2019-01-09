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

from six import iteritems

from twisted.internet import defer

from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.util.caches import CACHE_SIZE_FACTOR

from . import background_updates
from ._base import Cache

logger = logging.getLogger(__name__)

# Number of msec of granularity to store the user IP 'last seen' time. Smaller
# times give more inserts into the database even for readonly API hits
# 120 seconds == 2 minutes
LAST_SEEN_GRANULARITY = 120 * 1000


class ClientIpStore(background_updates.BackgroundUpdateStore):
    def __init__(self, db_conn, hs):

        self.client_ip_last_seen = Cache(
            name="client_ip_last_seen",
            keylen=4,
            max_entries=50000 * CACHE_SIZE_FACTOR,
        )

        super(ClientIpStore, self).__init__(db_conn, hs)

        self.register_background_index_update(
            "user_ips_device_index",
            index_name="user_ips_device_id",
            table="user_ips",
            columns=["user_id", "device_id", "last_seen"],
        )

        self.register_background_index_update(
            "user_ips_last_seen_index",
            index_name="user_ips_last_seen",
            table="user_ips",
            columns=["user_id", "last_seen"],
        )

        self.register_background_index_update(
            "user_ips_last_seen_only_index",
            index_name="user_ips_last_seen_only",
            table="user_ips",
            columns=["last_seen"],
        )

        self.register_background_update_handler(
            "user_ips_remove_dupes",
            self._remove_user_ip_dupes,
        )

        # Register a unique index
        self.register_background_index_update(
            "user_ips_device_unique_index",
            index_name="user_ips_device_unique_id",
            table="user_ips",
            columns=["access_token", "ip", "user_agent"],
            unique=True,
        )

        # (access_token, ip, user_agent) -> (user_id, device_id, last_seen)
        self._batch_row_update = {}

        self._client_ip_looper = self._clock.looping_call(
            self._update_client_ips_batch, 5 * 1000
        )
        self.hs.get_reactor().addSystemEventTrigger(
            "before", "shutdown", self._update_client_ips_batch
        )

    @defer.inlineCallbacks
    def _remove_user_ip_dupes(self, progress, batch_size):
        yield self._remove_user_ip_dupes_impl()
        yield self._end_background_update("user_ips_remove_dupes")
        defer.returnValue(1)

    @defer.inlineCallbacks
    def _remove_user_ip_dupes_impl(self):

        def get_users(txn):
            txn.execute("SELECT DISTINCT user_id FROM user_ips;")
            results = txn.fetchall()
            return results

        users = yield self.runInteraction("user_ips_dups_get_users", get_users)

        def _clean(txn, user_id):

            txn.execute(
                (
                    "SELECT access_token, ip, user_agent, last_seen "
                    "FROM user_ips WHERE user_id = ?"
                ),
                (user_id,)
            )
            results = txn.fetchall()

            seen_before = set()
            seen_before_latest = {}
            duplicates = set()

            for i in results:
                key = i[0:3]
                if key not in seen_before:
                    seen_before.add(key)
                    seen_before_latest[key] = i[3]
                else:
                    duplicates.add(key)
                    if seen_before_latest[key] < i[3]:
                        seen_before_latest[key] = i[3]

            for d in sorted(duplicates):
                access_token, ip, user_agent = d
                txn.execute(
                    (
                        "DELETE FROM user_ips WHERE access_token IS ? AND ip IS ? "
                        "AND user_agent IS ? AND last_seen != ?"
                    ),
                    (access_token, ip, user_agent, seen_before_latest[d])
                )

        for user in users:
            yield self.runInteraction("user_ips_clean", _clean, user[0])

    @defer.inlineCallbacks
    def insert_client_ip(self, user_id, access_token, ip, user_agent, device_id,
                         now=None):
        if not now:
            now = int(self._clock.time_msec())
        key = (access_token, ip, user_agent)

        try:
            last_seen = self.client_ip_last_seen.get(key)
        except KeyError:
            last_seen = None
        yield self.populate_monthly_active_users(user_id)
        # Rate-limited inserts
        if last_seen is not None and (now - last_seen) < LAST_SEEN_GRANULARITY:
            return

        self.client_ip_last_seen.prefill(key, now)

        self._batch_row_update[key] = (user_id, device_id, now)

    def _update_client_ips_batch(self):

        # If the DB pool has already terminated, don't try updating
        if not self.hs.get_db_pool().running:
            return

        def update():
            to_update = self._batch_row_update
            self._batch_row_update = {}
            return self.runInteraction(
                "_update_client_ips_batch", self._update_client_ips_batch_txn,
                to_update,
            )

        return run_as_background_process(
            "update_client_ips", update,
        )

    def _update_client_ips_batch_txn(self, txn, to_update):
        self.database_engine.lock_table(txn, "user_ips")

        for entry in iteritems(to_update):
            (access_token, ip, user_agent), (user_id, device_id, last_seen) = entry

            try:
                self._simple_upsert_txn(
                    txn,
                    table="user_ips",
                    keyvalues={
                        "access_token": access_token,
                        "ip": ip,
                        "user_agent": user_agent,
                    },
                    values={
                        "user_id": user_id,
                        "device_id": device_id,
                        "last_seen": last_seen,
                    },
                    lock=False,
                )
            except Exception as e:
                # Failed to upsert, log and continue
                logger.error("Failed to insert client IP %r: %r", entry, e)

    @defer.inlineCallbacks
    def get_last_client_ip_by_device(self, user_id, device_id):
        """For each device_id listed, give the user_ip it was last seen on

        Args:
            user_id (str)
            device_id (str): If None fetches all devices for the user

        Returns:
            defer.Deferred: resolves to a dict, where the keys
            are (user_id, device_id) tuples. The values are also dicts, with
            keys giving the column names
        """

        res = yield self.runInteraction(
            "get_last_client_ip_by_device",
            self._get_last_client_ip_by_device_txn,
            user_id, device_id,
            retcols=(
                "user_id",
                "access_token",
                "ip",
                "user_agent",
                "device_id",
                "last_seen",
            ),
        )

        ret = {(d["user_id"], d["device_id"]): d for d in res}
        for key in self._batch_row_update:
            access_token, ip, user_agent = key
            user_id, did, last_seen = self._batch_row_update[key]
            if user_id == user_id:
                if not device_id or did == device_id:
                    ret[(user_id, device_id)] = {
                        "user_id": user_id,
                        "access_token": access_token,
                        "ip": ip,
                        "user_agent": user_agent,
                        "device_id": did,
                        "last_seen": last_seen,
                    }
        defer.returnValue(ret)

    @classmethod
    def _get_last_client_ip_by_device_txn(cls, txn, user_id, device_id, retcols):
        where_clauses = []
        bindings = []
        if device_id is None:
            where_clauses.append("user_id = ?")
            bindings.extend((user_id, ))
        else:
            where_clauses.append("(user_id = ? AND device_id = ?)")
            bindings.extend((user_id, device_id))

        if not where_clauses:
            return []

        inner_select = (
            "SELECT MAX(last_seen) mls, user_id, device_id FROM user_ips "
            "WHERE %(where)s "
            "GROUP BY user_id, device_id"
        ) % {
            "where": " OR ".join(where_clauses),
        }

        sql = (
            "SELECT %(retcols)s FROM user_ips "
            "JOIN (%(inner_select)s) ips ON"
            "    user_ips.last_seen = ips.mls AND"
            "    user_ips.user_id = ips.user_id AND"
            "    (user_ips.device_id = ips.device_id OR"
            "         (user_ips.device_id IS NULL AND ips.device_id IS NULL)"
            "    )"
        ) % {
            "retcols": ",".join("user_ips." + c for c in retcols),
            "inner_select": inner_select,
        }

        txn.execute(sql, bindings)
        return cls.cursor_to_dict(txn)

    @defer.inlineCallbacks
    def get_user_ip_and_agents(self, user):
        user_id = user.to_string()
        results = {}

        for key in self._batch_row_update:
            access_token, ip, user_agent = key
            uid, _, last_seen = self._batch_row_update[key]
            if uid == user_id:
                results[(access_token, ip)] = (user_agent, last_seen)

        rows = yield self._simple_select_list(
            table="user_ips",
            keyvalues={"user_id": user_id},
            retcols=[
                "access_token", "ip", "user_agent", "last_seen"
            ],
            desc="get_user_ip_and_agents",
        )

        results.update(
            ((row["access_token"], row["ip"]), (row["user_agent"], row["last_seen"]))
            for row in rows
        )
        defer.returnValue(list(
            {
                "access_token": access_token,
                "ip": ip,
                "user_agent": user_agent,
                "last_seen": last_seen,
            }
            for (access_token, ip), (user_agent, last_seen) in iteritems(results)
        ))
