# -*- coding: utf-8 -*-
"""Lemur: Monitor and test your OpenStack.

usage: lemur [-h|--help] [-v|-vv|-vvv] <command> [<args> ...]

Options:
  -h --help      Show this help message.
  -v -vv -vvv    Verbose mode.

Commands:
  up             Get resources and install the docker registry.
  os             Run kolla and install OpenStack.
  init           TODO: explain me.
  bench          Run rally on this OpenStack.
  ssh-tunnel     Print configuration for port forwarding with horizon.
  info           Show information of the actual deployment.

See 'lemur <command> --help' for more information on a specific command.
"""
from utils.extra import *
from utils.lemurtask import lemurtask

from datetime import datetime
import logging

from docopt import docopt
import requests
import pprint
from operator import itemgetter, attrgetter

from keystoneauth1.identity import v3
from keystoneauth1 import session
from glanceclient import client as gclient
from keystoneclient.v3 import client as kclient

import os
import sys
from subprocess import call

import yaml

CALL_PATH = os.getcwd()
SCRIPT_PATH = os.path.dirname(os.path.realpath(__file__))
SYMLINK_NAME = os.path.join(SCRIPT_PATH, 'current')
TEMPLATE_DIR = os.path.join(SCRIPT_PATH, 'templates')

INTERNAL_IP = 0
REGISTRY_IP = 1
INFLUX_IP   = 2
GRAFANA_IP  = 3
NEUTRON_IP  = 4

NETWORK_IFACE  = 0
EXTERNAL_IFACE = 1

@lemurtask("""
usage: lemur up [-f CONFIG_PATH] [--force-deploy] [-t TAGS | --tags=TAGS]
                [--provider=PROVIDER] [-v|-vv|-vvv]

  -h --help            Show this help message.
  -f CONFIG_PATH       Path to the configuration file describing the
                       deployment [default: ./reservation.yaml].
  --force-deploy       Force deployment [default: False].
  -t TAGS --tags=TAGS  Only run ansible tasks tagged with these values.
  --provider=PROVIDER  The provider name [default: G5K].

""")
def up(provider=None, env=None, **kwargs):
    logging.info('phase[up]')

    # Loads the configuration file
    config_file = kwargs['-f']
    if os.path.isfile(config_file):
        env['config_file'] = config_file
        with open(config_file, 'r') as f:
            env['config'].update(yaml.load(f))
            logging.info("Reloaded config %s", env['config'])
    else:
        logging.error('Configuration file %s does not exist', config_file)

    # Calls the provider and initialise resources
    rsc,ips,eths = provider.init(env['config'], kwargs['--force-deploy'])

    env['rsc'] = rsc
    env['ips'] = ips
    env['eths'] = eths

    # Generates a directory for results
    resultdir_name = 'lemur_' + datetime.today().isoformat()
    resultdir = os.path.join(CALL_PATH, resultdir_name)
    os.mkdir(resultdir)
    logging.info('Generates result directory %s' % resultdir_name)

    env['resultdir'] = resultdir

    # Generates inventory for ansible/kolla
    base_inventory = env['config']['inventory']
    inventory = os.path.join(resultdir, 'multinode')
    generate_inventory(env['rsc'], base_inventory, inventory)
    logging.info('Generates inventory %s' % inventory)

    env['inventory'] = inventory

    # Set variables required by playbooks of the application
    env['config'].update({
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
    with open(passwords) as f:
        env['config'].update(yaml.load(f))

    # Executes hooks and runs playbook that initializes resources (eg,
    # installs the registry, install monitoring tools, ...)
    provider.before_preintsall(env)
    up_playbook = os.path.join(SCRIPT_PATH, 'ansible', 'up.yml')
    run_ansible([up_playbook], inventory, env['config'], kwargs['--tags'])
    provider.after_preintsall(env)

    # Symlink current directory
    link = os.path.abspath(SYMLINK_NAME)
    try:
        os.remove(link)
    except OSError:
        pass
    os.symlink(resultdir, link)
    logging.info("Symlinked %s to %s" % (resultdir, link))


@lemurtask("""
usage: lemur os [--reconfigure] [-t TAGS | --tags=TAGS] [-v|-vv|-vvv]

  -h --help            Show this help message.
  -t TAGS --tags=TAGS  Only run ansible tasks tagged with these values.
  --reconfigure        Reconfigure the services after a deployment.
""")
def install_os(env=None, **kwargs):
    # Generates kolla globals.yml, passwords.yml
    generate_kolla_files(env['config']["kolla"], env['config'], env['resultdir'])

    # Clone or pull Kolla
    if os.path.isdir('kolla'):
        logging.info("Remove previous Kolla installation")
        kolla_path = os.path.join(SCRIPT_PATH, "kolla")
        call("rm -rf %s" % kolla_path, shell=True)

    logging.info("Cloning Kolla")
    call("cd %s ; git clone %s -b %s > /dev/null" % (SCRIPT_PATH, env['kolla_repo'], env['kolla_branch']), shell=True)

    logging.warning(("Patching kolla, this should be ",
                     "deprecated with the new version of Kolla"))

    playbook = os.path.join(SCRIPT_PATH, "ansible", "patches.yml")
    run_ansible([playbook], env['inventory'], env['config'])

    kolla_cmd = [os.path.join(SCRIPT_PATH, "kolla", "tools", "kolla-ansible")]

    if kwargs.has_key('--reconfigure'):
        kolla_cmd.append('reconfigure')
    else:
        kolla_cmd.append('deploy')

    kolla_cmd.extend(["-i", "%s/multinode" % SYMLINK_NAME,
                      "--configdir", "%s" % SYMLINK_NAME])

    if kwargs.has_key('--tags'): kolla_cmd.extend(['--tags', args])

    call(kolla_cmd)


@lemurtask("""
usage: lemur init [-v|-vv|-vvv]

  -h --help            Show this help message.
""")
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
        logging.info("Creating role %s" % role_name)
        keystone.roles.create(role_name)

    # Install cirros with glance client if absent
    glance = gclient.Client('2', session=sess)
    cirros_name = 'cirros.uec'
    if cirros_name not in map(itemgetter('name'), glance.images.list()):
        # Download cirros
        image_url  = 'http://download.cirros-cloud.net/0.3.4/'
        image_name = 'cirros-0.3.4-x86_64-disk.img'
        logging.info("Downloading %s at %s..." % (cirros_name, image_url))
        cirros_img = requests.get(image_url + '/' + image_name)

        # Install cirros
        cirros = glance.images.create(name=cirros_name,
                                      container_format='bare',
                                      disk_format='qcow2',
                                      visibility='public')
        glance.images.upload(cirros.id, cirros_img.content)
        logging.info("%s has been created on OpenStack" %  cirros_name)

@lemurtask(
"""usage: lemur bench [--scenarios=SCENARIOS] [--times=TIMES]
                      [--concurrency=CONCURRENCY] [--wait=WAIT]
                      [-v|-vv|-vvv]

  -h --help                 Show this help message.
  --scenarios=SCENARIOS     Name of the files containing the scenarios
                            to launch. The file must reside under the
                            rally directory.
  --times=TIMES             Number of times to run each scenario
                            [default: 1].
  --concurrency=CONCURRENCY Concurrency level of the tasks in each
                            scenario [default: 1].
  --wait=WAIT               Seconds to wait between two scenarios
                            [default: 0].
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

    logging.info("ssh tunnel informations:")
    logging.info("___")

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

    logging.info(script)
    logging.info("___")

@lemurtask("usage: lemur info")
def info(env = None, **kwargs):
    pprint.pprint(env)


if __name__ == "__main__":
    args = docopt(__doc__,
                  version='lemur version 0.1',
                  options_first=True)

    if '-v' in args['<args>']:
        logging.basicConfig(level = logging.WARNING)
    elif '-vv' in args['<args>']:
        logging.basicConfig(level = logging.INFO)
    elif '-vvv' in args['<args>']:
        logging.basicConfig(level = logging.DEBUG)
    else:
        logging.basicConfig(level = logging.ERROR)

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
        ssh_tunnel(**docopt(ssh_tunnel.__doc__, argv=argv))
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
