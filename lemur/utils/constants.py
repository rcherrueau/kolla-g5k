import os

SCRIPT_PATH = os.path.dirname(os.path.realpath(__file__))
SYMLINK_NAME = os.path.join(SCRIPT_PATH, '..', '..', 'current')
TEMPLATE_DIR = os.path.join(SCRIPT_PATH, '..', 'templates')
ANSIBLE_DIR = os.path.join(SCRIPT_PATH, '..', 'ansible')

