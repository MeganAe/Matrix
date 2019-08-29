# -*- coding: utf-8 -*-
# Copyright 2015, 2016 OpenMarket Ltd
# Copyright 2017 Vector Creations Ltd
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

"""Utilities for interacting with Identity Servers"""

import logging

from canonicaljson import json

from twisted.internet import defer

from synapse.api.errors import CodeMessageException, HttpResponseException, SynapseError
from synapse.util.stringutils import random_string

from ._base import BaseHandler

logger = logging.getLogger(__name__)


class IdentityHandler(BaseHandler):
    def __init__(self, hs):
        super(IdentityHandler, self).__init__(hs)

        self.http_client = hs.get_simple_http_client()
        self.federation_http_client = hs.get_http_client()
        self.hs = hs

    @defer.inlineCallbacks
    def threepid_from_creds(self, creds):
        if "id_server" in creds:
            id_server = creds["id_server"]
        elif "idServer" in creds:
            id_server = creds["idServer"]
        else:
            raise SynapseError(400, "No id_server in creds")

        if "client_secret" in creds:
            client_secret = creds["client_secret"]
        elif "clientSecret" in creds:
            client_secret = creds["clientSecret"]
        else:
            raise SynapseError(400, "No client_secret in creds")

        try:
            data = yield self.http_client.get_json(
                "https://%s%s"
                % (id_server, "/_matrix/identity/api/v1/3pid/getValidated3pid"),
                {"sid": creds["sid"], "client_secret": client_secret},
            )
        except HttpResponseException as e:
            logger.info("getValidated3pid failed with Matrix error: %r", e)
            raise e.to_synapse_error()

        if "medium" in data:
            return data
        return None

    @defer.inlineCallbacks
    def bind_threepid(self, creds, mxid):
        logger.debug("binding threepid %r to %s", creds, mxid)
        data = None

        if "id_server" in creds:
            id_server = creds["id_server"]
        elif "idServer" in creds:
            id_server = creds["idServer"]
        else:
            raise SynapseError(400, "No id_server in creds")

        if "client_secret" in creds:
            client_secret = creds["client_secret"]
        elif "clientSecret" in creds:
            client_secret = creds["clientSecret"]
        else:
            raise SynapseError(400, "No client_secret in creds")

        try:
            data = yield self.http_client.post_json_get_json(
                "https://%s%s" % (id_server, "/_matrix/identity/api/v1/3pid/bind"),
                {"sid": creds["sid"], "client_secret": client_secret, "mxid": mxid},
            )
            logger.debug("bound threepid %r to %s", creds, mxid)

            # Remember where we bound the threepid
            yield self.store.add_user_bound_threepid(
                user_id=mxid,
                medium=data["medium"],
                address=data["address"],
                id_server=id_server,
            )
        except CodeMessageException as e:
            data = json.loads(e.msg)  # XXX WAT?
        return data

    @defer.inlineCallbacks
    def try_unbind_threepid(self, mxid, threepid):
        """Removes a binding from an identity server

        Args:
            mxid (str): Matrix user ID of binding to be removed
            threepid (dict): Dict with medium & address of binding to be
                removed, and an optional id_server.

        Raises:
            SynapseError: If we failed to contact the identity server

        Returns:
            Deferred[bool]: True on success, otherwise False if the identity
            server doesn't support unbinding (or no identity server found to
            contact).
        """
        if threepid.get("id_server"):
            id_servers = [threepid["id_server"]]
        else:
            id_servers = yield self.store.get_id_servers_user_bound(
                user_id=mxid, medium=threepid["medium"], address=threepid["address"]
            )

        # We don't know where to unbind, so we don't have a choice but to return
        if not id_servers:
            return False

        changed = True
        for id_server in id_servers:
            changed &= yield self.try_unbind_threepid_with_id_server(
                mxid, threepid, id_server
            )

        return changed

    @defer.inlineCallbacks
    def try_unbind_threepid_with_id_server(self, mxid, threepid, id_server):
        """Removes a binding from an identity server

        Args:
            mxid (str): Matrix user ID of binding to be removed
            threepid (dict): Dict with medium & address of binding to be removed
            id_server (str): Identity server to unbind from

        Raises:
            SynapseError: If we failed to contact the identity server

        Returns:
            Deferred[bool]: True on success, otherwise False if the identity
            server doesn't support unbinding
        """
        url = "https://%s/_matrix/identity/api/v1/3pid/unbind" % (id_server,)
        content = {
            "mxid": mxid,
            "threepid": {"medium": threepid["medium"], "address": threepid["address"]},
        }

        # we abuse the federation http client to sign the request, but we have to send it
        # using the normal http client since we don't want the SRV lookup and want normal
        # 'browser-like' HTTPS.
        auth_headers = self.federation_http_client.build_auth_headers(
            destination=None,
            method="POST",
            url_bytes="/_matrix/identity/api/v1/3pid/unbind".encode("ascii"),
            content=content,
            destination_is=id_server,
        )
        headers = {b"Authorization": auth_headers}

        try:
            yield self.http_client.post_json_get_json(url, content, headers)
            changed = True
        except HttpResponseException as e:
            changed = False
            if e.code in (400, 404, 501):
                # The remote server probably doesn't support unbinding (yet)
                logger.warn("Received %d response while unbinding threepid", e.code)
            else:
                logger.error("Failed to unbind threepid on identity server: %s", e)
                raise SynapseError(502, "Failed to contact identity server")

        yield self.store.remove_user_bound_threepid(
            user_id=mxid,
            medium=threepid["medium"],
            address=threepid["address"],
            id_server=id_server,
        )

        return changed

    @defer.inlineCallbacks
    def send_threepid_validation(
        self,
        email_address,
        client_secret,
        send_attempt,
        send_email_func,
        next_link=None,
    ):
        """Send a threepid validation email for password reset or
        registration purposes

        Args:
            email_address (str): The user's email address
            client_secret (str): The provided client secret
            send_attempt (int): Which send attempt this is
            send_email_func (func): A function that takes an email address, token,
                                    client_secret and session_id, sends an email
                                    and returns a Deferred.
            next_link (str|None): The URL to redirect the user to after validation

        Returns:
            The new session_id upon success

        Raises:
            SynapseError is an error occurred when sending the email
        """
        # Check that this email/client_secret/send_attempt combo is new or
        # greater than what we've seen previously
        session = yield self.store.get_threepid_validation_session(
            "email", client_secret, address=email_address, validated=False
        )

        # Check to see if a session already exists and that it is not yet
        # marked as validated
        if session and session.get("validated_at") is None:
            session_id = session["session_id"]
            last_send_attempt = session["last_send_attempt"]

            # Check that the send_attempt is higher than previous attempts
            if send_attempt <= last_send_attempt:
                # If not, just return a success without sending an email
                return session_id
        else:
            # An non-validated session does not exist yet.
            # Generate a session id
            session_id = random_string(16)

        # Generate a new validation token
        token = random_string(32)

        # Send the mail with the link containing the token, client_secret
        # and session_id
        try:
            yield send_email_func(email_address, token, client_secret, session_id)
        except Exception:
            logger.exception(
                "Error sending threepid validation email to %s", email_address
            )
            raise SynapseError(500, "An error was encountered when sending the email")

        token_expires = (
            self.hs.clock.time_msec() + self.hs.config.email_validation_token_lifetime
        )

        yield self.store.start_or_continue_validation_session(
            "email",
            email_address,
            session_id,
            client_secret,
            send_attempt,
            next_link,
            token,
            token_expires,
        )

        return session_id

    @defer.inlineCallbacks
    def requestEmailToken(
        self, id_server, email, client_secret, send_attempt, next_link=None
    ):
        """
        Request an external server send an email on our behalf for the purposes of threepid
        validation.

        Args:
            id_server (str): The identity server to proxy to
            email (str): The email to send the message to
            client_secret (str): The unique client_secret sends by the user
            send_attempt (int): Which attempt this is
            next_link: A link to redirect the user to once they submit the token

        Returns:
            The json response body from the server
        """
        params = {
            "email": email,
            "client_secret": client_secret,
            "send_attempt": send_attempt,
        }
        if next_link:
            params["next_link"] = next_link

        if self.hs.config.using_identity_server_from_trusted_list:
            # Warn that a deprecated config option is in use
            logger.warn(
                'The config option "trust_identity_server_for_password_resets" '
                'has been replaced by "account_threepid_delegate". '
                "Please consult the sample config at docs/sample_config.yaml for "
                "details and update your config file."
            )

        try:
            data = yield self.http_client.post_json_get_json(
                id_server + "/_matrix/identity/api/v1/validate/email/requestToken",
                params,
            )
            return data
        except HttpResponseException as e:
            logger.info("Proxied requestToken failed: %r", e)
            raise e.to_synapse_error()

    @defer.inlineCallbacks
    def requestMsisdnToken(
        self,
        id_server,
        country,
        phone_number,
        client_secret,
        send_attempt,
        next_link=None,
    ):
        """
        Request an external server send an SMS message on our behalf for the purposes of
        threepid validation.
        Args:
            id_server (str): The identity server to proxy to
            country (str): The country code of the phone number
            phone_number (str): The number to send the message to
            client_secret (str): The unique client_secret sends by the user
            send_attempt (int): Which attempt this is
            next_link: A link to redirect the user to once they submit the token

        Returns:
            The json response body from the server
        """
        params = {
            "country": country,
            "phone_number": phone_number,
            "client_secret": client_secret,
            "send_attempt": send_attempt,
        }
        if next_link:
            params["next_link"] = next_link

        if self.hs.config.using_identity_server_from_trusted_list:
            # Warn that a deprecated config option is in use
            logger.warn(
                'The config option "trust_identity_server_for_password_resets" '
                'has been replaced by "account_threepid_delegate". '
                "Please consult the sample config at docs/sample_config.yaml for "
                "details and update your config file."
            )

        try:
            data = yield self.http_client.post_json_get_json(
                id_server + "/_matrix/identity/api/v1/validate/msisdn/requestToken",
                params,
            )
            return data
        except HttpResponseException as e:
            logger.info("Proxied requestToken failed: %r", e)
            raise e.to_synapse_error()


class LookupAlgorithm:
    """
    Supported hashing algorithms when performing a 3PID lookup.

    SHA256 - Hashing an (address, medium, pepper) combo with sha256, then url-safe base64
        encoding
    NONE - Not performing any hashing. Simply sending an (address, medium) combo in plaintext
    """

    SHA256 = "sha256"
    NONE = "none"
