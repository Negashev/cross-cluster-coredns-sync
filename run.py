import os
import json
import asyncio
import time
import copy
from datetime import datetime

from aiohttp import web
import nats
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import dns.resolver
from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.api_client import ApiClient

class Component():

    def __init__(self):
        """
        """
        self.nc = None
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self.cccs_namespace = os.getenv("CCCS_NAMESPACE", "kube-system")
        self.cccs_configmap = os.getenv("CCCS_CONFIGNAME", "cccs-coredns")
        self.cccs_ignore_configmap_keys = os.getenv("CCCS_IGNORE_CONFIGMAP_KEYS", "").split(",")
        self.cccs_nats_url = os.getenv("CCCS_NATS_DSN", "nats://my-user:T0pS3cr3t@localhost:4222")
        self.cccs_nats_channel = os.getenv("CCCS_NATS_CHANNEL", "cross-cluster-coredns-sync")
        self.cccs_domain_suffix = os.getenv("CCCS_DOMAIN_SUFFIX", ".local.") # cluster.local.
        self.nats_status = None
        self.dns_server = None
        self.domain_suffix = None
        self.cross_cluster_rows = {}
        self.tpm_cross_cluster_rows = {}

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
        data_keys = data.keys()
        if "ip" not in data_keys or "domain" not in data_keys:
            return
        if data["ip"] == self.dns_server:
            return
        if data["domain"] == self.domain_suffix:
            return
        if data["ip"] not in self.tpm_cross_cluster_rows.keys():
            print(f'Find domain {data["domain"]} ({data["ip"]})')
        # set time for truncate
        data["time"] = time.time()
        self.tpm_cross_cluster_rows[data["ip"]] = data

    async def update_cross_cluster_rows(self):
        tpm_cross_cluster_rows = copy.deepcopy(self.tpm_cross_cluster_rows)
        cross_cluster_rows = {}
        for i in tpm_cross_cluster_rows:
            # cleanup expired IPs
            if tpm_cross_cluster_rows[i]["time"] < time.time() - 60:
                print(f'Removing domain {tpm_cross_cluster_rows[i]["domain"]} ({tpm_cross_cluster_rows[i]["ip"]})')
                del self.tpm_cross_cluster_rows[i]
            tmp_dns = """{domain}:53 {{
                forward . {ip}
            }}""".format(domain=tpm_cross_cluster_rows[i]["domain"], ip=tpm_cross_cluster_rows[i]["ip"])
            cross_cluster_rows[f'{tpm_cross_cluster_rows[i]["domain"]}.server'] = tmp_dns
        self.cross_cluster_rows = copy.deepcopy(cross_cluster_rows)

    async def k8s_get_or_create_if_not_exist_config_map(self, v1):
        ret = await v1.list_namespaced_config_map(namespace=self.cccs_namespace)
        for i in ret.items:
            if i.metadata.name == self.cccs_configmap:
                return i
        return await v1.create_namespaced_config_map(
            body={
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": self.cccs_configmap
                },
                "data": {}
            }, 
            namespace=self.cccs_namespace
        )

    async def update_cross_cluster_rows_in_k8s(self):
        async with ApiClient() as api:
            v1 = client.CoreV1Api(api)
            configmap = await self.k8s_get_or_create_if_not_exist_config_map(v1)
            source_configmap = copy.deepcopy(configmap.data)
            if self.cccs_ignore_configmap_keys and configmap.data:
                for i in self.cccs_ignore_configmap_keys:
                    del configmap.data[i]

            if configmap.data == self.cross_cluster_rows:
                # if array eq nothing to do
                return
            else:
                # update array
                configmap.data = self.cross_cluster_rows
            # restore ignore configmap keys
            for i in self.cccs_ignore_configmap_keys:
                configmap.data[i] = source_configmap[i]
            await v1.replace_namespaced_config_map(name=configmap.metadata.name, namespace = configmap.metadata.namespace, body=configmap)

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

        config.load_incluster_config()
        
        print("Kubernetes Connected.")
        
        await self.get_dns()
        # first ping on server start
        await self.ping_dns()

        await self.update_cross_cluster_rows()

        self.scheduler.add_job(self.ping_dns, "interval", seconds=10)
        self.scheduler.add_job(self.get_dns, "interval", seconds=60)
        self.scheduler.add_job(self.update_cross_cluster_rows, "interval", seconds=24)
        self.scheduler.add_job(self.update_cross_cluster_rows_in_k8s, "interval", seconds=60)
        self.scheduler.start()
        # Server
        app = web.Application()
        runner = web.AppRunner(app)

        # Routes
        app.router.add_get("/", self.parse_log)

        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", 8080)

        await site.start()
        
        # wait forever
        await asyncio.Event().wait()

    async def ping_dns(self):
        # Notify via NATS
        if self.nats_status and self.dns_server and self.domain_suffix:
            await self.nc.publish(self.cccs_nats_channel, json.dumps({"ip": self.dns_server, "domain": self.domain_suffix}).encode())

    async def get_dns(self):
        dns_resolver = dns.resolver.Resolver()
        tmp_dns_server = dns_resolver.nameservers[0]
        if self.dns_server != tmp_dns_server:
            print(f"Find DNS server {tmp_dns_server}, old {self.dns_server}")
            self.dns_server = tmp_dns_server
            # fix domain with endswith(domain_suffix) and min len
            domain_suffix = None
            endswith_domain_suffix = []
            if dns_resolver.search:
                endswith_domain_suffix = [element for element in dns_resolver.search if element.to_text().endswith(self.cccs_domain_suffix)]
            if endswith_domain_suffix:
                domain_suffix = min(endswith_domain_suffix, key=len)
                if domain_suffix is not None:
                    self.domain_suffix = domain_suffix.to_text()[:-1]
                    return
            print(f"Error: Domain not found")

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
