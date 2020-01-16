# Message retention policies

Synapse admins can enable support for message retention policies on
their homeserver. Message retention policies exist at a room level,
follow the semantics described in
[MSC1763](https://github.com/matrix-org/matrix-doc/blob/matthew/msc1763/proposals/1763-configurable-retention-periods.md),
and allow server and room admins to configure how long messages should
be kept in a homeserver's database before being purged from it.
**Please note that, as this feature isn't part of the Matrix
specification yet, this implementation is to be considered as
experimental.** 

A message retention policy is mainly defined by its `max_lifetime`
parameter, which defines how long a message can be kept around after
it was sent to the room. If a room doesn't have a message retention
policy, and there's no default one for a given server, then no message
sent in that room is ever purged on that server.

MSC1763 also specifies semantics for a `min_lifetime` parameter which
defines the amount of time after which an event _can_ get purged (after
it was sent to the room), but Synapse doesn't currently support it
beyond registering it.

Both `max_lifetime` and `min_lifetime` are optional parameters.

Note that message retention policies don't apply to state events.

Once an event reaches its expiry date (defined as the time it was sent
plus the value for `max_lifetime` in the room), two things happen:

* Synapse stops serving the event to clients via any endpoint.
* The message gets picked up by the next purge job (see the "Purge jobs"
  section) and is removed from Synapse's database.

Since purge jobs don't run continuously, this means that an event might
stay in a server's database for longer than the value for `max_lifetime`
in the room would allow, though hidden from clients.

Similarly, if a server (with support for message retention policies
enabled) receives from another server an event that should have been
purged according to its room's policy, then the receiving server will
process and store that event until it's picked up by the next purge job,
though it will always hide it from clients.


## Server configuration

Support for this feature can be enabled and configured in the
`retention` section of the Synapse configuration file (see the
[sample file](https://github.com/matrix-org/synapse/blob/v1.7.3/docs/sample_config.yaml#L332-L393)).

To enable support for message retention policies, set the setting
`enabled` in this section to `true`.


### Default policy

A default message retention policy is a policy defined in Synapse's
configuration that is used by Synapse for every room that doesn't have a
message retention policy configured in its state. This allows server
admins to ensure that messages are never kept indefinitely in a server's
database. 

A default policy can be defined as such, in the `retention` section of
the configuration file:

```yaml
  default_policy:
    min_lifetime: 1d
    max_lifetime: 1y
```

Here, `min_lifetime` and `max_lifetime` have the same meaning and level
of support as previously described. They can be expressed either as a
duration (using the units `s` (seconds), `m` (minutes), `h` (hours),
`d` (days), `w` (weeks) and `y` (years)) or as a number of milliseconds.


### Purge jobs

Purge jobs are the jobs that Synapse runs in the background to purge
expired events from the database. They are only run if support for
message retention policies is enabled in the server's configuration. If
no configuration for purge jobs is configured by the server admin,
Synapse will use a default configuration, which is described in the
[sample configuration file](https://github.com/matrix-org/synapse/blob/master/docs/sample_config.yaml#L332-L393).

Some server admins might want a finer control on when events are removed
depending on an event's room's policy. This can be done by setting the
`purge_jobs` sub-section in the `retention` section of the configuration
file. An example of such configuration could be:

```yaml
  purge_jobs:
    - longest_max_lifetime: 3d
      interval: 12h
    - shortest_max_lifetime: 3d
      longest_max_lifetime: 1w
      interval: 1d
    - shortest_max_lifetime: 1w
      interval: 2d
```

In this example, we define three jobs:

* one that runs twice a day (every 12 hours) and purges events in rooms
  which policy's `max_lifetime` is lower or equal to 3 days.
* one that runs once a day and purges events in rooms which policy's
  `max_lifetime` is between 3 days and a week.
* one that runs once every 2 days and purges events in rooms which
  policy's `max_lifetime` is greater than a week.

Note that this example is tailored to show different configurations and
features slightly more jobs than it's probably necessary (in practice, a
server admin would probably consider it better to replace the two last
jobs with one that runs once a day and handles rooms which which
policy's `max_lifetime` is greater than 3 days).

Keep in mind, when configuring these jobs, that a purge job can become
quite heavy on the server if it targets many rooms, therefore prefer
having jobs with a low interval that target a limited set of rooms. Also
make sure to include a job with no minimum and one with no maximum to
make sure your configuration handles every policy.

As previously mentioned in this documentation, while a purge job that
runs e.g. every day means that an expired event might stay in the
database for up to a day after its expiry, Synapse hides expired events
from clients as soon as they expire, so the event is not visible to
local users between its expiry date and the moment it gets purged from
the server's database.


### Lifetime limits

**Note: this feature is mainly useful within a closed federation or on
servers that don't federate, because there currently is no way to
enforce these limits in an open federation.**

Server admins can restrict the values their local users are allowed to
use for both `min_lifetime` and `max_lifetime`. These limits can be
defined as such in the `retention` section of the configuration file:

```yaml
  allowed_lifetime_min: 1d
  allowed_lifetime_max: 1y
```

Here, `allowed_lifetime_min` is the lowest value a local user can set
for both `min_lifetime` and `max_lifetime`, and `allowed_lifetime_max`
is the highest value. Both parameters are optional (e.g. setting
`allowed_lifetime_min` but not `allowed_lifetime_max` only enforces a
minimum and no maximum).

Like other settings in this section, these parameters can be expressed
either as a duration or as a number of milliseconds.


## Room configuration

To configure a room's message retention policy, a room's admin or
moderator needs to send a state event in that room with the type
`m.room.retention` and the following content:

```json
{
    "max_lifetime": ...
}
```

In this event's content, the `max_lifetime` parameter has the same
meaning as previously described, and needs to be expressed in
milliseconds. The event's content can also include a `min_lifetime`
parameter, which has the same meaning and limited support as previously
described.

Note that over every server in the room, only the ones with support for
message retention policies will actually remove expired events. This
support is currently not enabled by default in Synapse.


## Note on reclaiming disk space

While purge jobs actually delete data from the database, the disk space
used by the database might not decrease immediately on the database's
host. However, even though the database engine won't free up the disk
space, it will start writing new data into where the purged data was.

If you want to reclaim the freed disk space anyway and return it to the
operating system, the server admin needs to run `VACUUM FULL;` (or
`VACUUM;` for SQLite databases) on Synapse's database (see the related
[PostgreSQL documentation](https://www.postgresql.org/docs/current/sql-vacuum.html)).
