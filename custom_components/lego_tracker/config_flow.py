"""Config + options flow."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BRICKSET_KEY, CONF_DIGEST_TIME, CONF_IMPERSONATE, CONF_DISCOUNT_THRESHOLD, CONF_MIN_HISTORY_DAYS, CONF_RETAILERS,
    CONF_UPDATE_HOURS, DEFAULT_DIGEST_TIME, DEFAULT_DISCOUNT_THRESHOLD, DEFAULT_MIN_HISTORY_DAYS,
    DEFAULT_RETAILERS, DEFAULT_UPDATE_HOURS, DOMAIN, RETAILERS,
)


def _schema(d: dict[str, Any]) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_DISCOUNT_THRESHOLD, default=d.get(CONF_DISCOUNT_THRESHOLD, DEFAULT_DISCOUNT_THRESHOLD)):
            selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=90, step=1, unit_of_measurement="%",
                                                                  mode=selector.NumberSelectorMode.SLIDER)),
        vol.Required(CONF_RETAILERS, default=d.get(CONF_RETAILERS, DEFAULT_RETAILERS)):
            selector.SelectSelector(selector.SelectSelectorConfig(
                options=[{"value": k, "label": v[0]} for k, v in RETAILERS.items()], multiple=True)),
        vol.Required(CONF_UPDATE_HOURS, default=d.get(CONF_UPDATE_HOURS, DEFAULT_UPDATE_HOURS)):
            selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=48, step=1, unit_of_measurement="h",
                                                                  mode=selector.NumberSelectorMode.BOX)),
        vol.Required(CONF_DIGEST_TIME, default=d.get(CONF_DIGEST_TIME, DEFAULT_DIGEST_TIME)): selector.TimeSelector(),
        vol.Required(CONF_MIN_HISTORY_DAYS, default=d.get(CONF_MIN_HISTORY_DAYS, DEFAULT_MIN_HISTORY_DAYS)):
            selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=90, step=1, unit_of_measurement="d",
                                                                  mode=selector.NumberSelectorMode.BOX)),
        vol.Required(CONF_IMPERSONATE, default=d.get(CONF_IMPERSONATE, True)): selector.BooleanSelector(),
        vol.Optional(CONF_BRICKSET_KEY, description={"suggested_value": d.get(CONF_BRICKSET_KEY, "")}): str,
    })


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    out = dict(user_input)
    for k in (CONF_DISCOUNT_THRESHOLD, CONF_UPDATE_HOURS, CONF_MIN_HISTORY_DAYS):
        out[k] = int(out[k])
    return out


class LegoTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(title="LEGO Price Tracker", data={}, options=_clean(user_input))
        return self.async_show_form(step_id="user", data_schema=_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return LegoTrackerOptionsFlow()


class LegoTrackerOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=_clean(user_input))
        return self.async_show_form(step_id="init", data_schema=_schema(dict(self.config_entry.options)))
