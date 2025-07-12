"""The Crestron Integration Component"""

import logging

from homeassistant.config_entries import ConfigType
import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.event import TrackTemplate, async_track_template_result
from homeassistant.helpers.template import Template
from homeassistant.helpers.script import Script
from homeassistant.core import HomeAssistant, callback, Context
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    CONF_VALUE_TEMPLATE,
    CONF_ATTRIBUTE,
    CONF_ENTITY_ID,
    STATE_ON,
    STATE_OFF,
    CONF_SERVICE,
    CONF_SERVICE_DATA,
)

from .crestron import CrestronXsig
from .const import (
    CONF_PORT,
    HUB,
    DOMAIN,
    CONF_JOIN,
    CONF_SCRIPT,
    CONF_TO_HUB,
    CONF_FROM_HUB,
    CONF_VALUE_JOIN,
    CONF_GET_ANALOG,
    CONF_GET_DIGITAL,
    CONF_SET_ANALOG,
    CONF_SET_DIGITAL,
)

_LOGGER = logging.getLogger(__name__)

TO_JOINS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_JOIN): cv.string,
        vol.Optional(CONF_ENTITY_ID): cv.entity_id,
        vol.Optional(CONF_ATTRIBUTE): cv.string,
        vol.Optional(CONF_VALUE_TEMPLATE): cv.template,
    }
)

FROM_JOINS_SCHEMA = vol.Schema(
    {vol.Required(CONF_JOIN): cv.string, vol.Required(CONF_SCRIPT): cv.SCRIPT_SCHEMA}
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_PORT): cv.port,
                vol.Optional(CONF_TO_HUB): vol.All(cv.ensure_list, [TO_JOINS_SCHEMA]),
                vol.Optional(CONF_FROM_HUB): vol.All(
                    cv.ensure_list, [FROM_JOINS_SCHEMA]
                ),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

GET_ANALOG_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_JOIN): cv.positive_int,
    }
)

GET_DIGITAL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_JOIN): cv.positive_int,
    }
)

SET_ANALOG_SCHEME = vol.Schema(
    {
        vol.Required(CONF_JOIN): cv.positive_int,
        vol.Required(CONF_VALUE_JOIN): cv.boolean,
    }
)

SET_DIGITAL_SCHEME = vol.Schema(
    {
        vol.Required(CONF_JOIN): cv.positive_int,
        vol.Required(CONF_VALUE_JOIN): cv.boolean,
    }
)

PLATFORMS = [
    "binary_sensor",
    "sensor",
    "switch",
    "light",
    "climate",
    "cover",
    "media_player",
]


async def async_setup(hass: HomeAssistant, config: ConfigType):
    """Set up a the crestron component."""

    if config.get(DOMAIN) is not None:
        hass.data[DOMAIN] = {}
        hub = CrestronHub(hass, config[DOMAIN])

        await hub.start()
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, hub.stop)

        for platform in PLATFORMS:
            hass.async_create_task(
                async_load_platform(hass, platform, DOMAIN, {}, config)
            )

    return True


class CrestronHub:
    """Wrapper for the CrestronXsig library"""

    def __init__(self, hass, config):
        self.hass = hass
        self.hub = hass.data[DOMAIN][HUB] = CrestronXsig()
        self.port = config.get(CONF_PORT)
        self.context = Context()
        self.to_hub = {}
        self.hub.register_sync_all_joins_callback(self.sync_joins_to_hub)
        if CONF_TO_HUB in config:
            track_templates = []
            for entity in config[CONF_TO_HUB]:
                template_string = None
                if CONF_VALUE_TEMPLATE in entity:
                    template = entity[CONF_VALUE_TEMPLATE]
                    self.to_hub[entity[CONF_JOIN]] = template
                    track_templates.append(
                        TrackTemplate(template, None, rate_limit=0.5)
                    )
                elif CONF_ATTRIBUTE in entity and CONF_ENTITY_ID in entity:
                    template_string = (
                        "{{state_attr('"
                        + entity[CONF_ENTITY_ID]
                        + "','"
                        + entity[CONF_ATTRIBUTE]
                        + "')}}"
                    )
                    template = Template(template_string, hass)
                    self.to_hub[entity[CONF_JOIN]] = template
                    track_templates.append(
                        TrackTemplate(template, None, rate_limit=0.5)
                    )
                elif CONF_ENTITY_ID in entity:
                    template_string = "{{states('" + entity[CONF_ENTITY_ID] + "')}}"
                    template = Template(template_string, hass)
                    self.to_hub[entity[CONF_JOIN]] = template
                    track_templates.append(
                        TrackTemplate(template, None, rate_limit=0.5)
                    )
            self.tracker = async_track_template_result(
                self.hass, track_templates, self.template_change_callback
            )
        if CONF_FROM_HUB in config:
            self.from_hub = config[CONF_FROM_HUB]
            self.hub.register_callback(self.join_change_callback)

        async def async_get_analog(call):
            join = call.data[CONF_JOIN]
            value = self.hub.get_analog(join)
            self.hass.bus.async_fire(
                f"{DOMAIN}_get_analog_response",
                {CONF_JOIN: join, CONF_VALUE_JOIN: value},
            )
            _LOGGER.debug(f"{DOMAIN}.get_analog join {join}: {value}")

        self.hass.services.async_register(
            DOMAIN, CONF_GET_ANALOG, async_get_analog, schema=GET_ANALOG_SCHEMA
        )

        async def async_get_digital(call):
            join = call.data[CONF_JOIN]
            value = self.hub.get_digital(join)
            self.hass.bus.async_fire(
                f"{DOMAIN}_get_digital_response",
                {CONF_JOIN: join, CONF_VALUE_JOIN: value},
            )
            _LOGGER.debug(f"{DOMAIN}.get_digital join {join}: {value}")

        self.hass.services.async_register(
            DOMAIN, CONF_GET_DIGITAL, async_get_digital, schema=GET_DIGITAL_SCHEMA
        )

        async def async_set_analog(event):
            _LOGGER.debug(
                f"async_set_analog setting join {event.data[CONF_JOIN]} to {event.data[CONF_VALUE_JOIN]}"
            )
            self.hub.set_analog(event.data[CONF_JOIN], event.data[CONF_VALUE_JOIN])

        self.hass.services.async_register(
            DOMAIN,
            CONF_SET_ANALOG,
            async_set_analog,
            schema=SET_ANALOG_SCHEME,
        )

        async def async_set_digital(event):
            _LOGGER.debug(
                f"async_set_digital setting join {event.data[CONF_JOIN]} to {event.data[CONF_VALUE_JOIN]}"
            )
            self.hub.set_digital(event.data[CONF_JOIN], event.data[CONF_VALUE_JOIN])

        self.hass.services.async_register(
            DOMAIN,
            CONF_SET_DIGITAL,
            async_set_digital,
            schema=SET_DIGITAL_SCHEME,
        )

    async def start(self):
        await self.hub.listen(self.port)

    async def stop(self, event):
        """remove callback(s) and template trackers"""
        self.hub.remove_callback(self.join_change_callback)
        self.tracker.async_remove()
        await self.hub.stop()

    async def join_change_callback(self, cbtype, value):
        """Call service for tracked join change (from_hub)"""
        for join in self.from_hub:
            if cbtype == join[CONF_JOIN]:
                # For digital joins, ignore on>off transitions  (avoids double calls to service for momentary presses)
                if cbtype[:1] == "d" and value == "0":
                    pass
                else:
                    if CONF_SERVICE in join and CONF_SERVICE_DATA in join:
                        data = dict(join[CONF_SERVICE_DATA])
                        _LOGGER.debug(
                            f"join_change_callback calling service {join[CONF_SERVICE]} with data = {data} from join {cbtype} = {value}"
                        )
                        domain, service = join[CONF_SERVICE].split(".")
                        await self.hass.services.async_call(domain, service, data)
                    elif CONF_SCRIPT in join:
                        sequence = join[CONF_SCRIPT]
                        script = Script(
                            self.hass, sequence, "Crestron Join Change", DOMAIN
                        )
                        await script.async_run({"value": value}, self.context)
                        _LOGGER.debug(
                            f"join_change_callback calling script {join[CONF_SCRIPT]} from join {cbtype} = {value}"
                        )

    @callback
    def template_change_callback(self, event, updates):
        """Set join from value_template (to_hub)"""
        # track_template_result = updates.pop()
        for track_template_result in updates:
            update_result = track_template_result.result
            update_template = track_template_result.template
            if update_result != "None":
                for join, template in self.to_hub.items():
                    if template == update_template:
                        _LOGGER.debug(
                            f"processing template_change_callback for join {join} with result {update_result}"
                        )
                        # Digital Join
                        if join[:1] == "d":
                            value = None
                            if update_result == STATE_ON or update_result == "True":
                                value = True
                            elif update_result == STATE_OFF or update_result == "False":
                                value = False
                            if value is not None:
                                _LOGGER.debug(
                                    f"template_change_callback setting digital join {int(join[1:])} to {value}"
                                )
                                self.hub.set_digital(int(join[1:]), value)
                        # Analog Join
                        if join[:1] == "a":
                            _LOGGER.debug(
                                f"template_change_callback setting analog join {int(join[1:])} to {int(update_result)}"
                            )
                            self.hub.set_analog(int(join[1:]), int(update_result))
                        # Serial Join
                        if join[:1] == "s":
                            _LOGGER.debug(
                                f"template_change_callback setting serial join {int(join[1:])} to {str(update_result)}"
                            )
                            self.hub.set_serial(int(join[1:]), str(update_result))

    async def sync_joins_to_hub(self):
        _LOGGER.debug("Syncing joins to control system")
        for join, template in self.to_hub.items():
            result = template.async_render()
            # Digital Join
            if join[:1] == "d":
                value = None
                if result == STATE_ON or result == "True":
                    value = True
                elif result == STATE_OFF or result == "False":
                    value = False
                if value is not None:
                    _LOGGER.debug(
                        f"sync_joins_to_hub setting digital join {int(join[1:])} to {value}"
                    )
                    self.hub.set_digital(int(join[1:]), value)
            # Analog Join
            if join[:1] == "a":
                if result != "None":
                    _LOGGER.debug(
                        f"sync_joins_to_hub setting analog join {int(join[1:])} to {int(result)}"
                    )
                    self.hub.set_analog(int(join[1:]), int(result))
            # Serial Join
            if join[:1] == "s":
                if result != "None":
                    _LOGGER.debug(
                        f"sync_joins_to_hub setting serial join {int(join[1:])} to {str(result)}"
                    )
                    self.hub.set_serial(int(join[1:]), str(result))
