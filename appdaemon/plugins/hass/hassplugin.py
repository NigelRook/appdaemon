import asyncio
import json
import ssl
from websocket import create_connection
import traceback
import aiohttp
import pytz

import appdaemon.utils as utils

class HassPlugin:

    def __init__(self, ad, name, logger, error, loglevel, args):

        #Store args
        self.AD = ad
        self.logger = logger
        self.error = error
        self.config = args
        self.loglevel = loglevel
        self.name = name

        self.stopping = False
        self.ws = None
        self.reading_messages = False
        self.metadata = None
        self.oath = False

        self.log("INFO", "HASS Plugin Initializing")

        self.name = name

        if "namespace" in args:
            self.namespace = args["namespace"]
        else:
            self.namespace = "default"

        if "verbose" in args:
            self.verbose = args["verbose"]
        else:
            self.verbose = False

        if "ha_key" in args:
            self.ha_key = args["ha_key"]
            self.log("WARNING", "ha_key is deprecated please use HASS Long Lived Tokens instead")
        else:
            self.ha_key = None

        if "token" in args:
            self.token = args["token"]
        else:
            self.token = None

        if "ha_url" in args:
            self.ha_url = args["ha_url"]
        else:
            self.log("WARN", "ha_url not found in HASS configuration - module not initialized")

        if "cert_path" in args:
            self.cert_path = args["cert_path"]
        else:
            self.cert_path = None

        if "timeout" in args:
            self.timeout = args["timeout"]
        else:
            self.timeout = None

        if "cert_verify" in args:
            self.cert_verify = args["cert_verify"]
        else:
            self.cert_verify = True

        if "commtype" in args:
            self.commtype = args["commtype"]
        else:
            self.commtype = "WS"

        if "app_init_delay" in args:
            self.app_init_delay = args["app_init_delay"]
        else:
            self.app_init_delay = 0
        #
        # Set up HTTP Client
        #
        conn = aiohttp.TCPConnector()
        self.session = aiohttp.ClientSession(connector=conn)

        self.log("INFO", "HASS Plugin initialization complete")

    def log(self, level, message):
        self.AD.log(level, "{}: {}".format(self.name, message))

    def verbose_log(self, text):
        if self.verbose:
            self.log("INFO", text)

    def stop(self):
        self.verbose_log("*** Stopping ***")
        self.stopping = True
        if self.ws is not None:
            self.ws.close()

    #
    # Get initial state
    #

    async def get_complete_state(self):
        hass_state = await self.get_hass_state()
        states = {}
        for state in hass_state:
            states[state["entity_id"]] = state
        self.log("DEBUG", "Got state")
        self.verbose_log("*** Sending Complete State: {} ***".format(hass_state))
        return states

    #
    # Get HASS Metadata
    #

    async def get_metadata(self):
        return self.metadata

    #
    # Handle state updates
    #

    async def get_updates(self):

        _id = 0

        already_notified = False
        first_time = True
        while not self.stopping:
            _id += 1
            try:
                #
                # Connect to websocket interface
                #
                url = self.ha_url
                if url.startswith('https://'):
                    url = url.replace('https', 'wss', 1)
                elif url.startswith('http://'):
                    url = url.replace('http', 'ws', 1)

                sslopt = {}
                if self.cert_verify is False:
                    sslopt = {'cert_reqs': ssl.CERT_NONE}
                if self.cert_path:
                    sslopt['ca_certs'] = self.cert_path
                self.ws = create_connection(
                    "{}/api/websocket".format(url), sslopt=sslopt
                )
                res = await utils.run_in_executor(self.AD.loop, self.AD.executor, self.ws.recv)
                result = json.loads(res)
                self.log("INFO",
                          "Connected to Home Assistant {}".format(
                              result["ha_version"]))
                #
                # Check if auth required, if so send password
                #
                if result["type"] == "auth_required":
                    if self.token is not None:
                        auth = json.dumps({
                            "type": "auth",
                            "access_token": self.token
                        })
                    elif self.ha_key is not None:
                        auth = json.dumps({
                            "type": "auth",
                            "api_password": self.ha_key
                        })
                    else:
                        raise ValueError("HASS requires authentication and none provided in plugin config")

                    await utils.run_in_executor(self.AD.loop, self.AD.executor, self.ws.send, auth)
                    result = json.loads(self.ws.recv())
                    if result["type"] != "auth_ok":
                        self.log("WARNING",
                                  "Error in authentication")
                        raise ValueError("Error in authentication")
                #
                # Subscribe to event stream
                #
                sub = json.dumps({
                    "id": _id,
                    "type": "subscribe_events"
                })
                await utils.run_in_executor(self.AD.loop, self.AD.executor, self.ws.send, sub)
                result = json.loads(self.ws.recv())
                if not (result["id"] == _id and result["type"] == "result" and
                                result["success"] is True):
                    self.log(
                        "WARNING",
                        "Unable to subscribe to HA events, id = {}".format(_id)
                    )
                    self.log("WARNING", result)
                    raise ValueError("Error subscribing to HA Events")

                #
                # Grab Metadata
                #
                self.metadata = await self.get_hass_config()
                #
                # Get State
                #
                state = await self.get_complete_state()
                #
                # Wait for app delay
                #
                if self.app_init_delay > 0:
                    self.log(
                        "INFO",
                        "Delaying app initialization for {} seconds".format(
                            self.app_init_delay
                        )
                    )
                    await asyncio.sleep(self.app_init_delay)
                #
                # Fire HA_STARTED Events
                #
                await self.AD.plugins.notify_plugin_started(self.name, self.namespace, self.metadata, state, first_time)
                self.reading_messages = True

                already_notified = False

                #
                # Loop forever consuming events
                #
                while not self.stopping:
                    ret = await utils.run_in_executor(self.AD.loop, self.AD.executor, self.ws.recv)
                    result = json.loads(ret)

                    if not (result["id"] == _id and result["type"] == "event"):
                        self.log(
                            "WARNING",
                            "Unexpected result from Home Assistant, "
                            "id = {}".format(_id)
                        )
                        self.log("WARNING", result)
                        raise ValueError(
                            "Unexpected result from Home Assistant"
                        )

                    await self.AD.state.state_update(self.namespace, result["event"])

                self.reading_messages = False

            except:
                self.reading_messages = False
                first_time = False
                if not already_notified:
                    self.AD.plugins.notify_plugin_stopped(self.name, self.namespace)
                    already_notified = True
                if not self.stopping:
                    self.log(
                        "WARNING",
                        "Disconnected from Home Assistant, retrying in 5 seconds"
                    )
                    if self.loglevel == "DEBUG":
                        self.log( "WARNING", '-' * 60)
                        self.log( "WARNING", "Unexpected error:")
                        self.log("WARNING", '-' * 60)
                        self.log( "WARNING", traceback.format_exc())
                        self.log( "WARNING", '-' * 60)
                    await asyncio.sleep(5)

        self.log("INFO", "Disconnecting from Home Assistant")

    def get_namespace(self):
        return self.namespace

    #
    # Utility functions
    #

    def utility(self):
        self.log("DEBUG", "Utility")
        return None

    #
    # Home Assistant Interactions
    #

    async def get_hass_state(self, entity_id=None):
        if self.token is not None:
            headers = {'Authorization': "Bearer {}".format(self.token)}
        elif self.ha_key is not None:
            headers = {'x-ha-access': self.ha_key}
        else:
            headers = {}

        if entity_id is None:
            apiurl = "{}/api/states".format(self.ha_url)
        else:
            apiurl = "{}/api/states/{}".format(self.ha_url, entity_id)
        self.log("DEBUG", "get_ha_state: url is {}".format(apiurl))
        r = await self.session.get(apiurl, headers=headers, verify_ssl=self.cert_verify)
        r.raise_for_status()
        return await r.json()

    def validate_meta(self, meta, key):
        if key not in meta:
            self.log("WARNING", "Value for '{}' not found in metadata for plugin {}".format(key, self.name))
            raise ValueError
        try:
            value = float(meta[key])
        except:
            self.log("WARNING", "Invalid value for '{}' ('{}') in metadata for plugin {}".format(key, meta[key], self.name))
            raise

    def validate_tz(self, meta):
        if "time_zone" not in meta:
            self.log("WARNING", "Value for 'time_zone' not found in metadata for plugin {}".format( self.name))
            raise ValueError
        try:
            tz = pytz.timezone(meta["time_zone"])
        except pytz.exceptions.UnknownTimeZoneError:
            self.log("WARNING", "Invalid value for 'time_zone' ('{}') in metadata for plugin {}".format(meta["time_zone"], self.name))
            raise

    async def get_hass_config(self):
        try:
            self.log("DEBUG", "get_ha_config()")
            if self.token is not None:
                headers = {'Authorization': "Bearer {}".format(self.token)}
            elif self.ha_key is not None:
                headers = {'x-ha-access': self.ha_key}
            else:
                headers = {}

            apiurl = "{}/api/config".format(self.ha_url)
            self.log("DEBUG", "get_ha_config: url is {}".format(apiurl))
            r = await self.session.get(apiurl, headers=headers, verify_ssl=self.cert_verify)
            r.raise_for_status()
            meta = await r.json()
            #
            # Validate metadata is sane
            #
            self.validate_meta(meta, "latitude")
            self.validate_meta(meta, "longitude")
            self.validate_meta(meta, "elevation")
            self.validate_tz(meta)

            return meta
        except:
            self.log("WARNING", "Error getting metadata - retrying")
            raise
    #
    # Async version of call_service() for the hass proxy for HADashboard
    #

    @staticmethod
    def _check_service(service):
        if service.find("/") == -1:
            raise ValueError("Invalid Service Name: {}".format(service))

    async def call_service(self, service, **kwargs):
        try:
            self._check_service(service)
            d, s = service.split("/")
            self.log(
                "DEBUG",
                "call_service: {}/{}, {}".format(d, s, kwargs)
            )
            if self.token is not None:
                headers = {'Authorization': "Bearer {}".format(self.token)}
            elif self.ha_key is not None:
                headers = {'x-ha-access': self.ha_key}
            else:
                headers = {}

            apiurl = "{}/api/services/{}/{}".format(self.ha_url, d, s)

            r = await self.session.post(apiurl, headers=headers, json=kwargs, verify_ssl=self.cert_verify)
            r.raise_for_status()
            response = r.json
            return response
        except:
            self.log("WARNING", '-' * 60)
            self.log("WARNING", "Unexpected error during call_service()")
            self.log("WARNING", '-' * 60)
            self.log("WARNING", traceback.format_exc())
            self.log("WARNING", '-' * 60)
