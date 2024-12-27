'''
WyzeSense to MQTT Gateway
'''
import json
import logging
import logging.config
import logging.handlers
import os
import shutil
import subprocess
import yaml
import time
from datetime import date

import paho.mqtt.client as mqtt
import wyzesense
from retrying import retry


# Configuration File Locations
CONFIG_PATH = "config"
SAMPLES_PATH = "samples"
MAIN_CONFIG_FILE = "config.yaml"
LOGGING_CONFIG_FILE = "logging.yaml"
SENSORS_CONFIG_FILE = "sensors.yaml"

_DEVICE_MAPPING = {
    'motion': {
        'class': 'motion',
        'on': 'active',
        'off': 'inactive',
        'model': 'Wyze Sense Motion Sensor',
        'timeout_period': 8,
    },
    'motionv2': {
        'class': 'motion',
        'on': 'active',
        'off': 'inactive',
        'model': 'Wyze Sense Motion V2 Sensor',
        'timeout_period': 4,
    },
    'switch': {
        'class': 'openning',
        'on': 'open',
        'off': 'closed',
        'model': 'Wyze Sense Door/Window Sensor',
        'timeout_period': 8,
    },
    'switchv2': {
        'class': 'openning',
        'on': 'open',
        'off': 'closed',
        'model': 'Wyze Sense Door/Window V2 Sensor',
        'timeout_period': 4,
    },
    'leak': {
        'class': 'moisture',
        'on': 'wet',
        'off': 'dry',
        'model': 'Wyze Sense Leak Sensor',
        'timeout_period': 4,
    },
    'climate': {
        'class': 'temperature',
        'model': 'Wyze Climate Sensor',
        'timeout_period': 4,
    }
}

_BINARY_SENSORS = (
    'motion',
    'motionv2',
    'switch',
    'switchv2',
    'leak'
)

INITIALIZED = False
SENSORS = {}

# Read data from YAML file
def read_yaml_file(filename):
    try:
        with open(filename) as yaml_file:
            data = yaml.safe_load(yaml_file)
            return data
    except IOError as error:
        if (LOGGER is None):
            print(f"File error: {str(error)}")
        else:
            LOGGER.error(f"File error: {str(error)}")


# Write data to YAML file
def write_yaml_file(filename, data):
    try:
        with open(filename, 'w') as yaml_file:
            yaml_file.write(yaml.safe_dump(data))
    except IOError as error:
        if (LOGGER is None):
            print(f"File error: {str(error)}")
        else:
            LOGGER.error(f"File error: {str(error)}")


# Initialize logging
def init_logging():
    global LOGGER
    if (not os.path.isfile(os.path.join(CONFIG_PATH, LOGGING_CONFIG_FILE))):
        print("Copying default logging config file...")
        try:
            shutil.copy2(os.path.join(SAMPLES_PATH, LOGGING_CONFIG_FILE), CONFIG_PATH)
        except IOError as error:
            print(f"Unable to copy default logging config file. {str(error)}")
    logging_config = read_yaml_file(os.path.join(CONFIG_PATH, LOGGING_CONFIG_FILE))

    log_path = os.path.dirname(logging_config['handlers']['file']['filename'])
    try:
        if (not os.path.exists(log_path)):
            os.makedirs(log_path)
    except IOError:
        print("Unable to create log folder")
    logging.config.dictConfig(logging_config)
    LOGGER = logging.getLogger("wyzesense2mqtt")
    LOGGER.info("Logging initialized...")


# Initialize configuration
def init_config():
    global CONFIG
    LOGGER.info("Initializing configuration...")

    # load base config - allows for auto addition of new settings
    if (os.path.isfile(os.path.join(SAMPLES_PATH, MAIN_CONFIG_FILE))):
        CONFIG = read_yaml_file(os.path.join(SAMPLES_PATH, MAIN_CONFIG_FILE))

    # load user config over base
    if (os.path.isfile(os.path.join(CONFIG_PATH, MAIN_CONFIG_FILE))):
        user_config = read_yaml_file(os.path.join(CONFIG_PATH, MAIN_CONFIG_FILE))
        CONFIG.update(user_config)

    # fail on no config
    if (CONFIG is None):
        LOGGER.error(f"Failed to load configuration, please configure.")
        exit(1)

    # write updated config file if needed
    if (CONFIG != user_config):
        LOGGER.info("Writing updated config file")
        write_yaml_file(os.path.join(CONFIG_PATH, MAIN_CONFIG_FILE), CONFIG)


# Initialize MQTT client connection
def init_mqtt_client():
    global MQTT_CLIENT, CONFIG, LOGGER
    # Used for alternate MQTT connection method
    mqtt.Client.connected_flag = False

    # Configure MQTT Client
    MQTT_CLIENT = mqtt.Client(client_id=CONFIG['mqtt_client_id'], clean_session=CONFIG['mqtt_clean_session'])
    MQTT_CLIENT.username_pw_set(username=CONFIG['mqtt_username'], password=CONFIG['mqtt_password'])
    MQTT_CLIENT.reconnect_delay_set(min_delay=1, max_delay=120)
    MQTT_CLIENT.on_connect = on_connect
    MQTT_CLIENT.on_disconnect = on_disconnect
    MQTT_CLIENT.on_message = on_message
    MQTT_CLIENT.enable_logger(LOGGER)

    # Connect to MQTT
    LOGGER.info(f"Connecting to MQTT host {CONFIG['mqtt_host']}")
    MQTT_CLIENT.connect_async(CONFIG['mqtt_host'], port=CONFIG['mqtt_port'], keepalive=CONFIG['mqtt_keepalive'])

    # Used for alternate MQTT connection method
    MQTT_CLIENT.loop_start()
    while (not MQTT_CLIENT.connected_flag):
        time.sleep(1)

    # Make sure the service stays marked as offline until everything is initialized
    mqtt_publish(f"{CONFIG['self_topic_root']}/status", "offline", is_json=False)

# Retry forever on IO Error
def retry_if_io_error(exception):
    return isinstance(exception, IOError)


# Initialize USB dongle
@retry(wait_exponential_multiplier=1000, wait_exponential_max=30000, retry_on_exception=retry_if_io_error)
def init_wyzesense_dongle():
    global WYZESENSE_DONGLE, CONFIG, LOGGER
    if (CONFIG['usb_dongle'].lower() == "auto"):
        device_list = subprocess.check_output(["ls", "-la", "/sys/class/hidraw"]).decode("utf-8").lower()
        for line in device_list.split("\n"):
            if (("e024" in line) and ("1a86" in line)):
                for device_name in line.split(" "):
                    if ("hidraw" in device_name):
                        CONFIG['usb_dongle'] = f"/dev/{device_name}"
                        break

    LOGGER.info(f"Connecting to dongle {CONFIG['usb_dongle']}")
    try:
        WYZESENSE_DONGLE = wyzesense.Open(CONFIG['usb_dongle'], on_event, LOGGER)
        LOGGER.info(f"Dongle {CONFIG['usb_dongle']}: ["
                    f" MAC: {WYZESENSE_DONGLE.MAC},"
                    f" VER: {WYZESENSE_DONGLE.Version},"
                    f" ENR: {WYZESENSE_DONGLE.ENR}]")
    except IOError as error:
        LOGGER.error(f"No device found on path {CONFIG['usb_dongle']}: {str(error)}")


# Initialize sensor configuration
def init_sensors(wait=True):
    # Initialize sensor dictionary
    global SENSORS

    sensor_config_updated = False
    # Load config file
    if (os.path.isfile(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE))):
        LOGGER.info("Reading sensors configuration...")
        SENSORS = read_yaml_file(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE))
    else:
        LOGGER.warning("No sensors config file found.")
        sensor_config_updated = True

    # Check config against linked sensors
    LOGGER.info("Checking sensors against dongle list...")
    result = WYZESENSE_DONGLE.List()

    for sensor_mac in result:
        if not valid_sensor_mac(sensor_mac):
            LOGGER.warning(f"Unpairing bad MAC: {sensor_mac}")
            try:
                WYZESENSE_DONGLE.Delete(sensor_mac)
            except TimeoutError:
                LOGGER.error("Timeout removing bad mac")
            SENSORS.pop(sensor_mac)
            sensor_config_updated = True
            continue

        if sensor_mac not in SENSORS:
            add_sensor_to_config(sensor_mac)
            sensor_config_updated = True
            LOGGER.warning(f"Linked sensor with mac {sensor_mac} automatically added to sensors configuration")
            LOGGER.warning(f"Please update sensor configuration file {os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE)} restart the service/reload the sensors")
        
        s = SENSORS[sensor_mac]
        if 'timestamp' not in s:
            s['timestamp'] = time.time()
            s['online'] = True

        if 'mac' not in s:
            s['mac'] = sensor_mac

        if CONFIG['hass_discovery']:
            send_discovery_topics(sensor_mac, wait=wait)

    # Save sensors file if didn't exist
    if sensor_config_updated:
        LOGGER.info("Writing Sensors Config File")
        write_yaml_file(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE), SENSORS)

# Validate sensor MAC
def valid_sensor_mac(sensor_mac):
    invalid_mac_list = [
        "00000000",
        "\0\0\0\0\0\0\0\0",
        "\x00\x00\x00\x00\x00\x00\x00\x00"
    ]

    if len(sensor_mac) != 8:
        return False
    
    if sensor_mac in invalid_mac_list:
        return False

    return True

# Add sensor to config
def add_sensor_to_config(sensor_mac, sensor_type="unknown", sensor_version="unknown"):
    global SENSORS, SENSORS_STATE
    LOGGER.info(f"Adding sensor to config: {sensor_mac}")
    SENSORS[sensor_mac] = {
        'name': f"Wyze Sense {sensor_mac}",
        'type': sensor_type,
        'mac': sensor_mac,
        'sw_version': sensor_version,
    }

# Delete sensor from config
def delete_sensor_from_config(sensor_mac):
    global SENSORS
    LOGGER.info(f"Deleting sensor from config: {sensor_mac}")
    SENSORS.pop(sensor_mac)
    write_yaml_file(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE), SENSORS)

# Publish MQTT topic
def mqtt_publish(mqtt_topic, mqtt_payload, is_json=True, wait=True):
    global MQTT_CLIENT, CONFIG
    payload = json.dumps(mqtt_payload) if is_json else mqtt_payload
    LOGGER.debug(f"Publishing, {mqtt_topic=}, {payload=}")
    mqtt_message_info = MQTT_CLIENT.publish(
        mqtt_topic,
        payload=payload,
        qos=CONFIG['mqtt_qos'],
        retain=CONFIG['mqtt_retain']
    )
    if (mqtt_message_info.rc == mqtt.MQTT_ERR_SUCCESS):
        if (wait):
            mqtt_message_info.wait_for_publish(2)
        return

    LOGGER.warning(f"MQTT publish error: {mqtt.error_string(mqtt_message_info.rc)}")


# Send discovery topics
def send_discovery_topics(sensor_mac, wait=True):
    global SENSORS, CONFIG

    LOGGER.info(f"Publishing discovery topics for {sensor_mac}")

    sensor_type = SENSORS[sensor_mac]['type']
    if sensor_type not in _DEVICE_MAPPING:
        LOGGER.error(f'Unsupported sensor type: f{sensor_type}')
        return

    attr = {
        "sw_version": "unknown",
    }

    attr.update(SENSORS[sensor_mac])
    attr.update(_DEVICE_MAPPING[sensor_type])

    mac_topic = f"{CONFIG['self_topic_root']}/{sensor_mac}"

    entity_payloads = {}
    if sensor_type in _BINARY_SENSORS:
        entity_payloads['state'] = {
            'name': None,
            'device_class': attr['class'],
            'payload_on': attr['on'],
            'payload_off': attr['off'],
            'json_attributes_topic': mac_topic,
            'device' : {
               'identifiers': [f"wyzesense_{sensor_mac}", sensor_mac],
               'manufacturer': "Wyze",
               'model': attr['model'],
               'name': attr['name'],
               'sw_version': attr['sw_version'],
               'via_device': "wyzesense2mqtt"
            }
        }
    else:
        entity_payloads['temperature'] = {
            'name': 'Temperature',
            'device_class':'temperature',
            'state_class':'measurement',
            'unit_of_measurement': 'C',
            'json_attributes_topic': mac_topic,
            'device' : {
               'identifiers': [f"wyzesense_{sensor_mac}", sensor_mac],
               'manufacturer': "Wyze",
               'model': attr['model'],
               'name': attr['name'],
               'sw_version': attr['sw_version'],
               'via_device': "wyzesense2mqtt"
            }
        }

        entity_payloads['humidity'] = {
            'name': 'Humidity',
            'device_class':'humidity',
            'state_class':'measurement',
            'unit_of_measurement': '%',
            'json_attributes_topic': mac_topic,
            'device' : {
               'identifiers': [f"wyzesense_{sensor_mac}", sensor_mac],
               'name': attr['name'],
            }
        }

    entity_payloads['signal_strength'] = {
        'device_class': "signal_strength",
        'state_class': "measurement",
        'unit_of_measurement': "%",
        'entity_category': "diagnostic",
        'device' : {
            'identifiers': [f"wyzesense_{sensor_mac}", sensor_mac],
            'name': attr['name']
        }
    }

    entity_payloads['battery'] = {
        'device_class': "battery",
        'state_class': "measurement",
        'unit_of_measurement': "%",
        'entity_category': "diagnostic",
        'device' : {
            'identifiers': [f"wyzesense_{sensor_mac}", sensor_mac],
            'name': attr['name']
        }
    }

    availability_topics = [
        { 'topic': f"{CONFIG['self_topic_root']}/{sensor_mac}/status" },
        { 'topic': f"{CONFIG['self_topic_root']}/status" }
    ]

    for entity, entity_payload in entity_payloads.items():
        entity_payload['value_template'] = f"{{{{ value_json.{entity} }}}}"
        entity_payload['unique_id'] = f"wyzesense_{sensor_mac}_{entity}"
        entity_payload['state_topic'] = mac_topic
        entity_payload['availability'] = availability_topics
        entity_payload['availability_mode'] = "all"
        entity_payload['platform'] = "mqtt"
#        entity_payload['device'] = device_payload

        entity_topic = f"{CONFIG['hass_topic_root']}/{'binary_sensor' if (entity == 'state') else 'sensor'}/wyzesense_{sensor_mac}/{entity}/config"
        mqtt_publish(entity_topic, entity_payload, wait=wait)

        LOGGER.info(f"  {entity_topic}")
        LOGGER.info(f"  {json.dumps(entity_payload)}")
    mqtt_publish(f"{CONFIG['self_topic_root']}/{sensor_mac}/status", "online" if SENSORS[sensor_mac]['online'] else "offline", is_json=False, wait=wait)

# Clear any retained topics in MQTT
def clear_topics(sensor_mac, wait=True):
    global CONFIG
    LOGGER.info("Clearing sensor topics")
    mqtt_publish(f"{CONFIG['self_topic_root']}/{sensor_mac}/status", None, wait=wait)
    mqtt_publish(f"{CONFIG['self_topic_root']}/{sensor_mac}", None, wait=wait)

    # clear discovery topics if configured
    if not CONFIG['hass_discovery']:
        return

    sensor_type = SENSORS[sensor_mac]['type']
    if sensor_type not in _DEVICE_MAPPING:
        LOGGER.error(f'Unsupported sensor type: f{sensor_type}')
        return

    entity_types = ['signal_strength', 'battery']
    if sensor_type in _BINARY_SENSORS:
        entity_types.add('state')
    else:
        entity_types.extend(['temperature', 'humidity'])

    for entity_type in entity_types:
        component = "binary_sensor" if entity_type == "state" else "sensor"
        mqtt_publish(f"{CONFIG['hass_topic_root']}/{component}/wyzesense_{sensor_mac}/{entity_type}/config", None, wait=wait)
        mqtt_publish(f"{CONFIG['hass_topic_root']}/{component}/wyzesense_{sensor_mac}/{entity_type}", None, wait=wait)
        mqtt_publish(f"{CONFIG['hass_topic_root']}/{component}/wyzesense_{sensor_mac}", None, wait=wait)

def on_connect(MQTT_CLIENT, userdata, flags, rc):
    global CONFIG
    if rc == mqtt.MQTT_ERR_SUCCESS:
        MQTT_CLIENT.subscribe(
            [(SCAN_TOPIC, CONFIG['mqtt_qos']),
             (REMOVE_TOPIC, CONFIG['mqtt_qos']),
             (RELOAD_TOPIC, CONFIG['mqtt_qos'])]
        )
        MQTT_CLIENT.message_callback_add(SCAN_TOPIC, on_message_scan)
        MQTT_CLIENT.message_callback_add(REMOVE_TOPIC, on_message_remove)
        MQTT_CLIENT.message_callback_add(RELOAD_TOPIC, on_message_reload)
        MQTT_CLIENT.connected_flag = True
        LOGGER.info(f"Connected to MQTT: {mqtt.error_string(rc)}")
    else:
        LOGGER.warning(f"Connection to MQTT failed: {mqtt.error_string(rc)}")


def on_disconnect(MQTT_CLIENT, userdata, rc):
    MQTT_CLIENT.message_callback_remove(SCAN_TOPIC)
    MQTT_CLIENT.message_callback_remove(REMOVE_TOPIC)
    MQTT_CLIENT.message_callback_remove(RELOAD_TOPIC)
    MQTT_CLIENT.connected_flag = False
    LOGGER.info(f"Disconnected from MQTT: {mqtt.error_string(rc)}")


# We don't handle any additional messages from MQTT, just log them
def on_message(MQTT_CLIENT, userdata, msg):
    LOGGER.info(f"{msg.topic}: {str(msg.payload)}")


# Process message to scan for new sensors
def on_message_scan(MQTT_CLIENT, userdata, msg):
    global SENSORS, CONFIG
    result = None
    LOGGER.info(f"In on_message_scan: {msg.payload.decode()}")

    # The scan will do a couple additional calls even after the new sensor is found
    # These calls may time out, so catch it early so we can still add the sensor properly
    try:
        result = WYZESENSE_DONGLE.Scan()
    except TimeoutError:
        pass

    if not result:
        LOGGER.info("No new sensor found")
        return
        
    LOGGER.info(f"Scan result: {result}")
    sensor_mac, sensor_type, sensor_version = result
    if not valid_sensor(result):
        LOGGER.info(f"Invalid sensor found: {sensor_mac}")
        return
        
    if sensor_mac not in SENSORS:
        add_sensor_to_config(sensor_mac, sensor_type, sensor_version)
        if(CONFIG['hass_discovery']):
            # We are in a mqtt callback, so can not wait for new messages to publish
            send_discovery_topics(sensor_mac, wait=False)

# Process message to remove sensor
def on_message_remove(MQTT_CLIENT, userdata, msg):
    sensor_mac = msg.payload.decode()
    LOGGER.info(f"In on_message_remove: {sensor_mac}")

    if (valid_sensor_mac(sensor_mac)):
        # Deleting from the dongle may timeout, but we still need to do
        # the rest so catch it early
        try:
            WYZESENSE_DONGLE.Delete(sensor_mac)
        except TimeoutError:
            pass
        # We are in a mqtt callback so cannot wait for new messages to publish
        clear_topics(sensor_mac, wait=False)
        delete_sensor_from_config(sensor_mac)
    else:
        LOGGER.info(f"Invalid mac address: {sensor_mac}")


# Process message to reload sensors
def on_message_reload(MQTT_CLIENT, userdata, msg):
    LOGGER.info(f"In on_message_reload: {msg.payload.decode()}")

    # Save off the last known state so we don't overwrite new state by re-reading the previously saved file
    LOGGER.info("Writing Sensors Config File")
    write_yaml_file(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE), SENSORS)

    # We are in a mqtt callback so cannot wait for new messages to publish
    init_sensors(wait=False)

# Process event
def on_event(dongle, e):
    global SENSORS

    if not INITIALIZED:
        return

    LOGGER.info(f"State event data: {e}")
    if not valid_sensor_mac(e.mac):
        LOGGER.warning(f"!Invalid MAC detected")
        return

    if e.mac not in SENSORS:
        add_sensor_to_config(e.mac, None, None)
        if(CONFIG['hass_discovery']):
            send_discovery_topics(e.mac)
            LOGGER.warning(f"Linked sensor with mac {e.mac} automatically added to sensors configuration")
            LOGGER.warning(f"Please update sensor configuration file {os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE)} restart the service/reload the sensors")

    s = SENSORS[e.mac]
    old_type = s['type']
    s.update(vars(e))

    if s["type"] != old_type:
        #TODO: Update sensor config file
        if(CONFIG['hass_discovery']):
            send_discovery_topics(e.mac)
            LOGGER.warning(f"Linked sensor with mac {e.mac} automatically added to sensors configuration")
            LOGGER.warning(f"Please update sensor configuration file {os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE)} restart the service/reload the sensors")

    mqtt_publish(f"{CONFIG['self_topic_root']}/{e.mac}/status", "online", is_json=False)

    # Set back online if it was offline
    if not s['online']:
        s['online'] = True
        LOGGER.info(f"{e.mac} is back online!")

    if e.event not in ("alarm", "status"):
        LOGGER.info(f"Unknown event: {e}")
        return

    payload = {}
    payload.update(s)

    LOGGER.info(f"{CONFIG['self_topic_root']}/{e.mac}")
    LOGGER.info(payload)
    mqtt_publish(f"{CONFIG['self_topic_root']}/{e.mac}", payload)


def Stop():
    # Stop the dongle first, letting this thread finish anything it might be busy doing, like handling an event
    WYZESENSE_DONGLE.Stop()

    mqtt_publish(f"{CONFIG['self_topic_root']}/status", "offline", is_json=False)

    # All event handling should now be done, close the mqtt connection
    MQTT_CLIENT.loop_stop()
    MQTT_CLIENT.disconnect()

    # Save off the last known state
    LOGGER.info("Writing Sensors Config File")
    write_yaml_file(os.path.join(CONFIG_PATH, SENSORS_CONFIG_FILE), SENSORS)

    LOGGER.info("********************************** Wyzesense2mqtt stopped ***********************************")


if __name__ == "__main__":
    # Initialize logging
    init_logging()

    print("********************************** Wyzesense2mqtt starting **********************************")

    # Initialize configuration
    init_config()

    # Set MQTT Topics
    SCAN_TOPIC = f"{CONFIG['self_topic_root']}/scan"
    REMOVE_TOPIC = f"{CONFIG['self_topic_root']}/remove"
    RELOAD_TOPIC = f"{CONFIG['self_topic_root']}/reload"

    # Initialize MQTT client connection
    init_mqtt_client()

    # Initialize USB dongle
    init_wyzesense_dongle()

    # Initialize sensor configuration
    init_sensors()

    # All initialized now, so set the flag to allow message event to be processed
    INITIALIZED = True

    # And mark the service as online
    mqtt_publish(f"{CONFIG['self_topic_root']}/status", "online", is_json=False)

    # Loop forever until keyboard interrupt or SIGINT
    try:
        while True:
            time.sleep(5)
            # Check if there is any exceptions in the dongle thread
            WYZESENSE_DONGLE.CheckError()

            if not MQTT_CLIENT.connected_flag:
                LOGGER.warning("Reconnecting MQTT...")
                MQTT_CLIENT.reconnect()

            if MQTT_CLIENT.connected_flag:
                mqtt_publish(f"{CONFIG['self_topic_root']}/status", "online", is_json=False)

            # Check for availability of the devices
            now = time.time()
            for mac, sensor in SENSORS.items():
                sensor_type = sensor['type']
                if sensor_type not in _DEVICE_MAPPING:
                    LOGGER.warning('Invalid sensor type:{sensor_type}')
                    continue
                
                if not sensor['online']:
                    continue

                info = _DEVICE_MAPPING[sensor_type]
                timeout_period = info['timeout_period'] * 3600

                if now - sensor['timestamp'] > timeout_period:
                    mqtt_publish(f"{CONFIG['self_topic_root']}/{mac}/status", "offline", is_json=False)
                    LOGGER.warning(f"{mac} has gone offline!")
                    sensor['online'] = False
    except KeyboardInterrupt:
        LOGGER.warning("User interrupted")
    except Exception as e:
        LOGGER.error("An error occurred", exc_info=True)
    finally:
        Stop()
