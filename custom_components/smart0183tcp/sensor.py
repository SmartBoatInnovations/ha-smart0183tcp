import asyncio
import logging
import voluptuous as vol
import homeassistant.helpers.config_validation as cv
import json
import os


from homeassistant.helpers.entity import Entity
from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from datetime import datetime, timedelta
from asyncio import IncompleteReadError
from aiohttp.client_exceptions import ClientConnectorError

from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_NAME,
    EVENT_HOMEASSISTANT_STOP
)


# Setting up logging and configuring constants and default values

_LOGGER = logging.getLogger(__name__)

# Extending the schema to include configurations for the TCP connection
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
    vol.Required(CONF_HOST): cv.string,
    vol.Required(CONF_PORT): cv.port,
    vol.Optional(CONF_NAME, default="SMART0183 TCP SENSOR "): cv.string,
    }
)



async def update_sensor_availability(hass):
    """Update the availability of all sensors every 5 minutes."""
    while True:
        _LOGGER.debug("Running update_sensor_availability")
        await asyncio.sleep(300)  # wait for 5 minutes

        for sensor in hass.data["created_sensors"].values():
            sensor.update_availability()


# The main setup function to initialize the sensor platform

async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the TCP sensor platform."""
    name = config.get(CONF_NAME)
    port = config.get(CONF_PORT)
    host = config.get(CONF_HOST)

    # Log the retrieved configuration values for debugging purposes
    _LOGGER.debug(f"Configuring sensor with name: {name}, host: {host}, port: {port}")

    _LOGGER.debug("Setting up platform.")
    # Save a reference to the add_entities callback

    _LOGGER.debug("Assigning async_add_entities to hass.data.")

    hass.data["add_tcp_sensors"] = async_add_entities

    _LOGGER.debug("Assigned successfully.")

    # Initialize a dictionary to store references to the created sensors
    hass.data["created_sensors"] = {}


    # Load the 0183 data within setup_platform
    config_dir = hass.config.config_dir
    json_path = os.path.join(config_dir, 'custom_components', 'smart0183tcp', 'Smart0183tcp.json')
    try:
        with open(json_path, "r") as file:
            smart_data = json.load(file)

        result_dict = {}
        for sentence in smart_data:
            for field in sentence["fields"]:
                result_dict[field["unique_id"]] = field["short_description"]

        hass.data["smart0183tcp_data"] = result_dict

    except Exception as e:
        _LOGGER.error(f"Error loading Smart0183tcp.json: {e}")
        return

    _LOGGER.debug(f"Loaded smart data: {hass.data['smart0183tcp_data']}")


    sensor = TCPSensor(
        name,
        host,
        port,
    )

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, sensor.stop_tcp_read)
    async_add_entities([sensor], True)

    # Start the task that updates the sensor availability every 5 minutes
    hass.loop.create_task(update_sensor_availability(hass))

# SmartSensor class representing a basic sensor entity with state

class SmartSensor(Entity):
    def __init__(self, name, friendly_name, initial_state):
        """Initialize the sensor."""
        _LOGGER.info(f"Initializing sensor: {name} with state: {initial_state}")
        self._unique_id = name.lower().replace(" ", "_")
        self._name = friendly_name if friendly_name else self._unique_id
        self._state = initial_state
        self._last_updated = datetime.now()
        if initial_state is None or initial_state == "":
            self._available = False
            _LOGGER.debug(f"Setting sensor: '{self._name}' with unavailable")
        else:
            self._available = True

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name
    @property
    def unique_id(self):
        """Return a unique ID."""
        return self._unique_id

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def last_updated(self):
        """Return the last updated timestamp of the sensor."""
        return self._last_updated

    @property
    def available(self) -> bool:
        """Return True if the entity is available."""
        return self._available

    @property
    def should_poll(self) -> bool:
        """Return the polling requirement for this sensor."""
        return False


    def update_availability(self):
        """Update the availability status of the sensor."""

        new_availability = (datetime.now() - self._last_updated) < timedelta(minutes=4)

        if new_availability:
            _LOGGER.debug(f"Sensor '{self._name}' is being set to available at {datetime.now()}")
        else:
            _LOGGER.debug(f"Sensor '{self._name}' is being set to unavailable at {datetime.now()}")

        self._available = new_availability

        try:
            self.async_schedule_update_ha_state()
        except RuntimeError as re:
            if "Attribute hass is None" in str(re):
                pass  # Ignore this specific error
            else:
                _LOGGER.warning(f"Could not update state for sensor '{self._name}': {re}")
        except Exception as e:  # Catch all other exception types
            _LOGGER.warning(f"Could not update state for sensor '{self._name}': {e}")

    def set_state(self, new_state):
        """Set the state of the sensor."""
        _LOGGER.debug(f"Setting state for sensor: '{self._name}' to {new_state}")
        self._state = new_state
        if new_state is None or new_state == "":
            self._available = False
            _LOGGER.debug(f"Setting sensor:'{self._name}' with unavailable")
        else:
            self._available = True
        self._last_updated = datetime.now()

        try:
            self.async_schedule_update_ha_state()
        except RuntimeError as re:
            if "Attribute hass is None" in str(re):
                pass  # Ignore this specific error
            else:
                _LOGGER.warning(f"Could not update state for sensor '{self._name}': {re}")
        except Exception as e:  # Catch all other exception types
            _LOGGER.warning(f"Could not update state for sensor '{self._name}': {e}")

# TCPSensor class representing a sensor entity interacting with a TCP device

class TCPSensor(SensorEntity):
    """Representation of a TCP sensor."""

    _attr_should_poll = False

    def __init__(
        self,
        name,
        host,
        port,
    ):
        """Initialize the TCP sensor."""
        self._name = name
        self._state = None
        self._host = host
        self._port = int(port)
        self._connection_loop_task = None
        self._attributes = None

    async def async_added_to_hass(self) -> None:
        """Handle when an entity is about to be added to Home Assistant."""
        self._connection_loop_task = self.hass.loop.create_task(
            self.tcp_read(
                self._host,
                self._port
            )
        )



    async def set_smart_sensors(self, line):
        """Process the content of the line related to the smart sensors."""
        try:

            if not line or not line.startswith("$"):
                return

            # Splitting by comma and getting the data fields
            fields = line.split(',')
            if len(fields) < 1 or len(fields[0]) < 6:  # Ensure enough fields and length
                _LOGGER.error(f"Malformed line: {line}")
                return

            sentence_id = fields[0][1:6]  # Gets the 5-char word after the $

            _LOGGER.debug(f"Checking sensor: {sentence_id}")

            # Check if main sensor exists; if not, create one
            if sentence_id not in self.hass.data["created_sensors"]:
                _LOGGER.debug(f"Creating main sensor: {sentence_id}")
                sensor = SmartSensor(sentence_id, sentence_id, line)

                self.hass.data["add_tcp_sensors"]([sensor])
                self.hass.data["created_sensors"][sentence_id] = sensor
            else:
                # If the sensor already exists, update its state
                _LOGGER.debug(f"Updating main sensor: {sentence_id}")
                sensor = self.hass.data["created_sensors"][sentence_id]
                sensor.set_state(line)

            # Now creating or updating sensors for individual fields
            for idx, field_data in enumerate(fields[1:], 1):
                # Skip the last field since it's a check digit
                if idx == len(fields) - 1:
                    break

                sensor_name = f"{sentence_id}_{idx}"

                _LOGGER.debug(f"Checking field sensor: {sensor_name}")

                short_sensor_name = f"{sentence_id[2:]}_{idx}"

                # Check if this field sensor exists; if not, create one
                if sensor_name not in self.hass.data["created_sensors"]:
                    _LOGGER.debug(f"Creating field sensor: {sensor_name}")

                    short_desc = self.hass.data["smart0183tcp_data"].get(short_sensor_name, sensor_name)
                    _LOGGER.debug(f"Short descr sensor: {short_sensor_name} with : {short_desc}")

                    sensor = SmartSensor(sensor_name, short_desc, field_data)
                    self.hass.data["add_tcp_sensors"]([sensor])
                    self.hass.data["created_sensors"][sensor_name] = sensor
                else:
                    # If the sensor already exists, update its state
                    _LOGGER.debug(f"Updating field sensor: {sensor_name}")

                    short_desc = self.hass.data["smart0183tcp_data"].get(short_sensor_name, sensor_name)
                    _LOGGER.debug(f"Short descr sensor: {short_sensor_name} with : {short_desc}")

                    sensor = self.hass.data["created_sensors"][sensor_name]
                    sensor.set_state(field_data)

            self._state = line
            self.async_write_ha_state()

        except IndexError:
            _LOGGER.error(f"Index error for line: {line}")
        except KeyError as e:
            _LOGGER.error(f"Key error: {e}")
        except Exception as e:
            _LOGGER.error(f"An unexpected error occurred: {e}")



    async def tcp_read(self, host, port):
        """Read the data from the TCP connection with improved error handling."""
        retry_delay = 1  # Start with a 1-second delay
        max_retry_delay = 60  # Maximum delay of 60 seconds between retries
        writer = None

        while True:
            try:
                _LOGGER.info(f"Attempting to connect to TCP device {host}:{port} ")

                reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=10)

                
                _LOGGER.info(f"Connected to TCP device {host}:{port}")
                retry_delay = 1  # Reset retry delay after a successful connection

                while True:
                    line = await reader.readline()
                    if not line:
                        _LOGGER.info("TCP connection closed by the server.")
                        break  # Connection closed by the server

                    line = line.decode('utf-8').strip()
                    _LOGGER.debug(f"Received: {line}")
                    await self.set_smart_sensors(line)

            except (ClientConnectorError, IncompleteReadError, UnicodeDecodeError) as specific_exc:
                _LOGGER.error(f"Connection error to {host}:{port}: {specific_exc}")
                
            except asyncio.CancelledError:
                _LOGGER.info("Connection attempt to TCP device was cancelled.")
                raise

            except Exception as exc:
                _LOGGER.exception(f"Unexpected error with TCP device {host}:{port}: {exc}")

            finally:
                try:
                    if writer:
                        writer.close()
                        await writer.wait_closed()
                except Exception as e:
                    _LOGGER.error(f"Error closing writer: {e}")
                _LOGGER.info(f"Will retry in {retry_delay} seconds...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_retry_delay)

    @callback
    def stop_tcp_read(self, event):
        """Close resources for the TCP connection."""
        if self._connection_loop_task:
            self._connection_loop_task.cancel()
    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def extra_state_attributes(self):
        """Return the attributes of the entity (if any JSON present)."""
        return self._attributes

    @property
    def native_value(self):
        """Return the state of the sensor."""
        return self._state

