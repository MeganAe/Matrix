# -*- coding: utf-8 -*-
# Copyright 2017 Vector Creations Ltd
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

import itertools
import logging
from collections import namedtuple
from typing import Any, List, Optional, Tuple

import attr

from synapse.types import JsonDict

logger = logging.getLogger(__name__)


MAX_EVENTS_BEHIND = 500000


class Stream(object):
    """Base class for the streams.

    Provides a `get_updates()` function that returns new updates since the last
    time it was called.
    """

    NAME = None  # type: str  # The name of the stream
    # The type of the row. Used by the default impl of parse_row.
    ROW_TYPE = None  # type: Any

    @classmethod
    def parse_row(cls, row):
        """Parse a row received over replication

        By default, assumes that the row data is an array object and passes its contents
        to the constructor of the ROW_TYPE for this stream.

        Args:
            row: row data from the incoming RDATA command, after json decoding

        Returns:
            ROW_TYPE object for this stream
        """
        return cls.ROW_TYPE(*row)

    def __init__(self, hs):
        # The token from which we last asked for updates
        self.last_token = self.current_token()

    def discard_updates_and_advance(self):
        """Called when the stream should advance but the updates would be discarded,
        e.g. when there are no currently connected workers.
        """
        self.last_token = self.current_token()

    async def get_updates(self):
        """Gets all updates since the last time this function was called (or
        since the stream was constructed if it hadn't been called before).

        Returns:
            Deferred[Tuple[List[Tuple[int, Any]], int]:
                Resolves to a pair ``(updates, current_token)``, where ``updates`` is a
                list of ``(token, row)`` entries. ``row`` will be json-serialised and
                sent over the replication steam.
        """
        updates, current_token = await self.get_updates_since(self.last_token)
        self.last_token = current_token

        return updates, current_token

    async def get_updates_since(
        self, from_token: int
    ) -> Tuple[List[Tuple[int, JsonDict]], int]:
        """Like get_updates except allows specifying from when we should
        stream updates

        Returns:
            Resolves to a pair `(updates, new_last_token)`, where `updates` is
            a list of `(token, row)` entries and `new_last_token` is the new
            position in stream.
        """

        if from_token in ("NOW", "now"):
            return [], self.current_token()

        current_token = self.current_token()

        from_token = int(from_token)

        if from_token == current_token:
            return [], current_token

        rows = await self.update_function(
            from_token, current_token, limit=MAX_EVENTS_BEHIND + 1
        )

        # never turn more than MAX_EVENTS_BEHIND + 1 into updates.
        rows = itertools.islice(rows, MAX_EVENTS_BEHIND + 1)

        updates = [(row[0], row[1:]) for row in rows]

        # check we didn't get more rows than the limit.
        # doing it like this allows the update_function to be a generator.
        if len(updates) >= MAX_EVENTS_BEHIND:
            raise Exception("stream %s has fallen behind" % (self.NAME))

        # The update function didn't hit the limit, so we must have got all
        # the updates to `current_token`, and can return that as our new
        # stream position.
        return updates, current_token

    def current_token(self):
        """Gets the current token of the underlying streams. Should be provided
        by the sub classes

        Returns:
            int
        """
        raise NotImplementedError()

    def update_function(self, from_token, current_token, limit):
        """Get updates between from_token and to_token.

        Returns:
            Deferred(list(tuple)): the first entry in the tuple is the token for
                that update, and the rest of the tuple gets used to construct
                a ``ROW_TYPE`` instance
        """
        raise NotImplementedError()


class BackfillStream(Stream):
    """We fetched some old events and either we had never seen that event before
    or it went from being an outlier to not.
    """

    BackfillStreamRow = namedtuple(
        "BackfillStreamRow",
        (
            "event_id",  # str
            "room_id",  # str
            "type",  # str
            "state_key",  # str, optional
            "redacts",  # str, optional
            "relates_to",  # str, optional
        ),
    )

    NAME = "backfill"
    ROW_TYPE = BackfillStreamRow

    def __init__(self, hs):
        store = hs.get_datastore()
        self.current_token = store.get_current_backfill_token  # type: ignore
        self.update_function = store.get_all_new_backfill_event_rows  # type: ignore

        super(BackfillStream, self).__init__(hs)


class PresenceStream(Stream):
    PresenceStreamRow = namedtuple(
        "PresenceStreamRow",
        (
            "user_id",  # str
            "state",  # str
            "last_active_ts",  # int
            "last_federation_update_ts",  # int
            "last_user_sync_ts",  # int
            "status_msg",  # str
            "currently_active",  # bool
        ),
    )

    NAME = "presence"
    ROW_TYPE = PresenceStreamRow

    def __init__(self, hs):
        store = hs.get_datastore()
        presence_handler = hs.get_presence_handler()

        self.current_token = store.get_current_presence_token  # type: ignore
        self.update_function = presence_handler.get_all_presence_updates  # type: ignore

        super(PresenceStream, self).__init__(hs)


class TypingStream(Stream):
    TypingStreamRow = namedtuple(
        "TypingStreamRow", ("room_id", "user_ids")  # str  # list(str)
    )

    NAME = "typing"
    ROW_TYPE = TypingStreamRow

    def __init__(self, hs):
        typing_handler = hs.get_typing_handler()

        self.current_token = typing_handler.get_current_token  # type: ignore
        self.update_function = typing_handler.get_all_typing_updates  # type: ignore

        super(TypingStream, self).__init__(hs)


class ReceiptsStream(Stream):
    ReceiptsStreamRow = namedtuple(
        "ReceiptsStreamRow",
        (
            "room_id",  # str
            "receipt_type",  # str
            "user_id",  # str
            "event_id",  # str
            "data",  # dict
        ),
    )

    NAME = "receipts"
    ROW_TYPE = ReceiptsStreamRow

    def __init__(self, hs):
        store = hs.get_datastore()

        self.current_token = store.get_max_receipt_stream_id  # type: ignore
        self.update_function = store.get_all_updated_receipts  # type: ignore

        super(ReceiptsStream, self).__init__(hs)


class PushRulesStream(Stream):
    """A user has changed their push rules
    """

    PushRulesStreamRow = namedtuple("PushRulesStreamRow", ("user_id",))  # str

    NAME = "push_rules"
    ROW_TYPE = PushRulesStreamRow

    def __init__(self, hs):
        self.store = hs.get_datastore()
        super(PushRulesStream, self).__init__(hs)

    def current_token(self):
        push_rules_token, _ = self.store.get_push_rules_stream_token()
        return push_rules_token

    async def update_function(self, from_token, to_token, limit):
        rows = await self.store.get_all_push_rule_updates(from_token, to_token, limit)
        return [(row[0], row[2]) for row in rows]


class PushersStream(Stream):
    """A user has added/changed/removed a pusher
    """

    PushersStreamRow = namedtuple(
        "PushersStreamRow",
        ("user_id", "app_id", "pushkey", "deleted"),  # str  # str  # str  # bool
    )

    NAME = "pushers"
    ROW_TYPE = PushersStreamRow

    def __init__(self, hs):
        store = hs.get_datastore()

        self.current_token = store.get_pushers_stream_token  # type: ignore
        self.update_function = store.get_all_updated_pushers_rows  # type: ignore

        super(PushersStream, self).__init__(hs)


class CachesStream(Stream):
    """A cache was invalidated on the master and no other stream would invalidate
    the cache on the workers
    """

    @attr.s
    class CachesStreamRow:
        """Stream to inform workers they should invalidate their cache.

        Attributes:
            cache_func: Name of the cached function.
            keys: The entry in the cache to invalidate. If None then will
                invalidate all.
            invalidation_ts: Timestamp of when the invalidation took place.
        """

        cache_func = attr.ib(type=str)
        keys = attr.ib(type=Optional[List[Any]])
        invalidation_ts = attr.ib(type=int)

    NAME = "caches"
    ROW_TYPE = CachesStreamRow

    def __init__(self, hs):
        store = hs.get_datastore()

        self.current_token = store.get_cache_stream_token  # type: ignore
        self.update_function = store.get_all_updated_caches  # type: ignore

        super(CachesStream, self).__init__(hs)


class PublicRoomsStream(Stream):
    """The public rooms list changed
    """

    PublicRoomsStreamRow = namedtuple(
        "PublicRoomsStreamRow",
        (
            "room_id",  # str
            "visibility",  # str
            "appservice_id",  # str, optional
            "network_id",  # str, optional
        ),
    )

    NAME = "public_rooms"
    ROW_TYPE = PublicRoomsStreamRow

    def __init__(self, hs):
        store = hs.get_datastore()

        self.current_token = store.get_current_public_room_stream_id  # type: ignore
        self.update_function = store.get_all_new_public_rooms  # type: ignore

        super(PublicRoomsStream, self).__init__(hs)


class DeviceListsStream(Stream):
    """Either a user has updated their devices or a remote server needs to be
    told about a device update.
    """

    @attr.s
    class DeviceListsStreamRow:
        entity = attr.ib(type=str)

    NAME = "device_lists"
    ROW_TYPE = DeviceListsStreamRow

    def __init__(self, hs):
        store = hs.get_datastore()

        self.current_token = store.get_device_stream_token  # type: ignore
        self.update_function = store.get_all_device_list_changes_for_remotes  # type: ignore

        super(DeviceListsStream, self).__init__(hs)


class ToDeviceStream(Stream):
    """New to_device messages for a client
    """

    ToDeviceStreamRow = namedtuple("ToDeviceStreamRow", ("entity",))  # str

    NAME = "to_device"
    ROW_TYPE = ToDeviceStreamRow

    def __init__(self, hs):
        store = hs.get_datastore()

        self.current_token = store.get_to_device_stream_token  # type: ignore
        self.update_function = store.get_all_new_device_messages  # type: ignore

        super(ToDeviceStream, self).__init__(hs)


class TagAccountDataStream(Stream):
    """Someone added/removed a tag for a room
    """

    TagAccountDataStreamRow = namedtuple(
        "TagAccountDataStreamRow", ("user_id", "room_id", "data")  # str  # str  # dict
    )

    NAME = "tag_account_data"
    ROW_TYPE = TagAccountDataStreamRow

    def __init__(self, hs):
        store = hs.get_datastore()

        self.current_token = store.get_max_account_data_stream_id  # type: ignore
        self.update_function = store.get_all_updated_tags  # type: ignore

        super(TagAccountDataStream, self).__init__(hs)


class AccountDataStream(Stream):
    """Global or per room account data was changed
    """

    AccountDataStreamRow = namedtuple(
        "AccountDataStream", ("user_id", "room_id", "data_type")  # str  # str  # str
    )

    NAME = "account_data"
    ROW_TYPE = AccountDataStreamRow

    def __init__(self, hs):
        self.store = hs.get_datastore()

        self.current_token = self.store.get_max_account_data_stream_id  # type: ignore

        super(AccountDataStream, self).__init__(hs)

    async def update_function(self, from_token, to_token, limit):
        global_results, room_results = await self.store.get_all_updated_account_data(
            from_token, from_token, to_token, limit
        )

        results = list(room_results)
        results.extend(
            (stream_id, user_id, None, account_data_type)
            for stream_id, user_id, account_data_type in global_results
        )

        return results


class GroupServerStream(Stream):
    GroupsStreamRow = namedtuple(
        "GroupsStreamRow",
        ("group_id", "user_id", "type", "content"),  # str  # str  # str  # dict
    )

    NAME = "groups"
    ROW_TYPE = GroupsStreamRow

    def __init__(self, hs):
        store = hs.get_datastore()

        self.current_token = store.get_group_stream_token  # type: ignore
        self.update_function = store.get_all_groups_changes  # type: ignore

        super(GroupServerStream, self).__init__(hs)


class UserSignatureStream(Stream):
    """A user has signed their own device with their user-signing key
    """

    UserSignatureStreamRow = namedtuple("UserSignatureStreamRow", ("user_id"))  # str

    NAME = "user_signature"
    ROW_TYPE = UserSignatureStreamRow

    def __init__(self, hs):
        store = hs.get_datastore()

        self.current_token = store.get_device_stream_token  # type: ignore
        self.update_function = store.get_all_user_signature_changes_for_remotes  # type: ignore

        super(UserSignatureStream, self).__init__(hs)
