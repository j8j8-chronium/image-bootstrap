# Copyright (C) 2015 Sebastian Pipping <sebastian@pipping.org>
# Licensed under AGPL v3 or later

from __future__ import print_function

import errno
import glob
import os
import shutil

from textwrap import dedent

from directory_bootstrap.distros.gentoo import GentooBootstrapper
from directory_bootstrap.shared.commands import COMMAND_CHROOT

from image_bootstrap.distros.base import DISTRO_CLASS_FIELD, DistroStrategy


_ABS_PACKAGE_USE = '/etc/portage/package.use'
_ABS_PACKAGE_KEYWORDS = '/etc/portage/package.keywords'


class GentooStrategy(DistroStrategy):
    DISTRO_KEY = 'gentoo'
    DISTRO_NAME_SHORT = 'Gentoo'
    DISTRO_NAME_LONG = 'Gentoo'

    def __init__(self, messenger, executor, abs_cache_dir,
                mirror_url, max_age_days,
                stage3_date_triple_or_none, repository_date_triple_or_none,
                abs_resolv_conf):
        super(GentooStrategy, self).__init__(
                messenger,
                executor,
                abs_cache_dir,
                abs_resolv_conf,
                )

        self._mirror_url = mirror_url
        self._max_age_days = max_age_days
        self._stage3_date_triple_or_none = stage3_date_triple_or_none
        self._repository_date_triple_or_none = repository_date_triple_or_none

    def _write_etc_conf_d_hostname(self, abs_mountpoint, hostname):
        etc_conf_d = os.path.join(abs_mountpoint, 'etc/conf.d')
        try:
            os.makedirs(etc_conf_d, 0755)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise

        etc_conf_d_hostname = os.path.join(etc_conf_d, 'hostname')
        with open(etc_conf_d_hostname, 'w') as f:
            print(dedent("""\
                    # Set to the hostname of this machine
                    hostname="%s"
                    """ % hostname), file=f)

    def configure_hostname(self, abs_mountpoint, hostname):
        self._write_etc_conf_d_hostname(abs_mountpoint, hostname)

    def allow_autostart_of_services(self, abs_mountpoint, allow):
        pass  # services are not auto-started on Gentoo

    def _patch_etc_dhcpcd_conf(self, abs_mountpoint, use_mtu):
        etc_dhcpcd_conf = os.path.join(abs_mountpoint, 'etc/dhcpcd.conf')
        with open(etc_dhcpcd_conf) as f:
            input_lines = f.read().split('\n')

        ENABLED = 'option interface_mtu'
        DISABLED = '#option interface_mtu'

        output_lines = []
        configured = False
        for l in input_lines:
            if 'option interface_mtu' in l:
                commented_out = l.lstrip().startswith('#')
                if commented_out and use_mtu:
                    l = ENABLED
                elif not commented_out and not use_mtu:
                    l = DISABLE
                configured = True
            output_lines.append(l)

        if not configured:
            output_lines.append(ENABLED if use_mtu else DISABLED)

        with open(etc_dhcpcd_conf, 'w') as f:
            print('\n'.join(output_lines), file=f)

    def create_network_configuration(self, abs_mountpoint, use_mtu_tristate):
        etc_conf_d_net = os.path.join(abs_mountpoint, 'etc/conf.d/net')
        with open(etc_conf_d_net, 'w') as f:
            print(dedent("""\
                    # Generated by image-bootstrap
                    modules="dhcpcd"
                    config_eth0="dhcp"
                    """), file=f)

        if use_mtu_tristate is not None:
            self._patch_etc_dhcpcd_conf(abs_mountpoint, use_mtu_tristate)

    def _set_package_use_flags(self, abs_mountpoint, package_name, flags_str, package_atom=None):
        if package_atom is None:
            package_atom = package_name

        filename = os.path.join(abs_mountpoint, _ABS_PACKAGE_USE.lstrip('/'), package_name.replace('/', '--'))
        with open(filename, 'w') as f:
            print('# generated by image-bootstrap', file=f)
            print('%s %s' % (package_name, flags_str), file=f)

    def _set_package_keywords(self, abs_mountpoint, package_name, keywords_str, package_atom=None):
        if package_atom is None:
            package_atom = package_name

        filename = os.path.join(abs_mountpoint,
                _ABS_PACKAGE_KEYWORDS.lstrip('/'),
                package_name.replace('/', '--'),
                )
        with open(filename, 'w') as f:
            print('# generated by image-bootstrap', file=f)
            print('%s %s' % (package_name, keywords_str), file=f)

    def _install_package_atoms(self, abs_mountpoint, env, packages):
        env = env.copy().update({
            'DONT_MOUNT_BOOT': '1',  # sys-boot/grub
            'MAKEOPTS': '-j2',
        })
        self._executor.check_call([
                COMMAND_CHROOT,
                abs_mountpoint,
                'emerge',
                '--ignore-default-opts',
                '--tree',
                '--verbose',
                '--jobs', '2',
                ] + list(packages),
                env=env)

    def ensure_chroot_has_grub2_installed(self, abs_mountpoint, env):
        self._set_package_use_flags(abs_mountpoint,
                'sys-boot/grub', 'device-mapper grub_platforms_pc', 'sys-boot/grub:2')
        self._set_package_use_flags(abs_mountpoint,
                'sys-fs/lvm2', '-thin')
        self._install_package_atoms(abs_mountpoint, env, ['sys-boot/grub:2'])

    def _disable_grub2_gfxmode(self, abs_mountpoint, env):
        self._executor.check_call([
                COMMAND_CHROOT, abs_mountpoint,
                'sed',
                '/GRUB_TERMINAL=/ s,.*GRUB_TERMINAL=.*,GRUB_TERMINAL=console  # forced by image-bootstrap,',
                '-i', '/etc/default/grub',
                ], env=env)

    def _ensure_eth0_naming(self, abs_mountpoint, env):
        self._executor.check_call([
                COMMAND_CHROOT, abs_mountpoint,
                'sed',
                's,#GRUB_CMDLINE_LINUX=.*",GRUB_CMDLINE_LINUX="net.ifnames=0"  # set by image-bootstrap,',
                '-i', '/etc/default/grub',
                ], env=env)

    def generate_grub_cfg_from_inside_chroot(self, abs_mountpoint, env):
        # TODO Limit to use with OpenStack
        self._disable_grub2_gfxmode(abs_mountpoint, env)
        self._ensure_eth0_naming(abs_mountpoint, env)

        cmd = [
                COMMAND_CHROOT,
                abs_mountpoint,
                'grub2-mkconfig',
                '-o', '/boot/grub/grub.cfg',
                ]
        self._executor.check_call(cmd, env=env)

    def _get_installed_kernel_version(self, abs_mountpoint):
        prefix = 'vmlinuz-'
        kernel_bins = [os.path.basename(e) for e
                in sorted(glob.glob(os.path.join(abs_mountpoint, 'boot/%s*' % prefix)))]
        if not kernel_bins:
            raise ValueError('No kernel binary found')  # TODO proper exception

        kernel_version = kernel_bins[-1][len(prefix):]
        if len(kernel_bins) > 1:
            self._messenger.warn('Multiple kernel binaries found, picked "%s-%s" for version extraction' % (prefix, kernel_version))

        return kernel_version

    def _make_initramfs_symlink(self, abs_mountpoint):
        # NOTE: dracut default is /boot/initramfs-<kernel version>.img
        initramfs_images = [os.path.basename(e) for e
                in sorted(glob.glob(os.path.join(abs_mountpoint, 'boot/initramfs-*.img')))]
        if not initramfs_images:
            raise ValueError('No initramfs image found')  # TODO proper exception

        target_basename = initramfs_images[-1]
        if len(initramfs_images) > 1:
            self._messenger.warn('Multiple initramfs images found, picked "%s" for the symlink' % target_basename)

        os.symlink(target_basename, os.path.join(abs_mountpoint, self.get_initramfs_path().lstrip('/')))

    def generate_initramfs_from_inside_chroot(self, abs_mountpoint, env):
        kernel_version_str = self._get_installed_kernel_version(abs_mountpoint)

        self._set_package_keywords(abs_mountpoint, 'sys-kernel/dracut', '**')  # TODO ~arch
        self._install_package_atoms(abs_mountpoint, env, ['sys-kernel/dracut'])
        # NOTE: Pass kernel version to Dracut so it does not end up
        #       picking that of the host (rather than the chroot) from uname
        self._executor.check_call([
                COMMAND_CHROOT,
                abs_mountpoint,
                'dracut',
                '--kver', kernel_version_str,
                ], env=env)

        self._make_initramfs_symlink(abs_mountpoint)

    def get_chroot_command_grub2_install(self):
        return 'grub2-install'

    def get_cloud_init_datasource_cfg_path(self):
        return '/etc/cloud/cloud.cfg.d/90_datasource.cfg'

    def get_commands_to_check_for(self):
        return [
                COMMAND_CHROOT,
                ]

    def get_initramfs_path(self):
        return '/boot/initramfs'

    def get_vmlinuz_path(self):
        return '/boot/vmlinuz'

    def install_cloud_init_and_friends(self, abs_mountpoint, env):
        self._install_package_atoms(abs_mountpoint, env, ['app-emulation/cloud-init'])

    def install_sshd(self, abs_mountpoint, env):
        self._install_package_atoms(abs_mountpoint, env, ['net-misc/openssh'])

        init_script_path = os.path.join(abs_mountpoint, 'etc/init.d/sshd-need-root')
        with open(init_script_path, 'w') as f:
            print(dedent("""\
                    #!/sbin/runscript
                    # Workaround to ensure that sshd has a writable root file system
                    # during key generation
                    # https://bugs.gentoo.org/show_bug.cgi?id=554804
                    #
                    # Copyright (C) 2015 Sebastian Pipping <sebastian@pipping.org>
                    # Licensed under AGPL v3 or later

                    depend() {
                        if ! ls /etc/ssh/ssh_host_*_key 1>/dev/null 2>/dev/null; then
                            need root
                        fi
                        before sshd
                    }

                    start() { :; }
                    stop() { :; }
                    """), file=f)
            os.fchmod(f.fileno(), 0755)

    def install_dhcp_client(self, abs_mountpoint, env):
        self._install_package_atoms(abs_mountpoint, env, ['net-misc/dhcpcd'])

    def install_sudo(self, abs_mountpoint, env):
        self._set_package_use_flags(abs_mountpoint, 'app-admin/sudo', '-sendmail')
        self._install_package_atoms(abs_mountpoint, env, ['app-admin/sudo'])

    def _create_network_init_script_symlink(self, interface_name, abs_mountpoint):
        net_service = 'net.%s' % interface_name
        net_init_script = os.path.join(abs_mountpoint, 'etc/init.d', net_service)
        os.symlink('net.lo', net_init_script)
        return net_service

    def make_openstack_services_autostart(self, abs_mountpoint, env):
        net_service = self._create_network_init_script_symlink('eth0', abs_mountpoint)

        for service in (
                net_service,
                'sshd',
                'sshd-need-root',  # written by image-bootstrap above
                'cloud-init-local',
                'cloud-init',
                'cloud-config',
                'cloud-final',
                ):
            self._executor.check_call([
                COMMAND_CHROOT,
                abs_mountpoint,
                'rc-update',
                'add', service, 'default',
                ], env=env)

    def perform_in_chroot_shipping_clean_up(self, abs_mountpoint, env):
        pass  # TODO

    def perform_post_chroot_clean_up(self, abs_mountpoint):
        pass  # TODO

    def run_directory_bootstrap(self, abs_mountpoint, architecture, bootloader_approach):
        self._messenger.info('Bootstrapping %s into "%s"...'
                % (self.DISTRO_NAME_SHORT, abs_mountpoint))

        bootstrap = GentooBootstrapper(
                self._messenger,
                self._executor,
                abs_mountpoint,
                self._abs_cache_dir,
                architecture,
                self._mirror_url,
                self._max_age_days,
                self._stage3_date_triple_or_none,
                self._repository_date_triple_or_none,
                self._abs_resolv_conf,
                )
        bootstrap.run()

    def prepare_installation_of_packages(self, abs_mountpoint, env):
        for chroot_abs_path in (
                _ABS_PACKAGE_KEYWORDS,
                _ABS_PACKAGE_USE,
                ):
            try:
                os.makedirs(os.path.join(abs_mountpoint, chroot_abs_path.lstrip('/')), 0755)
            except OSError as e:
                if e.errno != errno.EEXIST:
                    raise

        self._executor.check_call([
                COMMAND_CHROOT,
                abs_mountpoint,
                'eselect', 'news', 'read', '--quiet', 'all',
                ], env=env)

    def _enable_kernel_option(self, option_name, abs_mountpoint, env):
        self._executor.check_call([
                COMMAND_CHROOT, abs_mountpoint,
                '/usr/src/linux/scripts/config',
                '--file', '/usr/src/linux/.config',
                '--enable', option_name,
                ], env=env)

    def _configure_kernel__enable_kvm_support(self, abs_mountpoint, env):
        tasks = dedent("""\
                # Based on linux-4.0.1/arch/x86/configs/kvm_guest.config
                CONFIG_NET=y
                CONFIG_NET_CORE=y
                CONFIG_NETDEVICES=y
                CONFIG_BLOCK=y
                CONFIG_BLK_DEV=y
                CONFIG_NETWORK_FILESYSTEMS=y
                CONFIG_INET=y
                CONFIG_TTY=y
                CONFIG_SERIAL_8250=y
                CONFIG_SERIAL_8250_CONSOLE=y
                CONFIG_IP_PNP=y
                CONFIG_IP_PNP_DHCP=y
                CONFIG_BINFMT_ELF=y
                CONFIG_PCI=y
                CONFIG_PCI_MSI=y
                # CONFIG_DEBUG_KERNEL=y
                CONFIG_VIRTUALIZATION=y
                CONFIG_HYPERVISOR_GUEST=y
                CONFIG_PARAVIRT=y
                CONFIG_KVM_GUEST=y
                CONFIG_VIRTIO=y
                CONFIG_VIRTIO_PCI=y
                CONFIG_VIRTIO_BLK=y
                CONFIG_VIRTIO_CONSOLE=y
                CONFIG_VIRTIO_NET=y
                CONFIG_9P_FS=y
                CONFIG_NET_9P=y
                CONFIG_NET_9P_VIRTIO=y
                """)
        for line in tasks.split('\n'):
            if not line or line.startswith('#'):
                continue
            assert line.startswith('CONFIG_')
            assert line.endswith('=y')
            option_name = line[len('CONFIG_'):-len('=y')]

            self._enable_kernel_option(option_name, abs_mountpoint, env)

    def _configure_kernel__finish(self, abs_mountpoint, env):
        self._executor.check_call([
                COMMAND_CHROOT, abs_mountpoint,
                'make',
                '-C', '/usr/src/linux',
                'olddefconfig',
                ], env=env)
        self._executor.check_call([
                COMMAND_CHROOT, abs_mountpoint,
                '/usr/src/linux/scripts/diffconfig',
                '-m',
                '/usr/src/linux/.config.initial',
                '/usr/src/linux/.config',
                ], env=env)

    def install_kernel(self, abs_mountpoint, env):
        self._set_package_keywords(abs_mountpoint, 'sys-kernel/vanilla-sources', '**')  # TODO ~arch
        self._set_package_use_flags(abs_mountpoint, 'sys-kernel/vanilla-sources', 'symlink')
        self._install_package_atoms(abs_mountpoint, env, ['sys-kernel/vanilla-sources'])
        self._executor.check_call([
                COMMAND_CHROOT, abs_mountpoint,
                'make', '-C', '/usr/src/linux', 'defconfig',
                ], env=env)
        shutil.copyfile(
                os.path.join(abs_mountpoint, 'usr/src/linux/.config'),
                os.path.join(abs_mountpoint, 'usr/src/linux/.config.initial'),
                )

        self._configure_kernel__enable_kvm_support(abs_mountpoint, env)
        self._configure_kernel__finish(abs_mountpoint, env)

        self._executor.check_call([
                COMMAND_CHROOT, abs_mountpoint,
                'make',
                '-C', '/usr/src/linux',
                '-j2',
                ], env=env)
        self._executor.check_call([
                COMMAND_CHROOT, abs_mountpoint,
                'make',
                '-C', '/usr/src/linux',
                'modules_install', 'install',
                ], env=env)

    @classmethod
    def add_parser_to(clazz, distros):
        gentoo = distros.add_parser(clazz.DISTRO_KEY, help=clazz.DISTRO_NAME_LONG)
        gentoo.set_defaults(**{DISTRO_CLASS_FIELD: clazz})

        GentooBootstrapper.add_arguments_to(gentoo)

    @classmethod
    def create(clazz, messenger, executor, options):
        return clazz(
                messenger,
                executor,
                os.path.abspath(options.cache_dir),
                options.mirror_url,
                options.max_age_days,
                options.stage3_date,
                options.repository_date,
                os.path.abspath(options.resolv_conf),
                )
