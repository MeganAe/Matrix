# -*- coding: utf-8 -*-
# Copyright 2016 OpenMarket Ltd
# Copyright 2017 Vector Creations Ltd
# Copyright 2018-2019 New Vector Ltd
# Copyright 2019 The Matrix.org Foundation C.I.C.
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
import re

from synapse.http.servlet import RestServlet

logger = logging.getLogger(__name__)


class VersionsRestServlet(RestServlet):
    PATTERNS = [re.compile("^/_matrix/client/versions$")]

    def __init__(self, hs):
        super(VersionsRestServlet, self).__init__()
        self.config = hs.config

    def on_GET(self, request):
        return (
            200,
            {
                "versions": [
                    # XXX: at some point we need to decide whether we need to include
                    # the previous version numbers, given we've defined r0.3.0 to be
                    # backwards compatible with r0.2.0.  But need to check how
                    # conscientious we've been in compatibility, and decide whether the
                    # middle number is the major revision when at 0.X.Y (as opposed to
                    # X.Y.Z).  And we need to decide whether it's fair to make clients
                    # parse the version string to figure out what's going on.
                    "r0.0.1",
                    "r0.1.0",
                    "r0.2.0",
                    "r0.3.0",
                    "r0.4.0",
                    "r0.5.0",
                ],
                # as per MSC1497:
                "unstable_features": {
                    "m.lazy_load_members": True,
                    # as per MSC2190, as amended by MSC2264
                    # to be removed in r0.6.0
                    "m.id_access_token": True,
                    # Advertise to clients that they need not include an `id_server`
                    # parameter during registration or password reset, as Synapse now decides
                    # itself which identity server to use (or none at all).
                    #
                    # This is also used by a client when they wish to bind a 3PID to their
                    # account, but not bind it to an identity server, the endpoint for which
                    # also requires `id_server`. If the homeserver is handling 3PID
                    # verification itself, there is no need to ask the user for `id_server` to
                    # be supplied.
                    "m.require_identity_server": False,
                    # as per MSC2290
                    "m.separate_add_and_bind": True,
                    # Implements support for label-based filtering as described in
                    # MSC2326.
                    "org.matrix.label_based_filtering": True,
                },
            },
        )


def register_servlets(hs, http_server):
    VersionsRestServlet(hs).register(http_server)
