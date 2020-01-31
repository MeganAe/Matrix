# -*- coding: utf-8 -*-
# Copyright 2016 OpenMarket Ltd
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

from six import iteritems

import attr
from canonicaljson import encode_canonical_json, json
from signedjson.key import decode_verify_key_bytes
from signedjson.sign import SignatureVerifyException, verify_signed_json
from unpaddedbase64 import decode_base64

from twisted.internet import defer

from synapse.api.errors import CodeMessageException, Codes, NotFoundError, SynapseError
from synapse.logging.context import make_deferred_yieldable, run_in_background
from synapse.logging.opentracing import log_kv, set_tag, tag_args, trace
from synapse.replication.http.devices import ReplicationUserDevicesResyncRestServlet
from synapse.types import (
    UserID,
    get_domain_from_id,
    get_verify_key_from_cross_signing_key,
)
from synapse.util import unwrapFirstError
from synapse.util.async_helpers import Linearizer
from synapse.util.caches.expiringcache import ExpiringCache
from synapse.util.retryutils import NotRetryingDestination

logger = logging.getLogger(__name__)


class E2eKeysHandler(object):
    def __init__(self, hs):
        self.store = hs.get_datastore()
        self.federation = hs.get_federation_client()
        self.device_handler = hs.get_device_handler()
        self.is_mine = hs.is_mine
        self.clock = hs.get_clock()

        self._edu_updater = SigningKeyEduUpdater(hs, self)

        self._is_master = hs.config.worker_app is None
        if not self._is_master:
            self._user_device_resync_client = ReplicationUserDevicesResyncRestServlet.make_client(
                hs
            )

        federation_registry = hs.get_federation_registry()

        # FIXME: switch to m.signing_key_update when MSC1756 is merged into the spec
        federation_registry.register_edu_handler(
            "org.matrix.signing_key_update",
            self._edu_updater.incoming_signing_key_update,
        )
        # doesn't really work as part of the generic query API, because the
        # query request requires an object POST, but we abuse the
        # "query handler" interface.
        federation_registry.register_query_handler(
            "client_keys", self.on_federation_query_client_keys
        )

    @trace
    @defer.inlineCallbacks
    def query_devices(self, query_body, timeout, from_user_id):
        """ Handle a device key query from a client

        {
            "device_keys": {
                "<user_id>": ["<device_id>"]
            }
        }
        ->
        {
            "device_keys": {
                "<user_id>": {
                    "<device_id>": {
                        ...
                    }
                }
            }
        }

        Args:
            from_user_id (str): the user making the query.  This is used when
                adding cross-signing signatures to limit what signatures users
                can see.
        """

        device_keys_query = query_body.get("device_keys", {})

        # separate users by domain.
        # make a map from domain to user_id to device_ids
        local_query = {}
        remote_queries = {}

        for user_id, device_ids in device_keys_query.items():
            # we use UserID.from_string to catch invalid user ids
            if self.is_mine(UserID.from_string(user_id)):
                local_query[user_id] = device_ids
            else:
                remote_queries[user_id] = device_ids

        set_tag("local_key_query", local_query)
        set_tag("remote_key_query", remote_queries)

        # First get local devices.
        failures = {}
        results = {}
        if local_query:
            local_result = yield self.query_local_devices(local_query)
            for user_id, keys in local_result.items():
                if user_id in local_query:
                    results[user_id] = keys

        # Now attempt to get any remote devices from our local cache.
        remote_queries_not_in_cache = {}
        if remote_queries:
            query_list = []
            for user_id, device_ids in iteritems(remote_queries):
                if device_ids:
                    query_list.extend((user_id, device_id) for device_id in device_ids)
                else:
                    query_list.append((user_id, None))

            (
                user_ids_not_in_cache,
                remote_results,
            ) = yield self.store.get_user_devices_from_cache(query_list)
            for user_id, devices in iteritems(remote_results):
                user_devices = results.setdefault(user_id, {})
                for device_id, device in iteritems(devices):
                    keys = device.get("keys", None)
                    device_display_name = device.get("device_display_name", None)
                    if keys:
                        result = dict(keys)
                        unsigned = result.setdefault("unsigned", {})
                        if device_display_name:
                            unsigned["device_display_name"] = device_display_name
                        user_devices[device_id] = result

            for user_id in user_ids_not_in_cache:
                domain = get_domain_from_id(user_id)
                r = remote_queries_not_in_cache.setdefault(domain, {})
                r[user_id] = remote_queries[user_id]

        # Get cached cross-signing keys
        cross_signing_keys = yield self.get_cross_signing_keys_from_cache(
            device_keys_query, from_user_id
        )

        # Now fetch any devices that we don't have in our cache
        @trace
        @defer.inlineCallbacks
        def do_remote_query(destination):
            """This is called when we are querying the device list of a user on
            a remote homeserver and their device list is not in the device list
            cache. If we share a room with this user and we're not querying for
            specific user we will update the cache
            with their device list."""

            destination_query = remote_queries_not_in_cache[destination]

            # We first consider whether we wish to update the device list cache with
            # the users device list. We want to track a user's devices when the
            # authenticated user shares a room with the queried user and the query
            # has not specified a particular device.
            # If we update the cache for the queried user we remove them from further
            # queries. We use the more efficient batched query_client_keys for all
            # remaining users
            user_ids_updated = []
            for (user_id, device_list) in destination_query.items():
                if user_id in user_ids_updated:
                    continue

                if device_list:
                    continue

                room_ids = yield self.store.get_rooms_for_user(user_id)
                if not room_ids:
                    continue

                # We've decided we're sharing a room with this user and should
                # probably be tracking their device lists. However, we haven't
                # done an initial sync on the device list so we do it now.
                try:
                    if self._is_master:
                        user_devices = yield self.device_handler.device_list_updater.user_device_resync(
                            user_id
                        )
                    else:
                        user_devices = yield self._user_device_resync_client(
                            user_id=user_id
                        )

                    user_devices = user_devices["devices"]
                    for device in user_devices:
                        results[user_id] = {device["device_id"]: device["keys"]}
                    user_ids_updated.append(user_id)
                except Exception as e:
                    failures[destination] = _exception_to_failure(e)

            if len(destination_query) == len(user_ids_updated):
                # We've updated all the users in the query and we do not need to
                # make any further remote calls.
                return

            # Remove all the users from the query which we have updated
            for user_id in user_ids_updated:
                destination_query.pop(user_id)

            try:
                remote_result = yield self.federation.query_client_keys(
                    destination, {"device_keys": destination_query}, timeout=timeout
                )

                for user_id, keys in remote_result["device_keys"].items():
                    if user_id in destination_query:
                        results[user_id] = keys

                if "master_keys" in remote_result:
                    for user_id, key in remote_result["master_keys"].items():
                        if user_id in destination_query:
                            cross_signing_keys["master_keys"][user_id] = key

                if "self_signing_keys" in remote_result:
                    for user_id, key in remote_result["self_signing_keys"].items():
                        if user_id in destination_query:
                            cross_signing_keys["self_signing_keys"][user_id] = key

            except Exception as e:
                failure = _exception_to_failure(e)
                failures[destination] = failure
                set_tag("error", True)
                set_tag("reason", failure)

        yield make_deferred_yieldable(
            defer.gatherResults(
                [
                    run_in_background(do_remote_query, destination)
                    for destination in remote_queries_not_in_cache
                ],
                consumeErrors=True,
            ).addErrback(unwrapFirstError)
        )

        ret = {"device_keys": results, "failures": failures}

        ret.update(cross_signing_keys)

        return ret

    @defer.inlineCallbacks
    def get_cross_signing_keys_from_cache(self, query, from_user_id):
        """Get cross-signing keys for users from the database

        Args:
            query (Iterable[string]) an iterable of user IDs.  A dict whose keys
                are user IDs satisfies this, so the query format used for
                query_devices can be used here.
            from_user_id (str): the user making the query.  This is used when
                adding cross-signing signatures to limit what signatures users
                can see.

        Returns:
            defer.Deferred[dict[str, dict[str, dict]]]: map from
                (master_keys|self_signing_keys|user_signing_keys) -> user_id -> key
        """
        master_keys = {}
        self_signing_keys = {}
        user_signing_keys = {}

        user_ids = list(query)

        keys = yield self.store.get_e2e_cross_signing_keys_bulk(user_ids, from_user_id)

        for user_id, user_info in keys.items():
            if user_info is None:
                continue
            if "master" in user_info:
                master_keys[user_id] = user_info["master"]
            if "self_signing" in user_info:
                self_signing_keys[user_id] = user_info["self_signing"]

        if (
            from_user_id in keys
            and keys[from_user_id] is not None
            and "user_signing" in keys[from_user_id]
        ):
            # users can see other users' master and self-signing keys, but can
            # only see their own user-signing keys
            user_signing_keys[from_user_id] = keys[from_user_id]["user_signing"]

        return {
            "master_keys": master_keys,
            "self_signing_keys": self_signing_keys,
            "user_signing_keys": user_signing_keys,
        }

    @trace
    @defer.inlineCallbacks
    def query_local_devices(self, query):
        """Get E2E device keys for local users

        Args:
            query (dict[string, list[string]|None): map from user_id to a list
                 of devices to query (None for all devices)

        Returns:
            defer.Deferred: (resolves to dict[string, dict[string, dict]]):
                 map from user_id -> device_id -> device details
        """
        set_tag("local_query", query)
        local_query = []

        result_dict = {}
        for user_id, device_ids in query.items():
            # we use UserID.from_string to catch invalid user ids
            if not self.is_mine(UserID.from_string(user_id)):
                logger.warning("Request for keys for non-local user %s", user_id)
                log_kv(
                    {
                        "message": "Requested a local key for a user which"
                        " was not local to the homeserver",
                        "user_id": user_id,
                    }
                )
                set_tag("error", True)
                raise SynapseError(400, "Not a user here")

            if not device_ids:
                local_query.append((user_id, None))
            else:
                for device_id in device_ids:
                    local_query.append((user_id, device_id))

            # make sure that each queried user appears in the result dict
            result_dict[user_id] = {}

        results = yield self.store.get_e2e_device_keys(local_query)

        # Build the result structure
        for user_id, device_keys in results.items():
            for device_id, device_info in device_keys.items():
                result_dict[user_id][device_id] = device_info

        log_kv(results)
        return result_dict

    @defer.inlineCallbacks
    def on_federation_query_client_keys(self, query_body):
        """ Handle a device key query from a federated server
        """
        device_keys_query = query_body.get("device_keys", {})
        res = yield self.query_local_devices(device_keys_query)
        ret = {"device_keys": res}

        # add in the cross-signing keys
        cross_signing_keys = yield self.get_cross_signing_keys_from_cache(
            device_keys_query, None
        )

        ret.update(cross_signing_keys)

        return ret

    @trace
    @defer.inlineCallbacks
    def claim_one_time_keys(self, query, timeout):
        local_query = []
        remote_queries = {}

        for user_id, device_keys in query.get("one_time_keys", {}).items():
            # we use UserID.from_string to catch invalid user ids
            if self.is_mine(UserID.from_string(user_id)):
                for device_id, algorithm in device_keys.items():
                    local_query.append((user_id, device_id, algorithm))
            else:
                domain = get_domain_from_id(user_id)
                remote_queries.setdefault(domain, {})[user_id] = device_keys

        set_tag("local_key_query", local_query)
        set_tag("remote_key_query", remote_queries)

        results = yield self.store.claim_e2e_one_time_keys(local_query)

        json_result = {}
        failures = {}
        for user_id, device_keys in results.items():
            for device_id, keys in device_keys.items():
                for key_id, json_bytes in keys.items():
                    json_result.setdefault(user_id, {})[device_id] = {
                        key_id: json.loads(json_bytes)
                    }

        @trace
        @defer.inlineCallbacks
        def claim_client_keys(destination):
            set_tag("destination", destination)
            device_keys = remote_queries[destination]
            try:
                remote_result = yield self.federation.claim_client_keys(
                    destination, {"one_time_keys": device_keys}, timeout=timeout
                )
                for user_id, keys in remote_result["one_time_keys"].items():
                    if user_id in device_keys:
                        json_result[user_id] = keys

            except Exception as e:
                failure = _exception_to_failure(e)
                failures[destination] = failure
                set_tag("error", True)
                set_tag("reason", failure)

        yield make_deferred_yieldable(
            defer.gatherResults(
                [
                    run_in_background(claim_client_keys, destination)
                    for destination in remote_queries
                ],
                consumeErrors=True,
            )
        )

        logger.info(
            "Claimed one-time-keys: %s",
            ",".join(
                (
                    "%s for %s:%s" % (key_id, user_id, device_id)
                    for user_id, user_keys in iteritems(json_result)
                    for device_id, device_keys in iteritems(user_keys)
                    for key_id, _ in iteritems(device_keys)
                )
            ),
        )

        log_kv({"one_time_keys": json_result, "failures": failures})
        return {"one_time_keys": json_result, "failures": failures}

    @defer.inlineCallbacks
    @tag_args
    def upload_keys_for_user(self, user_id, device_id, keys):

        time_now = self.clock.time_msec()

        # TODO: Validate the JSON to make sure it has the right keys.
        device_keys = keys.get("device_keys", None)
        if device_keys:
            logger.info(
                "Updating device_keys for device %r for user %s at %d",
                device_id,
                user_id,
                time_now,
            )
            log_kv(
                {
                    "message": "Updating device_keys for user.",
                    "user_id": user_id,
                    "device_id": device_id,
                }
            )
            # TODO: Sign the JSON with the server key
            changed = yield self.store.set_e2e_device_keys(
                user_id, device_id, time_now, device_keys
            )
            if changed:
                # Only notify about device updates *if* the keys actually changed
                yield self.device_handler.notify_device_update(user_id, [device_id])
        else:
            log_kv({"message": "Not updating device_keys for user", "user_id": user_id})
        one_time_keys = keys.get("one_time_keys", None)
        if one_time_keys:
            log_kv(
                {
                    "message": "Updating one_time_keys for device.",
                    "user_id": user_id,
                    "device_id": device_id,
                }
            )
            yield self._upload_one_time_keys_for_user(
                user_id, device_id, time_now, one_time_keys
            )
        else:
            log_kv(
                {"message": "Did not update one_time_keys", "reason": "no keys given"}
            )

        # the device should have been registered already, but it may have been
        # deleted due to a race with a DELETE request. Or we may be using an
        # old access_token without an associated device_id. Either way, we
        # need to double-check the device is registered to avoid ending up with
        # keys without a corresponding device.
        yield self.device_handler.check_device_registered(user_id, device_id)

        result = yield self.store.count_e2e_one_time_keys(user_id, device_id)

        set_tag("one_time_key_counts", result)
        return {"one_time_key_counts": result}

    @defer.inlineCallbacks
    def _upload_one_time_keys_for_user(
        self, user_id, device_id, time_now, one_time_keys
    ):
        logger.info(
            "Adding one_time_keys %r for device %r for user %r at %d",
            one_time_keys.keys(),
            device_id,
            user_id,
            time_now,
        )

        # make a list of (alg, id, key) tuples
        key_list = []
        for key_id, key_obj in one_time_keys.items():
            algorithm, key_id = key_id.split(":")
            key_list.append((algorithm, key_id, key_obj))

        # First we check if we have already persisted any of the keys.
        existing_key_map = yield self.store.get_e2e_one_time_keys(
            user_id, device_id, [k_id for _, k_id, _ in key_list]
        )

        new_keys = []  # Keys that we need to insert. (alg, id, json) tuples.
        for algorithm, key_id, key in key_list:
            ex_json = existing_key_map.get((algorithm, key_id), None)
            if ex_json:
                if not _one_time_keys_match(ex_json, key):
                    raise SynapseError(
                        400,
                        (
                            "One time key %s:%s already exists. "
                            "Old key: %s; new key: %r"
                        )
                        % (algorithm, key_id, ex_json, key),
                    )
            else:
                new_keys.append(
                    (algorithm, key_id, encode_canonical_json(key).decode("ascii"))
                )

        log_kv({"message": "Inserting new one_time_keys.", "keys": new_keys})
        yield self.store.add_e2e_one_time_keys(user_id, device_id, time_now, new_keys)

    @defer.inlineCallbacks
    def upload_signing_keys_for_user(self, user_id, keys):
        """Upload signing keys for cross-signing

        Args:
            user_id (string): the user uploading the keys
            keys (dict[string, dict]): the signing keys
        """

        # if a master key is uploaded, then check it.  Otherwise, load the
        # stored master key, to check signatures on other keys
        if "master_key" in keys:
            master_key = keys["master_key"]

            _check_cross_signing_key(master_key, user_id, "master")
        else:
            master_key = yield self.store.get_e2e_cross_signing_key(user_id, "master")

        # if there is no master key, then we can't do anything, because all the
        # other cross-signing keys need to be signed by the master key
        if not master_key:
            raise SynapseError(400, "No master key available", Codes.MISSING_PARAM)

        try:
            master_key_id, master_verify_key = get_verify_key_from_cross_signing_key(
                master_key
            )
        except ValueError:
            if "master_key" in keys:
                # the invalid key came from the request
                raise SynapseError(400, "Invalid master key", Codes.INVALID_PARAM)
            else:
                # the invalid key came from the database
                logger.error("Invalid master key found for user %s", user_id)
                raise SynapseError(500, "Invalid master key")

        # for the other cross-signing keys, make sure that they have valid
        # signatures from the master key
        if "self_signing_key" in keys:
            self_signing_key = keys["self_signing_key"]

            _check_cross_signing_key(
                self_signing_key, user_id, "self_signing", master_verify_key
            )

        if "user_signing_key" in keys:
            user_signing_key = keys["user_signing_key"]

            _check_cross_signing_key(
                user_signing_key, user_id, "user_signing", master_verify_key
            )

        # if everything checks out, then store the keys and send notifications
        deviceids = []
        if "master_key" in keys:
            yield self.store.set_e2e_cross_signing_key(user_id, "master", master_key)
            deviceids.append(master_verify_key.version)
        if "self_signing_key" in keys:
            yield self.store.set_e2e_cross_signing_key(
                user_id, "self_signing", self_signing_key
            )
            try:
                deviceids.append(
                    get_verify_key_from_cross_signing_key(self_signing_key)[1].version
                )
            except ValueError:
                raise SynapseError(400, "Invalid self-signing key", Codes.INVALID_PARAM)
        if "user_signing_key" in keys:
            yield self.store.set_e2e_cross_signing_key(
                user_id, "user_signing", user_signing_key
            )
            # the signature stream matches the semantics that we want for
            # user-signing key updates: only the user themselves is notified of
            # their own user-signing key updates
            yield self.device_handler.notify_user_signature_update(user_id, [user_id])

        # master key and self-signing key updates match the semantics of device
        # list updates: all users who share an encrypted room are notified
        if len(deviceids):
            yield self.device_handler.notify_device_update(user_id, deviceids)

        return {}

    @defer.inlineCallbacks
    def upload_signatures_for_device_keys(self, user_id, signatures):
        """Upload device signatures for cross-signing

        Args:
            user_id (string): the user uploading the signatures
            signatures (dict[string, dict[string, dict]]): map of users to
                devices to signed keys. This is the submission from the user; an
                exception will be raised if it is malformed.
        Returns:
            dict: response to be sent back to the client.  The response will have
                a "failures" key, which will be a dict mapping users to devices
                to errors for the signatures that failed.
        Raises:
            SynapseError: if the signatures dict is not valid.
        """
        failures = {}

        # signatures to be stored.  Each item will be a SignatureListItem
        signature_list = []

        # split between checking signatures for own user and signatures for
        # other users, since we verify them with different keys
        self_signatures = signatures.get(user_id, {})
        other_signatures = {k: v for k, v in signatures.items() if k != user_id}

        self_signature_list, self_failures = yield self._process_self_signatures(
            user_id, self_signatures
        )
        signature_list.extend(self_signature_list)
        failures.update(self_failures)

        other_signature_list, other_failures = yield self._process_other_signatures(
            user_id, other_signatures
        )
        signature_list.extend(other_signature_list)
        failures.update(other_failures)

        # store the signature, and send the appropriate notifications for sync
        logger.debug("upload signature failures: %r", failures)
        yield self.store.store_e2e_cross_signing_signatures(user_id, signature_list)

        self_device_ids = [item.target_device_id for item in self_signature_list]
        if self_device_ids:
            yield self.device_handler.notify_device_update(user_id, self_device_ids)
        signed_users = [item.target_user_id for item in other_signature_list]
        if signed_users:
            yield self.device_handler.notify_user_signature_update(
                user_id, signed_users
            )

        return {"failures": failures}

    @defer.inlineCallbacks
    def _process_self_signatures(self, user_id, signatures):
        """Process uploaded signatures of the user's own keys.

        Signatures of the user's own keys from this API come in two forms:
        - signatures of the user's devices by the user's self-signing key,
        - signatures of the user's master key by the user's devices.

        Args:
            user_id (string): the user uploading the keys
            signatures (dict[string, dict]): map of devices to signed keys

        Returns:
            (list[SignatureListItem], dict[string, dict[string, dict]]):
            a list of signatures to store, and a map of users to devices to failure
            reasons

        Raises:
            SynapseError: if the input is malformed
        """
        signature_list = []
        failures = {}
        if not signatures:
            return signature_list, failures

        if not isinstance(signatures, dict):
            raise SynapseError(400, "Invalid parameter", Codes.INVALID_PARAM)

        try:
            # get our self-signing key to verify the signatures
            (
                _,
                self_signing_key_id,
                self_signing_verify_key,
            ) = yield self._get_e2e_cross_signing_verify_key(user_id, "self_signing")

            # get our master key, since we may have received a signature of it.
            # We need to fetch it here so that we know what its key ID is, so
            # that we can check if a signature that was sent is a signature of
            # the master key or of a device
            (
                master_key,
                _,
                master_verify_key,
            ) = yield self._get_e2e_cross_signing_verify_key(user_id, "master")

            # fetch our stored devices.  This is used to 1. verify
            # signatures on the master key, and 2. to compare with what
            # was sent if the device was signed
            devices = yield self.store.get_e2e_device_keys([(user_id, None)])

            if user_id not in devices:
                raise NotFoundError("No device keys found")

            devices = devices[user_id]
        except SynapseError as e:
            failure = _exception_to_failure(e)
            failures[user_id] = {device: failure for device in signatures.keys()}
            return signature_list, failures

        for device_id, device in signatures.items():
            # make sure submitted data is in the right form
            if not isinstance(device, dict):
                raise SynapseError(400, "Invalid parameter", Codes.INVALID_PARAM)

            try:
                if "signatures" not in device or user_id not in device["signatures"]:
                    # no signature was sent
                    raise SynapseError(
                        400, "Invalid signature", Codes.INVALID_SIGNATURE
                    )

                if device_id == master_verify_key.version:
                    # The signature is of the master key. This needs to be
                    # handled differently from signatures of normal devices.
                    master_key_signature_list = self._check_master_key_signature(
                        user_id, device_id, device, master_key, devices
                    )
                    signature_list.extend(master_key_signature_list)
                    continue

                # at this point, we have a device that should be signed
                # by the self-signing key
                if self_signing_key_id not in device["signatures"][user_id]:
                    # no signature was sent
                    raise SynapseError(
                        400, "Invalid signature", Codes.INVALID_SIGNATURE
                    )

                try:
                    stored_device = devices[device_id]
                except KeyError:
                    raise NotFoundError("Unknown device")
                if self_signing_key_id in stored_device.get("signatures", {}).get(
                    user_id, {}
                ):
                    # we already have a signature on this device, so we
                    # can skip it, since it should be exactly the same
                    continue

                _check_device_signature(
                    user_id, self_signing_verify_key, device, stored_device
                )

                signature = device["signatures"][user_id][self_signing_key_id]
                signature_list.append(
                    SignatureListItem(
                        self_signing_key_id, user_id, device_id, signature
                    )
                )
            except SynapseError as e:
                failures.setdefault(user_id, {})[device_id] = _exception_to_failure(e)

        return signature_list, failures

    def _check_master_key_signature(
        self, user_id, master_key_id, signed_master_key, stored_master_key, devices
    ):
        """Check signatures of a user's master key made by their devices.

        Args:
            user_id (string): the user whose master key is being checked
            master_key_id (string): the ID of the user's master key
            signed_master_key (dict): the user's signed master key that was uploaded
            stored_master_key (dict): our previously-stored copy of the user's master key
            devices (iterable(dict)): the user's devices

        Returns:
            list[SignatureListItem]: a list of signatures to store

        Raises:
            SynapseError: if a signature is invalid
        """
        # for each device that signed the master key, check the signature.
        master_key_signature_list = []
        sigs = signed_master_key["signatures"]
        for signing_key_id, signature in sigs[user_id].items():
            _, signing_device_id = signing_key_id.split(":", 1)
            if (
                signing_device_id not in devices
                or signing_key_id not in devices[signing_device_id]["keys"]
            ):
                # signed by an unknown device, or the
                # device does not have the key
                raise SynapseError(400, "Invalid signature", Codes.INVALID_SIGNATURE)

            # get the key and check the signature
            pubkey = devices[signing_device_id]["keys"][signing_key_id]
            verify_key = decode_verify_key_bytes(signing_key_id, decode_base64(pubkey))
            _check_device_signature(
                user_id, verify_key, signed_master_key, stored_master_key
            )

            master_key_signature_list.append(
                SignatureListItem(signing_key_id, user_id, master_key_id, signature)
            )

        return master_key_signature_list

    @defer.inlineCallbacks
    def _process_other_signatures(self, user_id, signatures):
        """Process uploaded signatures of other users' keys.  These will be the
        target user's master keys, signed by the uploading user's user-signing
        key.

        Args:
            user_id (string): the user uploading the keys
            signatures (dict[string, dict]): map of users to devices to signed keys

        Returns:
            (list[SignatureListItem], dict[string, dict[string, dict]]):
            a list of signatures to store, and a map of users to devices to failure
            reasons

        Raises:
            SynapseError: if the input is malformed
        """
        signature_list = []
        failures = {}
        if not signatures:
            return signature_list, failures

        try:
            # get our user-signing key to verify the signatures
            (
                user_signing_key,
                user_signing_key_id,
                user_signing_verify_key,
            ) = yield self._get_e2e_cross_signing_verify_key(user_id, "user_signing")
        except SynapseError as e:
            failure = _exception_to_failure(e)
            for user, devicemap in signatures.items():
                failures[user] = {device_id: failure for device_id in devicemap.keys()}
            return signature_list, failures

        for target_user, devicemap in signatures.items():
            # make sure submitted data is in the right form
            if not isinstance(devicemap, dict):
                raise SynapseError(400, "Invalid parameter", Codes.INVALID_PARAM)
            for device in devicemap.values():
                if not isinstance(device, dict):
                    raise SynapseError(400, "Invalid parameter", Codes.INVALID_PARAM)

            device_id = None
            try:
                # get the target user's master key, to make sure it matches
                # what was sent
                (
                    master_key,
                    master_key_id,
                    _,
                ) = yield self._get_e2e_cross_signing_verify_key(
                    target_user, "master", user_id
                )

                # make sure that the target user's master key is the one that
                # was signed (and no others)
                device_id = master_key_id.split(":", 1)[1]
                if device_id not in devicemap:
                    logger.debug(
                        "upload signature: could not find signature for device %s",
                        device_id,
                    )
                    # set device to None so that the failure gets
                    # marked on all the signatures
                    device_id = None
                    raise NotFoundError("Unknown device")
                key = devicemap[device_id]
                other_devices = [k for k in devicemap.keys() if k != device_id]
                if other_devices:
                    # other devices were signed -- mark those as failures
                    logger.debug("upload signature: too many devices specified")
                    failure = _exception_to_failure(NotFoundError("Unknown device"))
                    failures[target_user] = {
                        device: failure for device in other_devices
                    }

                if user_signing_key_id in master_key.get("signatures", {}).get(
                    user_id, {}
                ):
                    # we already have the signature, so we can skip it
                    continue

                _check_device_signature(
                    user_id, user_signing_verify_key, key, master_key
                )

                signature = key["signatures"][user_id][user_signing_key_id]
                signature_list.append(
                    SignatureListItem(
                        user_signing_key_id, target_user, device_id, signature
                    )
                )
            except SynapseError as e:
                failure = _exception_to_failure(e)
                if device_id is None:
                    failures[target_user] = {
                        device_id: failure for device_id in devicemap.keys()
                    }
                else:
                    failures.setdefault(target_user, {})[device_id] = failure

        return signature_list, failures

    @defer.inlineCallbacks
    def _get_e2e_cross_signing_verify_key(self, user_id, key_type, from_user_id=None):
        """Fetch the cross-signing public key from storage and interpret it.

        Args:
            user_id (str): the user whose key should be fetched
            key_type (str): the type of key to fetch
            from_user_id (str): the user that we are fetching the keys for.
                This affects what signatures are fetched.

        Returns:
            dict, str, VerifyKey: the raw key data, the key ID, and the
                signedjson verify key

        Raises:
            NotFoundError: if the key is not found
        """
        key = yield self.store.get_e2e_cross_signing_key(
            user_id, key_type, from_user_id
        )
        if key is None:
            logger.debug("no %s key found for %s", key_type, user_id)
            raise NotFoundError("No %s key found for %s" % (key_type, user_id))
        key_id, verify_key = get_verify_key_from_cross_signing_key(key)
        return key, key_id, verify_key


def _check_cross_signing_key(key, user_id, key_type, signing_key=None):
    """Check a cross-signing key uploaded by a user.  Performs some basic sanity
    checking, and ensures that it is signed, if a signature is required.

    Args:
        key (dict): the key data to verify
        user_id (str): the user whose key is being checked
        key_type (str): the type of key that the key should be
        signing_key (VerifyKey): (optional) the signing key that the key should
            be signed with.  If omitted, signatures will not be checked.
    """
    if (
        key.get("user_id") != user_id
        or key_type not in key.get("usage", [])
        or len(key.get("keys", {})) != 1
    ):
        raise SynapseError(400, ("Invalid %s key" % (key_type,)), Codes.INVALID_PARAM)

    if signing_key:
        try:
            verify_signed_json(key, user_id, signing_key)
        except SignatureVerifyException:
            raise SynapseError(
                400, ("Invalid signature on %s key" % key_type), Codes.INVALID_SIGNATURE
            )


def _check_device_signature(user_id, verify_key, signed_device, stored_device):
    """Check that a signature on a device or cross-signing key is correct and
    matches the copy of the device/key that we have stored.  Throws an
    exception if an error is detected.

    Args:
        user_id (str): the user ID whose signature is being checked
        verify_key (VerifyKey): the key to verify the device with
        signed_device (dict): the uploaded signed device data
        stored_device (dict): our previously stored copy of the device

    Raises:
        SynapseError: if the signature was invalid or the sent device is not the
            same as the stored device

    """

    # make sure that the device submitted matches what we have stored
    stripped_signed_device = {
        k: v for k, v in signed_device.items() if k not in ["signatures", "unsigned"]
    }
    stripped_stored_device = {
        k: v for k, v in stored_device.items() if k not in ["signatures", "unsigned"]
    }
    if stripped_signed_device != stripped_stored_device:
        logger.debug(
            "upload signatures: key does not match %s vs %s",
            signed_device,
            stored_device,
        )
        raise SynapseError(400, "Key does not match")

    try:
        verify_signed_json(signed_device, user_id, verify_key)
    except SignatureVerifyException:
        logger.debug("invalid signature on key")
        raise SynapseError(400, "Invalid signature", Codes.INVALID_SIGNATURE)


def _exception_to_failure(e):
    if isinstance(e, SynapseError):
        return {"status": e.code, "errcode": e.errcode, "message": str(e)}

    if isinstance(e, CodeMessageException):
        return {"status": e.code, "message": str(e)}

    if isinstance(e, NotRetryingDestination):
        return {"status": 503, "message": "Not ready for retry"}

    # include ConnectionRefused and other errors
    #
    # Note that some Exceptions (notably twisted's ResponseFailed etc) don't
    # give a string for e.message, which json then fails to serialize.
    return {"status": 503, "message": str(e)}


def _one_time_keys_match(old_key_json, new_key):
    old_key = json.loads(old_key_json)

    # if either is a string rather than an object, they must match exactly
    if not isinstance(old_key, dict) or not isinstance(new_key, dict):
        return old_key == new_key

    # otherwise, we strip off the 'signatures' if any, because it's legitimate
    # for different upload attempts to have different signatures.
    old_key.pop("signatures", None)
    new_key_copy = dict(new_key)
    new_key_copy.pop("signatures", None)

    return old_key == new_key_copy


@attr.s
class SignatureListItem:
    """An item in the signature list as used by upload_signatures_for_device_keys.
    """

    signing_key_id = attr.ib()
    target_user_id = attr.ib()
    target_device_id = attr.ib()
    signature = attr.ib()


class SigningKeyEduUpdater(object):
    """Handles incoming signing key updates from federation and updates the DB"""

    def __init__(self, hs, e2e_keys_handler):
        self.store = hs.get_datastore()
        self.federation = hs.get_federation_client()
        self.clock = hs.get_clock()
        self.e2e_keys_handler = e2e_keys_handler

        self._remote_edu_linearizer = Linearizer(name="remote_signing_key")

        # user_id -> list of updates waiting to be handled.
        self._pending_updates = {}

        # Recently seen stream ids. We don't bother keeping these in the DB,
        # but they're useful to have them about to reduce the number of spurious
        # resyncs.
        self._seen_updates = ExpiringCache(
            cache_name="signing_key_update_edu",
            clock=self.clock,
            max_len=10000,
            expiry_ms=30 * 60 * 1000,
            iterable=True,
        )

    @defer.inlineCallbacks
    def incoming_signing_key_update(self, origin, edu_content):
        """Called on incoming signing key update from federation. Responsible for
        parsing the EDU and adding to pending updates list.

        Args:
            origin (string): the server that sent the EDU
            edu_content (dict): the contents of the EDU
        """

        user_id = edu_content.pop("user_id")
        master_key = edu_content.pop("master_key", None)
        self_signing_key = edu_content.pop("self_signing_key", None)

        if get_domain_from_id(user_id) != origin:
            logger.warning("Got signing key update edu for %r from %r", user_id, origin)
            return

        room_ids = yield self.store.get_rooms_for_user(user_id)
        if not room_ids:
            # We don't share any rooms with this user. Ignore update, as we
            # probably won't get any further updates.
            return

        self._pending_updates.setdefault(user_id, []).append(
            (master_key, self_signing_key)
        )

        yield self._handle_signing_key_updates(user_id)

    @defer.inlineCallbacks
    def _handle_signing_key_updates(self, user_id):
        """Actually handle pending updates.

        Args:
            user_id (string): the user whose updates we are processing
        """

        device_handler = self.e2e_keys_handler.device_handler

        with (yield self._remote_edu_linearizer.queue(user_id)):
            pending_updates = self._pending_updates.pop(user_id, [])
            if not pending_updates:
                # This can happen since we batch updates
                return

            device_ids = []

            logger.info("pending updates: %r", pending_updates)

            for master_key, self_signing_key in pending_updates:
                if master_key:
                    yield self.store.set_e2e_cross_signing_key(
                        user_id, "master", master_key
                    )
                    _, verify_key = get_verify_key_from_cross_signing_key(master_key)
                    # verify_key is a VerifyKey from signedjson, which uses
                    # .version to denote the portion of the key ID after the
                    # algorithm and colon, which is the device ID
                    device_ids.append(verify_key.version)
                if self_signing_key:
                    yield self.store.set_e2e_cross_signing_key(
                        user_id, "self_signing", self_signing_key
                    )
                    _, verify_key = get_verify_key_from_cross_signing_key(
                        self_signing_key
                    )
                    device_ids.append(verify_key.version)

            yield device_handler.notify_device_update(user_id, device_ids)
