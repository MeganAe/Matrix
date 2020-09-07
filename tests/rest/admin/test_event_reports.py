# -*- coding: utf-8 -*-
# Copyright 2020 Dirk Klimpel
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

import json

import synapse.rest.admin
from synapse.api.errors import Codes
from synapse.rest.client.v1 import login, room
from synapse.rest.client.v2_alpha import report_event

from tests import unittest


class EventReportsTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        report_event.register_servlets,
    ]

    def prepare(self, reactor, clock, hs):
        self.store = hs.get_datastore()

        self.admin_user = self.register_user("admin", "pass", admin=True)
        self.admin_user_tok = self.login("admin", "pass")

        self.other_user = self.register_user("user", "pass")
        self.other_user_tok = self.login("user", "pass")

        self.room_id1 = self.helper.create_room_as(
            self.other_user, tok=self.other_user_tok, is_public=True
        )
        self.helper.join(self.room_id1, user=self.admin_user, tok=self.admin_user_tok)

        self.room_id2 = self.helper.create_room_as(
            self.other_user, tok=self.other_user_tok, is_public=True
        )
        self.helper.join(self.room_id2, user=self.admin_user, tok=self.admin_user_tok)

        # Two rooms and two users. Every user sends and reports every room event
        for i in range(5):
            self._create_event_and_report(
                room_id=self.room_id1, user_tok=self.other_user_tok,
            )
        for i in range(5):
            self._create_event_and_report(
                room_id=self.room_id2, user_tok=self.other_user_tok,
            )
        for i in range(5):
            self._create_event_and_report(
                room_id=self.room_id1, user_tok=self.admin_user_tok,
            )
        for i in range(5):
            self._create_event_and_report(
                room_id=self.room_id2, user_tok=self.admin_user_tok,
            )

        self.url = "/_synapse/admin/v1/event_reports"

    def test_requester_is_no_admin(self):
        """
        If the user is not a server admin, an error 403 is returned.
        """

        request, channel = self.make_request(
            "GET", self.url, access_token=self.other_user_tok,
        )
        self.render(request)

        self.assertEqual(403, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(Codes.FORBIDDEN, channel.json_body["errcode"])

    def test_default_success(self):
        """
        Testing list of reported events
        """

        request, channel = self.make_request(
            "GET", self.url, access_token=self.admin_user_tok,
        )
        self.render(request)

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(channel.json_body["total"], 20)
        self.assertEqual(len(channel.json_body["event_reports"]), 20)
        self.assertNotIn("next_token", channel.json_body)
        self._check_fields(channel.json_body["event_reports"])

    def test_limit(self):
        """
        Testing list of reported events with limit
        """

        request, channel = self.make_request(
            "GET", self.url + "?limit=5", access_token=self.admin_user_tok,
        )
        self.render(request)

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(channel.json_body["total"], 20)
        self.assertEqual(len(channel.json_body["event_reports"]), 5)
        self.assertEqual(channel.json_body["next_token"], "5")
        self._check_fields(channel.json_body["event_reports"])

    def test_from(self):
        """
        Testing list of reported events with a defined starting point (from)
        """

        request, channel = self.make_request(
            "GET", self.url + "?from=5", access_token=self.admin_user_tok,
        )
        self.render(request)

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(channel.json_body["total"], 20)
        self.assertEqual(len(channel.json_body["event_reports"]), 15)
        self.assertNotIn("next_token", channel.json_body)
        self._check_fields(channel.json_body["event_reports"])

    def test_limit_and_from(self):
        """
        Testing list of reported events with a defined starting point and limit
        """

        request, channel = self.make_request(
            "GET", self.url + "?from=5&limit=10", access_token=self.admin_user_tok,
        )
        self.render(request)

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(channel.json_body["total"], 20)
        self.assertEqual(channel.json_body["next_token"], "15")
        self.assertEqual(len(channel.json_body["event_reports"]), 10)
        self._check_fields(channel.json_body["event_reports"])

    def test_filter_room(self):
        """
        Testing list of reported events with a filter of room
        """

        request, channel = self.make_request(
            "GET",
            self.url + "?room_id=%s" % self.room_id1,
            access_token=self.admin_user_tok,
        )
        self.render(request)

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(channel.json_body["total"], 10)
        self.assertEqual(len(channel.json_body["event_reports"]), 10)
        self.assertNotIn("next_token", channel.json_body)
        self._check_fields(channel.json_body["event_reports"])

        for report in channel.json_body["event_reports"]:
            self.assertEqual(report["room_id"], self.room_id1)

    def test_filter_user(self):
        """
        Testing list of reported events with a filter of user
        """

        request, channel = self.make_request(
            "GET",
            self.url + "?user_id=%s" % self.other_user,
            access_token=self.admin_user_tok,
        )
        self.render(request)

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(channel.json_body["total"], 10)
        self.assertEqual(len(channel.json_body["event_reports"]), 10)
        self.assertNotIn("next_token", channel.json_body)
        self._check_fields(channel.json_body["event_reports"])

        for report in channel.json_body["event_reports"]:
            self.assertEqual(report["user_id"], self.other_user)

    def test_filter_user_and_room(self):
        """
        Testing list of reported events with a filter of user and room
        """

        request, channel = self.make_request(
            "GET",
            self.url + "?user_id=%s&room_id=%s" % (self.other_user, self.room_id1),
            access_token=self.admin_user_tok,
        )
        self.render(request)

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(channel.json_body["total"], 5)
        self.assertEqual(len(channel.json_body["event_reports"]), 5)
        self.assertNotIn("next_token", channel.json_body)
        self._check_fields(channel.json_body["event_reports"])

        for report in channel.json_body["event_reports"]:
            self.assertEqual(report["user_id"], self.other_user)
            self.assertEqual(report["room_id"], self.room_id1)

    def test_valid_search_order(self):
        """
        Testing search order. Order by timestamps.
        """

        # fetch the most recent first, largest timestamp
        request, channel = self.make_request(
            "GET", self.url + "?dir=b", access_token=self.admin_user_tok,
        )
        self.render(request)

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(channel.json_body["total"], 20)
        self.assertEqual(len(channel.json_body["event_reports"]), 20)
        report = 1
        while report < len(channel.json_body["event_reports"]):
            self.assertGreaterEqual(
                channel.json_body["event_reports"][report - 1]["received_ts"],
                channel.json_body["event_reports"][report]["received_ts"],
            )
            report += 1

        # fetch the oldest first, smallest timestamp
        request, channel = self.make_request(
            "GET", self.url + "?dir=f", access_token=self.admin_user_tok,
        )
        self.render(request)

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(channel.json_body["total"], 20)
        self.assertEqual(len(channel.json_body["event_reports"]), 20)
        report = 1
        while report < len(channel.json_body["event_reports"]):
            self.assertLessEqual(
                channel.json_body["event_reports"][report - 1]["received_ts"],
                channel.json_body["event_reports"][report]["received_ts"],
            )
            report += 1

    def test_invalid_search_order(self):
        """
        Testing that a invalid search order returns a 400
        """

        request, channel = self.make_request(
            "GET", self.url + "?dir=bar", access_token=self.admin_user_tok,
        )
        self.render(request)

        self.assertEqual(400, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(Codes.INVALID_PARAM, channel.json_body["errcode"])
        self.assertEqual("Unknown direction: bar", channel.json_body["error"])

    def test_limit_is_negative(self):
        """
        Testing that a negative list parameter returns a 400
        """

        request, channel = self.make_request(
            "GET", self.url + "?limit=-5", access_token=self.admin_user_tok,
        )
        self.render(request)

        self.assertEqual(400, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(Codes.INVALID_PARAM, channel.json_body["errcode"])

    def test_from_is_negative(self):
        """
        Testing that a negative from parameter returns a 400
        """

        request, channel = self.make_request(
            "GET", self.url + "?from=-5", access_token=self.admin_user_tok,
        )
        self.render(request)

        self.assertEqual(400, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(Codes.INVALID_PARAM, channel.json_body["errcode"])

    def _create_event_and_report(self, room_id, user_tok):
        """Create and report events
        """
        resp = self.helper.send(room_id, tok=user_tok)
        event_id = resp["event_id"]

        request, channel = self.make_request(
            "POST",
            "rooms/%s/report/%s" % (room_id, event_id),
            json.dumps({"score": -100, "reason": "this makes me sad"}),
            access_token=user_tok,
        )
        self.render(request)
        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])

    def _check_fields(self, content):
        """Checks that all attributes are present in a event report
        """
        for c in content:
            self.assertIn("id", c)
            self.assertIn("received_ts", c)
            self.assertIn("room_id", c)
            self.assertIn("event_id", c)
            self.assertIn("user_id", c)
            self.assertIn("reason", c)
            self.assertIn("content", c)
