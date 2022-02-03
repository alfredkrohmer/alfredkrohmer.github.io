from typing import List
from bisect import bisect_left
from datetime import datetime, timedelta
import threading
import sys

import requests
import paho.mqtt.client as mqtt

openwb_url = 'http://192.168.1.123/connect.php'
openwb_query_interval_seconds = 1

openwb_status = {}

current_phases = 1
current_ampere = None
wait_time_phase_switch = timedelta(seconds=20)
last_phase_switch = datetime.now() - wait_time_phase_switch
phase_correction_timer = None

possible_milliamperes = range(6000, 16000 + 1, 100)
possible_watts_1ph = sorted([int(230*a/1000) for a in possible_milliamperes])
possible_watts_3ph = sorted([int(3*230*a/1000) for a in possible_milliamperes])
cutoff_on = possible_watts_1ph[0]
cutoff_limit = possible_watts_3ph[-1]
cutoff_1ph_3ph = possible_watts_3ph[0]

client = mqtt.Client()

def openwb_query_status() -> None:
    global client
    global openwb_status

    try:
        openwb_status = requests.get(openwb_url).json()
        if not isinstance(openwb_status, dict):
            raise ValueError(f"expected response to be of type dict: {openwb_status}")
    except Exception as exc:
        print(f"Failed: to retrieve status from OpenWB: {exc}")
        sys.exit(1)

    for key in (
        'power_all',
        'imported',
        'exported',
        'plug_state',
        'charge_state',
        'phases_actual',
        'phases_target',
        'phases_in_use',
        'offered_current',
    ):
        value = openwb_status.get(key)
        if value is None:
            continue
        if isinstance(value, bool):
            value = '1' if value else '0'
        if key in ('exported', 'imported') and value in (0, ''):
            continue
        try:
            client.publish(f"openwb/status/{key}", value)
        except Exception as exc:    
            print(f"Failed: to publish status to MQTT: {exc}")
            sys.exit(1)

    for key in [
        'powers',
        'currents',
    ]:
        values = openwb_status.get(key, [])
        if not isinstance(values, list) or len(values) < 3:
            print(f"Unexpected value for {key}: {values}")
            continue
        for i in (1, 2, 3):
            try:
                client.publish(f"openwb/status/{key}_{i}", values[i-1])
            except Exception as exc:    
                print(f"Failed: to publish status to MQTT: {exc}")
                sys.exit(1)

def get_closest_ampere(possible_watts: List[int], target_power: int, phases: int) -> float:
    idx = bisect_left(possible_watts, target_power)
    idx = min(idx, len(possible_watts) - 1)
    closest_power = possible_watts[idx]
    closest_ampere = closest_power / 230 / phases
    return closest_ampere

def set_target_power(target_power: int) -> None:
    global current_phases
    global current_ampere
    global last_phase_switch
    global wait_time_phase_switch
    global phase_correction_timer

    if phase_correction_timer is not None:
        phase_correction_timer.cancel()
        phase_correction_timer = None

    print(f"Request to set target power to: {target_power} W")
    
    if target_power > cutoff_limit:
        print(f"  ! Limiting power to {cutoff_limit} W")
        target_power = cutoff_limit

    if target_power < cutoff_on:
        target_ampere = 0
        target_phases = current_phases

    else:
        target_phases = 1 if target_power < cutoff_1ph_3ph else 3
        print(f"  Current / optimal number of phases: {current_phases} / {target_phases}")

        if target_phases != current_phases:
            now = datetime.now()
            if now - last_phase_switch < wait_time_phase_switch:
                print(f"  ! Wait time for switching number of phases not yet expired, keeping number of phases at {current_phases}")
                target_phases = current_phases
                phase_correction_timer = threading.Timer(wait_time_phase_switch.seconds - (now - last_phase_switch).seconds, lambda: set_target_power(target_power)).start()

        possible_watts = possible_watts_1ph if target_phases == 1 else possible_watts_3ph
        target_ampere = get_closest_ampere(possible_watts, target_power, target_phases)
        target_ampere = round(target_ampere, 1)

    print("  Setting OpenWB parameters:")
    print(f"    Effective power: {round(target_phases * target_ampere * 230)} W")

    if target_ampere > 0 and target_phases != current_phases:
        print(f"    Phases:          {target_phases}")
        current_phases = target_phases
        last_phase_switch = datetime.now()
        try:
            requests.post(openwb_url, data={'phasetarget': target_phases})
        except Exception as exc:
            print(f"Failed: to set number of phases on OpenWB: {exc}")
            sys.exit(1)

    if target_ampere != current_ampere:
        print(f"    Current:         {target_ampere} A")
        current_ampere = target_ampere
        try:
            requests.post(openwb_url, data={'ampere': target_ampere})
        except Exception as exc:
            print(f"Failed: to set target ampere on OpenWB: {exc}")
            sys.exit(1)

def on_connect(client, userdata, flags, rc):
    print("Connected to MQTT broker")
    client.subscribe('openwb/target-power-in-watt')

def on_message(client, userdata, msg):
    if msg.topic == 'openwb/target-power-in-watt':
        target_power = 0
        try:
            target_power = int(msg.payload)
        except ValueError as exc:
            print(f"Failed to read target power in watt from MQTT payload: {exc}")
            return
        set_target_power(target_power)

def openwb_query_status_with_timer() -> None:
    threading.Timer(openwb_query_interval_seconds, openwb_query_status_with_timer).start()
    openwb_query_status()

openwb_query_status_with_timer()

current_phases = openwb_status['phases_target']
current_ampere = openwb_status['offered_current']

client.on_connect = on_connect
client.on_message = on_message

client.connect("my-mqtt-broker.example.com", 1883, 60)

client.loop_forever()
