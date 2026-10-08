"""Config + options flow."""
from __future__ import annotations

import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_KNOWN_SHOPS, CONF_LANGUAGE, CONF_REFRESH_MODE, CONF_SPREAD_HOURS, DEFAULT_REFRESH_MODE, DEFAULT_SPREAD_HOURS, CONF_AUTO_REFRESH, CONF_SET_DATA_KEY, CONF_DIGEST_TIME, CONF_PARTS_KEY, CONF_REFRESH_TIMES, DEFAULT_REFRESH_TIMES, CONF_IMPERSONATE, CONF_NOTIFY, CONF_DISCOUNT_THRESHOLD, CONF_MIN_HISTORY_DAYS, CONF_RETAILERS,
    DEFAULT_DIGEST_TIME, DEFAULT_DISCOUNT_THRESHOLD, DEFAULT_MIN_HISTORY_DAYS,
    DOMAIN, RETAILERS, CYCLE_CHOICES, CONF_WATCH_CYCLE, WATCH_CYCLE_CHOICES, CONF_DEV_FIXED_TIMES,
    CONF_DEV_FREE_CYCLE,
)
from .i18n import DEFAULT_LANGUAGE, LANGUAGES
from .shops import CONF_KEY_HOSTS, CONF_SETUP_VERSION, KEY_SOURCES, profile_ids, source_hosts


def _mode(d: dict[str, Any]) -> str:
    if d.get(CONF_REFRESH_MODE):
        return d[CONF_REFRESH_MODE]
    return "off" if d.get(CONF_AUTO_REFRESH) is False else DEFAULT_REFRESH_MODE


def _nearest(h: Any) -> int:
    try:
        return min(CYCLE_CHOICES, key=lambda c: (abs(c - float(h)), -c))
    except (TypeError, ValueError):
        return DEFAULT_SPREAD_HOURS


def _schema(d: dict[str, Any]) -> vol.Schema:
    times, free = bool(d.get(CONF_DEV_FIXED_TIMES)), bool(d.get(CONF_DEV_FREE_CYCLE))
    return vol.Schema({
        vol.Required(CONF_DISCOUNT_THRESHOLD, default=d.get(CONF_DISCOUNT_THRESHOLD, DEFAULT_DISCOUNT_THRESHOLD)):
            selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=90, step=1, unit_of_measurement="%",
                                                                  mode=selector.NumberSelectorMode.SLIDER)),
        # the shops come from the shop settings file (imported in the panel): none before that
        **({vol.Required(CONF_RETAILERS, default=[r for r in d.get(CONF_RETAILERS, profile_ids()) if r in RETAILERS]):
            selector.SelectSelector(selector.SelectSelectorConfig(
                options=[{"value": k, "label": v[0]} for k, v in RETAILERS.items()], multiple=True))} if RETAILERS else {}),
        vol.Required(CONF_LANGUAGE, default=d.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)):
            selector.SelectSelector(selector.SelectSelectorConfig(
                options=[{"value": "auto", "label": "Auto (Home Assistant)"}] + [{"value": k, "label": v} for k, v in LANGUAGES.items()],
                mode=selector.SelectSelectorMode.DROPDOWN)),
        vol.Required(CONF_REFRESH_MODE, default=_mode(d) if times or _mode(d) != "times" else "spread"):
            selector.SelectSelector(selector.SelectSelectorConfig(options=["spread", "times", "off"] if times else ["spread", "off"],
                                                                  translation_key="refresh_mode")),
        **({vol.Required(CONF_SPREAD_HOURS, default=d.get(CONF_SPREAD_HOURS, DEFAULT_SPREAD_HOURS)):
            selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=168, step=1, unit_of_measurement="h",
                                                                  mode=selector.NumberSelectorMode.BOX))} if free else
           {vol.Required(CONF_SPREAD_HOURS, default=str(_nearest(d.get(CONF_SPREAD_HOURS, DEFAULT_SPREAD_HOURS)))):
            selector.SelectSelector(selector.SelectSelectorConfig(options=[str(h) for h in CYCLE_CHOICES], translation_key="cycle",
                                                                  mode=selector.SelectSelectorMode.DROPDOWN))}),
        vol.Required(CONF_WATCH_CYCLE, default=str(d.get(CONF_WATCH_CYCLE, 0) if int(d.get(CONF_WATCH_CYCLE, 0) or 0) in WATCH_CYCLE_CHOICES else 0)):
            selector.SelectSelector(selector.SelectSelectorConfig(options=[str(m) for m in WATCH_CYCLE_CHOICES], translation_key="watch_cycle",
                                                                  mode=selector.SelectSelectorMode.DROPDOWN)),
        **({vol.Required(CONF_REFRESH_TIMES, default=d.get(CONF_REFRESH_TIMES, DEFAULT_REFRESH_TIMES)): selector.TextSelector()} if times else {}),
        vol.Required(CONF_DIGEST_TIME, default=d.get(CONF_DIGEST_TIME, DEFAULT_DIGEST_TIME)): selector.TimeSelector(),
        vol.Required(CONF_MIN_HISTORY_DAYS, default=d.get(CONF_MIN_HISTORY_DAYS, DEFAULT_MIN_HISTORY_DAYS)):
            selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=90, step=1, unit_of_measurement="d",
                                                                  mode=selector.NumberSelectorMode.BOX)),
        vol.Required(CONF_IMPERSONATE, default=d.get(CONF_IMPERSONATE, True)): selector.BooleanSelector(),
        vol.Optional(CONF_NOTIFY, description={"suggested_value": d.get(CONF_NOTIFY, "")}): str,
        vol.Optional(CONF_PARTS_KEY, description={"suggested_value": d.get(CONF_PARTS_KEY, "")}): str,
        vol.Optional(CONF_SET_DATA_KEY, description={"suggested_value": d.get(CONF_SET_DATA_KEY, "")}): str,
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
    if CONF_REFRESH_TIMES in user_input:              # only in the form in developer mode
        if (mode == "times" and not valid) or len(valid) > 6:
            raise InvalidTimes
        out[CONF_REFRESH_TIMES] = ", ".join(valid) or DEFAULT_REFRESH_TIMES
    out.pop("update_hours", None)
    for k in (CONF_DISCOUNT_THRESHOLD, CONF_MIN_HISTORY_DAYS, CONF_SPREAD_HOURS, CONF_WATCH_CYCLE):
        if k not in out:
            continue
        out[k] = int(float(out[k]))
    return out


class LegoTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                return self.async_create_entry(title="B.R.I.C.K.", data={},
                                               options={**_clean(user_input), CONF_KNOWN_SHOPS: profile_ids(), CONF_SETUP_VERSION: 1})
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
                new = {**self.config_entry.options, **_clean(user_input)}
                hosts = dict(new.get(CONF_KEY_HOSTS) or {})   # a key you enter belongs to the addresses in force now
                for key, sources in KEY_SOURCES.items():
                    if new.get(key) != self.config_entry.options.get(key):
                        if new.get(key):
                            hosts[key] = source_hosts(sources)
                        else:
                            hosts.pop(key, None)
                new[CONF_KEY_HOSTS] = hosts
                return self.async_create_entry(data=new)
            except InvalidTimes:
                errors[CONF_REFRESH_TIMES] = "invalid_times"
        return self.async_show_form(step_id="init", data_schema=_schema({**self.config_entry.options, **(user_input or {})}),
                                    errors=errors)
