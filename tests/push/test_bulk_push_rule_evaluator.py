# Copyright 2022 The Matrix.org Foundation C.I.C.
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

from typing import Any
from unittest.mock import patch

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import EventContentFields
from synapse.api.room_versions import RoomVersions
from synapse.push.bulk_push_rule_evaluator import BulkPushRuleEvaluator
from synapse.rest import admin
from synapse.rest.client import login, register, room
from synapse.server import HomeServer
from synapse.types import create_requester
from synapse.util import Clock

from tests.test_utils import simple_async_mock
from tests.unittest import HomeserverTestCase, override_config


class TestBulkPushRuleEvaluator(HomeserverTestCase):

    servlets = [
        admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        login.register_servlets,
        register.register_servlets,
    ]

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        # Create a new user and room.
        self.alice = self.register_user("alice", "pass")
        self.token = self.login(self.alice, "pass")
        self.requester = create_requester(self.alice)

        self.room_id = self.helper.create_room_as(
            self.alice, room_version=RoomVersions.V9.identifier, tok=self.token
        )

        self.event_creation_handler = self.hs.get_event_creation_handler()

    def test_action_for_event_by_user_handles_noninteger_power_levels(self) -> None:
        """We should convert floats and strings to integers before passing to Rust.

        Reproduces #14060.

        A lack of validation: the gift that keeps on giving.
        """

        # Alter the power levels in that room to include stringy and floaty levels.
        # We need to suppress the validation logic or else it will reject these dodgy
        # values. (Presumably this validation was not always present.)
        with patch("synapse.events.validator.validate_canonicaljson"), patch(
            "synapse.events.validator.jsonschema.validate"
        ):
            self.helper.send_state(
                self.room_id,
                "m.room.power_levels",
                {
                    "users": {self.alice: "100"},  # stringy
                    "notifications": {"room": 100.0},  # float
                },
                self.token,
                state_key="",
            )

        # Create a new message event, and try to evaluate it under the dodgy
        # power level event.
        event, context = self.get_success(
            self.event_creation_handler.create_event(
                self.requester,
                {
                    "type": "m.room.message",
                    "room_id": self.room_id,
                    "content": {
                        "msgtype": "m.text",
                        "body": "helo",
                    },
                    "sender": self.alice,
                },
            )
        )

        bulk_evaluator = BulkPushRuleEvaluator(self.hs)
        # should not raise
        self.get_success(bulk_evaluator.action_for_events_by_user([(event, context)]))

    @override_config({"push": {"enabled": False}})
    def test_action_for_event_by_user_disabled_by_config(self) -> None:
        """Ensure that push rules are not calculated when disabled in the config"""

        # Create a new message event which should cause a notification.
        event, context = self.get_success(
            self.event_creation_handler.create_event(
                self.requester,
                {
                    "type": "m.room.message",
                    "room_id": self.room_id,
                    "content": {
                        "msgtype": "m.text",
                        "body": "helo",
                    },
                    "sender": self.alice,
                },
            )
        )

        bulk_evaluator = BulkPushRuleEvaluator(self.hs)
        # Mock the method which calculates push rules -- we do this instead of
        # e.g. checking the results in the database because we want to ensure
        # that code isn't even running.
        bulk_evaluator._action_for_event_by_user = simple_async_mock()  # type: ignore[assignment]

        # Ensure no actions are generated!
        self.get_success(bulk_evaluator.action_for_events_by_user([(event, context)]))
        bulk_evaluator._action_for_event_by_user.assert_not_called()

    @override_config({"experimental_features": {"msc3952_intentional_mentions": True}})
    def test_mentions(self) -> None:
        """Test the behavior of an event which includes invalid mentions."""
        bulk_evaluator = BulkPushRuleEvaluator(self.hs)

        sentinel = object()

        def create_and_process(mentions: Any = sentinel) -> bool:
            """Returns true iff the `mentions` trigger an event push action."""
            content = {}
            if mentions is not sentinel:
                content[EventContentFields.MSC3952_MENTIONS] = mentions

            # Create a new message event which should cause a notification.
            event, context = self.get_success(
                self.event_creation_handler.create_event(
                    self.requester,
                    {
                        "type": "test",
                        "room_id": self.room_id,
                        "content": content,
                        "sender": f"@bob:{self.hs.hostname}",
                    },
                )
            )

            # Ensure no actions are generated!
            self.get_success(
                bulk_evaluator.action_for_events_by_user([(event, context)])
            )

            # If any actions are generated for this event, return true.
            result = self.get_success(
                self.hs.get_datastores().main.db_pool.simple_select_list(
                    table="event_push_actions_staging",
                    keyvalues={"event_id": event.event_id},
                    retcols=("*",),
                    desc="get_event_push_actions_staging",
                )
            )
            return len(result) > 0

        # Not including the mentions field should not notify.
        self.assertFalse(create_and_process())
        # An empty mentions field should not notify.
        self.assertFalse(create_and_process({}))

        # Non-dict mentions should be ignored.
        mentions: Any
        for mentions in (None, True, False, 1, "foo", []):
            self.assertFalse(create_and_process(mentions))

        # A non-list should be ignored.
        for mentions in (None, True, False, 1, "foo", {}):
            self.assertFalse(create_and_process({"user_ids": mentions}))

        # The Matrix ID appearing anywhere in the list should notify.
        self.assertTrue(create_and_process({"user_ids": [self.alice]}))
        self.assertTrue(create_and_process({"user_ids": ["@another:test", self.alice]}))

        # Duplicate user IDs should notify.
        self.assertTrue(create_and_process({"user_ids": [self.alice, self.alice]}))

        # Invalid entries in the list are ignored.
        self.assertFalse(create_and_process({"user_ids": [None, True, False, {}, []]}))
        self.assertTrue(
            create_and_process({"user_ids": [None, True, False, {}, [], self.alice]})
        )

        # Room mentions from those without power should not notify.
        self.assertFalse(create_and_process({"room": True}))

        # Room mentions from those with power should notify.
        self.helper.send_state(
            self.room_id,
            "m.room.power_levels",
            {"notifications": {"room": 0}},
            self.token,
            state_key="",
        )
        self.assertTrue(create_and_process({"room": True}))

        # Invalid data should not notify.
        for mentions in (None, False, 1, "foo", [], {}):
            self.assertFalse(create_and_process({"room": mentions}))
