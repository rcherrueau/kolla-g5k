# -*- coding: utf-8 -*-

from ansible.inventory import Inventory
import ansible.callbacks
import ansible.playbook

import logging

def run_ansible(playbooks, inventory_path, extra_vars={}, tags=None):
    inventory = Inventory(inventory_path)

    for path in playbooks:
        logging.info("Running playbook %s with vars:\n%s" % (style.emph(path), extra_vars))
        stats = ansible.callbacks.AggregateStats()
        playbook_cb = ansible.callbacks.PlaybookCallbacks(verbose=1)

        pb = ansible.playbook.PlayBook(
            playbook=path,
            inventory=inventory,
            extra_vars=extra_vars,
            stats=stats,
            callbacks=playbook_cb,
            only_tags=tags,
            runner_callbacks=
              ansible.callbacks.PlaybookRunnerCallbacks(stats, verbose=1)
        )

        pb.run()

        hosts = pb.stats.processed.keys()
        failed_hosts = []
        unreachable_hosts = []

        for h in hosts:
            t = pb.stats.summarize(h)
            if t['failures'] > 0:
                failed_hosts.append(h)

            if t['unreachable'] > 0:
                unreachable_hosts.append(h)

        if len(failed_hosts) > 0:
            logger.error("Failed hosts: %s" % failed_hosts)
        if len(unreachable_hosts) > 0:
            logger.error("Unreachable hosts: %s" % unreachable_hosts)

def render_template(template_path, vars, output_path):
    loader = jinja2.FileSystemLoader(searchpath='.')
    env = jinja2.Environment(loader=loader)
    template = env.get_template(template_path)

    rendered_text = template.render(vars)
    with open(output_path, 'w') as f:
        f.write(rendered_text)

def generate_inventory(roles, base_inventory, dest):
    """
    Generate the inventory.
    It will generate a group for each role in roles and
    concatenate them with the base_inventory file.
    The generated inventory is written in dest
    """
    with open(dest, 'w') as f:
        f.write(to_ansible_group_string(roles))
        with open(base_inventory, 'r') as a:
            for line in a:
                f.write(line)

    logger.info("Inventory file written to " + style.emph(dest))

def to_ansible_group_string(roles):
    """
    Transform a role list (oar) to an ansible list of groups (inventory)
    Make sure the mandatory group are set as well
    e.g
    {
    'role1': ['n1', 'n2', 'n3'],
    'role12: ['n4']

    }
    ->
    [role1]
    n1
    n2
    n3
    [role2]
    n4
    """
    inventory = []
    mandatory = [group for group in KOLLA_MANDATORY_GROUPS if group not in roles.keys()]
    for group in mandatory:
        inventory.append("[%s]" % (group))

    for role, nodes in roles.items():
        inventory.append("[%s]" % (role))
        inventory.extend(map(lambda n: "%s ansible_ssh_user=root g5k_role=%s" % (n.address, role), nodes))
    inventory.append("\n")
    return "\n".join(inventory)

def generate_kolla_files(config_vars, kolla_vars, directory):
    # get the static parameters from the config file
    kolla_globals = config_vars
    # add the generated parameters
    kolla_globals.update(kolla_vars)
    # write to file in the result dir
    globals_path = os.path.join(directory, 'globals.yml')
    with open(globals_path, 'w') as f:
        yaml.dump(kolla_globals, f, default_flow_style=False)

    logger.info("Wrote " + style.emph(globals_path))

    # copy the passwords file
    passwords_path = os.path.join(directory, "passwords.yml")
    call("cp %s/passwords.yml %s" % (TEMPLATE_DIR, passwords_path), shell=True)
    logger.info("Password file is copied to  %s" % (passwords_path))

    # admin openrc
    admin_openrc_path = os.path.join(directory, 'admin-openrc')
    admin_openrc_vars = {
        'keystone_address': kolla_vars['kolla_internal_vip_address']
    }
    render_template('templates/admin-openrc.jinja2', admin_openrc_vars, admin_openrc_path)
    logger.info("admin-openrc generated in %s" % (admin_openrc_path))
