# -*- coding: utf-8 -*-
# Copyright 2020 The Matrix.org Foundation C.I.C.
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
from typing import TYPE_CHECKING, Awaitable, Callable, List, Optional

import attr

from twisted.web.http import Request

from synapse.api.errors import RedirectException
from synapse.http.server import respond_with_html
from synapse.types import UserID, contains_invalid_mxid_characters
from synapse.util.async_helpers import Linearizer

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class MappingException(Exception):
    """Used to catch errors when mapping an SSO response to user attributes.

    Note that the msg that is raised is shown to end-users.
    """


@attr.s
class UserAttributes:
    localpart = attr.ib(type=str)
    display_name = attr.ib(type=Optional[str], default=None)
    emails = attr.ib(type=List[str], default=attr.Factory(list))


class SsoHandler:
    # The number of attempts to ask the mapping provider for when generating an MXID.
    _MAP_USERNAME_RETRIES = 1000

    def __init__(self, hs: "HomeServer"):
        self._store = hs.get_datastore()
        self._server_name = hs.hostname
        self._registration_handler = hs.get_registration_handler()
        self._error_template = hs.config.sso_error_template
        self._auth_handler = hs.get_auth_handler()

        # a lock on the mappings
        self._mapping_lock = Linearizer(name="sso_user_mapping", clock=hs.get_clock())

    def render_error(
        self, request, error: str, error_description: Optional[str] = None
    ) -> None:
        """Renders the error template and responds with it.

        This is used to show errors to the user. The template of this page can
        be found under `synapse/res/templates/sso_error.html`.

        Args:
            request: The incoming request from the browser.
                We'll respond with an HTML page describing the error.
            error: A technical identifier for this error.
            error_description: A human-readable description of the error.
        """
        html = self._error_template.render(
            error=error, error_description=error_description
        )
        respond_with_html(request, 400, html)

    async def get_sso_user_by_remote_user_id(
        self, auth_provider_id: str, remote_user_id: str
    ) -> Optional[str]:
        """
        Maps the user ID of a remote IdP to a mxid for a previously seen user.

        If the user has not been seen yet, this will return None.

        Args:
            auth_provider_id: A unique identifier for this SSO provider, e.g.
                "oidc" or "saml".
            remote_user_id: The user ID according to the remote IdP. This might
                be an e-mail address, a GUID, or some other form. It must be
                unique and immutable.

        Returns:
            The mxid of a previously seen user.
        """
        logger.debug(
            "Looking for existing mapping for user %s:%s",
            auth_provider_id,
            remote_user_id,
        )

        # Check if we already have a mapping for this user.
        previously_registered_user_id = await self._store.get_user_by_external_id(
            auth_provider_id, remote_user_id,
        )

        # A match was found, return the user ID.
        if previously_registered_user_id is not None:
            logger.info(
                "Found existing mapping for IdP '%s' and remote_user_id '%s': %s",
                auth_provider_id,
                remote_user_id,
                previously_registered_user_id,
            )
            return previously_registered_user_id

        # No match.
        return None

    async def get_mxid_from_sso(
        self,
        auth_provider_id: str,
        remote_user_id: str,
        user_agent: str,
        ip_address: str,
        sso_to_matrix_id_mapper: Callable[[int], Awaitable[UserAttributes]],
        grandfather_existing_users: Optional[Callable[[], Awaitable[Optional[str]]]],
    ) -> str:
        """
        Given an SSO ID, retrieve the user ID for it and possibly register the user.

        This first checks if the SSO ID has previously been linked to a matrix ID,
        if it has that matrix ID is returned regardless of the current mapping
        logic.

        If a callable is provided for grandfathering users, it is called and can
        potentially return a matrix ID to use. If it does, the SSO ID is linked to
        this matrix ID for subsequent calls.

        The mapping function is called (potentially multiple times) to generate
        a localpart for the user.

        If an unused localpart is generated, the user is registered from the
        given user-agent and IP address and the SSO ID is linked to this matrix
        ID for subsequent calls.

        Args:
            auth_provider_id: A unique identifier for this SSO provider, e.g.
                "oidc" or "saml".
            remote_user_id: The unique identifier from the SSO provider.
            user_agent: The user agent of the client making the request.
            ip_address: The IP address of the client making the request.
            sso_to_matrix_id_mapper: A callable to generate the user attributes.
                The only parameter is an integer which represents the amount of
                times the returned mxid localpart mapping has failed.

                It is expected that the mapper can raise two exceptions, which
                will get passed through to the caller:

                    MappingException if there was a problem mapping the response
                        to the user.
                    RedirectException to redirect to an additional page (e.g.
                        to prompt the user for more information).
            grandfather_existing_users: A callable which can return an previously
                existing matrix ID. The SSO ID is then linked to the returned
                matrix ID.

        Returns:
             The user ID associated with the SSO response.

        Raises:
            MappingException if there was a problem mapping the response to a user.
            RedirectException: if the mapping provider needs to redirect the user
                to an additional page. (e.g. to prompt for more information)

        """
        # grab a lock while we try to find a mapping for this user. This seems...
        # optimistic, especially for implementations that end up redirecting to
        # interstitial pages.
        with await self._mapping_lock.queue(auth_provider_id):
            # first of all, check if we already have a mapping for this user
            previously_registered_user_id = await self.get_sso_user_by_remote_user_id(
                auth_provider_id, remote_user_id,
            )
            if previously_registered_user_id:
                return previously_registered_user_id

            # Check for grandfathering of users.
            if grandfather_existing_users:
                previously_registered_user_id = await grandfather_existing_users()
                if previously_registered_user_id:
                    # Future logins should also match this user ID.
                    await self._store.record_user_external_id(
                        auth_provider_id, remote_user_id, previously_registered_user_id
                    )
                    return previously_registered_user_id

            # Otherwise, generate a new user.
            attributes = await self._call_attribute_mapper(sso_to_matrix_id_mapper)
            user_id = await self._register_mapped_user(
                attributes, auth_provider_id, remote_user_id, user_agent, ip_address,
            )
            return user_id

    async def _call_attribute_mapper(
        self, sso_to_matrix_id_mapper: Callable[[int], Awaitable[UserAttributes]],
    ) -> UserAttributes:
        """Call the attribute mapper function in a loop, until we get a unique userid"""
        for i in range(self._MAP_USERNAME_RETRIES):
            try:
                attributes = await sso_to_matrix_id_mapper(i)
            except (RedirectException, MappingException):
                # Mapping providers are allowed to issue a redirect (e.g. to ask
                # the user for more information) and can issue a mapping exception
                # if a name cannot be generated.
                raise
            except Exception as e:
                # Any other exception is unexpected.
                raise MappingException(
                    "Could not extract user attributes from SSO response."
                ) from e

            logger.debug(
                "Retrieved user attributes from user mapping provider: %r (attempt %d)",
                attributes,
                i,
            )

            if not attributes.localpart:
                raise MappingException(
                    "Error parsing SSO response: SSO mapping provider plugin "
                    "did not return a localpart value"
                )

            # Check if this mxid already exists
            user_id = UserID(attributes.localpart, self._server_name).to_string()
            if not await self._store.get_users_by_id_case_insensitive(user_id):
                # This mxid is free
                break
        else:
            # Unable to generate a username in 1000 iterations
            # Break and return error to the user
            raise MappingException(
                "Unable to generate a Matrix ID from the SSO response"
            )
        return attributes

    async def _register_mapped_user(
        self,
        attributes: UserAttributes,
        auth_provider_id: str,
        remote_user_id: str,
        user_agent: str,
        ip_address: str,
    ) -> str:
        # Since the localpart is provided via a potentially untrusted module,
        # ensure the MXID is valid before registering.
        if contains_invalid_mxid_characters(attributes.localpart):
            raise MappingException("localpart is invalid: %s" % (attributes.localpart,))

        logger.debug("Mapped SSO user to local part %s", attributes.localpart)
        registered_user_id = await self._registration_handler.register_user(
            localpart=attributes.localpart,
            default_display_name=attributes.display_name,
            bind_emails=attributes.emails,
            user_agent_ips=[(user_agent, ip_address)],
        )

        await self._store.record_user_external_id(
            auth_provider_id, remote_user_id, registered_user_id
        )
        return registered_user_id

    async def complete_sso_ui_auth_request(
        self,
        auth_provider_id: str,
        remote_user_id: str,
        ui_auth_session_id: str,
        request: Request,
    ) -> None:
        """
        Given an SSO ID, retrieve the user ID for it and complete UIA.

        Note that this requires that the user is mapped in the "user_external_ids"
        table. This will be the case if they have ever logged in via SAML or OIDC in
        recentish synapse versions, but may not be for older users.

        Args:
            auth_provider_id: A unique identifier for this SSO provider, e.g.
                "oidc" or "saml".
            remote_user_id: The unique identifier from the SSO provider.
            ui_auth_session_id: The ID of the user-interactive auth session.
            request: The request to complete.
        """

        user_id = await self.get_sso_user_by_remote_user_id(
            auth_provider_id, remote_user_id,
        )

        if not user_id:
            logger.warning(
                "Remote user %s/%s has not previously logged in here: UIA will fail",
                auth_provider_id,
                remote_user_id,
            )
            # Let the UIA flow handle this the same as if they presented creds for a
            # different user.
            user_id = ""

        await self._auth_handler.complete_sso_ui_auth(
            user_id, ui_auth_session_id, request
        )
