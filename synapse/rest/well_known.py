# Copyright 2018 New Vector Ltd
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
from typing import TYPE_CHECKING, Optional

from twisted.web.resource import Resource
from twisted.web.server import Request

from synapse.http.server import set_cors_headers
from synapse.types import JsonDict
from synapse.util import json_encoder

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class WellKnownBuilder:
    def __init__(self, hs: "HomeServer"):
        self._config = hs.config

    def get_well_known(self) -> Optional[JsonDict]:
        # if we don't have a public_baseurl, we can't help much here.
        if self._config.server.public_baseurl is None:
            return None

        result = {"m.homeserver": {"base_url": self._config.server.public_baseurl}}

        if self._config.default_identity_server:
            result["m.identity_server"] = {
                "base_url": self._config.default_identity_server
            }

        return result


class WellKnownResource(Resource):
    """A Twisted web resource which renders the .well-known file"""

    isLeaf = 1

    def __init__(self, hs: "HomeServer"):
        Resource.__init__(self)
        self._well_known_builder = WellKnownBuilder(hs)

    def render_GET(self, request: Request) -> bytes:
        set_cors_headers(request)
        r = self._well_known_builder.get_well_known()
        if not r:
            request.setResponseCode(404)
            request.setHeader(b"Content-Type", b"text/plain")
            return b".well-known not available"

        logger.debug("returning: %s", r)
        request.setHeader(b"Content-Type", b"application/json")
        return json_encoder.encode(r).encode("utf-8")
