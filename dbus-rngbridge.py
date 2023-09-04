#!/usr/bin/env python
# vim: ts=2 sw=2 et

# import normal packages
import platform
import logging
import logging.handlers
import sys
import os
import sys

if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject
import sys
import time
import requests  # for http GET
import configparser  # for config/ini file

# our own packages from victron
sys.path.insert(
    1,
    os.path.join(
        os.path.dirname(__file__),
        "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python",
    ),
)
from vedbus import VeDbusService


class DbusRngbridgeService:
    def __init__(
        self, paths, productname="RNGBridge", connection="RNGBridge HTTP JSON service"
    ):
        config = self._getConfig()
        deviceinstance = int(config["DEFAULT"]["DeviceInstance"])
        customname = config["DEFAULT"]["CustomName"]
        servicename = "com.victronenergy.solarcharger"
        productid = 0  # TODO figure out correct role

        self._dbusservice = VeDbusService(
            "{}.http_{:02d}".format(servicename, deviceinstance)
        )
        self._paths = paths

        logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))

        # Create the management objects, as specified in the ccgx dbus-api document
        self._dbusservice.add_path("/Mgmt/ProcessName", __file__)
        self._dbusservice.add_path(
            "/Mgmt/ProcessVersion",
            "Version {} running on Python {}".format(1, platform.python_version()),
        )
        self._dbusservice.add_path("/Mgmt/Connection", connection)

        # Create the mandatory objects
        self._dbusservice.add_path("/DeviceInstance", deviceinstance)
        self._dbusservice.add_path("/ProductId", productid)
        # found on https://www.sascha-curth.de/projekte/005_Color_Control_GX.html#experiment - should be an ET340 Engerie Meter
        # self._dbusservice.add_path('/DeviceType', 345)
        self._dbusservice.add_path("/ProductName", productname)
        self._dbusservice.add_path("/CustomName", customname)
        self._dbusservice.add_path("/Latency", None)
        self._dbusservice.add_path("/FirmwareVersion", 0.2)
        self._dbusservice.add_path("/HardwareVersion", 0)
        self._dbusservice.add_path("/Connected", 1)
        self._dbusservice.add_path("/Role", "solarcharger")
        self._dbusservice.add_path("/Serial", config["DEFAULT"]["Serial"])
        self._dbusservice.add_path("/UpdateIndex", 0)

        # solarcharger specific
        self._dbusservice.add_path("/NrOfTrackers", 1)  # Single MPPT tracker

        # add path values to dbus
        for path, settings in self._paths.items():
            self._dbusservice.add_path(
                path,
                settings["initial"],
                gettextcallback=settings["textformat"],
                writeable=True,
                onchangecallback=self._handlechangedvalue,
            )

        # last update
        self._lastUpdate = 0

        # add _update function 'timer'
        # pause 2000ms before the next request
        gobject.timeout_add(2000, self._update)

        # add _signOfLife 'timer' to get feedback in log every 5minutes
        gobject.timeout_add(self._getSignOfLifeInterval() * 60 * 1000, self._signOfLife)

    def _getConfig(self):
        config = configparser.ConfigParser()
        config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
        return config

    def _getSignOfLifeInterval(self):
        config = self._getConfig()
        value = config["DEFAULT"]["SignOfLifeLog"]

        if not value:
            value = 0

        return int(value)

    def _getStatusUrl(self):
        config = self._getConfig()
        accessType = config["DEFAULT"]["AccessType"]

        if accessType == "OnPremise":
            URL = "http://%s/api/" % (config["ONPREMISE"]["Host"])
        else:
            raise ValueError(
                "AccessType %s is not supported" % (config["DEFAULT"]["AccessType"])
            )

        return URL

    def _requestData(self, url: str):
        response = requests.get(url=url, timeout=5)
        # check for response
        if not response:
            raise ConnectionError("No response from RNGBridge - %s" % (url))
        json = response.json()
        # check for json
        if not json:
            raise ValueError("Converting response to JSON failed")
        return json

    def _getRngBridgeConfig(self):
        URL = self._getStatusUrl() + "config"
        return self._requestData(URL)

    def _getRngBridgeState(self):
        URL = self._getStatusUrl() + "state"
        return self._requestData(URL)

    def _signOfLife(self):
        logging.info("--- Start: sign of life ---")
        logging.info("Last _update() call: %s" % (self._lastUpdate))
        logging.info("Last '/Yield/Power': %s" % (self._dbusservice["/Yield/Power"]))
        logging.info("--- End: sign of life ---")
        return True

    def _update(self):
        try:
            # get data from Shelly 3em
            state = self._getRngBridgeState()

            #            0, 1, 2, 3, 4, 5, 6
            state_map = [0, 0, 3, 7, 4, 5, 2]
            # 1: charging activated
            # Mapping `charging activated` to off might be counterintuitive, but this state is never seen
            # send data to DBus
            # /State    <- 0=Off                 -> 0: charging deactivated
            #              2=Fault               -> 6: current limiting (overpower)
            #              3=Bulk                -> 2: mppt charging mode
            #              4=Absorption          -> 4: boost charging mode
            #              5=Float               -> 5: floating charging mode
            #              6=Storage
            #              7=Equalize            -> 3: equalizing charging mode
            #              252=External control

            # PV array voltage, path exists only for single tracker product (all common MPPTs)
            self._dbusservice["/Pv/V"] = state["p"]["vo"]
            # Total PV power (Watts)
            self._dbusservice["/Yield/Power"] = state["p"]["vo"] * state["p"]["cu"]
            self._dbusservice["/State"] = state_map[state["c"]["st"]]
            # Total PV energy (Kilowatthours)
            self._dbusservice["/Yield/System"] = state["b"]["to"] / 1000.0
            self._dbusservice["/Yield/User"] = state["b"]["ge"] / 1000.0
            # Actual battery voltage
            self._dbusservice["/Dc/0/Voltage"] = state["b"]["vo"]
            # Actual charging current
            self._dbusservice["/Dc/0/Current"] = state["b"]["cu"]
            # Whether the load is on or off
            self._dbusservice["/Load/State"] = state["o"]["l"]
            # Current from the load output
            self._dbusservice["/Load/I"] = state["l"]["cu"]

            # logging
            logging.debug(
                "PV Power (/Yield/Power): %s" % (self._dbusservice["/Yield/Power"])
            )
            logging.debug(
                "Battery Voltage (/Dc/0/Voltage): %s"
                % (self._dbusservice["/Dc/0/Voltage"])
            )
            logging.debug(
                "Battery Current (/Dc/0/Current): %s"
                % (self._dbusservice["/Dc/0/Current"])
            )
            logging.debug("---")

            # increment UpdateIndex - to show that new data is available and wrap
            self._dbusservice["/UpdateIndex"] = (
                self._dbusservice["/UpdateIndex"] + 1
            ) % 256

            # update lastupdate vars
            self._lastUpdate = time.time()
        except (
            ValueError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            ConnectionError,
        ) as e:
            logging.critical(
                "Error getting data from RNGBridge - check network or RNGBridge status. Setting power values to 0. Details: %s",
                e,
                exc_info=e,
            )
            self._dbusservice["/Pv/V"] = 0
            self._dbusservice["/Yield/Power"] = 0
            self._dbusservice["/Yield/System"] = 0
            self._dbusservice["/Yield/User"] = 0
            self._dbusservice["/State"] = 0
            self._dbusservice["/Dc/0/Voltage"] = 0
            self._dbusservice["/Dc/0/Current"] = 0
            self._dbusservice["/Load/State"] = False
            self._dbusservice["/Load/I"] = 0
            # increment UpdateIndex - to show that new data is available and wrap
            self._dbusservice["/UpdateIndex"] = (
                self._dbusservice["/UpdateIndex"] + 1
            ) % 256
        except Exception as e:
            logging.critical("Error at %s", "_update", exc_info=e)
        return True

    def _handlechangedvalue(self, path, value):
        logging.debug("someone else updated %s to %s" % (path, value))
        return True  # accept the change


def getLogLevel():
    config = configparser.ConfigParser()
    config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
    logLevelString = config["DEFAULT"]["LogLevel"]

    if logLevelString:
        level = logging.getLevelName(logLevelString)
    else:
        level = logging.INFO

    return level


def main():
    # configure logging
    logging.basicConfig(
        format="%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=getLogLevel(),
        handlers=[
            logging.FileHandler(
                "%s/current.log" % (os.path.dirname(os.path.realpath(__file__)))
            ),
            logging.StreamHandler(),
        ],
    )

    try:
        logging.info("Start")

        from dbus.mainloop.glib import DBusGMainLoop

        # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
        DBusGMainLoop(set_as_default=True)

        # formatting
        def _kwh(p, v):
            return str(round(v, 2)) + " KWh"

        def _a(p, v):
            return str(round(v, 1)) + " A"

        def _w(p, v):
            return str(round(v, 1)) + " W"

        def _v(p, v):
            return str(round(v, 1)) + " V"

        def _s(p, v):
            return str(v)

        # start our main-service
        pvac_output = DbusRngbridgeService(
            paths={
                "/Pv/V": {"initial": 0, "textformat": _v},
                "/Yield/Power": {"initial": 0, "textformat": _w},
                "/Yield/System": {"initial": 0, "textformat": _kwh},
                "/Yield/User": {"initial": 0, "textformat": _kwh},
                "/State": {"initial": 0, "textformat": _s},
                "/Dc/0/Voltage": {"initial": 0, "textformat": _v},
                "/Dc/0/Current": {"initial": 0, "textformat": _a},
                "/Load/State": {"initial": 0, "textformat": _v},
                "/Load/I": {"initial": 0, "textformat": _a},
            }
        )

        logging.info(
            "Connected to dbus, and switching over to gobject.MainLoop() (= event based)"
        )
        mainloop = gobject.MainLoop()
        mainloop.run()
    except (
        ValueError,
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    ) as e:
        logging.critical("Error in main type %s", str(e))
    except Exception as e:
        logging.critical("Error at %s", "main", exc_info=e)


if __name__ == "__main__":
    main()
