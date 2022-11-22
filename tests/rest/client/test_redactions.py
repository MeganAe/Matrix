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
from typing import List, Optional

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import EventTypes, RelationTypes
from synapse.rest import admin
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util import Clock

from tests.unittest import HomeserverTestCase, override_config


class RedactionsTestCase(HomeserverTestCase):
    """Tests that various redaction events are handled correctly"""

    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
        sync.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        config = self.default_config()

        config["rc_message"] = {"per_second": 0.2, "burst_count": 10}
        config["rc_admin_redaction"] = {"per_second": 1, "burst_count": 100}

        return self.setup_test_homeserver(config=config)

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        # register a couple of users
        self.mod_user_id = self.register_user("user1", "pass")
        self.mod_access_token = self.login("user1", "pass")
        self.other_user_id = self.register_user("otheruser", "pass")
        self.other_access_token = self.login("otheruser", "pass")

        # Create a room
        self.room_id = self.helper.create_room_as(
            self.mod_user_id, tok=self.mod_access_token
        )

        # Invite the other user
        self.helper.invite(
            room=self.room_id,
            src=self.mod_user_id,
            tok=self.mod_access_token,
            targ=self.other_user_id,
        )
        # The other user joins
        self.helper.join(
            room=self.room_id, user=self.other_user_id, tok=self.other_access_token
        )

    def _redact_event(
        self,
        access_token: str,
        room_id: str,
        event_id: str,
        expect_code: int = 200,
        with_relations: Optional[List[str]] = None,
    ) -> JsonDict:
        """Helper function to send a redaction event.

        Returns the json body.
        """
        path = "/_matrix/client/r0/rooms/%s/redact/%s" % (room_id, event_id)

        request_content = {}
        if with_relations:
            request_content["org.matrix.msc3912.with_relations"] = with_relations

        channel = self.make_request(
            "POST", path, request_content, access_token=access_token
        )
        self.assertEqual(channel.code, expect_code)
        return channel.json_body

    def _sync_room_timeline(self, access_token: str, room_id: str) -> List[JsonDict]:
        channel = self.make_request("GET", "sync", access_token=self.mod_access_token)
        self.assertEqual(channel.code, 200)
        room_sync = channel.json_body["rooms"]["join"][room_id]
        return room_sync["timeline"]["events"]

    def test_redact_event_as_moderator(self) -> None:
        # as a regular user, send a message to redact
        b = self.helper.send(room_id=self.room_id, tok=self.other_access_token)
        msg_id = b["event_id"]

        # as the moderator, send a redaction
        b = self._redact_event(self.mod_access_token, self.room_id, msg_id)
        redaction_id = b["event_id"]

        # now sync
        timeline = self._sync_room_timeline(self.mod_access_token, self.room_id)

        # the last event should be the redaction
        self.assertEqual(timeline[-1]["event_id"], redaction_id)
        self.assertEqual(timeline[-1]["redacts"], msg_id)

        # and the penultimate should be the redacted original
        self.assertEqual(timeline[-2]["event_id"], msg_id)
        self.assertEqual(timeline[-2]["unsigned"]["redacted_by"], redaction_id)
        self.assertEqual(timeline[-2]["content"], {})

    def test_redact_event_as_normal(self) -> None:
        # as a regular user, send a message to redact
        b = self.helper.send(room_id=self.room_id, tok=self.other_access_token)
        normal_msg_id = b["event_id"]

        # also send one as the admin
        b = self.helper.send(room_id=self.room_id, tok=self.mod_access_token)
        admin_msg_id = b["event_id"]

        # as a normal, try to redact the admin's event
        self._redact_event(
            self.other_access_token, self.room_id, admin_msg_id, expect_code=403
        )

        # now try to redact our own event
        b = self._redact_event(self.other_access_token, self.room_id, normal_msg_id)
        redaction_id = b["event_id"]

        # now sync
        timeline = self._sync_room_timeline(self.other_access_token, self.room_id)

        # the last event should be the redaction of the normal event
        self.assertEqual(timeline[-1]["event_id"], redaction_id)
        self.assertEqual(timeline[-1]["redacts"], normal_msg_id)

        # the penultimate should be the unredacted one from the admin
        self.assertEqual(timeline[-2]["event_id"], admin_msg_id)
        self.assertNotIn("redacted_by", timeline[-2]["unsigned"])
        self.assertTrue(timeline[-2]["content"]["body"], {})

        # and the antepenultimate should be the redacted normal
        self.assertEqual(timeline[-3]["event_id"], normal_msg_id)
        self.assertEqual(timeline[-3]["unsigned"]["redacted_by"], redaction_id)
        self.assertEqual(timeline[-3]["content"], {})

    def test_redact_nonexistent_event(self) -> None:
        # control case: an existing event
        b = self.helper.send(room_id=self.room_id, tok=self.other_access_token)
        msg_id = b["event_id"]
        b = self._redact_event(self.other_access_token, self.room_id, msg_id)
        redaction_id = b["event_id"]

        # room moderators can send redactions for non-existent events
        self._redact_event(self.mod_access_token, self.room_id, "$zzz")

        # ... but normals cannot
        self._redact_event(
            self.other_access_token, self.room_id, "$zzz", expect_code=404
        )

        # when we sync, we should see only the valid redaction
        timeline = self._sync_room_timeline(self.other_access_token, self.room_id)
        self.assertEqual(timeline[-1]["event_id"], redaction_id)
        self.assertEqual(timeline[-1]["redacts"], msg_id)

        # and the penultimate should be the redacted original
        self.assertEqual(timeline[-2]["event_id"], msg_id)
        self.assertEqual(timeline[-2]["unsigned"]["redacted_by"], redaction_id)
        self.assertEqual(timeline[-2]["content"], {})

    def test_redact_create_event(self) -> None:
        # control case: an existing event
        b = self.helper.send(room_id=self.room_id, tok=self.mod_access_token)
        msg_id = b["event_id"]
        self._redact_event(self.mod_access_token, self.room_id, msg_id)

        # sync the room, to get the id of the create event
        timeline = self._sync_room_timeline(self.other_access_token, self.room_id)
        create_event_id = timeline[0]["event_id"]

        # room moderators cannot send redactions for create events
        self._redact_event(
            self.mod_access_token, self.room_id, create_event_id, expect_code=403
        )

        # and nor can normals
        self._redact_event(
            self.other_access_token, self.room_id, create_event_id, expect_code=403
        )

    def test_redact_event_as_moderator_ratelimit(self) -> None:
        """Tests that the correct ratelimiting is applied to redactions"""

        message_ids = []
        # as a regular user, send messages to redact
        for _ in range(20):
            b = self.helper.send(room_id=self.room_id, tok=self.other_access_token)
            message_ids.append(b["event_id"])
            self.reactor.advance(10)  # To get around ratelimits

        # as the moderator, send a bunch of redactions
        for msg_id in message_ids:
            # These should all succeed, even though this would be denied by
            # the standard message ratelimiter
            self._redact_event(self.mod_access_token, self.room_id, msg_id)

    @override_config({"experimental_features": {"msc3912_enabled": True}})
    def test_redact_relations(self) -> None:
        """Tests that we can redact the relations of an event at the same time as the
        event itself.
        """
        # Send a root event.
        res = self.helper.send_event(
            room_id=self.room_id,
            type=EventTypes.Message,
            content={"msgtype": "m.text", "body": "hello"},
            tok=self.mod_access_token,
        )
        root_event_id = res["event_id"]

        # Send an edit to this root event.
        res = self.helper.send_event(
            room_id=self.room_id,
            type=EventTypes.Message,
            content={
                "body": " * hello world",
                "m.new_content": {
                    "body": "hello world",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": root_event_id,
                    "rel_type": RelationTypes.REPLACE,
                },
                "msgtype": "m.text",
            },
            tok=self.mod_access_token,
        )
        edit_event_id = res["event_id"]

        # Also send a threaded message whose root is the same as the edit's.
        res = self.helper.send_event(
            room_id=self.room_id,
            type=EventTypes.Message,
            content={
                "msgtype": "m.text",
                "body": "message 1",
                "m.relates_to": {
                    "event_id": root_event_id,
                    "rel_type": RelationTypes.THREAD,
                },
            },
            tok=self.mod_access_token,
        )
        threaded_event_id = res["event_id"]

        # Also send a reaction, again with the same root.
        res = self.helper.send_event(
            room_id=self.room_id,
            type=EventTypes.Reaction,
            content={
                "m.relates_to": {
                    "rel_type": RelationTypes.ANNOTATION,
                    "event_id": root_event_id,
                    "key": "👍",
                }
            },
            tok=self.mod_access_token,
        )
        reaction_event_id = res["event_id"]

        # Redact the root event, specifying that we also want to delete events that
        # relate to it with m.replace.
        self._redact_event(
            self.mod_access_token,
            self.room_id,
            root_event_id,
            with_relations=[
                RelationTypes.REPLACE,
                RelationTypes.THREAD,
            ],
        )

        # Check that the root event got redacted.
        event_dict = self.helper.get_event(
            self.room_id, root_event_id, self.mod_access_token
        )
        self.assertIn("redacted_because", event_dict, event_dict)

        # Check that the edit got redacted.
        event_dict = self.helper.get_event(
            self.room_id, edit_event_id, self.mod_access_token
        )
        self.assertIn("redacted_because", event_dict, event_dict)

        # Check that the threaded message got redacted.
        event_dict = self.helper.get_event(
            self.room_id, threaded_event_id, self.mod_access_token
        )
        self.assertIn("redacted_because", event_dict, event_dict)

        # Check that the reaction did not get redacted.
        event_dict = self.helper.get_event(
            self.room_id, reaction_event_id, self.mod_access_token
        )
        self.assertNotIn("redacted_because", event_dict, event_dict)

    @override_config({"experimental_features": {"msc3912_enabled": True}})
    def test_redact_relations_no_perms(self) -> None:
        """Tests that, when redacting a message along with its relations, if not all
        the related messages can be redacted because of insufficient permissions, the
        server still redacts all the ones that can be.
        """
        # Send a root event.
        res = self.helper.send_event(
            room_id=self.room_id,
            type=EventTypes.Message,
            content={
                "msgtype": "m.text",
                "body": "root",
            },
            tok=self.other_access_token,
        )
        root_event_id = res["event_id"]

        # Send a first threaded message, this one from the moderator. We do this for the
        # first message with the m.thread relation (and not the last one) to ensure
        # that, when the server fails to redact it, it doesn't stop there, and it
        # instead goes on to redact the other one.
        res = self.helper.send_event(
            room_id=self.room_id,
            type=EventTypes.Message,
            content={
                "msgtype": "m.text",
                "body": "message 1",
                "m.relates_to": {
                    "event_id": root_event_id,
                    "rel_type": RelationTypes.THREAD,
                },
            },
            tok=self.mod_access_token,
        )
        first_threaded_event_id = res["event_id"]

        # Send a second threaded message, this time from the user who'll perform the
        # redaction.
        res = self.helper.send_event(
            room_id=self.room_id,
            type=EventTypes.Message,
            content={
                "msgtype": "m.text",
                "body": "message 2",
                "m.relates_to": {
                    "event_id": root_event_id,
                    "rel_type": RelationTypes.THREAD,
                },
            },
            tok=self.other_access_token,
        )
        second_threaded_event_id = res["event_id"]

        # Redact the thread's root, and request that all threaded messages are also
        # redacted. Send that request from the non-mod user, so that the first threaded
        # event cannot be redacted.
        self._redact_event(
            self.other_access_token,
            self.room_id,
            root_event_id,
            with_relations=[RelationTypes.THREAD],
        )

        # Check that the thread root got redacted.
        event_dict = self.helper.get_event(
            self.room_id, root_event_id, self.other_access_token
        )
        self.assertIn("redacted_because", event_dict, event_dict)

        # Check that the last message in the thread got redacted, despite failing to
        # redact the one before it.
        event_dict = self.helper.get_event(
            self.room_id, second_threaded_event_id, self.other_access_token
        )
        self.assertIn("redacted_because", event_dict, event_dict)

        # Check that the message that was sent into the tread by the mod user is not
        # redacted.
        event_dict = self.helper.get_event(
            self.room_id, first_threaded_event_id, self.other_access_token
        )
        self.assertIn("body", event_dict["content"], event_dict)
        self.assertEqual("message 1", event_dict["content"]["body"])

    @override_config({"experimental_features": {"msc3912_enabled": True}})
    def test_redact_relations_txn_id_reuse(self) -> None:
        """Tests that redacting a message using a transaction ID, then reusing the same
        transaction ID but providing an additional list of relations to redact, is
        effectively a no-op.
        """
        # Send a root event.
        res = self.helper.send_event(
            room_id=self.room_id,
            type=EventTypes.Message,
            content={
                "msgtype": "m.text",
                "body": "root",
            },
            tok=self.mod_access_token,
        )
        root_event_id = res["event_id"]

        # Send a first threaded message.
        res = self.helper.send_event(
            room_id=self.room_id,
            type=EventTypes.Message,
            content={
                "msgtype": "m.text",
                "body": "I'm in a thread!",
                "m.relates_to": {
                    "event_id": root_event_id,
                    "rel_type": RelationTypes.THREAD,
                },
            },
            tok=self.mod_access_token,
        )
        threaded_event_id = res["event_id"]

        # Send a first redaction request which redacts only the root event.
        channel = self.make_request(
            method="PUT",
            path=f"/rooms/{self.room_id}/redact/{root_event_id}/foo",
            content={},
            access_token=self.mod_access_token,
        )
        self.assertEqual(channel.code, 200)

        # Send a second redaction request which redacts the root event as well as
        # threaded messages.
        channel = self.make_request(
            method="PUT",
            path=f"/rooms/{self.room_id}/redact/{root_event_id}/foo",
            content={"org.matrix.msc3912.with_relations": [RelationTypes.THREAD]},
            access_token=self.mod_access_token,
        )
        self.assertEqual(channel.code, 200)

        # Check that the root event got redacted.
        event_dict = self.helper.get_event(
            self.room_id, root_event_id, self.mod_access_token
        )
        self.assertIn("redacted_because", event_dict)

        # Check that the threaded message didn't get redacted (since that wasn't part of
        # the original redaction).
        event_dict = self.helper.get_event(
            self.room_id, threaded_event_id, self.mod_access_token
        )
        self.assertIn("body", event_dict["content"], event_dict)
        self.assertEqual("I'm in a thread!", event_dict["content"]["body"])
