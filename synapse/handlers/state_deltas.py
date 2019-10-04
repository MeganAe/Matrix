# -*- coding: utf-8 -*-
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

from twisted.internet import defer

logger = logging.getLogger(__name__)


class StateDeltasHandler(object):
    def __init__(self, hs):
        self.store = hs.get_datastore()

    @defer.inlineCallbacks
    def _get_key_change(self, prev_event_id, event_id, key_name, public_value):
        """Given two events check if the `key_name` field in content changed
        from not matching `public_value` to doing so.

        For example, check if `history_visibility` (`key_name`) changed from
        `shared` to `world_readable` (`public_value`).

        Returns:
            None if the field in the events either both match `public_value`
            or if neither do, i.e. there has been no change.
            True if it didnt match `public_value` but now does
            False if it did match `public_value` but now doesn't
        """
        prev_event = None
        event = None
        if prev_event_id:
            prev_event = yield self.store.get_event(prev_event_id, allow_none=True)

        if event_id:
            event = yield self.store.get_event(event_id, allow_none=True)

        if not event and not prev_event:
            logger.debug("Neither event exists: %r %r", prev_event_id, event_id)
            return None

        prev_value = None
        value = None

        if prev_event:
            prev_value = prev_event.content.get(key_name)

        if event:
            value = event.content.get(key_name)

        logger.debug("prev_value: %r -> value: %r", prev_value, value)

        if value == public_value and prev_value != public_value:
            return True
        elif value != public_value and prev_value == public_value:
            return False
        else:
            return None
