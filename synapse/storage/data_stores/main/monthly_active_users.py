# -*- coding: utf-8 -*-
# Copyright 2018 New Vector
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

from twisted.internet import defer

from synapse.storage._base import SQLBaseStore
from synapse.storage.database import Database
from synapse.util.caches.descriptors import cached

logger = logging.getLogger(__name__)

# Number of msec of granularity to store the monthly_active_user timestamp
# This means it is not necessary to update the table on every request
LAST_SEEN_GRANULARITY = 60 * 60 * 1000


class MonthlyActiveUsersWorkerStore(SQLBaseStore):
    def __init__(self, database: Database, db_conn, hs):
        super(MonthlyActiveUsersWorkerStore, self).__init__(database, db_conn, hs)
        self._clock = hs.get_clock()
        self.hs = hs

    @cached(num_args=0)
    def get_monthly_active_count(self):
        """Generates current count of monthly active users

        Returns:
            Defered[int]: Number of current monthly active users
        """

        def _count_users(txn):
            sql = "SELECT COALESCE(count(*), 0) FROM monthly_active_users"
            txn.execute(sql)
            (count,) = txn.fetchone()
            return count

        return self.db.runInteraction("count_users", _count_users)

    @cached(num_args=0)
    def get_monthly_active_count_by_service(self):
        """Generates current count of monthly active users broken down by service.
        A service is typically an appservice but also includes native matrix users.
        Since the `monthly_active_users` table is populated from the `user_ips` table
        `config.track_appservice_user_ips` must be set to `true` for this
        method to return anything other than native matrix users.

        Returns:
            Deferred[dict]: dict that includes a mapping between app_service_id
                and the number of occurrences.

        """

        def _count_users_by_service(txn):
            sql = """
                SELECT COALESCE(appservice_id, 'native'), COALESCE(count(*), 0)
                FROM monthly_active_users
                LEFT JOIN users ON monthly_active_users.user_id=users.name
                GROUP BY appservice_id;
            """

            txn.execute(sql)
            result = txn.fetchall()
            return dict(result)

        return self.db.runInteraction("count_users_by_service", _count_users_by_service)

    @defer.inlineCallbacks
    def get_registered_reserved_users(self):
        """Of the reserved threepids defined in config, which are associated
        with registered users?

        Returns:
            Defered[list]: Real reserved users
        """
        users = []

        for tp in self.hs.config.mau_limits_reserved_threepids[
            : self.hs.config.max_mau_value
        ]:
            user_id = yield self.hs.get_datastore().get_user_id_by_threepid(
                tp["medium"], tp["address"]
            )
            if user_id:
                users.append(user_id)

        return users

    @cached(num_args=1)
    def user_last_seen_monthly_active(self, user_id):
        """
            Checks if a given user is part of the monthly active user group
            Arguments:
                user_id (str): user to add/update
            Return:
                Deferred[int] : timestamp since last seen, None if never seen

        """

        return self.db.simple_select_one_onecol(
            table="monthly_active_users",
            keyvalues={"user_id": user_id},
            retcol="timestamp",
            allow_none=True,
            desc="user_last_seen_monthly_active",
        )


class MonthlyActiveUsersStore(MonthlyActiveUsersWorkerStore):
    def __init__(self, database: Database, db_conn, hs):
        super(MonthlyActiveUsersStore, self).__init__(database, db_conn, hs)

        # Do not add more reserved users than the total allowable number
        # cur = LoggingTransaction(
        self.db.new_transaction(
            db_conn,
            "initialise_mau_threepids",
            [],
            [],
            self._initialise_reserved_users,
            hs.config.mau_limits_reserved_threepids[: self.hs.config.max_mau_value],
        )

    def _initialise_reserved_users(self, txn, threepids):
        """Ensures that reserved threepids are accounted for in the MAU table, should
        be called on start up.

        Args:
            txn (cursor):
            threepids (list[dict]): List of threepid dicts to reserve
        """

        for tp in threepids:
            user_id = self.get_user_id_by_threepid_txn(txn, tp["medium"], tp["address"])

            if user_id:
                is_support = self.is_support_user_txn(txn, user_id)
                if not is_support:
                    # We do this manually here to avoid hitting #6791
                    self.db.simple_upsert_txn(
                        txn,
                        table="monthly_active_users",
                        keyvalues={"user_id": user_id},
                        values={"timestamp": int(self._clock.time_msec())},
                    )
            else:
                logger.warning("mau limit reserved threepid %s not found in db" % tp)

    @defer.inlineCallbacks
    def reap_monthly_active_users(self):
        """Cleans out monthly active user table to ensure that no stale
        entries exist.

        Returns:
            Deferred[]
        """

        def _reap_users(txn, reserved_users):
            """
            Args:
                reserved_users (tuple): reserved users to preserve
            """

            thirty_days_ago = int(self._clock.time_msec()) - (1000 * 60 * 60 * 24 * 30)
            query_args = [thirty_days_ago]
            base_sql = "DELETE FROM monthly_active_users WHERE timestamp < ?"

            # Need if/else since 'AND user_id NOT IN ({})' fails on Postgres
            # when len(reserved_users) == 0. Works fine on sqlite.
            if len(reserved_users) > 0:
                # questionmarks is a hack to overcome sqlite not supporting
                # tuples in 'WHERE IN %s'
                question_marks = ",".join("?" * len(reserved_users))

                query_args.extend(reserved_users)
                sql = base_sql + " AND user_id NOT IN ({})".format(question_marks)
            else:
                sql = base_sql

            txn.execute(sql, query_args)

            max_mau_value = self.hs.config.max_mau_value
            if self.hs.config.limit_usage_by_mau:
                # If MAU user count still exceeds the MAU threshold, then delete on
                # a least recently active basis.
                # Note it is not possible to write this query using OFFSET due to
                # incompatibilities in how sqlite and postgres support the feature.
                # sqlite requires 'LIMIT -1 OFFSET ?', the LIMIT must be present
                # While Postgres does not require 'LIMIT', but also does not support
                # negative LIMIT values. So there is no way to write it that both can
                # support
                if len(reserved_users) == 0:
                    sql = """
                        DELETE FROM monthly_active_users
                        WHERE user_id NOT IN (
                            SELECT user_id FROM monthly_active_users
                            ORDER BY timestamp DESC
                            LIMIT ?
                        )
                        """
                    txn.execute(sql, (max_mau_value,))
                # Need if/else since 'AND user_id NOT IN ({})' fails on Postgres
                # when len(reserved_users) == 0. Works fine on sqlite.
                else:
                    # Must be >= 0 for postgres
                    num_of_non_reserved_users_to_remove = max(
                        max_mau_value - len(reserved_users), 0
                    )

                    # It is important to filter reserved users twice to guard
                    # against the case where the reserved user is present in the
                    # SELECT, meaning that a legitmate mau is deleted.
                    sql = """
                        DELETE FROM monthly_active_users
                        WHERE user_id NOT IN (
                            SELECT user_id FROM monthly_active_users
                            WHERE user_id NOT IN ({})
                            ORDER BY timestamp DESC
                            LIMIT ?
                        )
                        AND user_id NOT IN ({})
                    """.format(
                        question_marks, question_marks
                    )

                    query_args = [
                        *reserved_users,
                        num_of_non_reserved_users_to_remove,
                        *reserved_users,
                    ]

                    txn.execute(sql, query_args)

            # It seems poor to invalidate the whole cache, Postgres supports
            # 'Returning' which would allow me to invalidate only the
            # specific users, but sqlite has no way to do this and instead
            # I would need to SELECT and the DELETE which without locking
            # is racy.
            # Have resolved to invalidate the whole cache for now and do
            # something about it if and when the perf becomes significant
            self._invalidate_all_cache_and_stream(
                txn, self.user_last_seen_monthly_active
            )
            self._invalidate_cache_and_stream(txn, self.get_monthly_active_count, ())

        reserved_users = yield self.get_registered_reserved_users()
        yield self.db.runInteraction(
            "reap_monthly_active_users", _reap_users, reserved_users
        )

    @defer.inlineCallbacks
    def upsert_monthly_active_user(self, user_id):
        """Updates or inserts the user into the monthly active user table, which
        is used to track the current MAU usage of the server

        Args:
            user_id (str): user to add/update
        """
        # Support user never to be included in MAU stats. Note I can't easily call this
        # from upsert_monthly_active_user_txn because then I need a _txn form of
        # is_support_user which is complicated because I want to cache the result.
        # Therefore I call it here and ignore the case where
        # upsert_monthly_active_user_txn is called directly from
        # _initialise_reserved_users reasoning that it would be very strange to
        #  include a support user in this context.

        is_support = yield self.is_support_user(user_id)
        if is_support:
            return

        yield self.db.runInteraction(
            "upsert_monthly_active_user", self.upsert_monthly_active_user_txn, user_id
        )

    def upsert_monthly_active_user_txn(self, txn, user_id):
        """Updates or inserts monthly active user member

        We consciously do not call is_support_txn from this method because it
        is not possible to cache the response. is_support_txn will be false in
        almost all cases, so it seems reasonable to call it only for
        upsert_monthly_active_user and to call is_support_txn manually
        for cases where upsert_monthly_active_user_txn is called directly,
        like _initialise_reserved_users

        In short, don't call this method with support users. (Support users
        should not appear in the MAU stats).

        Args:
            txn (cursor):
            user_id (str): user to add/update

        Returns:
            bool: True if a new entry was created, False if an
            existing one was updated.
        """

        # Am consciously deciding to lock the table on the basis that is ought
        # never be a big table and alternative approaches (batching multiple
        # upserts into a single txn) introduced a lot of extra complexity.
        # See https://github.com/matrix-org/synapse/issues/3854 for more
        is_insert = self.db.simple_upsert_txn(
            txn,
            table="monthly_active_users",
            keyvalues={"user_id": user_id},
            values={"timestamp": int(self._clock.time_msec())},
        )

        self._invalidate_cache_and_stream(txn, self.get_monthly_active_count, ())
        self._invalidate_cache_and_stream(
            txn, self.get_monthly_active_count_by_service, ()
        )
        self._invalidate_cache_and_stream(
            txn, self.user_last_seen_monthly_active, (user_id,)
        )

        return is_insert

    @defer.inlineCallbacks
    def populate_monthly_active_users(self, user_id):
        """Checks on the state of monthly active user limits and optionally
        add the user to the monthly active tables

        Args:
            user_id(str): the user_id to query
        """
        if self.hs.config.limit_usage_by_mau or self.hs.config.mau_stats_only:
            # Trial users and guests should not be included as part of MAU group
            is_guest = yield self.is_guest(user_id)
            if is_guest:
                return
            is_trial = yield self.is_trial_user(user_id)
            if is_trial:
                return

            last_seen_timestamp = yield self.user_last_seen_monthly_active(user_id)
            now = self.hs.get_clock().time_msec()

            # We want to reduce to the total number of db writes, and are happy
            # to trade accuracy of timestamp in order to lighten load. This means
            # We always insert new users (where MAU threshold has not been reached),
            # but only update if we have not previously seen the user for
            # LAST_SEEN_GRANULARITY ms
            if last_seen_timestamp is None:
                # In the case where mau_stats_only is True and limit_usage_by_mau is
                # False, there is no point in checking get_monthly_active_count - it
                # adds no value and will break the logic if max_mau_value is exceeded.
                if not self.hs.config.limit_usage_by_mau:
                    yield self.upsert_monthly_active_user(user_id)
                else:
                    count = yield self.get_monthly_active_count()
                    if count < self.hs.config.max_mau_value:
                        yield self.upsert_monthly_active_user(user_id)
            elif now - last_seen_timestamp > LAST_SEEN_GRANULARITY:
                yield self.upsert_monthly_active_user(user_id)
