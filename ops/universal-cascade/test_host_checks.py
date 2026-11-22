"""Read-only host checks with real-shaped ip/ss/proc fixtures; no host commands."""
import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('cascade_host_checks_tests', HERE / 'host_checks.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class HostChecksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.calls = []
        self.links = [{'ifindex': 1, 'ifname': 'lo', 'flags': ['LOOPBACK', 'UP'], 'link_type': 'loopback'},
                      {'ifindex': 2, 'ifname': 'eth0', 'flags': ['BROADCAST', 'UP'], 'link_type': 'ether'}]
        self.sockets = ''
        self.package = 'zabbix-agent'
        self.executable = '/usr/sbin/zabbix_agentd'
        self.installed = True

    def command(self, *args):
        self.calls.append(args)
        if args == ('ip', '-j', '-d', 'link', 'show'):
            return json.dumps(self.links)
        if args == ('ip', '-j', 'link', 'show', 'type', 'wireguard'):
            return '[{},{}]'
        if args == ('ss', '-H', '-lntp', 'sport = :10050'):
            return self.sockets
        if args == ('dpkg-query', '-S', self.executable):
            return self.package + ': ' + self.executable + '\n'
        if args == ('dpkg-query', '-W', '-f=${Status}', self.package):
            return 'install ok installed' if self.installed else 'deinstall ok config-files'
        raise AssertionError('Unexpected command: ' + repr(args))

    def agent(self, executable='/usr/sbin/zabbix_agentd', service='zabbix-agent.service', pid=123):
        self.executable = executable
        self.package = 'zabbix-agent2' if executable.endswith('zabbix_agent2') else 'zabbix-agent'
        path = self.root / executable.lstrip('/')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture executable')
        path.chmod(0o755)
        proc = self.root / 'proc' / str(pid)
        proc.mkdir(parents=True)
        (proc / 'exe').symlink_to(executable)
        (proc / 'cgroup').write_text('0::/system.slice/' + service + '\n')
        self.sockets += 'LISTEN 0 4096 0.0.0.0:10050 0.0.0.0:* users:(("zabbix_agentd",pid=' + str(pid) + ',fd=4))\n'
        return path

    def monitoring(self):
        return h.verified_monitoring_ports(self.command, root=self.root, uid=os.getuid())

    def test_filtered_empty_record_bug_is_avoided_by_unfiltered_detailed_command(self):
        self.assertEqual(h.wireguard_interfaces(self.command), [])
        self.assertEqual(self.calls, [('ip', '-j', '-d', 'link', 'show')])

    def test_wireguard_discovery_ignores_other_kinds(self):
        self.links += [{'ifindex': 3, 'ifname': 'br0', 'linkinfo': {'info_kind': 'bridge'}},
                       {'ifindex': 4, 'ifname': 'spnwgabc123', 'linkinfo': {'info_kind': 'wireguard'}}]
        self.assertEqual(h.wireguard_interfaces(self.command), ['spnwgabc123'])

    def test_malformed_wireguard_records_cannot_silently_disappear(self):
        for row in ({}, {'linkinfo': {'info_kind': 'wireguard'}},
                    {'ifname': 'wg0', 'linkinfo': []}, {'ifname': 'wg0', 'linkinfo': {}}):
            with self.subTest(row=row), self.assertRaises(h.CheckError):
                self.links = [row]
                h.wireguard_interfaces(self.command)

    def test_no_monitoring_listener_requires_no_packages_or_proc_access(self):
        self.assertEqual(self.monitoring(), [])
        self.assertEqual(self.calls, [('ss', '-H', '-lntp', 'sport = :10050')])

    def test_installed_systemd_zabbix_agent_is_preserved(self):
        self.agent()
        self.assertEqual(self.monitoring(), [10050])

    def test_installed_agent2_and_ipv6_listener_are_verified(self):
        self.agent('/usr/sbin/zabbix_agent2', 'zabbix-agent2.service')
        self.sockets = self.sockets.replace('0.0.0.0:10050', '[::]:10050')
        self.assertEqual(self.monitoring(), [10050])

    def test_unknown_executable_cannot_hide_behind_zabbix_display_name(self):
        self.agent('/usr/sbin/not-zabbix')
        with self.assertRaises(h.CheckError) as caught:
            self.monitoring()
        self.assertNotIn('not-zabbix', str(caught.exception))

    def test_missing_process_owner_is_rejected(self):
        self.sockets = 'LISTEN 0 4096 0.0.0.0:10050 0.0.0.0:*\n'
        with self.assertRaises(h.CheckError):
            self.monitoring()

    def test_wrong_service_package_writable_binary_and_removed_package_are_rejected(self):
        path = self.agent()
        cgroup = self.root / 'proc/123/cgroup'
        cgroup.write_text('0::/system.slice/other.service\n')
        with self.assertRaises(h.CheckError):
            self.monitoring()
        cgroup.write_text('0::/system.slice/zabbix-agent.service\n')
        self.package = 'foreign-package'
        with self.assertRaises(h.CheckError):
            self.monitoring()
        self.package = 'zabbix-agent'
        path.chmod(0o777)
        with self.assertRaises(h.CheckError):
            self.monitoring()
        path.chmod(0o755)
        self.installed = False
        with self.assertRaises(h.CheckError):
            self.monitoring()

    def test_every_listener_process_must_pass(self):
        self.agent()
        self.sockets += 'LISTEN 0 4096 [::]:10050 [::]:* users:(("unknown",pid=456,fd=4))\n'
        with self.assertRaises(h.CheckError):
            self.monitoring()

    def test_explicit_unknown_tcp10050_is_kept_without_process_or_package_trust(self):
        self.sockets = 'LISTEN 0 4096 0.0.0.0:10050 0.0.0.0:* users:(("unknown",pid=987,fd=4))\n'
        self.assertEqual(h.verified_monitoring_ports(self.command, root=self.root,
                                                   uid=os.getuid(), keep_tcp_ports=[10050]), [10050])
        self.assertEqual(self.calls, [])
        with self.assertRaises(h.CheckError):
            self.monitoring()

    def test_explicit_policy_is_stable_while_the_service_is_down(self):
        self.assertEqual(h.verified_monitoring_ports(self.command, keep_tcp_ports=(10050,)), [10050])
        self.assertEqual(self.calls, [])

    def test_explicit_policy_never_accepts_other_ports_or_coerces_types(self):
        for value in (True, False, 10050, '10050', [True], [10050, 10050], [443], [10050.0],
                      {'port': 10050}, {10050}, None):
            with self.subTest(value=value), self.assertRaises(h.CheckError):
                h.verified_monitoring_ports(self.command, keep_tcp_ports=value)
        self.assertEqual(self.calls, [])

    def policy(self, value=None):
        parent = self.root / 'private-policy'
        parent.mkdir(mode=0o700, exist_ok=True)
        path = parent / 'host-policy.json'
        path.write_text(json.dumps(value if value is not None else {'version': 1, 'keepTcpPorts': [10050]}))
        path.chmod(0o600)
        return path

    def test_private_policy_roundtrip_and_empty_policy(self):
        for ports in ([], [10050]):
            self.assertEqual(h.load_policy(self.policy({'version': 1, 'keepTcpPorts': ports}), uid=os.getuid()),
                             {'version': 1, 'keepTcpPorts': ports})

    def test_policy_requires_exact_schema_and_supported_port(self):
        for value in ({}, {'version': 2, 'keepTcpPorts': []}, {'version': True, 'keepTcpPorts': []},
                      {'version': 1, 'keepTcpPorts': [443]}, {'version': 1, 'keepTcpPorts': [10050, 10050]},
                      {'version': 1, 'keepTcpPorts': [], 'ignoreOtherPorts': True}, []):
            with self.subTest(value=value), self.assertRaises(h.CheckError):
                h.load_policy(self.policy(value), uid=os.getuid())

    def test_policy_rejects_symlinks_hardlinks_wrong_modes_and_wrong_owner(self):
        path = self.policy()
        symlink = path.parent / 'symlink.json'
        symlink.symlink_to(path)
        with self.assertRaises(h.CheckError):
            h.load_policy(symlink, uid=os.getuid())
        hardlink = path.parent / 'hardlink.json'
        os.link(path, hardlink)
        with self.assertRaises(h.CheckError):
            h.load_policy(path, uid=os.getuid())
        hardlink.unlink()
        path.chmod(0o644)
        with self.assertRaises(h.CheckError):
            h.load_policy(path, uid=os.getuid())
        path.chmod(0o600)
        path.parent.chmod(0o755)
        with self.assertRaises(h.CheckError):
            h.load_policy(path, uid=os.getuid())
        path.parent.chmod(0o700)
        with self.assertRaises(h.CheckError):
            h.load_policy(path, uid=os.getuid() + 1)
        alias = self.root / 'alias'
        alias.symlink_to(path.parent, target_is_directory=True)
        with self.assertRaises(h.CheckError):
            h.load_policy(alias / path.name, uid=os.getuid())

    def test_damaged_policy_errors_never_echo_private_json_or_path(self):
        path = self.policy()
        path.write_text('PRIVATE-CONTENT-MUST-NOT-APPEAR')
        with self.assertRaises(h.CheckError) as caught:
            h.load_policy(path, uid=os.getuid())
        self.assertNotIn('PRIVATE-CONTENT', str(caught.exception))
        self.assertNotIn(str(path), str(caught.exception))

    def test_cli_monitoring_loads_policy_and_emits_json_only(self):
        with patch.object(h, 'load_policy', return_value={'version': 1, 'keepTcpPorts': [10050]}) as policy, \
                patch.object(h, '_run', side_effect=AssertionError('No host commands for explicit policy')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            h.main(['monitoring', '/root/private/host-policy.json'])
        policy.assert_called_once_with('/root/private/host-policy.json')
        self.assertEqual(output.getvalue(), '[10050]\n')
        with self.assertRaises(h.CheckError):
            h.main(['wireguard', '/root/private/host-policy.json'])


if __name__ == '__main__':
    unittest.main()
