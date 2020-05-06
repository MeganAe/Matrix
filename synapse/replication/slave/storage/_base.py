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
from typing import Optional

import six

from synapse.storage.data_stores.main.cache import CacheInvalidationWorkerStore
from synapse.storage.database import Database
from synapse.storage.engines import PostgresEngine
from synapse.storage.util.id_generators import MultiWriterIdGenerator

logger = logging.getLogger(__name__)


def __func__(inp):
    if six.PY3:
        return inp
    else:
        return inp.__func__


class BaseSlavedStore(CacheInvalidationWorkerStore):
    def __init__(self, database: Database, db_conn, hs):
        super(BaseSlavedStore, self).__init__(database, db_conn, hs)
        if isinstance(self.database_engine, PostgresEngine):
            self._cache_id_gen = MultiWriterIdGenerator(
                db_conn,
                database,
                instance_name=hs.get_instance_name(),
                table="cache_invalidation_stream",
                instance_column="instance_name",
                id_column="stream_id",
                sequence_name="cache_invalidation_stream_seq",
            )  # type: Optional[MultiWriterIdGenerator]
        else:
            self._cache_id_gen = None

        self.hs = hs
