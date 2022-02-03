---
layout: post
title: Charging your electric car with excess solar power
tags: photovoltaics electric-vehicle solar homeassistant
---

## Motivation

If you have solar panels on the roof of your house and a wallbox in your garage for charging your eletric car, it makes a lot of sense to try and charge as much solar power as possible directly into the car's battery and to consume the least possible amount from the grid.

A possible system to achieve this would need to have the following abilities:
* query the power data of your solar installation and house usage
* calculate the surplus power that you want to charge into the car's battery
* control the wallbox in a way that the car charges more or less exactly the surplus power, not more and not less

Some wallboxes already have the possibility to query the power data from the solar inverter (e.g. the control software of the [OpenWB](https://openwb.de)) and control the charging process in a way that only the surplus solar power will be charged into the car's battery.

<!--more-->

For charging my Hyundai Ioniq 5 I ordered a [OpenWB Pro](https://openwb.de/shop/?product=openwb-pro) instead of one the standard models because it supports HLC (High-Level Communication) and implements ISO 15118. This means:
* it is ready for V2G (vehicle-to-grid, powering your house with the battery of the car)
  * caveat: technical specifications and regulations for V2G are not ready yet, no car on the market supports it (yet) and hence this feature is still disabled
* it can potentially query the SoC (State of Charge) of the car battery via the charging cable; this would avoid the necessity to query this data via the cloud of the manufacturer (Hyundai BlueLink in my case)
  * caveat: while high power chargers can usually query the SoC "on the DC side", apparently no car supports this "on the AC side" yet

I hope for both features to be available at some point in time in the Hyundai Ioniq 5 after a software update.

The downside of chosing the OpenWB Pro over the standard models is that it doesn't come with any control software. At the time of writing, it can only be controlled with a HTTP API on `http://<IP>/connect.php`. (Support for controlling the charging process via OCPP (Open Charging Point Protocol) is supposed to come later.) However, it comes with a minimalistic web interface that shows some data about the charging process and debug logs:

![OpenWB Pro web interface screenshot](/assets/2022-02-03-ev-solar-surplus-charging/openwb-pro-web-ui.png)

As the OpenWB Pro doesn't have any control software for controling the charging process according to surplus solar power, I decided to implement this myself.

I solved the individual required abilities of such a control system with Home Assistant, MQTT and a bit of Python:
* all the data about power production and usage from the my LG Home ESS inverter is queried with [pyess](https://github.com/gluap/pyess) and published to Home Assistant via MQTT
* a `template` sensor in Home Assistant calculates the surplus solar power with quite a few knobs that can be tuned to one's desire
* a Python script to control the charging power that the wallbox offers to the car

## Controlling the OpenWB with the HTTP API

Everything is controlled via the single HTTP endpoint `http://<IP>/connect.php`.
* A `GET` request to this URL yields a JSON blob that contains status information:
```bash
$ curl http://<IP>/connect.php | jq
{
  "date": "2022:02:03-18:16:13",
  "timestamp": 1643912173,
  "powers": [
    0,
    0,
    0
  ],
  "power_all": 0,
  "currents": [
    0,
    0,
    0
  ],
  "imported": 14964,
  "exported": 0,
  "plug_state": true,
  "charge_state": false,
  "phases_actual": 0,
  "phases_target": 1,
  "phases_in_use": 3,
  "offered_current": 0,
  "evse_signaling": "basic iec61851",
  "v2g_ready": 0,
  "vehicle_id": "--",
  "serial": "<omitted>"
}
```

* A `POST` request to this URL with form data can control the following:
  * `ampere=9.6`: maximum offered current (`offered_current` in the status), value in ampere with an optional decimal point and one optimal decimal digit
  * `phasetarget=3`: number of phases to use (`phases_target` in the status)
  * `update=1`: triggers an update of the firmware (software?) on the device; look out for debug logs in the web interface afterwards
  * it's **not** possible to set multiple values at once (only the first provided value will be actually set)

### Setting the target charging power

That means that we can't directly set the target charging power here. For any given target power we need to calculate how many phases to use and how much current to offer, with the following restrictions:
* number of phases can be either 1 or 3
* offered current needs to be either 0 or between 6 and 16 ampere
  * 6 ampere is the lower limit that the charging protocol allows
  * 16 ampere is the upper limit that the circuit breakers in my installation (and the configuration of the wallbox and legal stuff...) allow

This yields two possible, non-overlapping power ranges:
* 1380 W (230 V * 6 A) - 3680 W (230 V * 16 A)
* 4140 W (3 * 230 V * 6 A) - 11040 W (230 V * 16 A)

and the following calculations for number of phases and offered current depending on the target power:
* target power below 1380 W: turn off (offerend current: 0 A)
* target power between 1380 W and 3680 W: use 1 phase, offering current: (Power in Watt / 230 V)
* target power between 3680 W and 4140 W: handle like 3680 W
* target power between 4140 W and 11040 W: use 3 phases, offering current: (Power in Watt / 230 V / 3)

## Controlling the charging power via MQTT

I created a small [Python script](/assets/2022-02-03-ev-solar-surplus-charging/openwb-mqtt.py) that receives the target charging power value via MQTT at `openwb/target-power-in-watt` and sets the control values (number of phases to use and the offered current) on the wallbox. It also publishes most of the values of the status query via MQTT at `openwb/status/<key>`.

It contains a bunch of safety and reliability features:
* powers below 1380 W will result in setting the offered current to 0 A
* powers above 11040 W will be limited to 11040 W
* it will only call the wallbox API if one of the control values actually changes compared to the last value that was set
* it introduces a wait time of 30 seconds when switching phases from 1 to 3 or from 3 to 1 and back

Drop this script anywhere you like, adjust the parameters, OpenWB Pro URL and MQTT connection settings to your environment and needs and start it e.g. as a systemd service (you can start it as a user instead of root since it doesn't need any systems permissions).

You can test it by publishing target charging power values and look at the status updates via MQTT and by having a look at the log output of the script itself.

## Calculate the solar surplus power and control the charging power via Home Assistant

This assumes that the values for
* power used by the wallbox (`sensor.openwb_power`; get this from status messages published via MQTT by the Python script)
* power generated by the solar panels (`sensor.pv_power_total`)
* SoC (in %) of the solar battery coupled to the inverter (`sensor.pv_battery_soc`)
* power used in the house (`sensor.house_power_usage`; this needs to include the `openwb_power`)

are already available as sensor values in Home Assistant.

### Fine-tuned control over the charging process

We will add the following "knobs" (automation helpers) in Home Assistant to be able to fine tune the charging behavior:
* `input_select.ioniq_5_maximum_charging_power`: with this slider, the maximum charging power for any operation mode can be configured 
* `input_boolean.ioniq_5_charge_surplus`: if this switch is not turned on, no solar surplus charging is happening and instead the configured charging power is offered to the car (see previous point)
* `input_boolean.ioniq_5_charge_only_on_limited_grid_feed`: if this switch is turned on, the charging only starts if there is enough solar power to saturate the grid feed-in limit (in my case that's 50 % of the peak power of 8.2 kW of my installation, so 4.1 kW)
* `input_boolean.ioniq_5_charge_when_solar_battery_empty`: if this switch is turned on, the charging process will be started if there is no solar power and the solar battery is empty; hence power will be drawn from the grid (this can be useful in the winter to collect some surplus energy over the day but still fully charge over night from the grid once the solar battery is empty)
* `input_boolean.ioniq_5_charge_prefer_solar_battery`: if this switch is turned on, the charging process only starts if there is the solar battery is full or if there is enough solar power to sature the charging power limit of the battery (in my case that's 5 kW);
* `input_datetime.ioniq_5_charge_prefer_solar_battery_starting`: this configures the clock time starting a which the prefered charging of the solar battery (see previous point) becomes active
* `input_number.ioniq_5_charge_prefer_solar_battery_up_to`: this configures the power that will be dedicated to the solar battery if it's charged preferably (can be used to dedicate a lower power than the charging power limit to the solar battery)

### Calculating the surplus solar power

Put this in your `configuration.yaml`:

{% raw  %}
```yaml
template:
- trigger:
  - platform: state
    entity_id:
    - input_select.ioniq_5_maximum_charging_power
    - input_boolean.ioniq_5_charge_surplus
    - input_boolean.ioniq_5_charge_prefer_solar_battery
    - input_datetime.ioniq_5_charge_prefer_solar_battery_starting
    - input_number.ioniq_5_charge_prefer_solar_battery_up_to
    - input_boolean.ioniq_5_charge_only_on_limited_grid_feed
    - input_boolean.ioniq_5_charge_when_solar_battery_empty
    - sensor.pv_power_total
    - sensor.pv_battery_soc
    - sensor.house_power_usage
    to: ~
  - platform: time
    at: input_datetime.ioniq_5_charge_prefer_solar_battery_starting
  sensor:
  - name: Ioniq 5 Target Charging Power
    icon: mdi:ev-station
    device_class: power
    unit_of_measurement: 'W'
    state: >-
      {% set max_power = states.input_select.ioniq_5_maximum_charging_power.state | regex_replace(' W$', '') | int %}
      {% if states.input_boolean.ioniq_5_charge_surplus.state == "on" %}
        {% set pv_power = states.sensor.pv_power_total.state | int %}
        {% set battery_soc = states.sensor.pv_battery_soc.state | int %}
        {% set house_power_usage = states.sensor.house_power_usage.state | int %}
        {% set openwb_power = states.sensor.openwb_power.state | int %}
        {% set grid_feed_in_limit = 4100 if (states.input_boolean.ioniq_5_charge_only_on_limited_grid_feed.state == "on") else 0 %}
        {%
          set solar_battery_preferred_power = (min([
            (states.input_number.ioniq_5_charge_prefer_solar_battery.state | int),
            2500+(100-battery_soc)*400 if battery_soc > 95 else 5000
          ]) | int) if (
            states.input_boolean.ioniq_5_charge_prefer_solar_battery.state == "on" and
            battery_soc < 100 and
            now().replace(
              hour=states.input_datetime.ioniq_5_charge_prefer_solar_battery_starting.attributes.hour,
              minute=states.input_datetime.ioniq_5_charge_prefer_solar_battery_starting.attributes.minute,
              second=0,
              microsecond=0
            ) <= now()
          ) else 0
        %}
        {%
          set surplus = (
            + pv_power
            - grid_feed_in_limit
            - solar_battery_preferred_power
            - max([0, house_power_usage - openwb_power])
           )
        %}
        {% if surplus > 1380 %}
          {{ min([max_power, surplus]) | string }}
        {% elif states.input_boolean.ioniq_5_charge_when_solar_battery_empty.state == "on" and battery_soc == 0 %}
          {{ max_power | string }}
        {% else %}
          0
        {% endif %}
      {% else %}
        {{ max_power | string }}
      {% endif %}
```
{% endraw  %}

**Notes**:
* The `trigger` setting is included here so that the `template` sensor value will be re-calculated when _any_ of the referenced state values change _except_ for when `openwb_power` changes because `openwb_power` will be updating more often than the other values, potentially causing problems when subtracting this from the `house_power_usage` value (that might not include be updated yet to include the `openwb_power`).
* The calculation for preferring the solar battery assumes the charging power limit to drop above 95 % SoC.

The calculated target charging power will be available as a sensor entity called `sensor.ioniq_5_target_charging_power`.


### Publishing the calculated the target charging power via MQTT to control the wallbox

Put this in your `automations.yaml`:

{% raw  %}
```yaml
- alias: Ioniq 5 Control Charging Power
  description: ''
  trigger:
  - platform: state
    entity_id: sensor.ioniq_5_target_charging_power
    to: ~
  condition: []
  action:
  - delay:
      hours: 0
      minutes: 0
      seconds: 1
      milliseconds: 0
  - service: mqtt.publish
    data_template:
      topic: openwb/target-power-in-watt
      retain: true
      payload: '{{ states.sensor.ioniq_5_target_charging_power.state }}'
  mode: restart
```
{% endraw  %}

### Configure the UI with the control knobs

![Home Assistant control knobs screenshot](/assets/2022-02-03-ev-solar-surplus-charging/ha-control-knobs.png)

That's it!
