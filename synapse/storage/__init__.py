# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
# Copyright 2018,2019 New Vector Ltd
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

"""
The storage layer is split up into multiple parts to allow Synapse to run
against different configurations of databases (e.g. single or multiple
databases). The `Database` class represents a single physical database. The
`data_stores` are classes that talk directly to a `Database` instance and have
associated schemas, background updates, etc. On top of those there are classes
that provide high level interfaces that combine calls to multiple `data_stores`.

There are also schemas that get applied to every database, regardless of the
data stores associated with them (e.g. the schema version tables), which are
stored in `synapse.storage.schema`.
"""

from synapse.storage.data_stores import DataStores
from synapse.storage.data_stores.main import DataStore
from synapse.storage.persist_events import EventsPersistenceStorage
from synapse.storage.purge_events import PurgeEventsStorage
from synapse.storage.state import StateGroupStorage

__all__ = ["DataStores", "DataStore"]


class Storage(object):
    """The high level interfaces for talking to various storage layers.
    """

    def __init__(self, hs, stores: DataStores):
        # We include the main data store here mainly so that we don't have to
        # rewrite all the existing code to split it into high vs low level
        # interfaces.
        self.main = stores.main

        self.persistence = EventsPersistenceStorage(hs, stores)
        self.purge_events = PurgeEventsStorage(hs, stores)
        self.state = StateGroupStorage(hs, stores)
