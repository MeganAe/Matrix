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
from .appservice import (
    ApplicationServiceStore, ApplicationServiceTransactionStore
)
from ._base import Cache
from .directory import DirectoryStore
from .events import EventsStore
from .presence import PresenceStore
from .profile import ProfileStore
from .registration import RegistrationStore
from .room import RoomStore
from .roommember import RoomMemberStore
from .stream import StreamStore
from .transactions import TransactionStore
from .keys import KeyStore
from .event_federation import EventFederationStore
from .pusher import PusherStore
from .push_rule import PushRuleStore
from .media_repository import MediaRepositoryStore
from .rejections import RejectionsStore

from .state import StateStore
from .signatures import SignatureStore
from .filtering import FilteringStore


import fnmatch
import imp
import logging
import os
import re


logger = logging.getLogger(__name__)


# Remember to update this number every time a change is made to database
# schema files, so the users will be informed on server restarts.
SCHEMA_VERSION = 19

dir_path = os.path.abspath(os.path.dirname(__file__))

# Number of msec of granularity to store the user IP 'last seen' time. Smaller
# times give more inserts into the database even for readonly API hits
# 120 seconds == 2 minutes
LAST_SEEN_GRANULARITY = 120*1000


class DataStore(RoomMemberStore, RoomStore,
                RegistrationStore, StreamStore, ProfileStore,
                PresenceStore, TransactionStore,
                DirectoryStore, KeyStore, StateStore, SignatureStore,
                ApplicationServiceStore,
                EventFederationStore,
                MediaRepositoryStore,
                RejectionsStore,
                FilteringStore,
                PusherStore,
                PushRuleStore,
                ApplicationServiceTransactionStore,
                EventsStore,
                ):

    def __init__(self, hs):
        super(DataStore, self).__init__(hs)
        self.hs = hs

        self.min_token_deferred = self._get_min_token()
        self.min_token = None

        self.client_ip_last_seen = Cache(
            name="client_ip_last_seen",
            keylen=4,
        )

    @defer.inlineCallbacks
    def insert_client_ip(self, user, access_token, device_id, ip, user_agent):
        now = int(self._clock.time_msec())
        key = (user.to_string(), access_token, device_id, ip)

        try:
            last_seen = self.client_ip_last_seen.get(*key)
        except KeyError:
            last_seen = None

        # Rate-limited inserts
        if last_seen is not None and (now - last_seen) < LAST_SEEN_GRANULARITY:
            defer.returnValue(None)

        self.client_ip_last_seen.prefill(*key + (now,))

        # It's safe not to lock here: a) no unique constraint,
        # b) LAST_SEEN_GRANULARITY makes concurrent updates incredibly unlikely
        yield self._simple_upsert(
            "user_ips",
            keyvalues={
                "user_id": user.to_string(),
                "access_token": access_token,
                "ip": ip,
                "user_agent": user_agent,
            },
            values={
                "device_id": device_id,
                "last_seen": now,
            },
            desc="insert_client_ip",
            lock=False,
        )

    def get_user_ip_and_agents(self, user):
        return self._simple_select_list(
            table="user_ips",
            keyvalues={"user_id": user.to_string()},
            retcols=[
                "device_id", "access_token", "ip", "user_agent", "last_seen"
            ],
            desc="get_user_ip_and_agents",
        )


def read_schema(path):
    """ Read the named database schema.

    Args:
        path: Path of the database schema.
    Returns:
        A string containing the database schema.
    """
    with open(path) as schema_file:
        return schema_file.read()


class PrepareDatabaseException(Exception):
    pass


class UpgradeDatabaseException(PrepareDatabaseException):
    pass


def prepare_database(db_conn, database_engine):
    """Prepares a database for usage. Will either create all necessary tables
    or upgrade from an older schema version.
    """
    try:
        cur = db_conn.cursor()
        version_info = _get_or_create_schema_state(cur, database_engine)

        if version_info:
            user_version, delta_files, upgraded = version_info
            _upgrade_existing_database(
                cur, user_version, delta_files, upgraded, database_engine
            )
        else:
            _setup_new_database(cur, database_engine)

        # cur.execute("PRAGMA user_version = %d" % (SCHEMA_VERSION,))

        cur.close()
        db_conn.commit()
    except:
        db_conn.rollback()
        raise


def _setup_new_database(cur, database_engine):
    """Sets up the database by finding a base set of "full schemas" and then
    applying any necessary deltas.

    The "full_schemas" directory has subdirectories named after versions. This
    function searches for the highest version less than or equal to
    `SCHEMA_VERSION` and executes all .sql files in that directory.

    The function will then apply all deltas for all versions after the base
    version.

    Example directory structure:

        schema/
            delta/
                ...
            full_schemas/
                3/
                    test.sql
                    ...
                11/
                    foo.sql
                    bar.sql
                ...

    In the example foo.sql and bar.sql would be run, and then any delta files
    for versions strictly greater than 11.
    """
    current_dir = os.path.join(dir_path, "schema", "full_schemas")
    directory_entries = os.listdir(current_dir)

    valid_dirs = []
    pattern = re.compile(r"^\d+(\.sql)?$")
    for filename in directory_entries:
        match = pattern.match(filename)
        abs_path = os.path.join(current_dir, filename)
        if match and os.path.isdir(abs_path):
            ver = int(match.group(0))
            if ver <= SCHEMA_VERSION:
                valid_dirs.append((ver, abs_path))
        else:
            logger.warn("Unexpected entry in 'full_schemas': %s", filename)

    if not valid_dirs:
        raise PrepareDatabaseException(
            "Could not find a suitable base set of full schemas"
        )

    max_current_ver, sql_dir = max(valid_dirs, key=lambda x: x[0])

    logger.debug("Initialising schema v%d", max_current_ver)

    directory_entries = os.listdir(sql_dir)

    for filename in fnmatch.filter(directory_entries, "*.sql"):
        sql_loc = os.path.join(sql_dir, filename)
        logger.debug("Applying schema %s", sql_loc)
        executescript(cur, sql_loc)

    cur.execute(
        database_engine.convert_param_style(
            "INSERT INTO schema_version (version, upgraded)"
            " VALUES (?,?)"
        ),
        (max_current_ver, False,)
    )

    _upgrade_existing_database(
        cur,
        current_version=max_current_ver,
        applied_delta_files=[],
        upgraded=False,
        database_engine=database_engine,
    )


def _upgrade_existing_database(cur, current_version, applied_delta_files,
                               upgraded, database_engine):
    """Upgrades an existing database.

    Delta files can either be SQL stored in *.sql files, or python modules
    in *.py.

    There can be multiple delta files per version. Synapse will keep track of
    which delta files have been applied, and will apply any that haven't been
    even if there has been no version bump. This is useful for development
    where orthogonal schema changes may happen on separate branches.

    Different delta files for the same version *must* be orthogonal and give
    the same result when applied in any order. No guarantees are made on the
    order of execution of these scripts.

    This is a no-op of current_version == SCHEMA_VERSION.

    Example directory structure:

        schema/
            delta/
                11/
                    foo.sql
                    ...
                12/
                    foo.sql
                    bar.py
                ...
            full_schemas/
                ...

    In the example, if current_version is 11, then foo.sql will be run if and
    only if `upgraded` is True. Then `foo.sql` and `bar.py` would be run in
    some arbitrary order.

    Args:
        cur (Cursor)
        current_version (int): The current version of the schema.
        applied_delta_files (list): A list of deltas that have already been
            applied.
        upgraded (bool): Whether the current version was generated by having
            applied deltas or from full schema file. If `True` the function
            will never apply delta files for the given `current_version`, since
            the current_version wasn't generated by applying those delta files.
    """

    if current_version > SCHEMA_VERSION:
        raise ValueError(
            "Cannot use this database as it is too " +
            "new for the server to understand"
        )

    start_ver = current_version
    if not upgraded:
        start_ver += 1

    logger.debug("applied_delta_files: %s", applied_delta_files)

    for v in range(start_ver, SCHEMA_VERSION + 1):
        logger.debug("Upgrading schema to v%d", v)

        delta_dir = os.path.join(dir_path, "schema", "delta", str(v))

        try:
            directory_entries = os.listdir(delta_dir)
        except OSError:
            logger.exception("Could not open delta dir for version %d", v)
            raise UpgradeDatabaseException(
                "Could not open delta dir for version %d" % (v,)
            )

        directory_entries.sort()
        for file_name in directory_entries:
            relative_path = os.path.join(str(v), file_name)
            logger.debug("Found file: %s", relative_path)
            if relative_path in applied_delta_files:
                continue

            absolute_path = os.path.join(
                dir_path, "schema", "delta", relative_path,
            )
            root_name, ext = os.path.splitext(file_name)
            if ext == ".py":
                # This is a python upgrade module. We need to import into some
                # package and then execute its `run_upgrade` function.
                module_name = "synapse.storage.v%d_%s" % (
                    v, root_name
                )
                with open(absolute_path) as python_file:
                    module = imp.load_source(
                        module_name, absolute_path, python_file
                    )
                logger.debug("Running script %s", relative_path)
                module.run_upgrade(cur, database_engine)
            elif ext == ".sql":
                # A plain old .sql file, just read and execute it
                logger.debug("Applying schema %s", relative_path)
                executescript(cur, absolute_path)
            else:
                # Not a valid delta file.
                logger.warn(
                    "Found directory entry that did not end in .py or"
                    " .sql: %s",
                    relative_path,
                )
                continue

            # Mark as done.
            cur.execute(
                database_engine.convert_param_style(
                    "INSERT INTO applied_schema_deltas (version, file)"
                    " VALUES (?,?)",
                ),
                (v, relative_path)
            )

            cur.execute("DELETE FROM schema_version")
            cur.execute(
                database_engine.convert_param_style(
                    "INSERT INTO schema_version (version, upgraded)"
                    " VALUES (?,?)",
                ),
                (v, True)
            )


def get_statements(f):
    statement_buffer = ""
    in_comment = False  # If we're in a /* ... */ style comment

    for line in f:
        line = line.strip()

        if in_comment:
            # Check if this line contains an end to the comment
            comments = line.split("*/", 1)
            if len(comments) == 1:
                continue
            line = comments[1]
            in_comment = False

        # Remove inline block comments
        line = re.sub(r"/\*.*\*/", " ", line)

        # Does this line start a comment?
        comments = line.split("/*", 1)
        if len(comments) > 1:
            line = comments[0]
            in_comment = True

        # Deal with line comments
        line = line.split("--", 1)[0]
        line = line.split("//", 1)[0]

        # Find *all* semicolons. We need to treat first and last entry
        # specially.
        statements = line.split(";")

        # We must prepend statement_buffer to the first statement
        first_statement = "%s %s" % (
            statement_buffer.strip(),
            statements[0].strip()
        )
        statements[0] = first_statement

        # Every entry, except the last, is a full statement
        for statement in statements[:-1]:
            yield statement.strip()

        # The last entry did *not* end in a semicolon, so we store it for the
        # next semicolon we find
        statement_buffer = statements[-1].strip()


def executescript(txn, schema_path):
    with open(schema_path, 'r') as f:
        for statement in get_statements(f):
            txn.execute(statement)


def _get_or_create_schema_state(txn, database_engine):
    # Bluntly try creating the schema_version tables.
    schema_path = os.path.join(
        dir_path, "schema", "schema_version.sql",
    )
    executescript(txn, schema_path)

    txn.execute("SELECT version, upgraded FROM schema_version")
    row = txn.fetchone()
    current_version = int(row[0]) if row else None
    upgraded = bool(row[1]) if row else None

    if current_version:
        txn.execute(
            database_engine.convert_param_style(
                "SELECT file FROM applied_schema_deltas WHERE version >= ?"
            ),
            (current_version,)
        )
        applied_deltas = [d for d, in txn.fetchall()]
        return current_version, applied_deltas, upgraded

    return None


def prepare_sqlite3_database(db_conn):
    """This function should be called before `prepare_database` on sqlite3
    databases.

    Since we changed the way we store the current schema version and handle
    updates to schemas, we need a way to upgrade from the old method to the
    new. This only affects sqlite databases since they were the only ones
    supported at the time.
    """
    with db_conn:
        schema_path = os.path.join(
            dir_path, "schema", "schema_version.sql",
        )
        create_schema = read_schema(schema_path)
        db_conn.executescript(create_schema)

        c = db_conn.execute("SELECT * FROM schema_version")
        rows = c.fetchall()
        c.close()

        if not rows:
            c = db_conn.execute("PRAGMA user_version")
            row = c.fetchone()
            c.close()

            if row and row[0]:
                db_conn.execute(
                    "REPLACE INTO schema_version (version, upgraded)"
                    " VALUES (?,?)",
                    (row[0], False)
                )


def are_all_users_on_domain(txn, database_engine, domain):
    sql = database_engine.convert_param_style(
        "SELECT COUNT(*) FROM users WHERE name NOT LIKE ?"
    )
    pat = "%:" + domain
    txn.execute(sql, (pat,))
    num_not_matching = txn.fetchall()[0][0]
    if num_not_matching == 0:
        return True
    return False
