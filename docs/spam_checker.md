# Handling spam in Synapse

Synapse has support to customize spam checking behavior. It can plug into a
variety of events and affect how they are presented to users on your homeserver.

The spam checking behavior is implemented as a Python class, which must be
able to be imported by the running Synapse.

## Python spam checker class

The Python class is instantiated with two objects:

* Any configuration (see below).
* An instance of `synapse.spam_checker_api.SpamCheckerApi`.

It then implements methods which return a boolean to alter behavior in Synapse.

There's a generic method for checking every event (`check_event_for_spam`), as
well as some specific methods:

* `user_may_invite`
* `user_may_create_room`
* `user_may_create_room_alias`
* `user_may_publish_room`

The details of the each of these methods (as well as their inputs and outputs)
are documented in the `synapse.events.spamcheck.SpamChecker` class.

The `SpamCheckerApi` class provides a way for the custom spam checker class to
call back into the homeserver internals. It currently implements the following
methods:

* `get_state_events_in_room`

### Example

```python
class ExampleSpamChecker:
    def __init__(self, config, api):
        self.config = config
        self.api = api

    def check_event_for_spam(self, foo):
        return False  # allow all events

    def user_may_invite(self, inviter_userid, invitee_userid, room_id):
        return True  # allow all invites

    def user_may_create_room(self, userid):
        return True  # allow all room creations

    def user_may_create_room_alias(self, userid, room_alias):
        return True  # allow all room aliases

    def user_may_publish_room(self, userid, room_id):
        return True  # allow publishing of all rooms

    def check_username_for_spam(self, user_profile):
        return False  # allow all usernames
```

## Configuration

Modify the `spam_checker` section of your `homeserver.yaml` in the following
manner:

`module` should point to the fully qualified Python class that implements your
custom logic, e.g. `my_module.ExampleSpamChecker`.

`config` is a dictionary that gets passed to the spam checker class.

### Example

This section might look like:

```yaml
spam_checker:
  module: my_module.ExampleSpamChecker
  config:
    # Enable or disable a specific option in ExampleSpamChecker.
    my_custom_option: true
```

## Examples

The [Mjolnir](https://github.com/matrix-org/mjolnir) project is a full fledged
example using the Synapse spam checking API, including a bot for dynamic
configuration.
