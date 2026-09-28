"""Sensors: summary sensors plus one best-price sensor per tracked set."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, RETAILERS
from .coordinator import LegoCoordinator

SUMMARY = (
    SensorEntityDescription(key="tracked_sets", translation_key="tracked_sets", icon="mdi:toy-brick-search",
                            state_class=SensorStateClass.MEASUREMENT),
    SensorEntityDescription(key="all_time_lows", translation_key="all_time_lows", icon="mdi:trending-down",
                            state_class=SensorStateClass.MEASUREMENT),
    SensorEntityDescription(key="high_discounts", translation_key="high_discounts", icon="mdi:sale",
                            state_class=SensorStateClass.MEASUREMENT),
    SensorEntityDescription(key="targets_reached", translation_key="targets_reached", icon="mdi:bullseye-arrow",
                            state_class=SensorStateClass.MEASUREMENT),
    SensorEntityDescription(key="wishlist_cost", translation_key="wishlist_cost", icon="mdi:cart-heart",
                            native_unit_of_measurement="EUR", state_class=SensorStateClass.MEASUREMENT,
                            suggested_display_precision=2),
    SensorEntityDescription(key="offers_with_errors", translation_key="offers_with_errors", icon="mdi:alert-circle-outline",
                            state_class=SensorStateClass.MEASUREMENT),
    SensorEntityDescription(key="collection_value", translation_key="collection_value", icon="mdi:cash-multiple",
                            native_unit_of_measurement="EUR", state_class=SensorStateClass.MEASUREMENT,
                            suggested_display_precision=2),
    SensorEntityDescription(key="collection_cost", translation_key="collection_cost", icon="mdi:cash-minus",
                            native_unit_of_measurement="EUR", state_class=SensorStateClass.MEASUREMENT,
                            suggested_display_precision=2),
    SensorEntityDescription(key="collection_growth", translation_key="collection_growth", icon="mdi:chart-line",
                            native_unit_of_measurement="%", state_class=SensorStateClass.MEASUREMENT,
                            suggested_display_precision=1),
)


def _device(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name="LEGO Price Tracker", manufacturer="Community",
                      entry_type=None)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coord: LegoCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(SummarySensor(coord, entry, d) for d in SUMMARY)
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new = [n for n in coord.store["sets"] if n not in known]
        if new:
            known.update(new)
            async_add_entities(SetPriceSensor(coord, entry, n) for n in new)

    _add_new()
    entry.async_on_unload(coord.async_add_listener(_add_new))


class SummarySensor(CoordinatorEntity[LegoCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coord: LegoCoordinator, entry: ConfigEntry, description: SensorEntityDescription) -> None:
        super().__init__(coord)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = _device(entry)

    def _deals(self, flag: str) -> list[str]:
        st = self.coordinator.data["statuses"]
        return [n for n, s in st.items() if s.get(flag)]

    @property
    def native_value(self) -> Any:
        d, key = self.coordinator.data, self.entity_description.key
        if key == "tracked_sets":
            return len(self.coordinator.store["sets"])
        if key == "all_time_lows":
            return len(self._deals("is_all_time_low"))
        if key == "high_discounts":
            return len(self._deals("high_discount"))
        if key == "targets_reached":
            return len(self._deals("target_hit"))
        if key == "wishlist_cost":
            return d["wishlist"]["cost"]
        if key == "offers_with_errors":
            return sum(s["offers_error"] for s in d["statuses"].values())
        return {"collection_value": d["summary"]["value"], "collection_cost": d["summary"]["cost"],
                "collection_growth": d["summary"]["growth_pct"]}[key]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        key = self.entity_description.key
        sets = self.coordinator.store["sets"]
        if key == "offers_with_errors":
            paused = {RETAILERS[r][0]: round(self.coordinator.fetcher.cooldown_left(r) / 3600, 1)
                      for r in self.coordinator.retailers if self.coordinator.fetcher.cooldown_left(r) > 0}
            return {"paused_hours": paused, "transport": self.coordinator.fetcher.transport}
        if key == "wishlist_cost":
            return self.coordinator.data["wishlist"]
        flag = {"all_time_lows": "is_all_time_low", "high_discounts": "high_discount",
                "targets_reached": "target_hit"}.get(key)
        if flag:
            st = self.coordinator.data["statuses"]
            return {"sets": [{"set_number": n, "name": sets[n].get("name"), "price": st[n]["best_price"],
                              "retailer": RETAILERS.get(st[n]["best_retailer"], ("",))[0],
                              "discount": st[n]["discount_rrp"]} for n in self._deals(flag)]}
        if key.startswith("collection"):
            return {"by_theme": self.coordinator.data["summary"]["by_theme"],
                    "sets_owned": self.coordinator.data["summary"]["sets"]}
        return None


class SetPriceSensor(CoordinatorEntity[LegoCoordinator], SensorEntity):
    """Cheapest current price of a set; long-term statistics give native HA graphs."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:toy-brick"
    _attr_native_unit_of_measurement = "EUR"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, coord: LegoCoordinator, entry: ConfigEntry, num: str) -> None:
        super().__init__(coord)
        self._num = num
        self._attr_unique_id = f"{entry.entry_id}_set_{num}"
        self._attr_device_info = _device(entry)

    @property
    def name(self) -> str:
        s = self.coordinator.store["sets"].get(self._num, {})
        return f"{self._num} {s.get('name') or ''}".strip()

    @property
    def available(self) -> bool:
        return self._num in self.coordinator.store["sets"]

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data["statuses"].get(self._num, {}).get("best_price")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = self.coordinator.store["sets"].get(self._num, {})
        st = self.coordinator.data["statuses"].get(self._num, {})
        offers = self.coordinator.store["offers"].get(self._num, {})
        return {
            "set_number": self._num, "theme": s.get("theme"), "subtheme": s.get("subtheme"), "rrp": s.get("rrp"),
            "best_retailer": RETAILERS.get(st.get("best_retailer"), ("",))[0] or None,
            "url": st.get("best_url"), "all_time_low": st.get("all_time_low"),
            "is_all_time_low": st.get("is_all_time_low"), "target_price": s.get("target_price"),
            "target_hit": st.get("target_hit"), "price_per_piece": st.get("price_per_piece"),
            "change_7d": st.get("change_7d"), "change_30d": st.get("change_30d"), "discount_rrp": st.get("discount_rrp"),
            "high_discount": st.get("high_discount"), "owned": self._num in self.coordinator.store["collection"],
            "entity_picture": s.get("image"),
            "prices": {RETAILERS[r][0]: o.get("last_price") for r, o in offers.items() if r in RETAILERS and o.get("available")},
        }
