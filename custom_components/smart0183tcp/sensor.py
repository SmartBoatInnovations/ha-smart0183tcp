# Standard Library Imports
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta

# Third-Party Library Imports
from asyncio import IncompleteReadError
from aiohttp.client_exceptions import ClientConnectorError

# Home Assistant Imports
from homeassistant.core import callback
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.helpers.entity import Entity

from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_NAME,
    EVENT_HOMEASSISTANT_STOP
)


# Setting up logging and configuring constants and default values

_LOGGER = logging.getLogger(__name__)


async def update_sensor_availability(hass,instance_name):
    """Update the availability of all sensors every 5 minutes."""
    
    created_sensors_key = f"{instance_name}_created_sensors"

    while True:
        _LOGGER.debug("Running update_sensor_availability")
        await asyncio.sleep(300)  # wait for 5 minutes

        for sensor in hass.data[created_sensors_key].values():
            sensor.update_availability()


# The main setup function to initialize the sensor platform

async def async_setup_entry(hass, entry, async_add_entities):
    # Retrieve configuration from entry
    name = entry.data[CONF_NAME]
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]

    # Log the retrieved configuration values for debugging purposes
    _LOGGER.info(f"Configuring sensor with name: {name}, host: {host}, port: {port}")
    
    # Initialize unique dictionary keys based on the integration name
    add_entities_key = f"{name}_add_entities"
    created_sensors_key = f"{name}_created_sensors"
    smart0183tcp_data_key = f"{name}_smart0183tcp_data"


     # Save a reference to the add_entities callback
    _LOGGER.debug(f"Assigning async_add_entities to hass.data[{add_entities_key}].")
    hass.data[add_entities_key] = async_add_entities


    # Initialize a dictionary to store references to the created sensors
    hass.data[created_sensors_key] = {}

    # Load the Smart0183 json data 
    config_dir = hass.config.config_dir
    json_path = os.path.join(config_dir, 'custom_components', 'smart0183tcp', 'Smart0183tcp.json')
    try:
        with open(json_path, "r") as file:
            smart_data = json.load(file)

        result_dict = {}
        for sentence in smart_data:
            group = sentence["group"]  # Capture the group for all fields within this sentence
            sentence_desc = sentence["sentence_description"]
            for field in sentence["fields"]:
                result_dict[field["unique_id"]] = {
                    "full_description": field["full_description"],
                    "group": group,
                    "sentence_description": sentence_desc,
                    "unit_of_measurement": field.get("unit_of_measurement", None)
                }


        hass.data[smart0183tcp_data_key] = result_dict

    except Exception as e:
        _LOGGER.error(f"Error loading Smart0183tcp.json: {e}")
        return

    _LOGGER.debug(f"Loaded smart data: {hass.data[smart0183tcp_data_key]}")


    sensor = TCPSensor(
        name,
        host,
        port,
    )

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, sensor.stop_tcp_read)
    async_add_entities([sensor], True)

    # Start the task that updates the sensor availability every 5 minutes
    hass.loop.create_task(update_sensor_availability(hass,name))

async def set_smart_sensors(hass, line, instance_name):
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
        device_id = sentence_id[0:2] # Gets the 2-char sender id (pos 2 & 3 of sentence)

        _LOGGER.debug(f"Sentence_id: {sentence_id}, device_id: {device_id}")
        
        # Dynamically construct the keys based on the instance name
        smart0183tcp_data_key = f"{instance_name}_smart0183tcp_data"
        created_sensors_key = f"{instance_name}_created_sensors"
        add_entities_key = f"{instance_name}_add_entities"

        for idx, field_data in enumerate(fields[1:], 1):
            if idx == len(fields) - 1:  # Skip the last field since it's a check digit
                break

            sentence_type = sentence_id[2:]
            sensor_name = f"{device_id}_{sentence_type}_{idx}"

            if sensor_name not in hass.data[created_sensors_key]:
                _LOGGER.debug(f"Creating field sensor: {sensor_name}")
                
                short_sensor_name = f"{sentence_id[2:]}_{idx}"
                sensor_info = hass.data[smart0183tcp_data_key].get(short_sensor_name)
                
                # If sensor_info does not exist, skip this loop iteration
                if sensor_info is None:
                    _LOGGER.debug(f"Skipping creation/update for undefined sensor: {sensor_name}")
                    continue


                full_desc = sensor_info["full_description"] if sensor_info else sensor_name
                group = sensor_info["group"]
                sentence_description = sensor_info["sentence_description"]
                unit_of_measurement = sensor_info.get("unit_of_measurement")

                device_name = sentence_description + ' (' + device_id + ')'

                sensor = SmartSensor(
                    sensor_name, 
                    full_desc, 
                    field_data, 
                    group, 
                    unit_of_measurement, 
                    device_name, 
                    sentence_type
                )
                
                # Add Sensor to Home Assistant
                hass.data[add_entities_key]([sensor])
                
                # Update dictionary with added sensor
                hass.data[created_sensors_key][sensor_name] = sensor
            else:
                _LOGGER.debug(f"Updating field sensor: {sensor_name}")
                sensor = hass.data[created_sensors_key][sensor_name]
                sensor.set_state(field_data)

    except IndexError:
        _LOGGER.error(f"Index error for line: {line}")
    except KeyError as e:
        _LOGGER.error(f"Key error: {e}")
    except Exception as e:
        _LOGGER.error(f"An unexpected error occurred: {e}")


# SmartSensor class representing a basic sensor entity with state

class SmartSensor(Entity):
    def __init__(
        self, 
        name, 
        friendly_name, 
        initial_state, 
        group=None, 
        unit_of_measurement=None, 
        device_name=None, 
        sentence_type=None
    ):
        """Initialize the sensor."""
        _LOGGER.info(f"Initializing sensor: {name} with state: {initial_state}")

        self._unique_id = name.lower().replace(" ", "_")
        self.entity_id = f"sensor.{self._unique_id}"
        self._name = friendly_name if friendly_name else self._unique_id
        self._state = initial_state
        self._group = group if group is not None else "Other"
        self._device_name = device_name
        self._sentence_type = sentence_type
        self._unit_of_measurement = unit_of_measurement
        self._state_class = SensorStateClass.MEASUREMENT
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
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return self._unit_of_measurement

    @property
    def device_info(self):
        """Return device information about this sensor."""
        return {
            "identifiers": {("smart0183tcp", self._device_name)},
            "name": self._device_name,
            "manufacturer": self._group,
            "model": self._sentence_type,
        }

    @property
    def state_class(self):
        """Return the state class of the sensor."""
        return self._state_class


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


    async def tcp_read(self, host, port):
        """Read the data from the TCP connection with improved error handling."""
        retry_delay = 1  # Start with a 1-second delay
        max_retry_delay = 60  # Maximum delay of 60 seconds between retries
        writer = None
        
        last_processed = {}  # Dictionary to store last processed timestamp for each sentence type
        min_interval = timedelta(seconds=3)  # Minimum time interval between processing each sentence type

        data_timeout = 60  # 60 seconds timeout for data reception

        while True:
            try:
                
                # Variable to track the current operation
                current_operation = "connecting"

                _LOGGER.info(f"Attempting to connect to TCP device {host}:{port} ")

                reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=10)

                
                _LOGGER.info(f"Connected to TCP device {host}:{port}")
                retry_delay = 1  # Reset retry delay after a successful connection
                
                current_operation = "receiving data"  

                while True:
                    line = await asyncio.wait_for(reader.readline(), timeout=data_timeout)
                    if not line:
                        _LOGGER.info("TCP connection closed by the server.")
                        break  # Connection closed by the server

                    line = line.decode('utf-8').strip()
                    _LOGGER.debug(f"Received: {line}")
                    
                    sentence_type = line[:6]  
                    
                    now = datetime.now()
                    
                    if sentence_type not in last_processed or now - last_processed[sentence_type] >= min_interval:
                        _LOGGER.debug(f"Processing: {line}")
                        await set_smart_sensors(self.hass, line, self.name)
                        last_processed[sentence_type] = now
                    else:
                        _LOGGER.debug(f"Skipping {sentence_type} due to throttling")

            # Handling connection errors more gracefully
            except asyncio.TimeoutError:
                if current_operation == "connecting":
                    _LOGGER.error(f"Timeout occurred while trying to connect to TCP device at {host}:{port}.")
                else:  # current_operation == "receiving data"
                    _LOGGER.error(f"No data received in the last {data_timeout} seconds from {host}:{port}.")
                    
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


