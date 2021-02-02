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

from synapse.storage.engines import BaseDatabaseEngine

logger = logging.getLogger(__name__)


def run_create(cur, database_engine: BaseDatabaseEngine, *args, **kwargs):
    if database_engine.sql_type.is_postgres():
        cur.execute("TRUNCATE received_transactions")
    else:
        cur.execute("DELETE FROM received_transactions")

    cur.execute("CREATE INDEX received_transactions_ts ON received_transactions(ts)")


def run_upgrade(cur, database_engine, *args, **kwargs):
    pass
