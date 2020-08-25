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

""" This module contains all the persistence actions done by the federation
package.

These actions are mostly only used by the :py:mod:`.replication` module.
"""

import logging

from synapse.federation.units import Transaction
from synapse.logging.utils import log_function
from synapse.types import JsonDict

logger = logging.getLogger(__name__)


class TransactionActions(object):
    """ Defines persistence actions that relate to handling Transactions.
    """

    def __init__(self, datastore):
        self.store = datastore

    @log_function
    def have_responded(self, origin, transaction):
        """ Have we already responded to a transaction with the same id and
        origin?

        Returns:
            Deferred: Results in `None` if we have not previously responded to
            this transaction or a 2-tuple of `(int, dict)` representing the
            response code and response body.
        """
        if not transaction.transaction_id:
            raise RuntimeError("Cannot persist a transaction with no transaction_id")

        return self.store.get_received_txn_response(transaction.transaction_id, origin)

    @log_function
    async def set_response(
        self, origin: str, transaction: Transaction, code: int, response: JsonDict
    ) -> None:
        """ Persist how we responded to a transaction.
        """
        transaction_id = transaction.transaction_id  # type: ignore
        if not transaction_id:
            raise RuntimeError("Cannot persist a transaction with no transaction_id")

        await self.store.set_received_txn_response(
            transaction_id, origin, code, response
        )
