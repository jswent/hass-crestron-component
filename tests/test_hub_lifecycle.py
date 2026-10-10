"""Hub shutdown tests with isolated Home Assistant import dependencies."""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch


def load_component():
    """Load the actual integration without needing a Home Assistant install.

    These tests exercise initialization and shutdown, not configuration
    validation or Home Assistant's template engine. Only those external
    imports are stubbed, and sys.modules is restored after loading.
    """
    modules = {}

    def module(name, **attributes):
        result = ModuleType(name)
        result.__dict__.update(attributes)
        modules[name] = result
        return result

    module("homeassistant", __path__=[])
    module("homeassistant.helpers", __path__=[])
    module("homeassistant.config_entries", ConfigType=dict)
    module(
        "homeassistant.helpers.config_validation",
        string=str,
        entity_id=str,
        template=lambda value: value,
        SCRIPT_SCHEMA=dict,
        port=int,
        ensure_list=lambda value: value,
        positive_int=int,
        boolean=bool,
    )
    module("homeassistant.helpers.discovery", async_load_platform=AsyncMock())
    module(
        "homeassistant.helpers.event",
        TrackTemplate=Mock(),
        async_track_template_result=Mock(),
    )
    module("homeassistant.helpers.template", Template=Mock())
    module("homeassistant.helpers.script", Script=Mock())
    module(
        "homeassistant.core",
        HomeAssistant=object,
        callback=lambda function: function,
        Context=Mock(),
    )
    module(
        "homeassistant.const",
        EVENT_HOMEASSISTANT_STOP="homeassistant_stop",
        CONF_VALUE_TEMPLATE="value_template",
        CONF_ATTRIBUTE="attribute",
        CONF_ENTITY_ID="entity_id",
        STATE_ON="on",
        STATE_OFF="off",
        CONF_SERVICE="service",
        CONF_SERVICE_DATA="service_data",
    )
    module(
        "voluptuous",
        Schema=lambda schema, **kwargs: schema,
        Required=lambda key: key,
        Optional=lambda key: key,
        All=lambda *validators: validators,
        ALLOW_EXTRA=object(),
    )

    path = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "crestron"
        / "__init__.py"
    )
    name = "_crestron_hub_lifecycle_test"
    spec = importlib.util.spec_from_file_location(name, path)
    component = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {**modules, name: component}):
        spec.loader.exec_module(component)
    return component


class HubLifecycleTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.component = load_component()
        self.hass = SimpleNamespace(
            data={self.component.DOMAIN: {}},
            services=SimpleNamespace(async_register=Mock()),
        )

    def make_hub(self, **options):
        hub = self.component.CrestronHub(
            self.hass, {self.component.CONF_PORT: 12345, **options}
        )
        hub.hub.stop = AsyncMock()
        return hub

    async def test_stop_without_optional_join_configuration(self):
        hub = self.make_hub()

        await hub.stop(None)

        self.assertIsNone(hub.tracker)
        hub.hub.stop.assert_awaited_once_with()

    async def test_repeated_stop_is_safe_with_tracker_and_callback(self):
        tracker = Mock()
        self.component.async_track_template_result.return_value = tracker
        hub = self.make_hub(
            **{
                self.component.CONF_TO_HUB: [],
                self.component.CONF_FROM_HUB: [],
            }
        )
        self.assertIn(hub.join_change_callback, hub.hub._callbacks)

        await hub.stop(None)
        await hub.stop(None)

        tracker.async_remove.assert_called_once_with()
        self.assertIsNone(hub.tracker)
        self.assertNotIn(hub.join_change_callback, hub.hub._callbacks)
        self.assertEqual(hub.hub.stop.await_count, 2)

    async def test_tracker_failure_still_stops_transport(self):
        tracker = Mock()
        tracker.async_remove.side_effect = RuntimeError("tracker cleanup failed")
        self.component.async_track_template_result.return_value = tracker
        hub = self.make_hub(**{self.component.CONF_TO_HUB: []})

        with self.assertRaisesRegex(RuntimeError, "tracker cleanup failed"):
            await hub.stop(None)

        hub.hub.stop.assert_awaited_once_with()
        self.assertIsNone(hub.tracker)

        await hub.stop(None)

        tracker.async_remove.assert_called_once_with()
        self.assertEqual(hub.hub.stop.await_count, 2)
