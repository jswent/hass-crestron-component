"""Platform for Crestron Switch integration."""

import voluptuous as vol
import logging
from asyncio import sleep

import homeassistant.helpers.config_validation as cv
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON, STATE_OFF, CONF_NAME, CONF_DEVICE_CLASS
from .const import HUB, DOMAIN, CONF_SWITCH_JOIN, CONF_PULSED

_LOGGER = logging.getLogger(__name__)

PLATFORM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Optional(CONF_DEVICE_CLASS): cv.string,
        vol.Required(CONF_SWITCH_JOIN): cv.positive_int,
        vol.Required(CONF_PULSED): cv.boolean,
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    if not config or len(config) <= 1:
        return

    hub = hass.data[DOMAIN][HUB]
    entity = [CrestronSwitch(hub, config)]
    async_add_entities(entity)


class CrestronSwitch(SwitchEntity):
    def __init__(self, hub, config):
        self._hub = hub
        self._name = config.get(CONF_NAME)
        self._switch_join = config.get(CONF_SWITCH_JOIN)
        self._device_class = config.get(CONF_DEVICE_CLASS, "switch")
        self._pulsed = config.get(CONF_PULSED)

    async def async_added_to_hass(self):
        self._hub.register_callback(self.process_callback)

    async def async_will_remove_from_hass(self):
        self._hub.remove_callback(self.process_callback)

    async def process_callback(self, cbtype, value):
        self.async_write_ha_state()

    @property
    def available(self):
        return self._hub.is_available()

    @property
    def name(self):
        return self._name

    @property
    def should_poll(self):
        return False

    @property
    def device_class(self):
        return self._device_class

    @property
    def state(self):
        if self._hub.get_digital(self._switch_join):
            return STATE_ON
        else:
            return STATE_OFF

    @property
    def is_on(self):
        return self._hub.get_digital(self._switch_join)

    async def async_turn_on(self, **kwargs):
        if self._pulsed:
            self._hub.set_digital(self._switch_join, True)
            await sleep(0.05)
            self._hub.set_digital(self._switch_join, False)
        else:
            self._hub.set_digital(self._switch_join, True)

    async def async_turn_off(self, **kwargs):
        if self._pulsed:
            self._hub.set_digital(self._switch_join, True)
            await sleep(0.05)
            self._hub.set_digital(self._switch_join, False)
        else:
            self._hub.set_digital(self._switch_join, False)
