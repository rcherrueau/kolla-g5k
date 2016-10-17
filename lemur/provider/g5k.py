# -*- coding: utf-8 -*-
from provider import Provider
import logging

import execo_g5k as EX5

NETWORK_FILE = 'g5k_networks.yaml'

class G5K(Provider):
    def init(self, config, force_deploy=False):
        """Provides resources and provisions the environment.

        Resources offers an ssh connection with an access for root
        user.

        The `config` parameter contains the client request (eg, number
        of compute per role among other things). This method returns a
        list of the form [{Role: [Host]}] and a pool of 5 ips.

        """
        g5k = G5kEngine(config, force)
        g5k.start(args=[])
        g5k.get_job()

        deployed, undeployed = g5k.deploy()
        if len(undeployed) > 0:
            sys.exit(31)

            roles = g5k.build_roles()

        # Get an IP for
        # kolla (haproxy)
        # docker registry
        # influx db
        # grafana
        vip_addresses = g5k.get_free_ip(5)
        # Get the NIC devices of the reserved cluster
        # XXX: this only works if all nodes are on the same cluster,
        # or if nodes from different clusters have the same devices
        interfaces = g5k.get_cluster_nics(STATE['config']['resources'].keys()[0])
        network_interface = str(interfaces[0])
        external_interface = None

        # TODO: Move this veth into the before preinstall
        if len(interfaces) > 1:
            external_interface = str(interfaces[1])
        else:
            external_interface = 'veth0'
            logger.warning("%s has only one NIC. The same interface "
                           "will be used for network_interface and "
                           "neutron_external_interface."
                           % STATE['config']['resources'].keys()[0])


        g5k.exec_command_on_nodes(
            g5k.deployed_nodes,
            'apt-get update && apt-get -y --force-yes install apt-transport-https',
            'Installing apt-transport-https...')

        # Install python on the nodes
        g5k.exec_command_on_nodes(
            g5k.deployed_nodes,
            'apt-get -y install python',
            'Installing Python on all the nodes...')

        return (roles, vip_addresses.map(str), [network_interface,
                                                external_interface])

    def before_preintsall(self, env):
        # TODO: Add veth management

        env.config['enable_veth'] = env.eths[1] == 'veth0'

        # TODO: Add veth management ansible rules
        return ""

    def after_preintsall(self, env):
        # TODO: Bind volumes of docker
        return ""
