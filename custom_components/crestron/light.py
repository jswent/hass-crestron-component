"""Platform for Crestron Light integration."""

import voluptuous as vol
import logging

import homeassistant.helpers.config_validation as cv
from homeassistant.components.light import LightEntity, ColorMode
from homeassistant.const import CONF_NAME, CONF_TYPE
from .const import HUB, DOMAIN, CONF_BRIGHTNESS_JOIN

_LOGGER = logging.getLogger(__name__)

PLATFORM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_TYPE): cv.string,
        vol.Required(CONF_BRIGHTNESS_JOIN): cv.positive_int,
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    if not config or len(config) <= 1:
        return

    hub = hass.data[DOMAIN][HUB]
    entity = [CrestronLight(hub, config)]
    async_add_entities(entity)


class CrestronLight(LightEntity):
    _attr_should_poll = False
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(self, hub, config):
        self._hub = hub
        self._name = config.get(CONF_NAME)
        self._brightness_join = config.get(CONF_BRIGHTNESS_JOIN)
        self._attr_name = self._name
        self._attr_is_on = False
        self._attr_brightness = 0

    async def async_added_to_hass(self):
        self._hub.register_callback(self.process_callback)

    async def async_will_remove_from_hass(self):
        self._hub.remove_callback(self.process_callback)

    async def process_callback(self, cbtype, value):
        analog_value = self._hub.get_analog(self._brightness_join)
        self._attr_brightness = int(analog_value / 257)
        self._attr_is_on = self._attr_brightness > 0
        self.async_write_ha_state()

    @property
    def available(self):
        return self._hub.is_available()

    async def async_turn_on(self, **kwargs):
        if (brightness := kwargs.get("brightness")) is not None:
            analog_value = int(brightness * 257)
        else:
            analog_value = 65535
        self._hub.set_analog(self._brightness_join, analog_value)
        self._attr_brightness = int(analog_value / 257)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        self._hub.set_analog(self._brightness_join, 0)
        self._attr_brightness = 0
        self._attr_is_on = False
        self.async_write_ha_state()

