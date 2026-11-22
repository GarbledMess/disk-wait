"""Config and options flow: pick the disk(s) to watch and the poll interval."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import proc
from .const import (
    CONF_DISKS,
    CONF_INTERFACES,
    CONF_REPAIRS,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)


def _discover() -> tuple[list[str], str | None, list[str], str | None]:
    return proc.list_disks(), proc.find_data_disk(), proc.list_nets(), proc.default_net()


def _multi(options: list[str]) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            multiple=True,
            custom_value=True,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _schema(
    disks: list[str],
    default_disks: list[str],
    nets: list[str],
    default_nets: list[str],
    interval: int,
    repairs: bool = True,
) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_DISKS, default=default_disks): _multi(disks),
            vol.Optional(CONF_INTERFACES, default=default_nets): _multi(nets),
            vol.Required(CONF_SCAN_INTERVAL, default=interval): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_SCAN_INTERVAL,
                    max=MAX_SCAN_INTERVAL,
                    step=5,
                    unit_of_measurement="s",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(CONF_REPAIRS, default=repairs): selector.BooleanSelector(),
        }
    )


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    return {
        CONF_DISKS: [d.strip() for d in user_input[CONF_DISKS] if d.strip()],
        CONF_INTERFACES: [n.strip() for n in user_input.get(CONF_INTERFACES, []) if n.strip()],
        CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
        CONF_REPAIRS: bool(user_input.get(CONF_REPAIRS, True)),
    }


class HostVitalsConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        disks, data_disk, nets, default_net = await self.hass.async_add_executor_job(
            _discover
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _clean(user_input)
            if data[CONF_DISKS]:
                return self.async_create_entry(title="Host Vitals", data=data)
            errors[CONF_DISKS] = "no_disks"
        default = [data_disk] if data_disk else disks[:1]
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(
                disks,
                default,
                nets,
                [default_net] if default_net else [],
                DEFAULT_SCAN_INTERVAL,
            ),
            errors=errors,
            description_placeholders={"data_disk": data_disk or "unknown"},
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return HostVitalsOptionsFlow()


class HostVitalsOptionsFlow(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        disks, data_disk, nets, _ = await self.hass.async_add_executor_job(_discover)
        current = {**self.config_entry.data, **self.config_entry.options}
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _clean(user_input)
            if data[CONF_DISKS]:
                return self.async_create_entry(data=data)
            errors[CONF_DISKS] = "no_disks"
        chosen = current.get(CONF_DISKS, [])
        chosen_nets = current.get(CONF_INTERFACES, [])
        return self.async_show_form(
            step_id="init",
            data_schema=_schema(
                sorted(set(disks) | set(chosen)),
                chosen,
                sorted(set(nets) | set(chosen_nets)),
                chosen_nets,
                current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                current.get(CONF_REPAIRS, True),
            ),
            errors=errors,
            description_placeholders={"data_disk": data_disk or "unknown"},
        )
