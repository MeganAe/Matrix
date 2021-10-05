# Copyright 2015, 2016 OpenMarket Ltd
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

from typing import TYPE_CHECKING

from twisted.web.resource import Resource

from .local_key_resource import LocalKey
from .remote_key_resource import RemoteKey

if TYPE_CHECKING:
    from synapse.server import HomeServer


class KeyApiV2Resource(Resource):
    def __init__(self, hs: "HomeServer"):
        Resource.__init__(self)
        self.putChild(b"server", LocalKey(hs))
        self.putChild(b"query", RemoteKey(hs))
