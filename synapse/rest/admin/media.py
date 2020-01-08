# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
# Copyright 2018-2019 New Vector Ltd
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

from synapse.api.errors import AuthError, Codes, SynapseError
from synapse.http.servlet import RestServlet, parse_integer
from synapse.rest.admin._base import (
    assert_requester_is_admin,
    assert_user_is_admin,
    historical_admin_path_patterns,
)

logger = logging.getLogger(__name__)


class QuarantineMediaInRoom(RestServlet):
    """Quarantines all media in a room or by a user so that no one can download it via
    this server. This endpoint accepts both a room ID and user ID.
    """

    PATTERNS = historical_admin_path_patterns("/quarantine_media/(?P<identifier>[^/]+)")

    def __init__(self, hs):
        self.store = hs.get_datastore()
        self.auth = hs.get_auth()

    async def on_POST(self, request, identifier: str):
        requester = await self.auth.get_user_by_req(request)
        await assert_user_is_admin(self.auth, requester.user)

        logging.info("Quarantining %s", identifier)

        if identifier.startswith("!"):
            # Quarantine all media in this room
            num_quarantined = await self.store.quarantine_media_ids_in_room(
                identifier, requester.user.to_string()
            )
        elif identifier.startswith("@"):
            # Quarantine all media this user has uploaded
            num_quarantined = await self.store.quarantine_media_ids_by_user(
                identifier, requester.user.to_string()
            )
        else:
            # Quarantine this media id
            server_and_media_id = identifier.split("/")
            if len(server_and_media_id) != 2:
                raise SynapseError(
                    400,
                    "Invalid media_id supplied: '%s'" % identifier,
                    errcode=Codes.INVALID_PARAM,
                )
            server, media_id = server_and_media_id
            await self.store.quarantine_media_by_id(
                server, media_id, requester.user.to_string()
            )
            return 200, {}

        return 200, {"num_quarantined": num_quarantined}


class ListMediaInRoom(RestServlet):
    """Lists all of the media in a given room.
    """

    PATTERNS = historical_admin_path_patterns("/room/(?P<room_id>[^/]+)/media")

    def __init__(self, hs):
        self.store = hs.get_datastore()
        self.auth = hs.get_auth()

    async def on_GET(self, request, room_id):
        requester = await self.auth.get_user_by_req(request)
        is_admin = await self.auth.is_server_admin(requester.user)
        if not is_admin:
            raise AuthError(403, "You are not a server admin")

        local_mxcs, remote_mxcs = await self.store.get_media_mxcs_in_room(room_id)

        return 200, {"local": local_mxcs, "remote": remote_mxcs}


class PurgeMediaCacheRestServlet(RestServlet):
    PATTERNS = historical_admin_path_patterns("/purge_media_cache")

    def __init__(self, hs):
        self.media_repository = hs.get_media_repository()
        self.auth = hs.get_auth()

    async def on_POST(self, request):
        await assert_requester_is_admin(self.auth, request)

        before_ts = parse_integer(request, "before_ts", required=True)
        logger.info("before_ts: %r", before_ts)

        ret = await self.media_repository.delete_old_remote_media(before_ts)

        return 200, ret


def register_servlets_for_media_repo(hs, http_server):
    """
    Media repo specific APIs.
    """
    PurgeMediaCacheRestServlet(hs).register(http_server)
    QuarantineMediaInRoom(hs).register(http_server)
    ListMediaInRoom(hs).register(http_server)
