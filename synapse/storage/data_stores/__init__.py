# -*- coding: utf-8 -*-
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

from synapse.storage.data_stores.state import StateGroupDataStore
from synapse.storage.database import Database, make_conn
from synapse.storage.engines import create_engine
from synapse.storage.prepare_database import prepare_database

logger = logging.getLogger(__name__)


class DataStores(object):
    """The various data stores.

    These are low level interfaces to physical databases.

    Attributes:
        main (DataStore)
    """

    def __init__(self, main_store_class, hs):
        # Note we pass in the main store class here as workers use a different main
        # store.

        self.databases = []

        for database_config in hs.config.database.databases:
            db_name = database_config.name
            engine = create_engine(database_config.config)

            with make_conn(database_config, engine) as db_conn:
                logger.info("Preparing database %r...", db_name)

                engine.check_database(db_conn.cursor())
                prepare_database(
                    db_conn, engine, hs.config, data_stores=database_config.data_stores,
                )

                database = Database(hs, database_config, engine)

                if "main" in database_config.data_stores:
                    logger.info("Starting 'main' data store")
                    self.main = main_store_class(database, db_conn, hs)

                if "state" in database_config.data_stores:
                    logger.info("Starting 'state' data store")
                    self.state = StateGroupDataStore(database, db_conn, hs)

                db_conn.commit()

                self.databases.append(database)

                logger.info("Database %r prepared", db_name)
