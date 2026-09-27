"""Event platform for the LibreNMS integration."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import ALERT_EVENT_TYPES, EVENT_TYPE_RECOVERED
from .coordinator import (
    LibreNMSAlert,
    LibreNMSConfigEntry,
    LibreNMSData,
    LibreNMSDataUpdateCoordinator,
)
from .entity import LibreNMSEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LibreNMSConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the LibreNMS alert event entity."""
    async_add_entities([LibreNMSAlertEvent(entry.runtime_data)])


class LibreNMSAlertEvent(LibreNMSEntity, EventEntity):
    """Fires when an alert opens, escalates, or clears.

    An alert that stays open at the same severity does not re-fire; the
    coordinator only reports it once.
    """

    _attr_event_types = ALERT_EVENT_TYPES
    _attr_translation_key = "alerts"

    def __init__(self, coordinator: LibreNMSDataUpdateCoordinator) -> None:
        """Initialize the event entity."""
        super().__init__(coordinator, "alerts")
        # The poll whose alerts were last turned into events. Listeners are
        # also called when an update fails, with the previous poll's data
        # still in place; that is not news and must not fire again.
        self._handled: LibreNMSData | None = coordinator.data

    @callback
    def _handle_coordinator_update(self) -> None:
        """Trigger an event for each alert change in a new, successful poll."""
        data = self.coordinator.data

        if self.coordinator.last_update_success and data is not self._handled:
            self._handled = data
            for alert in data.new_alerts:
                self._fire(alert, alert.severity)
            for alert in data.recovered_alerts:
                self._fire(alert, EVENT_TYPE_RECOVERED)

        super()._handle_coordinator_update()

    @callback
    def _fire(self, alert: LibreNMSAlert, event_type: str) -> None:
        """Trigger one event and write it out immediately.

        Each event gets its own state write so a burst of alerts in a single
        poll is not collapsed into just the last one.
        """
        self._trigger_event(event_type, alert.as_dict())
        self.async_write_ha_state()
