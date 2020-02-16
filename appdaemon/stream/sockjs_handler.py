import traceback
import json
import sockjs

from appdaemon import utils as utils


class SockJSHandler:
    def __init__(self, ADStream, app, path, ad):

        self.AD = ad
        self.ADStream = ADStream
        self.app = app

        self.logger = ad.logging.get_child("_stream")
        self.access = ad.logging.get_access()

        sockjs.add_endpoint(app, prefix=path, handler=self.sockjshandler)

    async def sockjshandler(self, msg, session):
        if msg.type == sockjs.MSG_OPEN:
            self.logger.debug("IOSocket connect session={}".format(session))
            await self.ADStream.on_connect(session)
        elif msg.type == sockjs.MSG_MESSAGE:
            self.logger.debug("IOSocket message session={} data={}".format(session, msg.data))
            try:
                msg = json.loads(msg.data)
                handler = self.ADStream.get_handler(session)
                await handler._on_message(msg)
            except TypeError as e:
                self.logger.debug("-" * 60)
                self.logger.warning("Unexpected error in JSON conversion when writing from stream")
                self.logger.debug("Data is: %s", msg.data)
                self.logger.debug("Error is: %s", e)
                self.logger.debug("-" * 60)
            except Exception:
                self.logger.debug("-" * 60)
                self.logger.debug("Client disconnected unexpectedly")
                self.access.info("Client disconnected unexpectedly")
                self.logger.debug("-" * 60)
                self.logger.debug(traceback.format_exc())
                self.logger.debug("-" * 60)

        elif msg.type == sockjs.MSG_CLOSED:
            self.logger.debug("IOSocket disconnect session={}".format(session))
            handler = self.ADStream.get_handler(session)
            await handler._on_disconnect()

    def makeStream(self, ad, request, **kwargs):
        return SockJSStream(ad, request, **kwargs)


class SockJSStream:
    def __init__(self, ad, session, **kwargs):

        self.AD = ad
        self.client_id = session
        self.on_message = kwargs["on_message"]
        self.on_disconnect = kwargs["on_disconnect"]

        self.logger = ad.logging.get_child("_stream")
        self.access = ad.logging.get_access()

    async def run(self):
        pass

    async def sendclient(self, data):
        try:
            msg = utils.convert_json(data)
            await utils.run_in_executor(self, self.client_id.send, msg)
            self.client_id.send(msg)
        except TypeError as e:
            self.logger.debug("-" * 60)
            self.logger.warning("Unexpected error in JSON conversion when writing to stream")
            self.logger.debug("Data is: %s", data)
            self.logger.debug("Error is: %s", e)
            self.logger.debug("-" * 60)

        except Exception:
            self.logger.debug("-" * 60)
            self.logger.debug("Client disconnected unexpectedly")
            self.access.info("Client disconnected unexpectedly")
            self.logger.debug("-" * 60)
            self.logger.debug(traceback.format_exc())
            self.logger.debug("-" * 60)
