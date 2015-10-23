# -*- coding: utf-8 -*-
# Copyright 2014 OpenMarket Ltd
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

from ._base import SQLBaseStore
from twisted.internet import defer

from synapse.api.errors import StoreError

from canonicaljson import encode_canonical_json

import logging
import simplejson as json
import types

logger = logging.getLogger(__name__)


class PusherStore(SQLBaseStore):
    def _decode_pushers_rows(self, rows):
        for r in rows:
            dataJson = r['data']
            r['data'] = None
            try:
                if isinstance(dataJson, types.BufferType):
                    dataJson = str(dataJson).decode("UTF8")

                r['data'] = json.loads(dataJson)
            except Exception as e:
                logger.warn(
                    "Invalid JSON in data for pusher %d: %s, %s",
                    r['id'], dataJson, e.message,
                )
                pass

            if isinstance(r['pushkey'], types.BufferType):
                r['pushkey'] = str(r['pushkey']).decode("UTF8")

        return rows

    @defer.inlineCallbacks
    def get_pushers_by_app_id_and_pushkey(self, app_id, pushkey):
        def r(txn):
            sql = (
                "SELECT * FROM pushers"
                " WHERE app_id = ? AND pushkey = ?"
            )

            txn.execute(sql, (app_id, pushkey,))
            rows = self.cursor_to_dict(txn)

            return self._decode_pushers_rows(rows)

        rows = yield self.runInteraction(
            "get_pushers_by_app_id_and_pushkey", r
        )

        defer.returnValue(rows)

    @defer.inlineCallbacks
    def get_all_pushers(self):
        def get_pushers(txn):
            txn.execute("SELECT * FROM pushers")
            rows = self.cursor_to_dict(txn)

            return self._decode_pushers_rows(rows)

        rows = yield self.runInteraction("get_all_pushers", get_pushers)
        defer.returnValue(rows)

    @defer.inlineCallbacks
    def add_pusher(self, user_name, access_token, profile_tag, kind, app_id,
                   app_display_name, device_display_name,
                   pushkey, pushkey_ts, lang, data):
        try:
            next_id = yield self._pushers_id_gen.get_next()
            yield self._simple_upsert(
                PushersTable.table_name,
                dict(
                    app_id=app_id,
                    pushkey=pushkey,
                    user_name=user_name,
                ),
                dict(
                    access_token=access_token,
                    kind=kind,
                    profile_tag=profile_tag,
                    app_display_name=app_display_name,
                    device_display_name=device_display_name,
                    ts=pushkey_ts,
                    lang=lang,
                    data=encode_canonical_json(data),
                ),
                insertion_values=dict(
                    id=next_id,
                ),
                desc="add_pusher",
            )
        except Exception as e:
            logger.error("create_pusher with failed: %s", e)
            raise StoreError(500, "Problem creating pusher.")

    @defer.inlineCallbacks
    def delete_pusher_by_app_id_pushkey_user_name(self, app_id, pushkey, user_name):
        yield self._simple_delete_one(
            PushersTable.table_name,
            {"app_id": app_id, "pushkey": pushkey, 'user_name': user_name},
            desc="delete_pusher_by_app_id_pushkey_user_name",
        )

    @defer.inlineCallbacks
    def update_pusher_last_token(self, app_id, pushkey, user_name, last_token):
        yield self._simple_update_one(
            PushersTable.table_name,
            {'app_id': app_id, 'pushkey': pushkey, 'user_name': user_name},
            {'last_token': last_token},
            desc="update_pusher_last_token",
        )

    @defer.inlineCallbacks
    def update_pusher_last_token_and_success(self, app_id, pushkey, user_name,
                                             last_token, last_success):
        yield self._simple_update_one(
            PushersTable.table_name,
            {'app_id': app_id, 'pushkey': pushkey, 'user_name': user_name},
            {'last_token': last_token, 'last_success': last_success},
            desc="update_pusher_last_token_and_success",
        )

    @defer.inlineCallbacks
    def update_pusher_failing_since(self, app_id, pushkey, user_name,
                                    failing_since):
        yield self._simple_update_one(
            PushersTable.table_name,
            {'app_id': app_id, 'pushkey': pushkey, 'user_name': user_name},
            {'failing_since': failing_since},
            desc="update_pusher_failing_since",
        )


class PushersTable(object):
    table_name = "pushers"
