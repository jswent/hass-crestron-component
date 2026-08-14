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
    CONF_DEFAULT_SOURCE,
    CONF_MUTE_JOIN,
    CONF_POWER_OFF_JOIN,
    CONF_POWER_ON_JOIN,
    CONF_SOURCE_DEFAULT,
    CONF_SOURCE_DIGITAL_JOINS,
    CONF_SOURCE_NUM_JOIN,
    CONF_SOURCES,
    CONF_VOLUME_JOIN,
    DOMAIN,
    HUB,
)

_LOGGER = logging.getLogger(__name__)

PULSE_SECONDS = 0.05

SOURCES_SCHEMA = vol.All(
    vol.Schema(
        {
            cv.positive_int: cv.string,
        }
    ),
    vol.Length(min=1),
)


def _validate_platform_config(config):
    """Validate and cross-check the configured source selection method."""
    has_source_number_join = CONF_SOURCE_NUM_JOIN in config
    has_sources = CONF_SOURCES in config
    has_digital_sources = CONF_SOURCE_DIGITAL_JOINS in config

    if has_digital_sources and (has_source_number_join or has_sources):
        raise vol.Invalid(
            f"Configure either {CONF_SOURCE_NUM_JOIN} and {CONF_SOURCES}, or "
            f"{CONF_SOURCE_DIGITAL_JOINS}, not both"
        )

    if not has_digital_sources and not (has_source_number_join and has_sources):
        raise vol.Invalid(
            f"Configure either both {CONF_SOURCE_NUM_JOIN} and {CONF_SOURCES}, "
            f"or {CONF_SOURCE_DIGITAL_JOINS}"
        )

    source_map = (
        config[CONF_SOURCE_DIGITAL_JOINS]
        if has_digital_sources
        else config[CONF_SOURCES]
    )
    source_names = list(source_map.values())
    if len(source_names) != len(set(source_names)):
        raise vol.Invalid("Source names must be unique")

    if CONF_DEFAULT_SOURCE in config and CONF_SOURCE_DEFAULT in config:
        raise vol.Invalid(
            f"Configure only {CONF_DEFAULT_SOURCE}; {CONF_SOURCE_DEFAULT} is a "
            "deprecated alias"
        )
    if CONF_SOURCE_DEFAULT in config:
        _LOGGER.warning(
            "%s is deprecated; use %s instead",
            CONF_SOURCE_DEFAULT,
            CONF_DEFAULT_SOURCE,
        )

    default_source = config.get(
        CONF_DEFAULT_SOURCE, config.get(CONF_SOURCE_DEFAULT)
    )
    if default_source is not None and default_source not in source_map:
        raise vol.Invalid(
            f"{CONF_DEFAULT_SOURCE} must be one of the configured source keys"
        )

    return config


PLATFORM_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(CONF_NAME): cv.string,
            vol.Required(CONF_POWER_ON_JOIN): cv.positive_int,
            vol.Required(CONF_POWER_OFF_JOIN): cv.positive_int,
            vol.Required(CONF_MUTE_JOIN): cv.positive_int,
            vol.Required(CONF_VOLUME_JOIN): cv.positive_int,
            vol.Optional(CONF_SOURCE_NUM_JOIN): cv.positive_int,
            vol.Optional(CONF_SOURCES): SOURCES_SCHEMA,
            vol.Optional(CONF_SOURCE_DIGITAL_JOINS): SOURCES_SCHEMA,
            vol.Optional(CONF_DEFAULT_SOURCE): cv.positive_int,
            vol.Optional(CONF_SOURCE_DEFAULT): cv.positive_int,
        },
        extra=vol.ALLOW_EXTRA,
    ),
    _validate_platform_config,
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
        self._source_digital_joins = config.get(CONF_SOURCE_DIGITAL_JOINS, {})
        self._sources = config.get(CONF_SOURCES, self._source_digital_joins)
        self._source_selector_by_name = {
            name: selector for selector, name in self._sources.items()
        }
        self._default_source = config.get(
            CONF_DEFAULT_SOURCE, config.get(CONF_SOURCE_DEFAULT)
        )
        self._active_source_conflict = None

    async def async_added_to_hass(self):
        self._hub.register_callback(self.process_callback)

    async def async_will_remove_from_hass(self):
        self._hub.remove_callback(self.process_callback)

    async def process_callback(self, cbtype, value):
        self.async_write_ha_state()

    async def _async_pulse_digital(self, join):
        """Pulse a digital join, ensuring it is released if cancelled."""
        self._hub.set_digital(join, True)
        try:
            await sleep(PULSE_SECONDS)
        finally:
            self._hub.set_digital(join, False)

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
        if self._source_number_join is not None:
            source_num = self._hub.get_analog(self._source_number_join)
            return self._sources.get(source_num)

        active_sources = [
            (join, name)
            for join, name in self._source_digital_joins.items()
            if self._hub.get_digital(join)
        ]
        if not active_sources:
            self._active_source_conflict = None
            return None

        conflict = tuple(join for join, _ in active_sources)
        if len(active_sources) > 1 and conflict != self._active_source_conflict:
            _LOGGER.warning(
                "%s has multiple active source joins %s; using %s",
                self._name,
                list(conflict),
                active_sources[0][1],
            )
            self._active_source_conflict = conflict
        elif len(active_sources) == 1:
            self._active_source_conflict = None

        return active_sources[0][1]

    @property
    def state(self):  # type: ignore
        if self._hub.get_digital(self._power_on_join):
            return STATE_ON
        return STATE_OFF

    @property
    def is_volume_muted(self):  # type: ignore
        return self._hub.get_digital(self._mute_join)

    @property
    def volume_level(self):  # type: ignore
        return self._hub.get_analog(self._volume_join) / 65535

    async def async_mute_volume(self, mute):
        await self._async_pulse_digital(self._mute_join)

    async def async_set_volume_level(self, volume):
        self._hub.set_analog(self._volume_join, int(volume * 65535))

    async def async_select_source(self, source):
        selector = self._source_selector_by_name.get(source)
        if selector is None:
            _LOGGER.warning("%s: unknown source %s", self._name, source)
            return

        if self._source_number_join is not None:
            self._hub.set_analog(self._source_number_join, selector)
        else:
            await self._async_pulse_digital(selector)

    async def async_turn_off(self):
        await self._async_pulse_digital(self._power_off_join)

        if self._source_number_join is not None:
            await sleep(PULSE_SECONDS)
            self._hub.set_analog(self._source_number_join, 0)

    async def async_turn_on(self):
        await self._async_pulse_digital(self._power_on_join)

        if self._default_source is not None:
            await self.async_select_source(self._sources[self._default_source])
