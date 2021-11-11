from mock import AsyncMock, Mock

from twisted.internet.defer import Deferred, ensureDeferred

from synapse.storage.background_updates import (
    BackgroundUpdateController,
    BackgroundUpdater,
)

from tests import unittest


class BackgroundUpdateTestCase(unittest.HomeserverTestCase):
    def prepare(self, reactor, clock, homeserver):
        self.updates: BackgroundUpdater = self.hs.get_datastore().db_pool.updates
        # the base test class should have run the real bg updates for us
        self.assertTrue(
            self.get_success(self.updates.has_completed_background_updates())
        )

        self.update_handler = Mock()
        self.updates.register_background_update_handler(
            "test_update", self.update_handler
        )

    def test_do_background_update(self):
        # the time we claim each update takes
        duration_ms = 0.2

        # the target runtime for each bg update
        target_background_update_duration_ms = 100

        store = self.hs.get_datastore()
        self.get_success(
            store.db_pool.simple_insert(
                "background_updates",
                values={"update_name": "test_update", "progress_json": '{"my_key": 1}'},
            )
        )

        # first step: make a bit of progress
        async def update(progress, count):
            await self.clock.sleep((count * duration_ms) / 1000)
            progress = {"my_key": progress["my_key"] + 1}
            await store.db_pool.runInteraction(
                "update_progress",
                self.updates._background_update_progress_txn,
                "test_update",
                progress,
            )
            return count

        self.update_handler.side_effect = update
        self.update_handler.reset_mock()
        res = self.get_success(
            self.updates.do_next_background_update(False),
            by=0.01,
        )
        self.assertFalse(res)

        # on the first call, we should get run with the default background update size
        self.update_handler.assert_called_once_with({"my_key": 1}, 100)

        # second step: complete the update
        # we should now get run with a much bigger number of items to update
        async def update(progress, count):
            self.assertEqual(progress, {"my_key": 2})
            self.assertAlmostEqual(
                count,
                target_background_update_duration_ms / duration_ms,
                places=0,
            )
            await self.updates._end_background_update("test_update")
            return count

        self.update_handler.side_effect = update
        self.update_handler.reset_mock()
        result = self.get_success(self.updates.do_next_background_update(False))
        self.assertFalse(result)
        self.update_handler.assert_called_once()

        # third step: we don't expect to be called any more
        self.update_handler.reset_mock()
        result = self.get_success(self.updates.do_next_background_update(False))
        self.assertTrue(result)
        self.assertFalse(self.update_handler.called)


class BackgroundUpdateControllerTestCase(unittest.HomeserverTestCase):
    def prepare(self, reactor, clock, homeserver):
        self.updates: BackgroundUpdater = self.hs.get_datastore().db_pool.updates
        # the base test class should have run the real bg updates for us
        self.assertTrue(
            self.get_success(self.updates.has_completed_background_updates())
        )

        self.update_deferred = Deferred()
        self.update_handler = Mock(return_value=self.update_deferred)
        self.updates.register_background_update_handler(
            "test_update", self.update_handler
        )

        self._controller_ctx_mgr = AsyncMock(name="_controller_ctx_mgr")
        self._controller = AsyncMock(BackgroundUpdateController)
        self._controller.run_update.return_value = self._controller_ctx_mgr

        self.updates.register_update_controller(self._controller)

    def test_controller(self):
        store = self.hs.get_datastore()
        self.get_success(
            store.db_pool.simple_insert(
                "background_updates",
                values={"update_name": "test_update", "progress_json": "{}"},
            )
        )

        default_batch_size = 100

        # Set up the return values of the controller.
        enter_defer = Deferred()
        self._controller_ctx_mgr.__aenter__ = Mock(return_value=enter_defer)
        self._controller.default_batch_size.return_value = default_batch_size
        self._controller.min_batch_size.return_value = default_batch_size

        # Start the background update.
        do_update_d = ensureDeferred(self.updates.do_next_background_update(True))

        self.pump()

        # `run_update` should have been called, but the update handler won't be
        # called until the `enter_defer` (returned by `__aenter__`) is resolved.
        self._controller.run_update.assert_called_once_with(
            update_name="test_update",
            database_name="master",
            oneshot=False,
        )
        self.assertFalse(do_update_d.called)
        self.assertFalse(self.update_deferred.called)

        # Resolving the `enter_defer` should call the update handler, which then
        # blocks.
        enter_defer.callback(100)
        self.pump()
        self.update_handler.assert_called_once_with({}, default_batch_size)
        self.assertFalse(self.update_deferred.called)
        self._controller_ctx_mgr.__aexit__.assert_not_awaited()

        # Resolving the update handler deferred should cause the
        # `do_next_background_update` to finish and return
        self.update_deferred.callback(100)
        self.pump()
        self._controller_ctx_mgr.__aexit__.assert_awaited()
        self.get_success(do_update_d)
