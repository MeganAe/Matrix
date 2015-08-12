# -*- coding: utf-8 -*-
# Copyright 2015 OpenMarket Ltd
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

from synapse.api.errors import SynapseError
from synapse.http.servlet import RestServlet
from syutil.jsonutil import encode_canonical_json

from ._base import client_v2_pattern

import simplejson as json
import logging

logger = logging.getLogger(__name__)


class KeyUploadServlet(RestServlet):
    """
    POST /keys/upload/<device_id> HTTP/1.1
    Content-Type: application/json

    {
      "device_keys": {
        "user_id": "<user_id>",
        "device_id": "<device_id>",
        "valid_until_ts": <millisecond_timestamp>,
        "algorithms": [
          "m.olm.curve25519-aes-sha256",
        ]
        "keys": {
          "<algorithm>:<device_id>": "<key_base64>",
        },
        "signatures:" {
          "<user_id>" {
            "<algorithm>:<device_id>": "<signature_base64>"
      } } },
      "one_time_keys": {
        "<algorithm>:<key_id>": "<key_base64>"
      },
    }
    """
    PATTERN = client_v2_pattern("/keys/upload/(?P<device_id>[^/]*)")

    def __init__(self, hs):
        super(KeyUploadServlet, self).__init__()
        self.store = hs.get_datastore()
        self.clock = hs.get_clock()
        self.auth = hs.get_auth()

    @defer.inlineCallbacks
    def on_POST(self, request, device_id):
        auth_user, client_info = yield self.auth.get_user_by_req(request)
        user_id = auth_user.to_string()
        # TODO: Check that the device_id matches that in the authentication
        # or derive the device_id from the authentication instead.
        try:
            body = json.loads(request.content.read())
        except:
            raise SynapseError(400, "Invalid key JSON")
        time_now = self.clock.time_msec()

        # TODO: Validate the JSON to make sure it has the right keys.
        device_keys = body.get("device_keys", None)
        if device_keys:
            logger.info(
                "Updating device_keys for device %r for user %r at %d",
                device_id, auth_user, time_now
            )
            # TODO: Sign the JSON with the server key
            yield self.store.set_e2e_device_keys(
                user_id, device_id, time_now,
                encode_canonical_json(device_keys)
            )

        one_time_keys = body.get("one_time_keys", None)
        if one_time_keys:
            logger.info(
                "Adding %d one_time_keys for device %r for user %r at %d",
                len(one_time_keys), device_id, user_id, time_now
            )
            key_list = []
            for key_id, key_json in one_time_keys.items():
                algorithm, key_id = key_id.split(":")
                key_list.append((
                    algorithm, key_id, encode_canonical_json(key_json)
                ))

            yield self.store.add_e2e_one_time_keys(
                user_id, device_id, time_now, key_list
            )

        result = yield self.store.count_e2e_one_time_keys(user_id, device_id)
        defer.returnValue((200, {"one_time_key_counts": result}))

    @defer.inlineCallbacks
    def on_GET(self, request, device_id):
        auth_user, client_info = yield self.auth.get_user_by_req(request)
        user_id = auth_user.to_string()

        result = yield self.store.count_e2e_one_time_keys(user_id, device_id)
        defer.returnValue((200, {"one_time_key_counts": result}))


class KeyQueryServlet(RestServlet):
    """
    GET /keys/query/<user_id> HTTP/1.1

    GET /keys/query/<user_id>/<device_id> HTTP/1.1

    POST /keys/query HTTP/1.1
    Content-Type: application/json
    {
      "device_keys": {
        "<user_id>": ["<device_id>"]
    } }

    HTTP/1.1 200 OK
    {
      "device_keys": {
        "<user_id>": {
          "<device_id>": {
            "user_id": "<user_id>", // Duplicated to be signed
            "device_id": "<device_id>", // Duplicated to be signed
            "valid_until_ts": <millisecond_timestamp>,
            "algorithms": [ // List of supported algorithms
              "m.olm.curve25519-aes-sha256",
            ],
            "keys": { // Must include a ed25519 signing key
              "<algorithm>:<key_id>": "<key_base64>",
            },
            "signatures:" {
              // Must be signed with device's ed25519 key
              "<user_id>/<device_id>": {
                "<algorithm>:<key_id>": "<signature_base64>"
              }
              // Must be signed by this server.
              "<server_name>": {
                "<algorithm>:<key_id>": "<signature_base64>"
    } } } } } }
    """

    PATTERN = client_v2_pattern(
        "/keys/query(?:"
        "/(?P<user_id>[^/]*)(?:"
        "/(?P<device_id>[^/]*)"
        ")?"
        ")?"
    )

    def __init__(self, hs):
        super(KeyQueryServlet, self).__init__()
        self.store = hs.get_datastore()
        self.auth = hs.get_auth()

    @defer.inlineCallbacks
    def on_POST(self, request, user_id, device_id):
        logger.debug("onPOST")
        yield self.auth.get_user_by_req(request)
        try:
            body = json.loads(request.content.read())
        except:
            raise SynapseError(400, "Invalid key JSON")
        query = []
        for user_id, device_ids in body.get("device_keys", {}).items():
            if not device_ids:
                query.append((user_id, None))
            else:
                for device_id in device_ids:
                    query.append((user_id, device_id))
        results = yield self.store.get_e2e_device_keys(query)
        defer.returnValue(self.json_result(request, results))

    @defer.inlineCallbacks
    def on_GET(self, request, user_id, device_id):
        auth_user, client_info = yield self.auth.get_user_by_req(request)
        auth_user_id = auth_user.to_string()
        if not user_id:
            user_id = auth_user_id
        if not device_id:
            device_id = None
        # Returns a map of user_id->device_id->json_bytes.
        results = yield self.store.get_e2e_device_keys([(user_id, device_id)])
        defer.returnValue(self.json_result(request, results))

    def json_result(self, request, results):
        json_result = {}
        for user_id, device_keys in results.items():
            for device_id, json_bytes in device_keys.items():
                json_result.setdefault(user_id, {})[device_id] = json.loads(
                    json_bytes
                )
        return (200, {"device_keys": json_result})


class OneTimeKeyServlet(RestServlet):
    """
    GET /keys/claim/<user-id>/<device-id>/<algorithm> HTTP/1.1

    POST /keys/claim HTTP/1.1
    {
      "one_time_keys": {
        "<user_id>": {
          "<device_id>": "<algorithm>"
    } } }

    HTTP/1.1 200 OK
    {
      "one_time_keys": {
        "<user_id>": {
          "<device_id>": {
            "<algorithm>:<key_id>": "<key_base64>"
    } } } }

    """
    PATTERN = client_v2_pattern(
        "/keys/claim(?:/?|(?:/"
        "(?P<user_id>[^/]*)/(?P<device_id>[^/]*)/(?P<algorithm>[^/]*)"
        ")?)"
    )

    def __init__(self, hs):
        super(OneTimeKeyServlet, self).__init__()
        self.store = hs.get_datastore()
        self.auth = hs.get_auth()
        self.clock = hs.get_clock()

    @defer.inlineCallbacks
    def on_GET(self, request, user_id, device_id, algorithm):
        yield self.auth.get_user_by_req(request)
        results = yield self.store.claim_e2e_one_time_keys(
            [(user_id, device_id, algorithm)]
        )
        defer.returnValue(self.json_result(request, results))

    @defer.inlineCallbacks
    def on_POST(self, request, user_id, device_id, algorithm):
        yield self.auth.get_user_by_req(request)
        try:
            body = json.loads(request.content.read())
        except:
            raise SynapseError(400, "Invalid key JSON")
        query = []
        for user_id, device_keys in body.get("one_time_keys", {}).items():
            for device_id, algorithm in device_keys.items():
                query.append((user_id, device_id, algorithm))
        results = yield self.store.claim_e2e_one_time_keys(query)
        defer.returnValue(self.json_result(request, results))

    def json_result(self, request, results):
        json_result = {}
        for user_id, device_keys in results.items():
            for device_id, keys in device_keys.items():
                for key_id, json_bytes in keys.items():
                    json_result.setdefault(user_id, {})[device_id] = {
                        key_id: json.loads(json_bytes)
                    }
        return (200, {"one_time_keys": json_result})


def register_servlets(hs, http_server):
    KeyUploadServlet(hs).register(http_server)
    KeyQueryServlet(hs).register(http_server)
    OneTimeKeyServlet(hs).register(http_server)
