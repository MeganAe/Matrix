Upgrading Synapse
=================

Before upgrading check if any special steps are required to upgrade from the
what you currently have installed to current version of synapse. The extra
instructions that may be required are listed later in this document.

1. If synapse was installed in a virtualenv then activate that virtualenv before
   upgrading. If synapse is installed in a virtualenv in ``~/synapse/env`` then
   run:

   .. code:: bash

       source ~/synapse/env/bin/activate

2. If synapse was installed using pip then upgrade to the latest version by
   running:

   .. code:: bash

       pip install --upgrade matrix-synapse[all]

       # restart synapse
       synctl restart


   If synapse was installed using git then upgrade to the latest version by
   running:

   .. code:: bash

       # Pull the latest version of the master branch.
       git pull

       # Update synapse and its python dependencies.
       pip install --upgrade .[all]

       # restart synapse
       ./synctl restart


To check whether your update was successful, you can check the Server header
returned by the Client-Server API:

.. code:: bash

    # replace <host.name> with the hostname of your synapse homeserver.
    # You may need to specify a port (eg, :8448) if your server is not
    # configured on port 443.
    curl -kv https://<host.name>/_matrix/client/versions 2>&1 | grep "Server:"

Upgrading to v1.4.0
===================

Config options
--------------

**Note: Registration by email address or phone number will not work in this release unless
some config options are changed from their defaults.**

This is due to Synapse v1.4.0 now defaulting to sending registration and password reset tokens
itself. This is for security reasons as well as putting less reliance on identity servers.
However, currently Synapse only supports sending emails, and does not have support for
phone-based password reset or account registration. If Synapse is configured to handle these on
its own, phone-based password resets and registration will be disabled. For Synapse to send
emails, the ``email`` block of the config must be filled out. If not, then password resets and
registration via email will be disabled entirely.

This release also deprecates the ``email.trust_identity_server_for_password_resets`` option and
replaces it with the ``account_threepid_delegates`` dictionary. This option defines whether the
homeserver should delegate an external server (typically an `identity server
<https://matrix.org/docs/spec/identity_service/r0.2.1>`_) to handle sending password reset or
registration messages via email and SMS.

If ``email.trust_identity_server_for_password_resets`` is set to ``true``, and
``account_threepid_delegates.email`` is not set, then the first entry in
``trusted_third_party_id_servers`` will be used as the account threepid delegate for email.
This is to ensure compatibility with existing Synapse installs that set up external server
handling for these tasks before v1.4.0. If ``email.trust_identity_server_for_password_resets``
is ``true`` and no trusted identity server domains are configured, Synapse will throw an error.

If ``email.trust_identity_server_for_password_resets`` is ``false`` or absent and a threepid
type in ``account_threepid_delegates`` is not set to a domain, then Synapse will attempt to
send password reset and registration messages for that type.

Email templates
---------------

If you have configured a custom template directory with the ``email.template_dir`` option, be
aware that there are new templates regarding registration. ``registration.html`` and
``registration.txt`` have been added and contain the content that is sent to a client upon
registering via an email address.

``registration_success.html`` and ``registration_failure.html`` are also new HTML templates
that will be shown to the user when they click the link in their registration emai , either
showing them a success or failure page (assuming a redirect URL is not configured).

Synapse will expect these files to exist inside the configured template directory. To view the
default templates, see `synapse/res/templates
<https://github.com/matrix-org/synapse/tree/master/synapse/res/templates>`_.

Rolling back to v1.3.1
----------------------

If you encounter problems with v1.4.0, it should be possible to roll back to
v1.3.1, subject to the following:

* The 'room statistics' engine was heavily reworked in this release (see
  `#5971 <https://github.com/matrix-org/synapse/pull/5971>`_), including
  significant changes to the database schema, which are not easily
  reverted. This will cause the room statistics engine to stop updating when
  you downgrade.

  The room statistics are essentially unused in v1.3.1 (in future versions of
  Synapse, they will be used to populate the room directory), so there should
  be no loss of functionality. However, the statistics engine will write errors
  to the logs, which can be avoided by setting the following in `homeserver.yaml`:

  .. code:: yaml

    stats:
      enabled: false

  Don't forget to re-enable it when you upgrade again, in preparation for its
  use in the room directory!

Upgrading to v1.2.0
===================

Some counter metrics have been renamed, with the old names deprecated. See
`the metrics documentation <docs/metrics-howto.md#renaming-of-metrics--deprecation-of-old-names-in-12>`_
for details.

Upgrading to v1.1.0
===================

Synapse v1.1.0 removes support for older Python and PostgreSQL versions, as
outlined in `our deprecation notice <https://matrix.org/blog/2019/04/08/synapse-deprecating-postgres-9-4-and-python-2-x>`_.

Minimum Python Version
----------------------

Synapse v1.1.0 has a minimum Python requirement of Python 3.5. Python 3.6 or
Python 3.7 are recommended as they have improved internal string handling,
significantly reducing memory usage.

If you use current versions of the Matrix.org-distributed Debian packages or
Docker images, action is not required.

If you install Synapse in a Python virtual environment, please see "Upgrading to
v0.34.0" for notes on setting up a new virtualenv under Python 3.

Minimum PostgreSQL Version
--------------------------

If using PostgreSQL under Synapse, you will need to use PostgreSQL 9.5 or above.
Please see the
`PostgreSQL documentation <https://www.postgresql.org/docs/11/upgrading.html>`_
for more details on upgrading your database.

Upgrading to v1.0
=================

Validation of TLS certificates
------------------------------

Synapse v1.0 is the first release to enforce
validation of TLS certificates for the federation API. It is therefore
essential that your certificates are correctly configured. See the `FAQ
<docs/MSC1711_certificates_FAQ.md>`_ for more information.

Note, v1.0 installations will also no longer be able to federate with servers
that have not correctly configured their certificates.

In rare cases, it may be desirable to disable certificate checking: for
example, it might be essential to be able to federate with a given legacy
server in a closed federation. This can be done in one of two ways:-

* Configure the global switch ``federation_verify_certificates`` to ``false``.
* Configure a whitelist of server domains to trust via ``federation_certificate_verification_whitelist``.

See the `sample configuration file <docs/sample_config.yaml>`_
for more details on these settings.

Email
-----
When a user requests a password reset, Synapse will send an email to the
user to confirm the request.

Previous versions of Synapse delegated the job of sending this email to an
identity server. If the identity server was somehow malicious or became
compromised, it would be theoretically possible to hijack an account through
this means.

Therefore, by default, Synapse v1.0 will send the confirmation email itself. If
Synapse is not configured with an SMTP server, password reset via email will be
disabled.

To configure an SMTP server for Synapse, modify the configuration section
headed ``email``, and be sure to have at least the ``smtp_host``, ``smtp_port``
and ``notif_from`` fields filled out. You may also need to set ``smtp_user``,
``smtp_pass``, and ``require_transport_security``.

If you are absolutely certain that you wish to continue using an identity
server for password resets, set ``trust_identity_server_for_password_resets`` to ``true``.

See the `sample configuration file <docs/sample_config.yaml>`_
for more details on these settings.

New email templates
---------------
Some new templates have been added to the default template directory for the purpose of the
homeserver sending its own password reset emails. If you have configured a custom
``template_dir`` in your Synapse config, these files will need to be added.

``password_reset.html`` and ``password_reset.txt`` are HTML and plain text templates
respectively that contain the contents of what will be emailed to the user upon attempting to
reset their password via email. ``password_reset_success.html`` and
``password_reset_failure.html`` are HTML files that the content of which (assuming no redirect
URL is set) will be shown to the user after they attempt to click the link in the email sent
to them.

Upgrading to v0.99.0
====================

Please be aware that, before Synapse v1.0 is released around March 2019, you
will need to replace any self-signed certificates with those verified by a
root CA. Information on how to do so can be found at `the ACME docs
<docs/ACME.md>`_.

For more information on configuring TLS certificates see the `FAQ <docs/MSC1711_certificates_FAQ.md>`_.

Upgrading to v0.34.0
====================

1. This release is the first to fully support Python 3. Synapse will now run on
   Python versions 3.5, or 3.6 (as well as 2.7). We recommend switching to
   Python 3, as it has been shown to give performance improvements.

   For users who have installed Synapse into a virtualenv, we recommend doing
   this by creating a new virtualenv. For example::

       virtualenv -p python3 ~/synapse/env3
       source ~/synapse/env3/bin/activate
       pip install matrix-synapse

   You can then start synapse as normal, having activated the new virtualenv::

       cd ~/synapse
       source env3/bin/activate
       synctl start

   Users who have installed from distribution packages should see the relevant
   package documentation. See below for notes on Debian packages.

   * When upgrading to Python 3, you **must** make sure that your log files are
     configured as UTF-8, by adding ``encoding: utf8`` to the
     ``RotatingFileHandler`` configuration (if you have one) in your
     ``<server>.log.config`` file. For example, if your ``log.config`` file
     contains::

       handlers:
         file:
           class: logging.handlers.RotatingFileHandler
           formatter: precise
           filename: homeserver.log
           maxBytes: 104857600
           backupCount: 10
           filters: [context]
         console:
           class: logging.StreamHandler
           formatter: precise
           filters: [context]

     Then you should update this to be::

       handlers:
         file:
           class: logging.handlers.RotatingFileHandler
           formatter: precise
           filename: homeserver.log
           maxBytes: 104857600
           backupCount: 10
           filters: [context]
           encoding: utf8
         console:
           class: logging.StreamHandler
           formatter: precise
           filters: [context]

     There is no need to revert this change if downgrading to Python 2.

   We are also making available Debian packages which will run Synapse on
   Python 3. You can switch to these packages with ``apt-get install
   matrix-synapse-py3``, however, please read `debian/NEWS
   <https://github.com/matrix-org/synapse/blob/release-v0.34.0/debian/NEWS>`_
   before doing so. The existing ``matrix-synapse`` packages will continue to
   use Python 2 for the time being.

2. This release removes the ``riot.im`` from the default list of trusted
   identity servers.

   If ``riot.im`` is in your homeserver's list of
   ``trusted_third_party_id_servers``, you should remove it. It was added in
   case a hypothetical future identity server was put there. If you don't
   remove it, users may be unable to deactivate their accounts.

3. This release no longer installs the (unmaintained) Matrix Console web client
   as part of the default installation. It is possible to re-enable it by
   installing it separately and setting the ``web_client_location`` config
   option, but please consider switching to another client.

Upgrading to v0.33.7
====================

This release removes the example email notification templates from
``res/templates`` (they are now internal to the python package). This should
only affect you if you (a) deploy your Synapse instance from a git checkout or
a github snapshot URL, and (b) have email notifications enabled.

If you have email notifications enabled, you should ensure that
``email.template_dir`` is either configured to point at a directory where you
have installed customised templates, or leave it unset to use the default
templates.

Upgrading to v0.27.3
====================

This release expands the anonymous usage stats sent if the opt-in
``report_stats`` configuration is set to ``true``. We now capture RSS memory
and cpu use at a very coarse level. This requires administrators to install
the optional ``psutil`` python module.

We would appreciate it if you could assist by ensuring this module is available
and ``report_stats`` is enabled. This will let us see if performance changes to
synapse are having an impact to the general community.

Upgrading to v0.15.0
====================

If you want to use the new URL previewing API (/_matrix/media/r0/preview_url)
then you have to explicitly enable it in the config and update your dependencies
dependencies.  See README.rst for details.


Upgrading to v0.11.0
====================

This release includes the option to send anonymous usage stats to matrix.org,
and requires that administrators explictly opt in or out by setting the
``report_stats`` option to either ``true`` or ``false``.

We would really appreciate it if you could help our project out by reporting
anonymized usage statistics from your homeserver. Only very basic aggregate
data (e.g. number of users) will be reported, but it helps us to track the
growth of the Matrix community, and helps us to make Matrix a success, as well
as to convince other networks that they should peer with us.


Upgrading to v0.9.0
===================

Application services have had a breaking API change in this version.

They can no longer register themselves with a home server using the AS HTTP API. This
decision was made because a compromised application service with free reign to register
any regex in effect grants full read/write access to the home server if a regex of ``.*``
is used. An attack where a compromised AS re-registers itself with ``.*`` was deemed too
big of a security risk to ignore, and so the ability to register with the HS remotely has
been removed.

It has been replaced by specifying a list of application service registrations in
``homeserver.yaml``::

  app_service_config_files: ["registration-01.yaml", "registration-02.yaml"]

Where ``registration-01.yaml`` looks like::

  url: <String>  # e.g. "https://my.application.service.com"
  as_token: <String>
  hs_token: <String>
  sender_localpart: <String>  # This is a new field which denotes the user_id localpart when using the AS token
  namespaces:
    users:
      - exclusive: <Boolean>
        regex: <String>  # e.g. "@prefix_.*"
    aliases:
      - exclusive: <Boolean>
        regex: <String>
    rooms:
      - exclusive: <Boolean>
        regex: <String>

Upgrading to v0.8.0
===================

Servers which use captchas will need to add their public key to::

  static/client/register/register_config.js

    window.matrixRegistrationConfig = {
        recaptcha_public_key: "YOUR_PUBLIC_KEY"
    };

This is required in order to support registration fallback (typically used on
mobile devices).


Upgrading to v0.7.0
===================

New dependencies are:

- pydenticon
- simplejson
- syutil
- matrix-angular-sdk

To pull in these dependencies in a virtual env, run::

    python synapse/python_dependencies.py | xargs -n 1 pip install

Upgrading to v0.6.0
===================

To pull in new dependencies, run::

    python setup.py develop --user

This update includes a change to the database schema. To upgrade you first need
to upgrade the database by running::

    python scripts/upgrade_db_to_v0.6.0.py <db> <server_name> <signing_key>

Where `<db>` is the location of the database, `<server_name>` is the
server name as specified in the synapse configuration, and `<signing_key>` is
the location of the signing key as specified in the synapse configuration.

This may take some time to complete. Failures of signatures and content hashes
can safely be ignored.


Upgrading to v0.5.1
===================

Depending on precisely when you installed v0.5.0 you may have ended up with
a stale release of the reference matrix webclient installed as a python module.
To uninstall it and ensure you are depending on the latest module, please run::

    $ pip uninstall syweb

Upgrading to v0.5.0
===================

The webclient has been split out into a seperate repository/pacakage in this
release. Before you restart your homeserver you will need to pull in the
webclient package by running::

  python setup.py develop --user

This release completely changes the database schema and so requires upgrading
it before starting the new version of the homeserver.

The script "database-prepare-for-0.5.0.sh" should be used to upgrade the
database. This will save all user information, such as logins and profiles,
but will otherwise purge the database. This includes messages, which
rooms the home server was a member of and room alias mappings.

If you would like to keep your history, please take a copy of your database
file and ask for help in #matrix:matrix.org. The upgrade process is,
unfortunately, non trivial and requires human intervention to resolve any
resulting conflicts during the upgrade process.

Before running the command the homeserver should be first completely
shutdown. To run it, simply specify the location of the database, e.g.:

  ./scripts/database-prepare-for-0.5.0.sh "homeserver.db"

Once this has successfully completed it will be safe to restart the
homeserver. You may notice that the homeserver takes a few seconds longer to
restart than usual as it reinitializes the database.

On startup of the new version, users can either rejoin remote rooms using room
aliases or by being reinvited. Alternatively, if any other homeserver sends a
message to a room that the homeserver was previously in the local HS will
automatically rejoin the room.

Upgrading to v0.4.0
===================

This release needs an updated syutil version. Run::

    python setup.py develop

You will also need to upgrade your configuration as the signing key format has
changed. Run::

    python -m synapse.app.homeserver --config-path <CONFIG> --generate-config


Upgrading to v0.3.0
===================

This registration API now closely matches the login API. This introduces a bit
more backwards and forwards between the HS and the client, but this improves
the overall flexibility of the API. You can now GET on /register to retrieve a list
of valid registration flows. Upon choosing one, they are submitted in the same
way as login, e.g::

  {
    type: m.login.password,
    user: foo,
    password: bar
  }

The default HS supports 2 flows, with and without Identity Server email
authentication. Enabling captcha on the HS will add in an extra step to all
flows: ``m.login.recaptcha`` which must be completed before you can transition
to the next stage. There is a new login type: ``m.login.email.identity`` which
contains the ``threepidCreds`` key which were previously sent in the original
register request. For more information on this, see the specification.

Web Client
----------

The VoIP specification has changed between v0.2.0 and v0.3.0. Users should
refresh any browser tabs to get the latest web client code. Users on
v0.2.0 of the web client will not be able to call those on v0.3.0 and
vice versa.


Upgrading to v0.2.0
===================

The home server now requires setting up of SSL config before it can run. To
automatically generate default config use::

    $ python synapse/app/homeserver.py \
        --server-name machine.my.domain.name \
        --bind-port 8448 \
        --config-path homeserver.config \
        --generate-config

This config can be edited if desired, for example to specify a different SSL
certificate to use. Once done you can run the home server using::

    $ python synapse/app/homeserver.py --config-path homeserver.config

See the README.rst for more information.

Also note that some config options have been renamed, including:

- "host" to "server-name"
- "database" to "database-path"
- "port" to "bind-port" and "unsecure-port"


Upgrading to v0.0.1
===================

This release completely changes the database schema and so requires upgrading
it before starting the new version of the homeserver.

The script "database-prepare-for-0.0.1.sh" should be used to upgrade the
database. This will save all user information, such as logins and profiles,
but will otherwise purge the database. This includes messages, which
rooms the home server was a member of and room alias mappings.

Before running the command the homeserver should be first completely
shutdown. To run it, simply specify the location of the database, e.g.:

  ./scripts/database-prepare-for-0.0.1.sh "homeserver.db"

Once this has successfully completed it will be safe to restart the
homeserver. You may notice that the homeserver takes a few seconds longer to
restart than usual as it reinitializes the database.

On startup of the new version, users can either rejoin remote rooms using room
aliases or by being reinvited. Alternatively, if any other homeserver sends a
message to a room that the homeserver was previously in the local HS will
automatically rejoin the room.
