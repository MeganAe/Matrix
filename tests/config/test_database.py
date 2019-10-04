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

import yaml

from synapse.config.database import DatabaseConfig

from tests import unittest


class DatabaseConfigTestCase(unittest.TestCase):
    def test_database_configured_correctly_no_database_conf_param(self):
        conf = yaml.safe_load(
            DatabaseConfig().generate_config_section("/data_dir_path", None)
        )

        expected_database_conf = {
            "name": "sqlite3",
            "args": {"database": "/data_dir_path/homeserver.db"},
        }

        self.assertEqual(conf["database"], expected_database_conf)

    def test_database_configured_correctly_database_conf_param(self):

        database_conf = {
            "name": "my super fast datastore",
            "args": {
                "user": "matrix",
                "password": "synapse_database_password",
                "host": "synapse_database_host",
                "database": "matrix",
            },
        }

        conf = yaml.safe_load(
            DatabaseConfig().generate_config_section("/data_dir_path", database_conf)
        )

        self.assertEqual(conf["database"], database_conf)
