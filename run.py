import os
import json
import asyncio
from datetime import datetime

from aiohttp import web
import nats
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import dns.resolver

class Component():

    def __init__(self):
        """
        """
        self.nc = None
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self.cccs_nats_url = os.getenv("CCCS_NATS_DSN", "nats://my-user:T0pS3cr3t@localhost:4222")
        self.cccs_nats_channel = os.getenv("CCCS_NATS_CHANNEL", "cross-cluster-coredns-sync")
        self.nats_status = None
        self.dns_server = None

    async def parse_log(self, request):
        # Set status for responce by nats connection
        status = 200
        if self.nats_status != True:
            status = 500
        return web.Response(text=f"nats: {self.nats_status}", status=status)

    async def message_handler(self, msg):
        subject = msg.subject
        reply = msg.reply
        data = json.loads(msg.data.decode())
        if "ip" not in data.keys():
            return
        if data["ip"] == self.dns_server:
            return
        print(f"Received a message on '{subject} {reply}': {data}")

    async def start(self):
        # NATS client
        self.nc = await nats.connect(
            name="cccs-server",
            servers=[self.cccs_nats_url],
            reconnected_cb=self.reconnected_cb,
            disconnected_cb=self.disconnected_cb,
            error_cb=self.error_cb,
            closed_cb=self.closed_cb,
            )

        print("NATS Connected.")

        self.nats_status = True

        await self.nc.subscribe(self.cccs_nats_channel, cb=self.message_handler)

        await self.get_dns()
        # first ping on server start
        await self.ping_dns()

        self.scheduler.add_job(self.ping_dns, "interval", seconds=10)
        self.scheduler.add_job(self.get_dns, "interval", seconds=60)
        self.scheduler.start()
        # Server
        app = web.Application()
        runner = web.AppRunner(app)

        # Routes
        app.router.add_get("/", self.parse_log)

        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", 80)

        print("Server listening at '0.0.0.0:80'")
        await site.start()
        
        # wait forever
        await asyncio.Event().wait()

    async def ping_dns(self):
        # Notify via NATS
        if self.nats_status:
            await self.nc.publish(self.cccs_nats_channel, json.dumps({"ip":self.dns_server}).encode())

    async def get_dns(self):
        dns_resolver = dns.resolver.Resolver()
        tmp_dns_server = dns_resolver.nameservers[0]
        if self.dns_server != tmp_dns_server:
            print(f"Find DNS server {tmp_dns_server}, old {self.dns_server}")
            self.dns_server = tmp_dns_server

    async def disconnected_cb(self):
        self.nats_status = False

    async def reconnected_cb(self):
        self.nats_status = True
        
    async def error_cb(self, e):
        print(f"There was an error with nats: {e}")
        self.nats_status = False

    async def closed_cb(self):
        self.nats_status = False

component = Component()
asyncio.run(component.start())