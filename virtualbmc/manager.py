#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import errno
import os
import sys
import shutil
import signal

import daemon
from six.moves import configparser

import exception
import log
from virtualbmc import VirtualBMC
import utils
import config as vbmc_config

LOG = log.get_logger()

# BMC status
RUNNING = 'running'
DOWN = 'down'

DEFAULT_SECTION = 'VirtualBMC'

CONF = vbmc_config.get_config()


class VirtualBMCManager(object):

    def _parse_config(self, domain_name):
        config_path = os.path.join(utils.CONFIG_PATH, domain_name, 'config')
        if not os.path.exists(config_path):
            raise exception.DomainNotFound(domain=domain_name)

        config = configparser.ConfigParser()
        config.read(config_path)

        bmc = {}
        for item in ('username', 'password', 'address', 'domain_name',
                     'libvirt_uri', 'libvirt_sasl_username',
                     'libvirt_sasl_password'):
            try:
                value = config.get(DEFAULT_SECTION, item)
            except configparser.NoOptionError:
                value = None

            bmc[item] = value

        # Port needs to be int
        bmc['port'] = config.getint(DEFAULT_SECTION, 'port')

        return bmc

    def _show(self, domain_name):
        running = False
        try:
            pidfile_path = os.path.join(utils.CONFIG_PATH, domain_name, 'pid')
            with open(pidfile_path, 'r') as f:
                pid = int(f.read())

            running = utils.is_pid_running(pid)
        except IOError:
            pass

        bmc_config = self._parse_config(domain_name)
        bmc_config['status'] = RUNNING if running else DOWN

        # mask the passwords if requested
        if not CONF['default']['show_passwords']:
            bmc_config = utils.mask_dict_password(bmc_config)

        return bmc_config

    def add(self, username, password, port, address, domain_name, libvirt_uri,
            libvirt_sasl_username, libvirt_sasl_password):

        import pdb; pdb.set_trace()
        # check libvirt's connection and if domain exist prior to adding it
        utils.check_libvirt_connection_and_domain(
            libvirt_uri, domain_name,
            sasl_username=libvirt_sasl_username,
            sasl_password=libvirt_sasl_password)

        domain_path = os.path.join(utils.CONFIG_PATH, domain_name)
        try:
            os.makedirs(domain_path)
        except OSError as e:
            if e.errno == errno.EEXIST:
                sys.exit('Domain %s already exist' % domain_name)

        config_path = os.path.join(domain_path, 'config')
        with open(config_path, 'w') as f:
            config = configparser.ConfigParser()
            config.add_section(DEFAULT_SECTION)
            config.set(DEFAULT_SECTION, 'username', username)
            config.set(DEFAULT_SECTION, 'password', password)
            config.set(DEFAULT_SECTION, 'port', port)
            config.set(DEFAULT_SECTION, 'address', address)
            config.set(DEFAULT_SECTION, 'domain_name', domain_name)
            config.set(DEFAULT_SECTION, 'libvirt_uri', libvirt_uri)

            if libvirt_sasl_username and libvirt_sasl_password:
                config.set(DEFAULT_SECTION, 'libvirt_sasl_username',
                           libvirt_sasl_username)
                config.set(DEFAULT_SECTION, 'libvirt_sasl_password',
                           libvirt_sasl_password)

            config.write(f)

    def delete(self, domain_name):
        domain_path = os.path.join(utils.CONFIG_PATH, domain_name)
        if not os.path.exists(domain_path):
            raise exception.DomainNotFound(domain=domain_name)

        try:
            self.stop(domain_name)
        except exception.VirtualBMCError:
            pass

        shutil.rmtree(domain_path)

    def start(self, domain_name):
        domain_path = os.path.join(utils.CONFIG_PATH, domain_name)
        if not os.path.exists(domain_path):
            raise exception.DomainNotFound(domain=domain_name)

        bmc_config = self._parse_config(domain_name)

        # check libvirt's connection and domain prior to starting the BMC
        utils.check_libvirt_connection_and_domain(
            bmc_config['libvirt_uri'], domain_name,
            sasl_username=bmc_config['libvirt_sasl_username'],
            sasl_password=bmc_config['libvirt_sasl_password'])

        # mask the passwords if requested
        log_config = bmc_config.copy()
        if not CONF['default']['show_passwords']:
            log_config = utils.mask_dict_password(bmc_config)

        LOG.debug('Starting a Virtual BMC for domain %(domain)s with the '
                  'following configuration options: %(config)s',
                  {'domain': domain_name,
                   'config': ' '.join(['%s="%s"' % (k, log_config[k])
                                       for k in log_config])})

        with daemon.DaemonContext(stderr=sys.stderr,
                                  files_preserve=[LOG.handler.stream, ]):
            # FIXME(lucasagomes): pyghmi start the sockets when the
            # class is instantiated, therefore we need to create the object
            # within the daemon context

            try:
                vbmc = VirtualBMC(**bmc_config)
            except Exception as e:
                msg = ('Error starting a Virtual BMC for domain %(domain)s. '
                       'Error: %(error)s' % {'domain': domain_name,
                                             'error': e})
                LOG.error(msg)
                raise exception.VirtualBMCError(msg)

            # Save the PID number
            pidfile_path = os.path.join(domain_path, 'pid')
            with open(pidfile_path, 'w') as f:
                f.write(str(os.getpid()))

            LOG.info('Virtual BMC for domain %s started', domain_name)
            vbmc.listen()

    def stop(sel, domain_name):
        LOG.debug('Stopping Virtual BMC for domain %s', domain_name)
        domain_path = os.path.join(utils.CONFIG_PATH, domain_name)
        if not os.path.exists(domain_path):
            raise exception.DomainNotFound(domain=domain_name)

        pidfile_path = os.path.join(domain_path, 'pid')
        pid = None
        try:
            with open(pidfile_path, 'r') as f:
                pid = int(f.read())
        except IOError:
            raise exception.VirtualBMCError(
                'Error stopping the domain %s: PID file not '
                'found' % domain_name)
        else:
            os.remove(pidfile_path)

        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    def list(self):
        bmcs = []
        try:
            for domain in os.listdir(utils.CONFIG_PATH):
                if os.path.isdir(os.path.join(utils.CONFIG_PATH, domain)):
                    bmcs.append(self._show(domain))
        except OSError as e:
            if e.errno == errno.EEXIST:
                return bmcs

        return bmcs

    def show(self, domain_name):
        return self._show(domain_name)
