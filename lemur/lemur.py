# -*- coding: utf-8 -*-
"""Lemur: Monitor and test your OpenStack.

usage: lemur [-h|--help] <command> [<args> ...]

Options:
  -h --help     Show this help message.

Commands:
  up            Make a G5K reservation and install the docker registry
  os            Run kolla and install OpenStack
  init          TODO: explain me
  bench         Run rally on this OpenStack
  ssh-tunnel    Print configuration for port forwarding with horizon
  info          Show information of the actual deployment

See 'lemur help <command>' for more information on a specific command.
"""
# TODO: Add next command
#  lemur [-h | --help] [-f CONFIG_PATH] [--force-deploy]
#  lemur info
from utils import *
from provider.g5k import G5K

import functools

from datetime import datetime
import logging

from docopt import docopt
from subprocess import call
import pickle
import requests
import pprint
from operator import itemgetter, attrgetter

from keystoneauth1.identity import v3
from keystoneauth1 import session
from glanceclient import client as gclient
from keystoneclient.v3 import client as kclient

import sys, os, subprocess
from ansible.inventory import Inventory
import ansible.callbacks
import ansible.playbook

import jinja2

import yaml

SCRIPT_PATH = os.path.dirname(os.path.realpath(__file__))
SYMLINK_NAME = os.path.join(SCRIPT_PATH, 'current')
TEMPLATE_DIR = os.path.join(SCRIPT_PATH, 'templates')

KOLLA_REPO = 'https://git.openstack.org/openstack/kolla'
KOLLA_BRANCH = 'stable/mitaka'
# These roles are mandatory for the
# the original inventory to be valid
# Note that they may be empy
# e.g. if cinder isn't installed storage may be a empty group
# in the inventory
KOLLA_MANDATORY_GROUPS = [
    "control",
    "compute",
    "network",
    "storage"
]

# State of the script
ENV = {
    'config' : {}, # The config
    'config_file' : '', # The initial config file
    'nodes'  : {}, # Roles with nodes
    'phase'  : '', # Last phase that have been run
    'user'   : ''  # User id for this job
}

INTERNAL_IP = 0
REGISTRY_IP = 1
INFLUX_IP   = 2
GRAFANA_IP  = 3
NEUTRON_IP  = 4

NETWORK_IFACE  = 0
EXTERNAL_IFACE = 1

def lemurtask(doc):
    """Decorator for a Lemur Task."""
    def decorator(fn):
        fn.__doc__ = doc
        @functools.wraps(fn)
        def decorated(*args, **kwargs):
            # TODO: Load dynamically the provider
            kwargs['provider'] = G5K();
            # TODO: handle the loading of env
            kwargs['env'] = ENV;
            fn(*args, **kwargs)
            # TODO: handle the save of env
        return decorated
    return decorator

def save_state():
    state_path = os.path.join(SYMLINK_NAME, '.state')
    with open(state_path, 'wb') as state_file:
        pickle.dump(ENV, state_file)

def load_env():
    state_path = os.path.join(SYMLINK_NAME, '.state')
    if os.path.isfile(state_path):
        with open(state_path, 'rb') as state_file:
            ENV.update(pickle.load(state_file))

def update_config_state():
    """
    Update ENV['config'] with the config file options
    """
    config_file = ENV['config_file']
    with open(config_file, 'r') as f:
        ENV['config'].update(yaml.load(f))
    logger.info("Reloaded config %s", ENV['config'] )


@lemurtask(
"""usage: lemur up [-f CONFIG_PATH] [--force-deploy] [-t TAGS | --tags=TAGS]

  -h --help             Show this help message.

  -f CONFIG_PATH        Path to the configuration file describing the
                        Grid'5000 deployment [default: ./reservation.yaml].
  --force-deploy        Force deployment.
  -t TAGS --tags=TAGS   Only run ansible tasks tagged with these values.

""")
def up(provider=None, env=None, **kwargs):
    logging.info('phase[up]')
    print(provider)
    print(env)
    print(kwargs)
    rsc, ips, eths = provider.initialize(env.config)

    env.watch(rsc) #, ips, eths)

    # def preinstall(provider, tag=None, env=None):
    # logging.info('phase[preinstall]')

    # ips  = env.ips
    # eths = env.eths

    # Generates a directory for results
    resultdir_name = 'lemur_' + datetime.today().isoformat()
    resultdir = os.path.join(SCRIPT_PATH, resultdir_name)
    os.mkdir(resultdir)
    logging.info('Generates result directory %s' % resultdir_name)

    env.watch(resultdir)

    # Generates inventory for ansible/kolla
    base_inventory = config['inventory']
    inventory = os.path.join(resultdir, 'multinode')
    generate_inventory(env.rsc, base_inventory, inventory)
    logging.info('Generates inventory %s' % inventory)

    env.watch(inventory)

    # Set variables required by playbooks of the application
    config.update({
        # Lemur/Kolla
        'vip':          ips[INTERNAL_IP],
        'registry_vip': ips[REGISTRY_IP],
        'influx_vip':   ips[INFLUX_IP],
        'grafana_vip':  ips[GRAFANA_IP],
        'neutron_external_address': ips[NEUTRON_IP],
        'network_interface': eths[NETWORK_IFACE],

        # Kolla specific
        'kolla_internal_vip_address': ips[INTERNAL_IP],
        'neutron_external_interface': eths[EXTERNAL_IFACE]
    })
    passwords = os.path.join(TEMPLATE_DIR, "passwords.yml")
    with open(passwords_path) as passwords_file:
        env.config.update(yaml.load(passwords_file))

    # Grabs playbooks & runs them
    playbooks = []
    pb_before = provider.before_preintsall(env)
    if pb_before: playbooks.append(pb_before)
    pb_up = os.path.join(SCRIPT_PATH, 'ansible', 'up.yml')
    playbooks.append(pb_up)
    pb_after = provider.after_preintsall(env)
    if pb_after: playbooks.append(pb_after)

    run_ansible(playbooks, inventory, env.config, tags)

    # Symlink current directory
    link = os.path.abspath(SYMLINK_NAME)
    try:
        os.remove(link)
    except OSError:
        pass
    os.symlink(resultdir, link)
    logger.info("Symlinked %s to %s" % (resultdir, link))


@lemurtask(
"""usage: lemur os [--reconfigure] [-t TAGS | --tags=TAGS]

Options:
  -h --help              Show this help message.
  -t TAGS --tags=TAGS    Only run ansible tasks tagged with these values.
  --reconfigure          Reconfigure the services after a deployment.

""")
def install_os(reconfigure, env=None, **kwargs):
    update_config_state()

    # Generates kolla globals.yml, passwords.yml
    generate_kolla_files(env.config["kolla"], env.config, env.resultdir)

    # Clone or pull Kolla
    if os.path.isdir('kolla'):
        logger.info("Remove previous Kolla installation")
        kolla_path = os.path.join(SCRIPT_PATH, "kolla")
        call("rm -rf %s" % kolla_path, shell=True)

    logger.info("Cloning Kolla")
    call("cd %s ; git clone %s -b %s > /dev/null" % (SCRIPT_PATH, KOLLA_REPO, KOLLA_BRANCH), shell=True)

    logging.warning(("Patching kolla, this should be ",
                     "deprecated with the new version of Kolla"))

    playbook = os.path.join(SCRIPT_PATH, "ansible", "patches.yml")
    run_ansible([playbook], env.inventory, env.config)

    kolla_cmd = [os.path.join(SCRIPT_PATH, "kolla", "tools", "kolla-ansible")]

    if reconfigure:
        kolla_cmd.append('reconfigure')
    else:
        kolla_cmd.append('deploy')

    kolla_cmd.extend(["-i", "%s/multinode" % SYMLINK_NAME,
                      "--configdir", "%s" % SYMLINK_NAME])

    if tags: kolla_cmd.extend(["--tags", args])

    call(kolla_cmd)


@lemurtask("""usage: lemur init""")
def init_os(**kwargs):
    # Authenticate to keystone
    # http://docs.openstack.org/developer/keystoneauth/using-sessions.html
    # http://docs.openstack.org/developer/python-glanceclient/apiv2.html
    keystone_addr = ENV['config']['vip']
    auth = v3.Password(auth_url='http://%s:5000/v3' % keystone_addr,
                       username='admin',
                       password='demo',
                       project_name='admin',
                       user_domain_id='Default',
                       project_domain_id='default')
    sess = session.Session(auth=auth)

    # Install `member` role
    keystone = kclient.Client(session=sess)
    role_name = 'member'
    if role_name not in map(attrgetter('name'), keystone.roles.list()):
        logger.info("Creating role %s" % role_name)
        keystone.roles.create(role_name)

    # Install cirros with glance client if absent
    glance = gclient.Client('2', session=sess)
    cirros_name = 'cirros.uec'
    if cirros_name not in map(itemgetter('name'), glance.images.list()):
        # Download cirros
        image_url  = 'http://download.cirros-cloud.net/0.3.4/'
        image_name = 'cirros-0.3.4-x86_64-disk.img'
        logger.info("Downloading %s at %s..." % (cirros_name, image_url))
        cirros_img = requests.get(image_url + '/' + image_name)

        # Install cirros
        cirros = glance.images.create(name=cirros_name,
                                      container_format='bare',
                                      disk_format='qcow2',
                                      visibility='public')
        glance.images.upload(cirros.id, cirros_img.content)
        logger.info("%s has been created on OpenStack" %  cirros_name)

@lemurtask(
"""usage: lemur bench [--scenarios=SCENARIOS] [--times=TIMES] [--concurrency=CONCURRENCY] [--wait=WAIT]

  -h --help                    Show this help message.
  --scenarios=SCENARIOS        Name of the files containing the scenarios to launch.
                               The file must reside under the rally directory.
  --times=TIMES                Number of times to run each scenario [default: 1].
  --concurrency=CONCURRENCY    Concurrency level of the tasks in each scenario [default: 1].
  --wait=WAIT                  Seconds to wait between two scenarios [default: 0].
""")
def bench(**kwargs):
    playbook_path = os.path.join(SCRIPT_PATH, 'ansible', 'run-bench.yml')
    inventory_path = os.path.join(SYMLINK_NAME, 'multinode')
    if scenario_list:
        ENV['config']['rally_scenarios_list'] = scenario_list
    ENV['config']['rally_times'] = times
    ENV['config']['rally_concurrency'] = concurrency
    ENV['config']['rally_wait'] = wait
    run_ansible([playbook_path], inventory_path, ENV['config'])

@lemurtask("""usage: lemur ssh-tunnel""")
def ssh_tunnel(**kwargs):
    user = ENV['user']
    internal_vip_address = ENV['config']['vip']

    logger.info("ssh tunnel informations:")
    logger.info("___")

    script = "cat > /tmp/openstack_ssh_config <<EOF\n"
    script += "Host *.grid5000.fr\n"
    script += "  User " + user + " \n"
    script += "  ProxyCommand ssh -q " + user + "@194.254.60.4 nc -w1 %h %p # Access South\n"
    script += "EOF\n"

    port = 8080
    script += "ssh -F /tmp/openstack_ssh_config -N -L " + \
              str(port) + ":" + internal_vip_address + ":80 " + \
              user + "@access.grid5000.fr &\n"

    script += "echo 'http://localhost:8080'\n"

    logger.info(script)
    logger.info("___")


if __name__ == "__main__":
    args = docopt(__doc__,
                  version='lemur version 0.1',
                  options_first=True)

    argv = [args['<command>']] + args['<args>']

    if args['<command>'] == 'up':
        up(**docopt(up.__doc__, argv=argv))
    elif args['<command>'] == 'os':
        install_os(**docopt(install_os.__doc__, argv=argv))
    elif args['<command>'] == 'init':
        init_os(**docopt(init_os.__doc__, argv=argv))
    elif args['<command>'] == 'bench':
        bench(**docopt(bench.__doc__, argv=argv))
    elif args['<command>'] == 'ssh-tunnel':
        ssh_tunnel(**docopt(info.__doc__, argv=argv))
    elif args['<command>'] == 'info':
        info(**docopt(info.__doc__, argv=argv))
    else: pass

    # # If the user doesn't specify a phase in particular, then run all
    # if not args['prepare-node'] and \
    #    not args['install-os'] and \
    #    not args['init-os'] and \
    #    not args['bench'] and \
    #    not args['ssh-tunnel'] and \
    #    not args['info']:
    #    args['prepare-node'] = True
    #    args['install-os'] = True
    #    args['init-os'] = True

    # # Prepare node phase
    # if args['prepare-node']:
    #     ENV['phase'] = 'prepare-node'
    #     config_file = args['-f']
    #     ENV['config_file'] = config_file
    #     force_deploy = args['--force-deploy']
    #     tags = args['--tags'].split(',') if args['--tags'] else None
    #     prepare_node(config_file, force_deploy, tags)
    #     save_state()

    # # Run kolla phase
    # if args['install-os']:
    #     ENV['phase'] = 'install-os'
    #     install_os(args['--reconfigure'], args['--tags'])
    #     save_state()

    # # Run init phase
    # if args['init-os']:
    #     ENV['phase'] = 'init-os'
    #     init_os()
    #     save_state()

    # # Run bench phase
    # if args['bench']:
    #     ENV['phase'] = 'run-bench'
    #     bench(args['--scenarios'], args['--times'], args['--concurrency'], args['--wait'])
    #     save_state()

    # # Print information for port forwarding
    # if args['ssh-tunnel']:
    #     ssh_tunnel()

    # # Show info
    # if args ['info']:
    #     pprint.pprint(ENV)
