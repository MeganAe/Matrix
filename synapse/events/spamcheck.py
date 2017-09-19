# -*- coding: utf-8 -*-
# Copyright 2017 New Vector Ltd.
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


def check_event_for_spam(event):
    """Checks if a given event is considered "spammy" by this server.

    If the server considers an event spammy, then it will be rejected if
    sent by a local user. If it is sent by a user on another server, then
    users

    Args:
        event (synapse.events.EventBase): the event to be checked

    Returns:
        bool: True if the event is spammy.
    """
    if not hasattr(event, "content") or "body" not in event.content:
        return False

    # for example:
    #
    # if "the third flower is green" in event.content["body"]:
    #    return True

    return False
