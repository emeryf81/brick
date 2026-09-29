"""Config + options flow."""
from __future__ import annotations

import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    BUILTIN_RETAILERS, CONF_KNOWN_SHOPS, CONF_LANGUAGE, CONF_REFRESH_MODE, CONF_SPREAD_HOURS, DEFAULT_REFRESH_MODE, DEFAULT_SPREAD_HOURS, CONF_AUTO_REFRESH, CONF_BRICKSET_KEY, CONF_DIGEST_TIME, CONF_REBRICKABLE_KEY, CONF_REFRESH_TIMES, DEFAULT_REFRESH_TIMES, CONF_IMPERSONATE, CONF_NOTIFY, CONF_DISCOUNT_THRESHOLD, CONF_MIN_HISTORY_DAYS, CONF_RETAILERS,
    DEFAULT_DIGEST_TIME, DEFAULT_DISCOUNT_THRESHOLD, DEFAULT_MIN_HISTORY_DAYS,
    DEFAULT_RETAILERS, DOMAIN, RETAILERS,
)
from .i18n import DEFAULT_LANGUAGE, LANGUAGES


def _mode(d: dict[str, Any]) -> str:
    if d.get(CONF_REFRESH_MODE):
        return d[CONF_REFRESH_MODE]
    return "off" if d.get(CONF_AUTO_REFRESH) is False else DEFAULT_REFRESH_MODE


def _schema(d: dict[str, Any]) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_DISCOUNT_THRESHOLD, default=d.get(CONF_DISCOUNT_THRESHOLD, DEFAULT_DISCOUNT_THRESHOLD)):
            selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=90, step=1, unit_of_measurement="%",
                                                                  mode=selector.NumberSelectorMode.SLIDER)),
        vol.Required(CONF_RETAILERS, default=d.get(CONF_RETAILERS, DEFAULT_RETAILERS)):
            selector.SelectSelector(selector.SelectSelectorConfig(
                options=[{"value": k, "label": v[0]} for k, v in RETAILERS.items()], multiple=True)),
        vol.Required(CONF_LANGUAGE, default=d.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)):
            selector.SelectSelector(selector.SelectSelectorConfig(
                options=[{"value": "auto", "label": "Auto (Home Assistant)"}] + [{"value": k, "label": v} for k, v in LANGUAGES.items()],
                mode=selector.SelectSelectorMode.DROPDOWN)),
        vol.Required(CONF_REFRESH_MODE, default=_mode(d)):
            selector.SelectSelector(selector.SelectSelectorConfig(options=["spread", "times", "off"], translation_key="refresh_mode")),
        vol.Required(CONF_SPREAD_HOURS, default=d.get(CONF_SPREAD_HOURS, DEFAULT_SPREAD_HOURS)):
            selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=168, step=1, unit_of_measurement="h",
                                                                  mode=selector.NumberSelectorMode.BOX)),
        vol.Required(CONF_REFRESH_TIMES, default=d.get(CONF_REFRESH_TIMES, DEFAULT_REFRESH_TIMES)): selector.TextSelector(),
        vol.Required(CONF_DIGEST_TIME, default=d.get(CONF_DIGEST_TIME, DEFAULT_DIGEST_TIME)): selector.TimeSelector(),
        vol.Required(CONF_MIN_HISTORY_DAYS, default=d.get(CONF_MIN_HISTORY_DAYS, DEFAULT_MIN_HISTORY_DAYS)):
            selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=90, step=1, unit_of_measurement="d",
                                                                  mode=selector.NumberSelectorMode.BOX)),
        vol.Required(CONF_IMPERSONATE, default=d.get(CONF_IMPERSONATE, True)): selector.BooleanSelector(),
        vol.Optional(CONF_NOTIFY, description={"suggested_value": d.get(CONF_NOTIFY, "")}): str,
        vol.Optional(CONF_REBRICKABLE_KEY, description={"suggested_value": d.get(CONF_REBRICKABLE_KEY, "")}): str,
        vol.Optional(CONF_BRICKSET_KEY, description={"suggested_value": d.get(CONF_BRICKSET_KEY, "")}): str,
    })


class InvalidTimes(ValueError):
    pass


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    out = dict(user_input)
    times = re.findall(r"(\d{1,2})[:.hu](\d{2})", str(out.get(CONF_REFRESH_TIMES, "")))
    valid = sorted({f"{int(h):02d}:{m}" for h, m in times if int(h) < 24 and int(m) < 60})
    mode = _mode(out)
    out[CONF_REFRESH_MODE] = mode
    out[CONF_AUTO_REFRESH] = mode != "off"
    if mode == "times" and not valid:
        raise InvalidTimes
    if len(valid) > 6:
        raise InvalidTimes
    out[CONF_REFRESH_TIMES] = ", ".join(valid) or DEFAULT_REFRESH_TIMES
    out.pop("update_hours", None)
    for k in (CONF_DISCOUNT_THRESHOLD, CONF_MIN_HISTORY_DAYS, CONF_SPREAD_HOURS):
        if k not in out:
            continue
        out[k] = int(out[k])
    return out


class LegoTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                return self.async_create_entry(title="LEGO Price Tracker", data={},
                                               options={**_clean(user_input), CONF_KNOWN_SHOPS: list(BUILTIN_RETAILERS)})
            except InvalidTimes:
                errors[CONF_REFRESH_TIMES] = "invalid_times"
        return self.async_show_form(step_id="user", data_schema=_schema(user_input or {}), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return LegoTrackerOptionsFlow()


class LegoTrackerOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                # merge: settings made in the panel (custom shops, keys, pauses…) must survive
                return self.async_create_entry(data={**self.config_entry.options, **_clean(user_input)})
            except InvalidTimes:
                errors[CONF_REFRESH_TIMES] = "invalid_times"
        return self.async_show_form(step_id="init", data_schema=_schema(user_input or dict(self.config_entry.options)),
                                    errors=errors)
