# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
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
import os
from textwrap import indent
from typing import List

import yaml

from synapse.config._base import Config, ConfigError

logger = logging.getLogger(__name__)


class DatabaseConnectionConfig:
    """Contains the connection config for a particular database.

    Args:
        name: A label for the database, used for logging.
        db_config: The config for a particular database, as per `database`
            section of main config. Has two fields: `name` for database
            module name, and `args` for the args to give to the database
            connector.
        data_stores: The list of data stores that should be provisioned on the
            database. Defaults to all data stores.
    """

    def __init__(
        self, name: str, db_config: dict, data_stores: List[str] = ["main", "state"]
    ):
        if db_config["name"] not in ("sqlite3", "psycopg2"):
            raise ConfigError("Unsupported database type %r" % (db_config["name"],))

        if db_config["name"] == "sqlite3":
            db_config.setdefault("args", {}).update(
                {"cp_min": 1, "cp_max": 1, "check_same_thread": False}
            )

        self.name = name
        self.config = db_config
        self.data_stores = data_stores


class DatabaseConfig(Config):
    section = "database"

    def read_config(self, config, **kwargs):
        self.event_cache_size = self.parse_size(config.get("event_cache_size", "10K"))

        database_config = config.get("database")

        if database_config is None:
            database_config = {"name": "sqlite3", "args": {}}

        self.databases = [DatabaseConnectionConfig("master", database_config)]

        self.set_databasepath(config.get("database_path"))

    def generate_config_section(self, data_dir_path, database_conf, **kwargs):
        if not database_conf:
            database_path = os.path.join(data_dir_path, "homeserver.db")
            database_conf = (
                """# The database engine name
          name: "sqlite3"
          # Arguments to pass to the engine
          args:
            # Path to the database
            database: "%(database_path)s"
            """
                % locals()
            )
        else:
            database_conf = indent(yaml.dump(database_conf), " " * 10).lstrip()

        return (
            """\
        ## Database ##

        database:
          %(database_conf)s
        # Number of events to cache in memory.
        #
        #event_cache_size: 10K
        """
            % locals()
        )

    def read_arguments(self, args):
        self.set_databasepath(args.database_path)

    def set_databasepath(self, database_path):
        if database_path is None:
            return

        if database_path != ":memory:":
            database_path = self.abspath(database_path)

        # We only support setting a database path if we have a single sqlite3
        # database.
        if len(self.databases) != 1:
            raise ConfigError("Cannot specify 'database_path' with multiple databases")

        database = self.get_single_database()
        if database.config["name"] != "sqlite3":
            # We don't raise here as we haven't done so before for this case.
            logger.warn("Ignoring 'database_path' for non-sqlite3 database")
            return

        database.config["args"]["database"] = database_path

    @staticmethod
    def add_arguments(parser):
        db_group = parser.add_argument_group("database")
        db_group.add_argument(
            "-d",
            "--database-path",
            metavar="SQLITE_DATABASE_PATH",
            help="The path to a sqlite database to use.",
        )

    def get_single_database(self) -> DatabaseConnectionConfig:
        """Returns the database if there is only one, useful for e.g. tests
        """
        if len(self.databases) != 1:
            raise Exception("More than one database exists")

        return self.databases[0]
