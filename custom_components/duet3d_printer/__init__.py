"""Support for monitoring Duet 3D printers."""
import logging
import time

import requests
import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PATH,
    CONF_PORT,
    CONF_SSL,
    TEMP_CELSIUS,
    CONF_MONITORED_CONDITIONS,
    CONF_SENSORS,
    CONF_BINARY_SENSORS,
)
from homeassistant.helpers.discovery import load_platform
from homeassistant.util import slugify as util_slugify

_LOGGER = logging.getLogger(__name__)

CONF_BED = "bed"
CONF_NUMBER_OF_TOOLS = "number_of_tools"

DEFAULT_NAME = "Duet3D Printer"
DOMAIN = "duet3d_printer"

MIN_INTERVAL = 10.0


def has_all_unique_names(value):
    """Validate that printers have an unique name."""
    names = [util_slugify(printer["name"]) for printer in value]
    vol.Schema(vol.Unique())(names)
    return value


def ensure_valid_path(value):
    """Validate the path, ensuring it starts and ends with a /."""
    vol.Schema(cv.string)(value)
    if value[0] != "/":
        value = "/" + value
    if value[-1] != "/":
        value += "/"
    return value


BINARY_SENSOR_TYPES = {
    # API Endpoint, Group, Key, unit
    "Printing": ["job", "status", "printing", None],
    # "Printing Error": ['printer', 'state', 'error', None]
}

BINARY_SENSOR_SCHEMA = vol.Schema(
    {
        vol.Optional(
            CONF_MONITORED_CONDITIONS, default=list(BINARY_SENSOR_TYPES)
        ): vol.All(cv.ensure_list, [vol.In(BINARY_SENSOR_TYPES)]),
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    }
)

SENSOR_TYPES = {
    # API Endpoint, Group, Key, unit, icon
    # Group, subgroup, key, unit, icon
    "Temperatures": ["heat", "heaters", "0,1", TEMP_CELSIUS],
    "Current State": ["state", "status", "text", None, "mdi:printer-3d"],
    "Job Percentage": ["job", "fractionPrinted", "completion", "%", "mdi:file-percent"],
    "Time Remaining": ["job", "timesLeft", "file", "seconds", "mdi:clock-end"],
    "Time Elapsed": ["job", "printDuration", "printTime", "seconds", "mdi:clock-start"],
    "Job Name": ["job", "fileName", "text", None, "mdi:printer-3d"],
    "Position": ["move", "axes", "0,1,2", "mm,mm,mm", "mdi:axis-x-arrow,mdi:axis-y-arrow,mdi:axis-z-arrow",
                 ],
}

SENSOR_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_MONITORED_CONDITIONS, default=list(SENSOR_TYPES)): vol.All(
            cv.ensure_list, [vol.In(SENSOR_TYPES)]
        ),
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(
            cv.ensure_list,
            [
                vol.Schema(
                    {
                        # vol.Required(CONF_API_KEY): cv.string,
                        vol.Required(CONF_HOST): cv.string,
                        vol.Optional(CONF_SSL, default=False): cv.boolean,
                        vol.Optional(CONF_PORT, default=80): cv.port,
                        # type 2, extended infos, type 3, print status infos
                        vol.Optional(
                            CONF_PATH, default="/rr_model?flags=d99vn"
                        ): ensure_valid_path,
                        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
                        vol.Optional(CONF_NUMBER_OF_TOOLS, default=0): cv.positive_int,
                        vol.Optional(CONF_BED, default=False): cv.boolean,
                        vol.Optional(CONF_SENSORS, default={}): SENSOR_SCHEMA,
                        vol.Optional(
                            CONF_BINARY_SENSORS, default={}
                        ): BINARY_SENSOR_SCHEMA,
                    }
                )
            ],
            has_all_unique_names,
        ),
    },
    extra=vol.ALLOW_EXTRA,
)


def setup(hass, config):
    printers = hass.data[DOMAIN] = {}
    success = False

    if DOMAIN not in config:
        # Skip the setup if there is no configuration present
        return True

    for printer in config[DOMAIN]:
        name = printer[CONF_NAME]
        ssl = "s" if printer[CONF_SSL] else ""
        base_url = "http{}://{}:{}{}".format(
            ssl, printer[CONF_HOST], printer[CONF_PORT], printer[CONF_PATH]
        )
        number_of_tools = printer[CONF_NUMBER_OF_TOOLS]
        bed = printer[CONF_BED]
        try:
            octoprint_api = Duet3dAPI(base_url, bed, number_of_tools)
            printers[base_url] = octoprint_api
            octoprint_api.get("heat")
            octoprint_api.get("job")
            octoprint_api.get("move")
            octoprint_api.get("state")
        except requests.exceptions.RequestException as conn_err:
            _LOGGER.error("Error setting up Duet3d API: %r", conn_err)
            continue

        sensors = printer[CONF_SENSORS][CONF_MONITORED_CONDITIONS]
        load_platform(
            hass,
            "sensor",
            DOMAIN,
            {"name": name, "base_url": base_url, "sensors": sensors},
            config,
        )
        b_sensors = printer[CONF_BINARY_SENSORS][CONF_MONITORED_CONDITIONS]
        load_platform(
            hass,
            "binary_sensor",
            DOMAIN,
            {"name": name, "base_url": base_url, "sensors": b_sensors},
            config,
        )
        success = True

    return success


class Duet3dAPI:
    """Simple JSON wrapper for Duet3D's API."""

    def __init__(self, api_url, bed, number_of_tools):
        """Initialize Duet3D API and set headers needed later."""
        self.api_url = api_url
        self.headers = {
            'Content-Type': 'application/json',
        }
        self.job_last_reading = [{}, 0.0]
        self.fans_last_reading = [{}, 0.0]
        self.move_last_reading = [{}, 0.0]
        self.heat_last_reading = [{}, 0.0]
        self.state_last_reading = [{}, 0.0]
        self.job_available = False
        self.heat_available = False
        self.move_available = False
        self.state_available = False
        self.available = False
        self.job_error_logged = False
        self.heat_error_logged = False
        self.move_error_logged = False
        self.state_error_logged = False
        self.bed = bed
        self.number_of_tools = number_of_tools

    def get_tools(self):
        """Get the list of tools that temperature is monitored on."""
        tools = []
        if self.number_of_tools > 0:
            # tools start at 1 bed is 0
            for tool_number in range(1, self.number_of_tools + 1):
                tools.append(tool_number)  # 'tool' + str(tool_number))
        if self.bed:
            tools.append("bed")
        if not self.bed and self.number_of_tools == 0:
            temps = self.heat_last_reading.get("temperature")
            if temps is not None:
                tools = temps.keys()
        return tools

    def get(self, endpoint):
        """Send a get request, and return the response as a dict."""
        # Only query the API every 30 seconds at most
        now = time.time()
        if endpoint == "job":
            last_time = self.job_last_reading[1]
            if now - last_time < MIN_INTERVAL:
                return self.job_last_reading[0]
        if endpoint == "heat":
            last_time = self.heat_last_reading[1]
            if now - last_time < MIN_INTERVAL:
                return self.heat_last_reading[0]
        if endpoint == "move":
            last_time = self.move_last_reading[1]
            if now - last_time < MIN_INTERVAL:
                return self.move_last_reading[0]
        if endpoint == "state":
            last_time = self.state_last_reading[1]
            if now - last_time < MIN_INTERVAL:
                return self.state_last_reading[0]

        url = self.api_url + "&key=" + endpoint
        url = url.replace("/&", "&")
        try:
            response = requests.get(url, headers=self.headers, timeout=2)
            response.raise_for_status()
            if endpoint == "job":
                self.job_last_reading[0] = response.json()
                self.job_last_reading[1] = time.time()
                self.job_available = True
            elif endpoint == "move":
                self.move_last_reading[0] = response.json()
                self.move_last_reading[1] = time.time()
                self.move_available = True
            elif endpoint == "heat":
                self.heat_last_reading[0] = response.json()
                self.heat_last_reading[1] = time.time()
                self.heat_available = True
            elif endpoint == "state":
                self.state_last_reading[0] = response.json()
                self.state_last_reading[1] = time.time()
                self.state_available = True
            self.available = self.state_available and self.job_available and self.move_available and self.heat_available
            if self.available:
                self.job_error_logged = False
                self.heat_error_logged = False
                self.move_error_logged = False
                self.state_error_logged = False
            return response.json()
        except Exception as conn_exc:  # pylint: disable=broad-except
            log_string = "Failed to update Duet3D status. " + "  Error: %s" % (
                conn_exc
            )
            # Only log the first failure
            log_string = "Endpoint: " + endpoint + " " + log_string
            if endpoint == "job":
                if not self.job_error_logged:
                    _LOGGER.error(log_string)
                    self.job_error_logged = True
                    self.job_available = False
            if endpoint == "heat":
                if not self.heat_error_logged:
                    _LOGGER.error(log_string)
                    self.heat_error_logged = True
                    self.heat_available = False
            if endpoint == "move":
                if not self.move_error_logged:
                    _LOGGER.error(log_string)
                    self.move_error_logged = True
                    self.move_available = False
            if endpoint == "state":
                if not self.state_error_logged:
                    _LOGGER.error(log_string)
                    self.state_error_logged = True
                    self.state_available = False

            self.available = False
            return None

    def update(self, sensor_type, end_point, group, tool=None):
        """Return the value for sensor_type from the provided endpoint."""
        _LOGGER.debug(
            "Updating API Duet3D sensor %r, Type: %s, End Point: %s, Group: %s, Tool: %s",
            self,
            sensor_type,
            end_point,
            group,
            tool,
        )
        response = self.get(end_point)
        if response is not None:
            return get_value_from_json(response, end_point, sensor_type, group, tool)
        return response


def get_value_from_json(json_dict, end_point, sensor_type, group, tool):
    """Return the value for sensor_type from the JSON."""
    if end_point == "heat":
        if tool == "bed":
            return json_dict["result"][group][0][sensor_type]
        else:
            return json_dict["result"][group][int(tool)][sensor_type]
    elif end_point == "move":
        return json_dict["result"][group][int(sensor_type)]["userPosition"]
    elif end_point == "job":
        if group == "fileName":
            return json_dict["result"]["file"][group] or json_dict["result"]["lastFileName"]
        elif group == "fractionPrinted":
            filesize = json_dict["result"]["file"]["size"]
            if filesize > 0:
                return 100.0 * json_dict["result"]["filePosition"] / filesize
            else:
                return 0
        elif group == "timesLeft":
            return json_dict["result"]["timesLeft"]["slicer"]
        elif group == "printDuration":
            return json_dict["result"]["duration"]
        else:
            return None  # HACK
    elif end_point == "state":
        return json_dict["result"]["status"]
    else:
        return None
