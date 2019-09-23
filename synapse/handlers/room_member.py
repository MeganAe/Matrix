# -*- coding: utf-8 -*-
# Copyright 2016 OpenMarket Ltd
# Copyright 2018 New Vector Ltd
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

import abc
import logging

from six.moves import http_client

from signedjson.key import decode_verify_key_bytes
from signedjson.sign import verify_signed_json
from unpaddedbase64 import decode_base64

from twisted.internet import defer
from twisted.internet.error import TimeoutError

from synapse import types
from synapse.api.constants import EventTypes, Membership
from synapse.api.errors import AuthError, Codes, HttpResponseException, SynapseError
from synapse.handlers.identity import LookupAlgorithm, create_id_access_token_header
from synapse.http.client import SimpleHttpClient
from synapse.types import RoomID, UserID
from synapse.util.async_helpers import Linearizer
from synapse.util.distributor import user_joined_room, user_left_room
from synapse.util.hash import sha256_and_url_safe_base64

from ._base import BaseHandler

logger = logging.getLogger(__name__)

id_server_scheme = "https://"


class RoomMemberHandler(object):
    # TODO(paul): This handler currently contains a messy conflation of
    #   low-level API that works on UserID objects and so on, and REST-level
    #   API that takes ID strings and returns pagination chunks. These concerns
    #   ought to be separated out a lot better.

    __metaclass__ = abc.ABCMeta

    def __init__(self, hs):
        """

        Args:
            hs (synapse.server.HomeServer):
        """
        self.hs = hs
        self.store = hs.get_datastore()
        self.auth = hs.get_auth()
        self.state_handler = hs.get_state_handler()
        self.config = hs.config
        # We create a blacklisting instance of SimpleHttpClient for contacting identity
        # servers specified by clients
        self.simple_http_client = SimpleHttpClient(
            hs, ip_blacklist=hs.config.federation_ip_range_blacklist
        )

        self.federation_handler = hs.get_handlers().federation_handler
        self.directory_handler = hs.get_handlers().directory_handler
        self.registration_handler = hs.get_registration_handler()
        self.profile_handler = hs.get_profile_handler()
        self.event_creation_handler = hs.get_event_creation_handler()

        self.member_linearizer = Linearizer(name="member")

        self.clock = hs.get_clock()
        self.spam_checker = hs.get_spam_checker()
        self.third_party_event_rules = hs.get_third_party_event_rules()
        self._server_notices_mxid = self.config.server_notices_mxid
        self._enable_lookup = hs.config.enable_3pid_lookup
        self.allow_per_room_profiles = self.config.allow_per_room_profiles

        # This is only used to get at ratelimit function, and
        # maybe_kick_guest_users. It's fine there are multiple of these as
        # it doesn't store state.
        self.base_handler = BaseHandler(hs)

    @abc.abstractmethod
    def _remote_join(self, requester, remote_room_hosts, room_id, user, content):
        """Try and join a room that this server is not in

        Args:
            requester (Requester)
            remote_room_hosts (list[str]): List of servers that can be used
                to join via.
            room_id (str): Room that we are trying to join
            user (UserID): User who is trying to join
            content (dict): A dict that should be used as the content of the
                join event.

        Returns:
            Deferred
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def _remote_reject_invite(self, requester, remote_room_hosts, room_id, target):
        """Attempt to reject an invite for a room this server is not in. If we
        fail to do so we locally mark the invite as rejected.

        Args:
            requester (Requester)
            remote_room_hosts (list[str]): List of servers to use to try and
                reject invite
            room_id (str)
            target (UserID): The user rejecting the invite

        Returns:
            Deferred[dict]: A dictionary to be returned to the client, may
            include event_id etc, or nothing if we locally rejected
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def _user_joined_room(self, target, room_id):
        """Notifies distributor on master process that the user has joined the
        room.

        Args:
            target (UserID)
            room_id (str)

        Returns:
            Deferred|None
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def _user_left_room(self, target, room_id):
        """Notifies distributor on master process that the user has left the
        room.

        Args:
            target (UserID)
            room_id (str)

        Returns:
            Deferred|None
        """
        raise NotImplementedError()

    @defer.inlineCallbacks
    def _local_membership_update(
        self,
        requester,
        target,
        room_id,
        membership,
        prev_events_and_hashes,
        txn_id=None,
        ratelimit=True,
        content=None,
        require_consent=True,
    ):
        user_id = target.to_string()

        if content is None:
            content = {}

        content["membership"] = membership
        if requester.is_guest:
            content["kind"] = "guest"

        event, context = yield self.event_creation_handler.create_event(
            requester,
            {
                "type": EventTypes.Member,
                "content": content,
                "room_id": room_id,
                "sender": requester.user.to_string(),
                "state_key": user_id,
                # For backwards compatibility:
                "membership": membership,
            },
            token_id=requester.access_token_id,
            txn_id=txn_id,
            prev_events_and_hashes=prev_events_and_hashes,
            require_consent=require_consent,
        )

        # Check if this event matches the previous membership event for the user.
        duplicate = yield self.event_creation_handler.deduplicate_state_event(
            event, context
        )
        if duplicate is not None:
            # Discard the new event since this membership change is a no-op.
            return duplicate

        yield self.event_creation_handler.handle_new_client_event(
            requester, event, context, extra_users=[target], ratelimit=ratelimit
        )

        prev_state_ids = yield context.get_prev_state_ids(self.store)

        prev_member_event_id = prev_state_ids.get((EventTypes.Member, user_id), None)

        if event.membership == Membership.JOIN:
            # Only fire user_joined_room if the user has actually joined the
            # room. Don't bother if the user is just changing their profile
            # info.
            newly_joined = True
            if prev_member_event_id:
                prev_member_event = yield self.store.get_event(prev_member_event_id)
                newly_joined = prev_member_event.membership != Membership.JOIN
            if newly_joined:
                yield self._user_joined_room(target, room_id)

            # Copy over direct message status and room tags if this is a join
            # on an upgraded room

            # Check if this is an upgraded room
            predecessor = yield self.store.get_room_predecessor(room_id)

            if predecessor:
                # It is an upgraded room. Copy over old tags
                self.copy_room_tags_and_direct_to_room(
                    predecessor["room_id"], room_id, user_id
                )
                # Move over old push rules
                self.store.move_push_rules_from_room_to_room_for_user(
                    predecessor["room_id"], room_id, user_id
                )
        elif event.membership == Membership.LEAVE:
            if prev_member_event_id:
                prev_member_event = yield self.store.get_event(prev_member_event_id)
                if prev_member_event.membership == Membership.JOIN:
                    yield self._user_left_room(target, room_id)

        return event

    @defer.inlineCallbacks
    def copy_room_tags_and_direct_to_room(self, old_room_id, new_room_id, user_id):
        """Copies the tags and direct room state from one room to another.

        Args:
            old_room_id (str)
            new_room_id (str)
            user_id (str)

        Returns:
            Deferred[None]
        """
        # Retrieve user account data for predecessor room
        user_account_data, _ = yield self.store.get_account_data_for_user(user_id)

        # Copy direct message state if applicable
        direct_rooms = user_account_data.get("m.direct", {})

        # Check which key this room is under
        if isinstance(direct_rooms, dict):
            for key, room_id_list in direct_rooms.items():
                if old_room_id in room_id_list and new_room_id not in room_id_list:
                    # Add new room_id to this key
                    direct_rooms[key].append(new_room_id)

                    # Save back to user's m.direct account data
                    yield self.store.add_account_data_for_user(
                        user_id, "m.direct", direct_rooms
                    )
                    break

        # Copy room tags if applicable
        room_tags = yield self.store.get_tags_for_room(user_id, old_room_id)

        # Copy each room tag to the new room
        for tag, tag_content in room_tags.items():
            yield self.store.add_tag_to_room(user_id, new_room_id, tag, tag_content)

    @defer.inlineCallbacks
    def update_membership(
        self,
        requester,
        target,
        room_id,
        action,
        txn_id=None,
        remote_room_hosts=None,
        third_party_signed=None,
        ratelimit=True,
        content=None,
        require_consent=True,
    ):
        key = (room_id,)

        with (yield self.member_linearizer.queue(key)):
            result = yield self._update_membership(
                requester,
                target,
                room_id,
                action,
                txn_id=txn_id,
                remote_room_hosts=remote_room_hosts,
                third_party_signed=third_party_signed,
                ratelimit=ratelimit,
                content=content,
                require_consent=require_consent,
            )

        return result

    @defer.inlineCallbacks
    def _update_membership(
        self,
        requester,
        target,
        room_id,
        action,
        txn_id=None,
        remote_room_hosts=None,
        third_party_signed=None,
        ratelimit=True,
        content=None,
        require_consent=True,
    ):
        content_specified = bool(content)
        if content is None:
            content = {}
        else:
            # We do a copy here as we potentially change some keys
            # later on.
            content = dict(content)

        if not self.allow_per_room_profiles:
            # Strip profile data, knowing that new profile data will be added to the
            # event's content in event_creation_handler.create_event() using the target's
            # global profile.
            content.pop("displayname", None)
            content.pop("avatar_url", None)

        effective_membership_state = action
        if action in ["kick", "unban"]:
            effective_membership_state = "leave"

        # if this is a join with a 3pid signature, we may need to turn a 3pid
        # invite into a normal invite before we can handle the join.
        if third_party_signed is not None:
            yield self.federation_handler.exchange_third_party_invite(
                third_party_signed["sender"],
                target.to_string(),
                room_id,
                third_party_signed,
            )

        if not remote_room_hosts:
            remote_room_hosts = []

        if effective_membership_state not in ("leave", "ban"):
            is_blocked = yield self.store.is_room_blocked(room_id)
            if is_blocked:
                raise SynapseError(403, "This room has been blocked on this server")

        if effective_membership_state == Membership.INVITE:
            # block any attempts to invite the server notices mxid
            if target.to_string() == self._server_notices_mxid:
                raise SynapseError(http_client.FORBIDDEN, "Cannot invite this user")

            block_invite = False

            if (
                self._server_notices_mxid is not None
                and requester.user.to_string() == self._server_notices_mxid
            ):
                # allow the server notices mxid to send invites
                is_requester_admin = True

            else:
                is_requester_admin = yield self.auth.is_server_admin(requester.user)

            if not is_requester_admin:
                if self.config.block_non_admin_invites:
                    logger.info(
                        "Blocking invite: user is not admin and non-admin "
                        "invites disabled"
                    )
                    block_invite = True

                if not self.spam_checker.user_may_invite(
                    requester.user.to_string(), target.to_string(), room_id
                ):
                    logger.info("Blocking invite due to spam checker")
                    block_invite = True

            if block_invite:
                raise SynapseError(403, "Invites have been disabled on this server")

        prev_events_and_hashes = yield self.store.get_prev_events_for_room(room_id)
        latest_event_ids = (event_id for (event_id, _, _) in prev_events_and_hashes)

        current_state_ids = yield self.state_handler.get_current_state_ids(
            room_id, latest_event_ids=latest_event_ids
        )

        # TODO: Refactor into dictionary of explicitly allowed transitions
        # between old and new state, with specific error messages for some
        # transitions and generic otherwise
        old_state_id = current_state_ids.get((EventTypes.Member, target.to_string()))
        if old_state_id:
            old_state = yield self.store.get_event(old_state_id, allow_none=True)
            old_membership = old_state.content.get("membership") if old_state else None
            if action == "unban" and old_membership != "ban":
                raise SynapseError(
                    403,
                    "Cannot unban user who was not banned"
                    " (membership=%s)" % old_membership,
                    errcode=Codes.BAD_STATE,
                )
            if old_membership == "ban" and action != "unban":
                raise SynapseError(
                    403,
                    "Cannot %s user who was banned" % (action,),
                    errcode=Codes.BAD_STATE,
                )

            if old_state:
                same_content = content == old_state.content
                same_membership = old_membership == effective_membership_state
                same_sender = requester.user.to_string() == old_state.sender
                if same_sender and same_membership and same_content:
                    return old_state

            if old_membership in ["ban", "leave"] and action == "kick":
                raise AuthError(403, "The target user is not in the room")

            # we don't allow people to reject invites to the server notice
            # room, but they can leave it once they are joined.
            if (
                old_membership == Membership.INVITE
                and effective_membership_state == Membership.LEAVE
            ):
                is_blocked = yield self._is_server_notice_room(room_id)
                if is_blocked:
                    raise SynapseError(
                        http_client.FORBIDDEN,
                        "You cannot reject this invite",
                        errcode=Codes.CANNOT_LEAVE_SERVER_NOTICE_ROOM,
                    )
        else:
            if action == "kick":
                raise AuthError(403, "The target user is not in the room")

        is_host_in_room = yield self._is_host_in_room(current_state_ids)

        if effective_membership_state == Membership.JOIN:
            if requester.is_guest:
                guest_can_join = yield self._can_guest_join(current_state_ids)
                if not guest_can_join:
                    # This should be an auth check, but guests are a local concept,
                    # so don't really fit into the general auth process.
                    raise AuthError(403, "Guest access not allowed")

            if not is_host_in_room:
                inviter = yield self._get_inviter(target.to_string(), room_id)
                if inviter and not self.hs.is_mine(inviter):
                    remote_room_hosts.append(inviter.domain)

                content["membership"] = Membership.JOIN

                profile = self.profile_handler
                if not content_specified:
                    content["displayname"] = yield profile.get_displayname(target)
                    content["avatar_url"] = yield profile.get_avatar_url(target)

                if requester.is_guest:
                    content["kind"] = "guest"

                ret = yield self._remote_join(
                    requester, remote_room_hosts, room_id, target, content
                )
                return ret

        elif effective_membership_state == Membership.LEAVE:
            if not is_host_in_room:
                # perhaps we've been invited
                inviter = yield self._get_inviter(target.to_string(), room_id)
                if not inviter:
                    raise SynapseError(404, "Not a known room")

                if self.hs.is_mine(inviter):
                    # the inviter was on our server, but has now left. Carry on
                    # with the normal rejection codepath.
                    #
                    # This is a bit of a hack, because the room might still be
                    # active on other servers.
                    pass
                else:
                    # send the rejection to the inviter's HS.
                    remote_room_hosts = remote_room_hosts + [inviter.domain]
                    res = yield self._remote_reject_invite(
                        requester, remote_room_hosts, room_id, target
                    )
                    return res

        res = yield self._local_membership_update(
            requester=requester,
            target=target,
            room_id=room_id,
            membership=effective_membership_state,
            txn_id=txn_id,
            ratelimit=ratelimit,
            prev_events_and_hashes=prev_events_and_hashes,
            content=content,
            require_consent=require_consent,
        )
        return res

    @defer.inlineCallbacks
    def send_membership_event(self, requester, event, context, ratelimit=True):
        """
        Change the membership status of a user in a room.

        Args:
            requester (Requester): The local user who requested the membership
                event. If None, certain checks, like whether this homeserver can
                act as the sender, will be skipped.
            event (SynapseEvent): The membership event.
            context: The context of the event.
            ratelimit (bool): Whether to rate limit this request.
        Raises:
            SynapseError if there was a problem changing the membership.
        """
        target_user = UserID.from_string(event.state_key)
        room_id = event.room_id

        if requester is not None:
            sender = UserID.from_string(event.sender)
            assert (
                sender == requester.user
            ), "Sender (%s) must be same as requester (%s)" % (sender, requester.user)
            assert self.hs.is_mine(sender), "Sender must be our own: %s" % (sender,)
        else:
            requester = types.create_requester(target_user)

        prev_event = yield self.event_creation_handler.deduplicate_state_event(
            event, context
        )
        if prev_event is not None:
            return

        prev_state_ids = yield context.get_prev_state_ids(self.store)
        if event.membership == Membership.JOIN:
            if requester.is_guest:
                guest_can_join = yield self._can_guest_join(prev_state_ids)
                if not guest_can_join:
                    # This should be an auth check, but guests are a local concept,
                    # so don't really fit into the general auth process.
                    raise AuthError(403, "Guest access not allowed")

        if event.membership not in (Membership.LEAVE, Membership.BAN):
            is_blocked = yield self.store.is_room_blocked(room_id)
            if is_blocked:
                raise SynapseError(403, "This room has been blocked on this server")

        yield self.event_creation_handler.handle_new_client_event(
            requester, event, context, extra_users=[target_user], ratelimit=ratelimit
        )

        prev_member_event_id = prev_state_ids.get(
            (EventTypes.Member, event.state_key), None
        )

        if event.membership == Membership.JOIN:
            # Only fire user_joined_room if the user has actually joined the
            # room. Don't bother if the user is just changing their profile
            # info.
            newly_joined = True
            if prev_member_event_id:
                prev_member_event = yield self.store.get_event(prev_member_event_id)
                newly_joined = prev_member_event.membership != Membership.JOIN
            if newly_joined:
                yield self._user_joined_room(target_user, room_id)
        elif event.membership == Membership.LEAVE:
            if prev_member_event_id:
                prev_member_event = yield self.store.get_event(prev_member_event_id)
                if prev_member_event.membership == Membership.JOIN:
                    yield self._user_left_room(target_user, room_id)

    @defer.inlineCallbacks
    def _can_guest_join(self, current_state_ids):
        """
        Returns whether a guest can join a room based on its current state.
        """
        guest_access_id = current_state_ids.get((EventTypes.GuestAccess, ""), None)
        if not guest_access_id:
            return False

        guest_access = yield self.store.get_event(guest_access_id)

        return (
            guest_access
            and guest_access.content
            and "guest_access" in guest_access.content
            and guest_access.content["guest_access"] == "can_join"
        )

    @defer.inlineCallbacks
    def lookup_room_alias(self, room_alias):
        """
        Get the room ID associated with a room alias.

        Args:
            room_alias (RoomAlias): The alias to look up.
        Returns:
            A tuple of:
                The room ID as a RoomID object.
                Hosts likely to be participating in the room ([str]).
        Raises:
            SynapseError if room alias could not be found.
        """
        directory_handler = self.directory_handler
        mapping = yield directory_handler.get_association(room_alias)

        if not mapping:
            raise SynapseError(404, "No such room alias")

        room_id = mapping["room_id"]
        servers = mapping["servers"]

        # put the server which owns the alias at the front of the server list.
        if room_alias.domain in servers:
            servers.remove(room_alias.domain)
        servers.insert(0, room_alias.domain)

        return RoomID.from_string(room_id), servers

    @defer.inlineCallbacks
    def _get_inviter(self, user_id, room_id):
        invite = yield self.store.get_invite_for_user_in_room(
            user_id=user_id, room_id=room_id
        )
        if invite:
            return UserID.from_string(invite.sender)

    @defer.inlineCallbacks
    def do_3pid_invite(
        self,
        room_id,
        inviter,
        medium,
        address,
        id_server,
        requester,
        txn_id,
        id_access_token=None,
    ):
        if self.config.block_non_admin_invites:
            is_requester_admin = yield self.auth.is_server_admin(requester.user)
            if not is_requester_admin:
                raise SynapseError(
                    403, "Invites have been disabled on this server", Codes.FORBIDDEN
                )

        # We need to rate limit *before* we send out any 3PID invites, so we
        # can't just rely on the standard ratelimiting of events.
        yield self.base_handler.ratelimit(requester)

        can_invite = yield self.third_party_event_rules.check_threepid_can_be_invited(
            medium, address, room_id
        )
        if not can_invite:
            raise SynapseError(
                403,
                "This third-party identifier can not be invited in this room",
                Codes.FORBIDDEN,
            )

        if not self._enable_lookup:
            raise SynapseError(
                403, "Looking up third-party identifiers is denied from this server"
            )

        invitee = yield self._lookup_3pid(id_server, medium, address, id_access_token)

        if invitee:
            yield self.update_membership(
                requester, UserID.from_string(invitee), room_id, "invite", txn_id=txn_id
            )
        else:
            yield self._make_and_store_3pid_invite(
                requester,
                id_server,
                medium,
                address,
                room_id,
                inviter,
                txn_id=txn_id,
                id_access_token=id_access_token,
            )

    @defer.inlineCallbacks
    def _lookup_3pid(self, id_server, medium, address, id_access_token=None):
        """Looks up a 3pid in the passed identity server.

        Args:
            id_server (str): The server name (including port, if required)
                of the identity server to use.
            medium (str): The type of the third party identifier (e.g. "email").
            address (str): The third party identifier (e.g. "foo@example.com").
            id_access_token (str|None): The access token to authenticate to the identity
                server with

        Returns:
            str|None: the matrix ID of the 3pid, or None if it is not recognized.
        """
        if id_access_token is not None:
            try:
                results = yield self._lookup_3pid_v2(
                    id_server, id_access_token, medium, address
                )
                return results

            except Exception as e:
                # Catch HttpResponseExcept for a non-200 response code
                # Check if this identity server does not know about v2 lookups
                if isinstance(e, HttpResponseException) and e.code == 404:
                    # This is an old identity server that does not yet support v2 lookups
                    logger.warning(
                        "Attempted v2 lookup on v1 identity server %s. Falling "
                        "back to v1",
                        id_server,
                    )
                else:
                    logger.warning("Error when looking up hashing details: %s", e)
                    return None

        return (yield self._lookup_3pid_v1(id_server, medium, address))

    @defer.inlineCallbacks
    def _lookup_3pid_v1(self, id_server, medium, address):
        """Looks up a 3pid in the passed identity server using v1 lookup.

        Args:
            id_server (str): The server name (including port, if required)
                of the identity server to use.
            medium (str): The type of the third party identifier (e.g. "email").
            address (str): The third party identifier (e.g. "foo@example.com").

        Returns:
            str: the matrix ID of the 3pid, or None if it is not recognized.
        """
        try:
            data = yield self.simple_http_client.get_json(
                "%s%s/_matrix/identity/api/v1/lookup" % (id_server_scheme, id_server),
                {"medium": medium, "address": address},
            )

            if "mxid" in data:
                if "signatures" not in data:
                    raise AuthError(401, "No signatures on 3pid binding")
                yield self._verify_any_signature(data, id_server)
                return data["mxid"]
        except TimeoutError:
            raise SynapseError(500, "Timed out contacting identity server")
        except IOError as e:
            logger.warning("Error from v1 identity server lookup: %s" % (e,))

        return None

    @defer.inlineCallbacks
    def _lookup_3pid_v2(self, id_server, id_access_token, medium, address):
        """Looks up a 3pid in the passed identity server using v2 lookup.

        Args:
            id_server (str): The server name (including port, if required)
                of the identity server to use.
            id_access_token (str): The access token to authenticate to the identity server with
            medium (str): The type of the third party identifier (e.g. "email").
            address (str): The third party identifier (e.g. "foo@example.com").

        Returns:
            Deferred[str|None]: the matrix ID of the 3pid, or None if it is not recognised.
        """
        # Check what hashing details are supported by this identity server
        try:
            hash_details = yield self.simple_http_client.get_json(
                "%s%s/_matrix/identity/v2/hash_details" % (id_server_scheme, id_server),
                {"access_token": id_access_token},
            )
        except TimeoutError:
            raise SynapseError(500, "Timed out contacting identity server")

        if not isinstance(hash_details, dict):
            logger.warning(
                "Got non-dict object when checking hash details of %s%s: %s",
                id_server_scheme,
                id_server,
                hash_details,
            )
            raise SynapseError(
                400,
                "Non-dict object from %s%s during v2 hash_details request: %s"
                % (id_server_scheme, id_server, hash_details),
            )

        # Extract information from hash_details
        supported_lookup_algorithms = hash_details.get("algorithms")
        lookup_pepper = hash_details.get("lookup_pepper")
        if (
            not supported_lookup_algorithms
            or not isinstance(supported_lookup_algorithms, list)
            or not lookup_pepper
            or not isinstance(lookup_pepper, str)
        ):
            raise SynapseError(
                400,
                "Invalid hash details received from identity server %s%s: %s"
                % (id_server_scheme, id_server, hash_details),
            )

        # Check if any of the supported lookup algorithms are present
        if LookupAlgorithm.SHA256 in supported_lookup_algorithms:
            # Perform a hashed lookup
            lookup_algorithm = LookupAlgorithm.SHA256

            # Hash address, medium and the pepper with sha256
            to_hash = "%s %s %s" % (address, medium, lookup_pepper)
            lookup_value = sha256_and_url_safe_base64(to_hash)

        elif LookupAlgorithm.NONE in supported_lookup_algorithms:
            # Perform a non-hashed lookup
            lookup_algorithm = LookupAlgorithm.NONE

            # Combine together plaintext address and medium
            lookup_value = "%s %s" % (address, medium)

        else:
            logger.warning(
                "None of the provided lookup algorithms of %s are supported: %s",
                id_server,
                supported_lookup_algorithms,
            )
            raise SynapseError(
                400,
                "Provided identity server does not support any v2 lookup "
                "algorithms that this homeserver supports.",
            )

        # Authenticate with identity server given the access token from the client
        headers = {"Authorization": create_id_access_token_header(id_access_token)}

        try:
            lookup_results = yield self.simple_http_client.post_json_get_json(
                "%s%s/_matrix/identity/v2/lookup" % (id_server_scheme, id_server),
                {
                    "addresses": [lookup_value],
                    "algorithm": lookup_algorithm,
                    "pepper": lookup_pepper,
                },
                headers=headers,
            )
        except TimeoutError:
            raise SynapseError(500, "Timed out contacting identity server")
        except Exception as e:
            logger.warning("Error when performing a v2 3pid lookup: %s", e)
            raise SynapseError(
                500, "Unknown error occurred during identity server lookup"
            )

        # Check for a mapping from what we looked up to an MXID
        if "mappings" not in lookup_results or not isinstance(
            lookup_results["mappings"], dict
        ):
            logger.warning("No results from 3pid lookup")
            return None

        # Return the MXID if it's available, or None otherwise
        mxid = lookup_results["mappings"].get(lookup_value)
        return mxid

    @defer.inlineCallbacks
    def _verify_any_signature(self, data, server_hostname):
        if server_hostname not in data["signatures"]:
            raise AuthError(401, "No signature from server %s" % (server_hostname,))
        for key_name, signature in data["signatures"][server_hostname].items():
            try:
                key_data = yield self.simple_http_client.get_json(
                    "%s%s/_matrix/identity/api/v1/pubkey/%s"
                    % (id_server_scheme, server_hostname, key_name)
                )
            except TimeoutError:
                raise SynapseError(500, "Timed out contacting identity server")
            if "public_key" not in key_data:
                raise AuthError(
                    401, "No public key named %s from %s" % (key_name, server_hostname)
                )
            verify_signed_json(
                data,
                server_hostname,
                decode_verify_key_bytes(
                    key_name, decode_base64(key_data["public_key"])
                ),
            )
            return

    @defer.inlineCallbacks
    def _make_and_store_3pid_invite(
        self,
        requester,
        id_server,
        medium,
        address,
        room_id,
        user,
        txn_id,
        id_access_token=None,
    ):
        room_state = yield self.state_handler.get_current_state(room_id)

        inviter_display_name = ""
        inviter_avatar_url = ""
        member_event = room_state.get((EventTypes.Member, user.to_string()))
        if member_event:
            inviter_display_name = member_event.content.get("displayname", "")
            inviter_avatar_url = member_event.content.get("avatar_url", "")

        # if user has no display name, default to their MXID
        if not inviter_display_name:
            inviter_display_name = user.to_string()

        canonical_room_alias = ""
        canonical_alias_event = room_state.get((EventTypes.CanonicalAlias, ""))
        if canonical_alias_event:
            canonical_room_alias = canonical_alias_event.content.get("alias", "")

        room_name = ""
        room_name_event = room_state.get((EventTypes.Name, ""))
        if room_name_event:
            room_name = room_name_event.content.get("name", "")

        room_join_rules = ""
        join_rules_event = room_state.get((EventTypes.JoinRules, ""))
        if join_rules_event:
            room_join_rules = join_rules_event.content.get("join_rule", "")

        room_avatar_url = ""
        room_avatar_event = room_state.get((EventTypes.RoomAvatar, ""))
        if room_avatar_event:
            room_avatar_url = room_avatar_event.content.get("url", "")

        token, public_keys, fallback_public_key, display_name = (
            yield self._ask_id_server_for_third_party_invite(
                requester=requester,
                id_server=id_server,
                medium=medium,
                address=address,
                room_id=room_id,
                inviter_user_id=user.to_string(),
                room_alias=canonical_room_alias,
                room_avatar_url=room_avatar_url,
                room_join_rules=room_join_rules,
                room_name=room_name,
                inviter_display_name=inviter_display_name,
                inviter_avatar_url=inviter_avatar_url,
                id_access_token=id_access_token,
            )
        )

        yield self.event_creation_handler.create_and_send_nonmember_event(
            requester,
            {
                "type": EventTypes.ThirdPartyInvite,
                "content": {
                    "display_name": display_name,
                    "public_keys": public_keys,
                    # For backwards compatibility:
                    "key_validity_url": fallback_public_key["key_validity_url"],
                    "public_key": fallback_public_key["public_key"],
                },
                "room_id": room_id,
                "sender": user.to_string(),
                "state_key": token,
            },
            ratelimit=False,
            txn_id=txn_id,
        )

    @defer.inlineCallbacks
    def _ask_id_server_for_third_party_invite(
        self,
        requester,
        id_server,
        medium,
        address,
        room_id,
        inviter_user_id,
        room_alias,
        room_avatar_url,
        room_join_rules,
        room_name,
        inviter_display_name,
        inviter_avatar_url,
        id_access_token=None,
    ):
        """
        Asks an identity server for a third party invite.

        Args:
            requester (Requester)
            id_server (str): hostname + optional port for the identity server.
            medium (str): The literal string "email".
            address (str): The third party address being invited.
            room_id (str): The ID of the room to which the user is invited.
            inviter_user_id (str): The user ID of the inviter.
            room_alias (str): An alias for the room, for cosmetic notifications.
            room_avatar_url (str): The URL of the room's avatar, for cosmetic
                notifications.
            room_join_rules (str): The join rules of the email (e.g. "public").
            room_name (str): The m.room.name of the room.
            inviter_display_name (str): The current display name of the
                inviter.
            inviter_avatar_url (str): The URL of the inviter's avatar.
            id_access_token (str|None): The access token to authenticate to the identity
                server with

        Returns:
            A deferred tuple containing:
                token (str): The token which must be signed to prove authenticity.
                public_keys ([{"public_key": str, "key_validity_url": str}]):
                    public_key is a base64-encoded ed25519 public key.
                fallback_public_key: One element from public_keys.
                display_name (str): A user-friendly name to represent the invited
                    user.
        """
        invite_config = {
            "medium": medium,
            "address": address,
            "room_id": room_id,
            "room_alias": room_alias,
            "room_avatar_url": room_avatar_url,
            "room_join_rules": room_join_rules,
            "room_name": room_name,
            "sender": inviter_user_id,
            "sender_display_name": inviter_display_name,
            "sender_avatar_url": inviter_avatar_url,
        }

        # Add the identity service access token to the JSON body and use the v2
        # Identity Service endpoints if id_access_token is present
        data = None
        base_url = "%s%s/_matrix/identity" % (id_server_scheme, id_server)

        if id_access_token:
            key_validity_url = "%s%s/_matrix/identity/v2/pubkey/isvalid" % (
                id_server_scheme,
                id_server,
            )

            # Attempt a v2 lookup
            url = base_url + "/v2/store-invite"
            try:
                data = yield self.simple_http_client.post_json_get_json(
                    url,
                    invite_config,
                    {"Authorization": create_id_access_token_header(id_access_token)},
                )
            except TimeoutError:
                raise SynapseError(500, "Timed out contacting identity server")
            except HttpResponseException as e:
                if e.code != 404:
                    logger.info("Failed to POST %s with JSON: %s", url, e)
                    raise e

        if data is None:
            key_validity_url = "%s%s/_matrix/identity/api/v1/pubkey/isvalid" % (
                id_server_scheme,
                id_server,
            )
            url = base_url + "/api/v1/store-invite"

            try:
                data = yield self.simple_http_client.post_json_get_json(
                    url, invite_config
                )
            except TimeoutError:
                raise SynapseError(500, "Timed out contacting identity server")
            except HttpResponseException as e:
                logger.warning(
                    "Error trying to call /store-invite on %s%s: %s",
                    id_server_scheme,
                    id_server,
                    e,
                )

            if data is None:
                # Some identity servers may only support application/x-www-form-urlencoded
                # types. This is especially true with old instances of Sydent, see
                # https://github.com/matrix-org/sydent/pull/170
                try:
                    data = yield self.simple_http_client.post_urlencoded_get_json(
                        url, invite_config
                    )
                except HttpResponseException as e:
                    logger.warning(
                        "Error calling /store-invite on %s%s with fallback "
                        "encoding: %s",
                        id_server_scheme,
                        id_server,
                        e,
                    )
                    raise e

        # TODO: Check for success
        token = data["token"]
        public_keys = data.get("public_keys", [])
        if "public_key" in data:
            fallback_public_key = {
                "public_key": data["public_key"],
                "key_validity_url": key_validity_url,
            }
        else:
            fallback_public_key = public_keys[0]

        if not public_keys:
            public_keys.append(fallback_public_key)
        display_name = data["display_name"]
        return token, public_keys, fallback_public_key, display_name

    @defer.inlineCallbacks
    def _is_host_in_room(self, current_state_ids):
        # Have we just created the room, and is this about to be the very
        # first member event?
        create_event_id = current_state_ids.get(("m.room.create", ""))
        if len(current_state_ids) == 1 and create_event_id:
            # We can only get here if we're in the process of creating the room
            return True

        for etype, state_key in current_state_ids:
            if etype != EventTypes.Member or not self.hs.is_mine_id(state_key):
                continue

            event_id = current_state_ids[(etype, state_key)]
            event = yield self.store.get_event(event_id, allow_none=True)
            if not event:
                continue

            if event.membership == Membership.JOIN:
                return True

        return False

    @defer.inlineCallbacks
    def _is_server_notice_room(self, room_id):
        if self._server_notices_mxid is None:
            return False
        user_ids = yield self.store.get_users_in_room(room_id)
        return self._server_notices_mxid in user_ids


class RoomMemberMasterHandler(RoomMemberHandler):
    def __init__(self, hs):
        super(RoomMemberMasterHandler, self).__init__(hs)

        self.distributor = hs.get_distributor()
        self.distributor.declare("user_joined_room")
        self.distributor.declare("user_left_room")

    @defer.inlineCallbacks
    def _is_remote_room_too_complex(self, room_id, remote_room_hosts):
        """
        Check if complexity of a remote room is too great.

        Args:
            room_id (str)
            remote_room_hosts (list[str])

        Returns: bool of whether the complexity is too great, or None
            if unable to be fetched
        """
        max_complexity = self.hs.config.limit_remote_rooms.complexity
        complexity = yield self.federation_handler.get_room_complexity(
            remote_room_hosts, room_id
        )

        if complexity:
            return complexity["v1"] > max_complexity
        return None

    @defer.inlineCallbacks
    def _is_local_room_too_complex(self, room_id):
        """
        Check if the complexity of a local room is too great.

        Args:
            room_id (str)

        Returns: bool
        """
        max_complexity = self.hs.config.limit_remote_rooms.complexity
        complexity = yield self.store.get_room_complexity(room_id)

        return complexity["v1"] > max_complexity

    @defer.inlineCallbacks
    def _remote_join(self, requester, remote_room_hosts, room_id, user, content):
        """Implements RoomMemberHandler._remote_join
        """
        # filter ourselves out of remote_room_hosts: do_invite_join ignores it
        # and if it is the only entry we'd like to return a 404 rather than a
        # 500.
        remote_room_hosts = [
            host for host in remote_room_hosts if host != self.hs.hostname
        ]

        if len(remote_room_hosts) == 0:
            raise SynapseError(404, "No known servers")

        if self.hs.config.limit_remote_rooms.enabled:
            # Fetch the room complexity
            too_complex = yield self._is_remote_room_too_complex(
                room_id, remote_room_hosts
            )
            if too_complex is True:
                raise SynapseError(
                    code=400,
                    msg=self.hs.config.limit_remote_rooms.complexity_error,
                    errcode=Codes.RESOURCE_LIMIT_EXCEEDED,
                )

        # We don't do an auth check if we are doing an invite
        # join dance for now, since we're kinda implicitly checking
        # that we are allowed to join when we decide whether or not we
        # need to do the invite/join dance.
        yield self.federation_handler.do_invite_join(
            remote_room_hosts, room_id, user.to_string(), content
        )
        yield self._user_joined_room(user, room_id)

        # Check the room we just joined wasn't too large, if we didn't fetch the
        # complexity of it before.
        if self.hs.config.limit_remote_rooms.enabled:
            if too_complex is False:
                # We checked, and we're under the limit.
                return

            # Check again, but with the local state events
            too_complex = yield self._is_local_room_too_complex(room_id)

            if too_complex is False:
                # We're under the limit.
                return

            # The room is too large. Leave.
            requester = types.create_requester(user, None, False, None)
            yield self.update_membership(
                requester=requester, target=user, room_id=room_id, action="leave"
            )
            raise SynapseError(
                code=400,
                msg=self.hs.config.limit_remote_rooms.complexity_error,
                errcode=Codes.RESOURCE_LIMIT_EXCEEDED,
            )

    @defer.inlineCallbacks
    def _remote_reject_invite(self, requester, remote_room_hosts, room_id, target):
        """Implements RoomMemberHandler._remote_reject_invite
        """
        fed_handler = self.federation_handler
        try:
            ret = yield fed_handler.do_remotely_reject_invite(
                remote_room_hosts, room_id, target.to_string()
            )
            return ret
        except Exception as e:
            # if we were unable to reject the exception, just mark
            # it as rejected on our end and plough ahead.
            #
            # The 'except' clause is very broad, but we need to
            # capture everything from DNS failures upwards
            #
            logger.warning("Failed to reject invite: %s", e)

            yield self.store.locally_reject_invite(target.to_string(), room_id)
            return {}

    def _user_joined_room(self, target, room_id):
        """Implements RoomMemberHandler._user_joined_room
        """
        return user_joined_room(self.distributor, target, room_id)

    def _user_left_room(self, target, room_id):
        """Implements RoomMemberHandler._user_left_room
        """
        return user_left_room(self.distributor, target, room_id)

    @defer.inlineCallbacks
    def forget(self, user, room_id):
        user_id = user.to_string()

        member = yield self.state_handler.get_current_state(
            room_id=room_id, event_type=EventTypes.Member, state_key=user_id
        )
        membership = member.membership if member else None

        if membership is not None and membership not in [
            Membership.LEAVE,
            Membership.BAN,
        ]:
            raise SynapseError(400, "User %s in room %s" % (user_id, room_id))

        if membership:
            yield self.store.forget(user_id, room_id)
