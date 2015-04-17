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
import logging
import urllib
import yaml
from simplejson import JSONDecodeError
import simplejson as json
from twisted.internet import defer

from synapse.api.constants import Membership
from synapse.appservice import ApplicationService, AppServiceTransaction
from synapse.storage.roommember import RoomsForUser
from synapse.types import UserID
from ._base import SQLBaseStore


logger = logging.getLogger(__name__)


class ApplicationServiceStore(SQLBaseStore):

    def __init__(self, hs):
        super(ApplicationServiceStore, self).__init__(hs)
        self.hostname = hs.hostname
        self.services_cache = []
        self._populate_appservice_cache(
            hs.config.app_service_config_files
        )

    def get_app_services(self):
        return defer.succeed(self.services_cache)

    def get_app_service_by_user_id(self, user_id):
        """Retrieve an application service from their user ID.

        All application services have associated with them a particular user ID.
        There is no distinguishing feature on the user ID which indicates it
        represents an application service. This function allows you to map from
        a user ID to an application service.

        Args:
            user_id(str): The user ID to see if it is an application service.
        Returns:
            synapse.appservice.ApplicationService or None.
        """
        for service in self.services_cache:
            if service.sender == user_id:
                return defer.succeed(service)
        return defer.succeed(None)

    def get_app_service_by_token(self, token):
        """Get the application service with the given appservice token.

        Args:
            token (str): The application service token.
        Returns:
            synapse.appservice.ApplicationService or None.
        """
        for service in self.services_cache:
            if service.token == token:
                return defer.succeed(service)
        return defer.succeed(None)

    def get_app_service_rooms(self, service):
        """Get a list of RoomsForUser for this application service.

        Application services may be "interested" in lots of rooms depending on
        the room ID, the room aliases, or the members in the room. This function
        takes all of these into account and returns a list of RoomsForUser which
        represent the entire list of room IDs that this application service
        wants to know about.

        Args:
            service: The application service to get a room list for.
        Returns:
            A list of RoomsForUser.
        """
        return self.runInteraction(
            "get_app_service_rooms",
            self._get_app_service_rooms_txn,
            service,
        )

    def _get_app_service_rooms_txn(self, txn, service):
        # get all rooms matching the room ID regex.
        room_entries = self._simple_select_list_txn(
            txn=txn, table="rooms", keyvalues=None, retcols=["room_id"]
        )
        matching_room_list = set([
            r["room_id"] for r in room_entries if
            service.is_interested_in_room(r["room_id"])
        ])

        # resolve room IDs for matching room alias regex.
        room_alias_mappings = self._simple_select_list_txn(
            txn=txn, table="room_aliases", keyvalues=None,
            retcols=["room_id", "room_alias"]
        )
        matching_room_list |= set([
            r["room_id"] for r in room_alias_mappings if
            service.is_interested_in_alias(r["room_alias"])
        ])

        # get all rooms for every user for this AS. This is scoped to users on
        # this HS only.
        user_list = self._simple_select_list_txn(
            txn=txn, table="users", keyvalues=None, retcols=["name"]
        )
        user_list = [
            u["name"] for u in user_list if
            service.is_interested_in_user(u["name"])
        ]
        rooms_for_user_matching_user_id = set()  # RoomsForUser list
        for user_id in user_list:
            # FIXME: This assumes this store is linked with RoomMemberStore :(
            rooms_for_user = self._get_rooms_for_user_where_membership_is_txn(
                txn=txn,
                user_id=user_id,
                membership_list=[Membership.JOIN]
            )
            rooms_for_user_matching_user_id |= set(rooms_for_user)

        # make RoomsForUser tuples for room ids and aliases which are not in the
        # main rooms_for_user_list - e.g. they are rooms which do not have AS
        # registered users in it.
        known_room_ids = [r.room_id for r in rooms_for_user_matching_user_id]
        missing_rooms_for_user = [
            RoomsForUser(r, service.sender, "join") for r in
            matching_room_list if r not in known_room_ids
        ]
        rooms_for_user_matching_user_id |= set(missing_rooms_for_user)

        return rooms_for_user_matching_user_id

    def _parse_services_dict(self, results):
        # SQL results in the form:
        # [
        #   {
        #     'regex': "something",
        #     'url': "something",
        #     'namespace': enum,
        #     'as_id': 0,
        #     'token': "something",
        #     'hs_token': "otherthing",
        #     'id': 0
        #   }
        # ]
        services = {}
        for res in results:
            as_token = res["token"]
            if as_token is None:
                continue
            if as_token not in services:
                # add the service
                services[as_token] = {
                    "id": res["id"],
                    "url": res["url"],
                    "token": as_token,
                    "hs_token": res["hs_token"],
                    "sender": res["sender"],
                    "namespaces": {
                        ApplicationService.NS_USERS: [],
                        ApplicationService.NS_ALIASES: [],
                        ApplicationService.NS_ROOMS: []
                    }
                }
            # add the namespace regex if one exists
            ns_int = res["namespace"]
            if ns_int is None:
                continue
            try:
                services[as_token]["namespaces"][
                    ApplicationService.NS_LIST[ns_int]].append(
                    json.loads(res["regex"])
                )
            except IndexError:
                logger.error("Bad namespace enum '%s'. %s", ns_int, res)
            except JSONDecodeError:
                logger.error("Bad regex object '%s'", res["regex"])

        service_list = []
        for service in services.values():
            service_list.append(ApplicationService(
                token=service["token"],
                url=service["url"],
                namespaces=service["namespaces"],
                hs_token=service["hs_token"],
                sender=service["sender"],
                id=service["id"]
            ))
        return service_list

    def _load_appservice(self, as_info):
        required_string_fields = [
            "url", "as_token", "hs_token", "sender_localpart"
        ]
        for field in required_string_fields:
            if not isinstance(as_info.get(field), basestring):
                raise KeyError("Required string field: '%s'", field)

        localpart = as_info["sender_localpart"]
        if urllib.quote(localpart) != localpart:
            raise ValueError(
                "sender_localpart needs characters which are not URL encoded."
            )
        user = UserID(localpart, self.hostname)
        user_id = user.to_string()

        # namespace checks
        if not isinstance(as_info.get("namespaces"), dict):
            raise KeyError("Requires 'namespaces' object.")
        for ns in ApplicationService.NS_LIST:
            # specific namespaces are optional
            if ns in as_info["namespaces"]:
                # expect a list of dicts with exclusive and regex keys
                for regex_obj in as_info["namespaces"][ns]:
                    if not isinstance(regex_obj, dict):
                        raise ValueError(
                            "Expected namespace entry in %s to be an object,"
                            " but got %s", ns, regex_obj
                        )
                    if not isinstance(regex_obj.get("regex"), basestring):
                        raise ValueError(
                            "Missing/bad type 'regex' key in %s", regex_obj
                        )
                    if not isinstance(regex_obj.get("exclusive"), bool):
                        raise ValueError(
                            "Missing/bad type 'exclusive' key in %s", regex_obj
                        )
        return ApplicationService(
            token=as_info["as_token"],
            url=as_info["url"],
            namespaces=as_info["namespaces"],
            hs_token=as_info["hs_token"],
            sender=user_id,
            id=as_info["as_token"]  # the token is the only unique thing here
        )

    def _populate_appservice_cache(self, config_files):
        """Populates a cache of Application Services from the config files."""
        if not isinstance(config_files, list):
            logger.warning(
                "Expected %s to be a list of AS config files.", config_files
            )
            return

        for config_file in config_files:
            try:
                with open(config_file, 'r') as f:
                    appservice = self._load_appservice(yaml.load(f))
                    logger.info("Loaded application service: %s", appservice)
                    self.services_cache.append(appservice)
            except Exception as e:
                logger.error("Failed to load appservice from '%s'", config_file)
                logger.exception(e)


class ApplicationServiceTransactionStore(SQLBaseStore):

    def __init__(self, hs):
        super(ApplicationServiceTransactionStore, self).__init__(hs)

    @defer.inlineCallbacks
    def get_appservices_by_state(self, state):
        """Get a list of application services based on their state.

        Args:
            state(ApplicationServiceState): The state to filter on.
        Returns:
            A Deferred which resolves to a list of ApplicationServices, which
            may be empty.
        """
        results = yield self._simple_select_list(
            "application_services_state",
            dict(state=state),
            ["as_id"]
        )
        # NB: This assumes this class is linked with ApplicationServiceStore
        as_list = yield self.get_app_services()
        services = []

        for res in results:
            for service in as_list:
                if service.id == res["as_id"]:
                    services.append(service)
        defer.returnValue(services)

    @defer.inlineCallbacks
    def get_appservice_state(self, service):
        """Get the application service state.

        Args:
            service(ApplicationService): The service whose state to set.
        Returns:
            A Deferred which resolves to ApplicationServiceState.
        """
        result = yield self._simple_select_one(
            "application_services_state",
            dict(as_id=service.id),
            ["state"],
            allow_none=True
        )
        if result:
            defer.returnValue(result.get("state"))
            return
        defer.returnValue(None)

    def set_appservice_state(self, service, state):
        """Set the application service state.

        Args:
            service(ApplicationService): The service whose state to set.
            state(ApplicationServiceState): The connectivity state to apply.
        Returns:
            A Deferred which resolves when the state was set successfully.
        """
        return self._simple_upsert(
            "application_services_state",
            dict(as_id=service.id),
            dict(state=state)
        )

    def create_appservice_txn(self, service, events):
        """Atomically creates a new transaction for this application service
        with the given list of events.

        Args:
            service(ApplicationService): The service who the transaction is for.
            events(list<Event>): A list of events to put in the transaction.
        Returns:
            AppServiceTransaction: A new transaction.
        """
        return self.runInteraction(
            "create_appservice_txn",
            self._create_appservice_txn,
            service, events
        )

    def _create_appservice_txn(self, txn, service, events):
        # work out new txn id (highest txn id for this service += 1)
        # The highest id may be the last one sent (in which case it is last_txn)
        # or it may be the highest in the txns list (which are waiting to be/are
        # being sent)
        last_txn_id = self._get_last_txn(txn, service.id)

        result = txn.execute(
            "SELECT MAX(txn_id) FROM application_services_txns WHERE as_id=?",
            (service.id,)
        )
        highest_txn_id = result.fetchone()[0]
        if highest_txn_id is None:
            highest_txn_id = 0

        new_txn_id = max(highest_txn_id, last_txn_id) + 1

        # Insert new txn into txn table
        event_ids = [e.event_id for e in events]
        txn.execute(
            "INSERT INTO application_services_txns(as_id, txn_id, event_ids) "
            "VALUES(?,?,?)",
            (service.id, new_txn_id, json.dumps(event_ids))
        )
        return AppServiceTransaction(
            service=service, id=new_txn_id, events=events
        )

    def complete_appservice_txn(self, txn_id, service):
        """Completes an application service transaction.

        Args:
            txn_id(str): The transaction ID being completed.
            service(ApplicationService): The application service which was sent
            this transaction.
        Returns:
            A Deferred which resolves if this transaction was stored
            successfully.
        """
        return self.runInteraction(
            "complete_appservice_txn",
            self._complete_appservice_txn,
            txn_id, service
        )

    def _complete_appservice_txn(self, txn, txn_id, service):
        txn_id = int(txn_id)

        # Debugging query: Make sure the txn being completed is EXACTLY +1 from
        # what was there before. If it isn't, we've got problems (e.g. the AS
        # has probably missed some events), so whine loudly but still continue,
        # since it shouldn't fail completion of the transaction.
        last_txn_id = self._get_last_txn(txn, service.id)
        if (last_txn_id + 1) != txn_id:
            logger.error(
                "appservice: Completing a transaction which has an ID > 1 from "
                "the last ID sent to this AS. We've either dropped events or "
                "sent it to the AS out of order. FIX ME. last_txn=%s "
                "completing_txn=%s service_id=%s", last_txn_id, txn_id,
                service.id
            )

        # Set current txn_id for AS to 'txn_id'
        self._simple_upsert_txn(
            txn, "application_services_state", dict(as_id=service.id),
            dict(last_txn=txn_id)
        )

        # Delete txn
        self._simple_delete_txn(
            txn, "application_services_txns",
            dict(txn_id=txn_id, as_id=service.id)
        )

    def get_oldest_unsent_txn(self, service):
        """Get the oldest transaction which has not been sent for this
        service.

        Args:
            service(ApplicationService): The app service to get the oldest txn.
        Returns:
            A Deferred which resolves to an AppServiceTransaction or
            None.
        """
        return self.runInteraction(
            "get_oldest_unsent_appservice_txn",
            self._get_oldest_unsent_txn,
            service
        )

    def _get_oldest_unsent_txn(self, txn, service):
        # Monotonically increasing txn ids, so just select the smallest
        # one in the txns table (we delete them when they are sent)
        result = txn.execute(
            "SELECT MIN(txn_id), * FROM application_services_txns WHERE as_id=?",
            (service.id,)
        )
        entry = self.cursor_to_dict(result)[0]
        if not entry or entry["txn_id"] is None:
            # the min(txn_id) part will force a row, so entry may not be None
            return None

        event_ids = json.loads(entry["event_ids"])
        events = self._get_events_txn(txn, event_ids)

        return AppServiceTransaction(
            service=service, id=entry["txn_id"], events=events
        )

    def _get_last_txn(self, txn, service_id):
        result = txn.execute(
            "SELECT last_txn FROM application_services_state WHERE as_id=?",
            (service_id,)
        )
        last_txn_id = result.fetchone()
        if last_txn_id is None or last_txn_id[0] is None:  # no row exists
            return 0
        else:
            return int(last_txn_id[0])  # select 'last_txn' col
