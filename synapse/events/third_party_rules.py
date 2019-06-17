# -*- coding: utf-8 -*-
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

from twisted.internet import defer


class ThirdPartyEventRules(object):
    """Allows server admins to provide a Python module implementing an extra set of rules
    to apply when processing events.

    This is designed to help admins of closed federations with enforcing custom
    behaviours.
    """

    def __init__(self, hs):
        self.third_party_rules = None

        self.store = hs.get_datastore()

        module = None
        config = None
        if hs.config.third_party_event_rules:
            module, config = hs.config.third_party_event_rules

        if module is not None:
            self.third_party_rules = module(config=config)

    @defer.inlineCallbacks
    def check_event_allowed(self, event, context):
        """Check if a provided event should be allowed in the given context.

        Args:
            event (synapse.events.EventBase): The event to be checked.
            context (synapse.events.snapshot.EventContext): The context of the event.

        Returns:
            defer.Deferred(bool), True if the event should be allowed, False if not.
        """
        if self.third_party_rules is None:
            defer.returnValue(True)

        prev_state_ids = yield context.get_prev_state_ids(self.store)

        # Retrieve the state events from the database.
        state_events = {}
        for key, event_id in prev_state_ids.items():
            state_events[key] = yield self.store.get_event(event_id, allow_none=True)

        ret = yield self.third_party_rules.check_event_allowed(event, state_events)
        defer.returnValue(ret)
