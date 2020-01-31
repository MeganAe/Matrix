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

import re

from synapse.api.errors import AuthError


def historical_admin_path_patterns(path_regex):
    """Returns the list of patterns for an admin endpoint, including historical ones

    This is a backwards-compatibility hack. Previously, the Admin API was exposed at
    various paths under /_matrix/client. This function returns a list of patterns
    matching those paths (as well as the new one), so that existing scripts which rely
    on the endpoints being available there are not broken.

    Note that this should only be used for existing endpoints: new ones should just
    register for the /_synapse/admin path.
    """
    return list(
        re.compile(prefix + path_regex)
        for prefix in (
            "^/_synapse/admin/v1",
            "^/_matrix/client/api/v1/admin",
            "^/_matrix/client/unstable/admin",
            "^/_matrix/client/r0/admin",
        )
    )


def admin_patterns(path_regex: str):
    """Returns the list of patterns for an admin endpoint

    Args:
        path_regex: The regex string to match. This should NOT have a ^
            as this will be prefixed.

    Returns:
        A list of regex patterns.
    """
    admin_prefix = "^/_synapse/admin/v1"
    patterns = [re.compile(admin_prefix + path_regex)]
    return patterns


async def assert_requester_is_admin(auth, request):
    """Verify that the requester is an admin user

    WARNING: MAKE SURE YOU YIELD ON THE RESULT!

    Args:
        auth (synapse.api.auth.Auth):
        request (twisted.web.server.Request): incoming request

    Returns:
        Deferred

    Raises:
        AuthError if the requester is not an admin
    """
    requester = await auth.get_user_by_req(request)
    await assert_user_is_admin(auth, requester.user)


async def assert_user_is_admin(auth, user_id):
    """Verify that the given user is an admin user

    WARNING: MAKE SURE YOU YIELD ON THE RESULT!

    Args:
        auth (synapse.api.auth.Auth):
        user_id (UserID):

    Returns:
        Deferred

    Raises:
        AuthError if the user is not an admin
    """

    is_admin = await auth.is_server_admin(user_id)
    if not is_admin:
        raise AuthError(403, "You are not a server admin")
