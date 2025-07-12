"""Platform for Crestron Media Player integration."""

import logging
from asyncio import sleep
from functools import cached_property

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
)
from homeassistant.const import CONF_NAME, STATE_OFF, STATE_ON
from homeassistant.util import slugify

from custom_components.crestron.crestron import CrestronXsig

from .const import (
    CONF_SOURCE_DEFAULT,
    CONF_MUTE_JOIN,
    CONF_POWER_OFF_JOIN,
    CONF_POWER_ON_JOIN,
    CONF_SOURCE_NUM_JOIN,
    CONF_SOURCES,
    CONF_VOLUME_JOIN,
    DOMAIN,
    HUB,
)

_LOGGER = logging.getLogger(__name__)

SOURCES_SCHEMA = vol.Schema(
    {
        cv.positive_int: cv.string,
    }
)

PLATFORM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_POWER_ON_JOIN): cv.positive_int,
        vol.Required(CONF_POWER_OFF_JOIN): cv.positive_int,
        vol.Required(CONF_MUTE_JOIN): cv.positive_int,
        vol.Required(CONF_SOURCE_NUM_JOIN): cv.positive_int,
        vol.Required(CONF_VOLUME_JOIN): cv.positive_int,
        vol.Required(CONF_SOURCES): SOURCES_SCHEMA,
        vol.Optional(CONF_SOURCE_DEFAULT): cv.positive_int,
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    if not config or len(config) <= 1:
        return

    hub = hass.data[DOMAIN][HUB]
    entity = [CrestronRoom(hub, config)]
    async_add_entities(entity)


class CrestronRoom(MediaPlayerEntity):
    _attr_should_poll = False
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = (
        MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.TURN_ON
    )

    def __init__(self, hub: CrestronXsig, config):
        self._hub = hub
        self._name = config.get(CONF_NAME)
        self._power_on_join = config.get(CONF_POWER_ON_JOIN)
        self._power_off_join = config.get(CONF_POWER_OFF_JOIN)
        self._mute_join = config.get(CONF_MUTE_JOIN)
        self._volume_join = config.get(CONF_VOLUME_JOIN)
        self._source_number_join = config.get(CONF_SOURCE_NUM_JOIN)
        self._sources = config.get(CONF_SOURCES)
        self._default_source = self._get_default_source_safe(
            config.get(CONF_SOURCE_DEFAULT)
        )

    def _get_default_source_safe(self, cfg_src):
        if cfg_src is not None:
            max_idx = len(self._sources)
            if 1 <= cfg_src <= max_idx:
                self._default_source = cfg_src
            else:
                _LOGGER.error(
                    "%s: invalid default_source %s, must be between 1 and %s",
                    self.entity_id,
                    cfg_src,
                    max_idx,
                )

    async def async_added_to_hass(self):
        self._hub.register_callback(self.process_callback)

    async def async_will_remove_from_hass(self):
        self._hub.remove_callback(self.process_callback)

    async def process_callback(self, cbtype, value):
        self.async_write_ha_state()

    @cached_property
    def name(self):
        return self._name

    @cached_property
    def unique_id(self):
        return slugify(self._name)

    @property
    def available(self):  # type: ignore
        return self._hub.is_available()

    @cached_property
    def source_list(self):
        return list(self._sources.values())

    @property
    def source(self):  # type: ignore
        source_num = self._hub.get_analog(self._source_number_join)
        if source_num == 0:
            return None
        else:
            return self._sources[source_num]

    @property
    def state(self):  # type: ignore
        if self._hub.get_digital(self._power_on_join):
            return STATE_ON
        else:
            return STATE_OFF

    @property
    def is_volume_muted(self):  # type: ignore
        return self._hub.get_digital(self._mute_join)

    @property
    def volume_level(self):  # type: ignore
        return self._hub.get_analog(self._volume_join) / 65535

    async def async_mute_volume(self, mute):
        self._hub.set_digital(self._mute_join, True)
        await sleep(0.05)
        self._hub.set_digital(self._mute_join, False)

    async def async_set_volume_level(self, volume):
        self._hub.set_analog(self._volume_join, int(volume * 65535))

    async def async_select_source(self, source):
        for input_num, name in self._sources.items():
            if name == source:
                self._hub.set_analog(self._source_number_join, input_num)

    async def async_turn_off(self):
        self._hub.set_digital(self._power_off_join, True)
        await sleep(0.05)
        self._hub.set_digital(self._power_off_join, False)
        await sleep(0.05)
        self._hub.set_analog(self._source_number_join, 0)

    async def async_turn_on(self):
        self._hub.set_digital(self._power_on_join, True)
        await sleep(0.05)
        self._hub.set_digital(self._power_on_join, False)

        if self._default_source is not None:
            self._hub.set_analog(self._source_number_join, self._default_source)
