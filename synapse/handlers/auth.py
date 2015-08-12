# -*- coding: utf-8 -*-
# Copyright 2014, 2015 OpenMarket Ltd
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

from ._base import BaseHandler
from synapse.api.constants import LoginType
from synapse.types import UserID
from synapse.api.errors import LoginError, Codes
from synapse.http.client import SimpleHttpClient
from synapse.util.async import run_on_reactor

from twisted.web.client import PartialDownloadError

import logging
import bcrypt
import simplejson

import synapse.util.stringutils as stringutils


logger = logging.getLogger(__name__)


class AuthHandler(BaseHandler):

    def __init__(self, hs):
        super(AuthHandler, self).__init__(hs)
        self.checkers = {
            LoginType.PASSWORD: self._check_password_auth,
            LoginType.RECAPTCHA: self._check_recaptcha,
            LoginType.EMAIL_IDENTITY: self._check_email_identity,
            LoginType.DUMMY: self._check_dummy_auth,
        }
        self.sessions = {}

    @defer.inlineCallbacks
    def check_auth(self, flows, clientdict, clientip):
        """
        Takes a dictionary sent by the client in the login / registration
        protocol and handles the login flow.

        As a side effect, this function fills in the 'creds' key on the user's
        session with a map, which maps each auth-type (str) to the relevant
        identity authenticated by that auth-type (mostly str, but for captcha, bool).

        Args:
            flows (list): A list of login flows. Each flow is an ordered list of
                          strings representing auth-types. At least one full
                          flow must be completed in order for auth to be successful.
            clientdict: The dictionary from the client root level, not the
                        'auth' key: this method prompts for auth if none is sent.
            clientip (str): The IP address of the client.
        Returns:
            A tuple of (authed, dict, dict) where authed is true if the client
            has successfully completed an auth flow. If it is true, the first
            dict contains the authenticated credentials of each stage.

            If authed is false, the first dictionary is the server response to
            the login request and should be passed back to the client.

            In either case, the second dict contains the parameters for this
            request (which may have been given only in a previous call).
        """

        authdict = None
        sid = None
        if clientdict and 'auth' in clientdict:
            authdict = clientdict['auth']
            del clientdict['auth']
            if 'session' in authdict:
                sid = authdict['session']
        session = self._get_session_info(sid)

        if len(clientdict) > 0:
            # This was designed to allow the client to omit the parameters
            # and just supply the session in subsequent calls so it split
            # auth between devices by just sharing the session, (eg. so you
            # could continue registration from your phone having clicked the
            # email auth link on there). It's probably too open to abuse
            # because it lets unauthenticated clients store arbitrary objects
            # on a home server.
            # Revisit: Assumimg the REST APIs do sensible validation, the data
            # isn't arbintrary.
            session['clientdict'] = clientdict
            self._save_session(session)
        elif 'clientdict' in session:
            clientdict = session['clientdict']

        if not authdict:
            defer.returnValue(
                (False, self._auth_dict_for_flows(flows, session), clientdict)
            )

        if 'creds' not in session:
            session['creds'] = {}
        creds = session['creds']

        # check auth type currently being presented
        if 'type' in authdict:
            if authdict['type'] not in self.checkers:
                raise LoginError(400, "", Codes.UNRECOGNIZED)
            result = yield self.checkers[authdict['type']](authdict, clientip)
            if result:
                creds[authdict['type']] = result
                self._save_session(session)

        for f in flows:
            if len(set(f) - set(creds.keys())) == 0:
                logger.info("Auth completed with creds: %r", creds)
                self._remove_session(session)
                defer.returnValue((True, creds, clientdict))

        ret = self._auth_dict_for_flows(flows, session)
        ret['completed'] = creds.keys()
        defer.returnValue((False, ret, clientdict))

    @defer.inlineCallbacks
    def add_oob_auth(self, stagetype, authdict, clientip):
        """
        Adds the result of out-of-band authentication into an existing auth
        session. Currently used for adding the result of fallback auth.
        """
        if stagetype not in self.checkers:
            raise LoginError(400, "", Codes.MISSING_PARAM)
        if 'session' not in authdict:
            raise LoginError(400, "", Codes.MISSING_PARAM)

        sess = self._get_session_info(
            authdict['session']
        )
        if 'creds' not in sess:
            sess['creds'] = {}
        creds = sess['creds']

        result = yield self.checkers[stagetype](authdict, clientip)
        if result:
            creds[stagetype] = result
            self._save_session(sess)
            defer.returnValue(True)
        defer.returnValue(False)

    @defer.inlineCallbacks
    def _check_password_auth(self, authdict, _):
        if "user" not in authdict or "password" not in authdict:
            raise LoginError(400, "", Codes.MISSING_PARAM)

        user = authdict["user"]
        password = authdict["password"]
        if not user.startswith('@'):
            user = UserID.create(user, self.hs.hostname).to_string()

        user_info = yield self.store.get_user_by_id(user_id=user)
        if not user_info:
            logger.warn("Attempted to login as %s but they do not exist", user)
            raise LoginError(401, "", errcode=Codes.UNAUTHORIZED)

        stored_hash = user_info["password_hash"]
        if bcrypt.checkpw(password, stored_hash):
            defer.returnValue(user)
        else:
            logger.warn("Failed password login for user %s", user)
            raise LoginError(401, "", errcode=Codes.UNAUTHORIZED)

    @defer.inlineCallbacks
    def _check_recaptcha(self, authdict, clientip):
        try:
            user_response = authdict["response"]
        except KeyError:
            # Client tried to provide captcha but didn't give the parameter:
            # bad request.
            raise LoginError(
                400, "Captcha response is required",
                errcode=Codes.CAPTCHA_NEEDED
            )

        logger.info(
            "Submitting recaptcha response %s with remoteip %s",
            user_response, clientip
        )

        # TODO: get this from the homeserver rather than creating a new one for
        # each request
        try:
            client = SimpleHttpClient(self.hs)
            resp_body = yield client.post_urlencoded_get_json(
                self.hs.config.recaptcha_siteverify_api,
                args={
                    'secret': self.hs.config.recaptcha_private_key,
                    'response': user_response,
                    'remoteip': clientip,
                }
            )
        except PartialDownloadError as pde:
            # Twisted is silly
            data = pde.response
            resp_body = simplejson.loads(data)

        if 'success' in resp_body and resp_body['success']:
            defer.returnValue(True)
        raise LoginError(401, "", errcode=Codes.UNAUTHORIZED)

    @defer.inlineCallbacks
    def _check_email_identity(self, authdict, _):
        yield run_on_reactor()

        if 'threepid_creds' not in authdict:
            raise LoginError(400, "Missing threepid_creds", Codes.MISSING_PARAM)

        threepid_creds = authdict['threepid_creds']
        identity_handler = self.hs.get_handlers().identity_handler

        logger.info("Getting validated threepid. threepidcreds: %r" % (threepid_creds,))
        threepid = yield identity_handler.threepid_from_creds(threepid_creds)

        if not threepid:
            raise LoginError(401, "", errcode=Codes.UNAUTHORIZED)

        threepid['threepid_creds'] = authdict['threepid_creds']

        defer.returnValue(threepid)

    @defer.inlineCallbacks
    def _check_dummy_auth(self, authdict, _):
        yield run_on_reactor()
        defer.returnValue(True)

    def _get_params_recaptcha(self):
        return {"public_key": self.hs.config.recaptcha_public_key}

    def _auth_dict_for_flows(self, flows, session):
        public_flows = []
        for f in flows:
            public_flows.append(f)

        get_params = {
            LoginType.RECAPTCHA: self._get_params_recaptcha,
        }

        params = {}

        for f in public_flows:
            for stage in f:
                if stage in get_params and stage not in params:
                    params[stage] = get_params[stage]()

        return {
            "session": session['id'],
            "flows": [{"stages": f} for f in public_flows],
            "params": params
        }

    def _get_session_info(self, session_id):
        if session_id not in self.sessions:
            session_id = None

        if not session_id:
            # create a new session
            while session_id is None or session_id in self.sessions:
                session_id = stringutils.random_string(24)
            self.sessions[session_id] = {
                "id": session_id,
            }

        return self.sessions[session_id]

    @defer.inlineCallbacks
    def login_with_password(self, user_id, password):
        """
        Authenticates the user with their username and password.

        Used only by the v1 login API.

        Args:
            user_id (str): User ID
            password (str): Password
        Returns:
            The access token for the user's session.
        Raises:
            StoreError if there was a problem storing the token.
            LoginError if there was an authentication problem.
        """
        user_info = yield self.store.get_user_by_id(user_id=user_id)
        if not user_info:
            logger.warn("Attempted to login as %s but they do not exist", user_id)
            raise LoginError(403, "", errcode=Codes.FORBIDDEN)

        stored_hash = user_info["password_hash"]
        if not bcrypt.checkpw(password, stored_hash):
            logger.warn("Failed password login for user %s", user_id)
            raise LoginError(403, "", errcode=Codes.FORBIDDEN)

        reg_handler = self.hs.get_handlers().registration_handler
        access_token = reg_handler.generate_token(user_id)
        logger.info("Adding token %s for user %s", access_token, user_id)
        yield self.store.add_access_token_to_user(user_id, access_token)
        defer.returnValue(access_token)

    @defer.inlineCallbacks
    def set_password(self, user_id, newpassword):
        password_hash = bcrypt.hashpw(newpassword, bcrypt.gensalt())

        yield self.store.user_set_password_hash(user_id, password_hash)
        yield self.store.user_delete_access_tokens(user_id)
        yield self.hs.get_pusherpool().remove_pushers_by_user(user_id)
        yield self.store.flush_user(user_id)

    @defer.inlineCallbacks
    def add_threepid(self, user_id, medium, address, validated_at):
        yield self.store.user_add_threepid(
            user_id, medium, address, validated_at,
            self.hs.get_clock().time_msec()
        )

    def _save_session(self, session):
        # TODO: Persistent storage
        logger.debug("Saving session %s", session)
        self.sessions[session["id"]] = session

    def _remove_session(self, session):
        logger.debug("Removing session %s", session)
        del self.sessions[session["id"]]
