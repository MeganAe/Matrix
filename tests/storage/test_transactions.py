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

from twisted.test.proto_helpers import MemoryReactor

from synapse.server import HomeServer
from synapse.storage.databases.main.transactions import DestinationRetryTimings
from synapse.util import Clock
from synapse.util.retryutils import MAX_RETRY_INTERVAL

from tests.unittest import HomeserverTestCase


class TransactionStoreTestCase(HomeserverTestCase):
    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.store = homeserver.get_datastores().main

    def test_get_set_transactions(self) -> None:
        """Tests that we can successfully get a non-existent entry for
        destination retries, as well as testing tht we can set and get
        correctly.
        """
        r = self.get_success(self.store.get_destination_retry_timings("example.com"))
        self.assertIsNone(r)

        self.get_success(
            self.store.set_destination_retry_timings("example.com", 1000, 50, 100)
        )

        r = self.get_success(self.store.get_destination_retry_timings("example.com"))

        self.assertEqual(
            DestinationRetryTimings(
                retry_last_ts=50, retry_interval=100, failure_ts=1000
            ),
            r,
        )

    def test_initial_set_transactions(self) -> None:
        """Tests that we can successfully set the destination retries (there
        was a bug around invalidating the cache that broke this)
        """
        d = self.store.set_destination_retry_timings("example.com", 1000, 50, 100)
        self.get_success(d)

    def test_large_destination_retry(self) -> None:
        d = self.store.set_destination_retry_timings(
            "example.com", MAX_RETRY_INTERVAL, MAX_RETRY_INTERVAL, MAX_RETRY_INTERVAL
        )
        self.get_success(d)

        d2 = self.store.get_destination_retry_timings("example.com")
        self.get_success(d2)
