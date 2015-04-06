# Copyright 2014, 2015 IBM Corp.
#
# All Rights Reserved.
#
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
#

import logging

import mock

from nova.compute import power_state
from nova import exception
from nova import objects
from nova import test
from pypowervm import adapter as pvm_adp
from pypowervm import exceptions as pvm_exc
from pypowervm.tests.wrappers.util import pvmhttp
from pypowervm.wrappers import logical_partition as pvm_lpar

from nova_powervm.tests.virt import powervm
from nova_powervm.tests.virt.powervm import fixtures as fx
from nova_powervm.virt.powervm import vm

LPAR_HTTPRESP_FILE = "lpar.txt"
LPAR_MAPPING = (
    {
        'z3-9-5-126-127-00000001': '089ffb20-5d19-4a8c-bb80-13650627d985',
        'z3-9-5-126-208-000001f0': '668b0882-c24a-4ae9-91c8-297e95e3fe29'
    })

LOG = logging.getLogger(__name__)
logging.basicConfig()


class FakeAdapterResponse(object):
    def __init__(self, status):
        self.status = status


class TestVM(test.TestCase):
    def setUp(self):
        super(TestVM, self).setUp()
        lpar_http = pvmhttp.load_pvm_resp(LPAR_HTTPRESP_FILE)
        self.assertNotEqual(lpar_http, None,
                            "Could not load %s " %
                            LPAR_HTTPRESP_FILE)

        self.resp = lpar_http.response

        self.pypvm = self.useFixture(fx.PyPowerVM())
        self.apt = self.pypvm.apt

    def test_uuid_cache(self):
        cache = vm.UUIDCache(self.apt)
        cache.add('n1', '123')
        self.assertEqual(cache.lookup('n1'), '123')

        self.assertEqual(cache.lookup('nothing', fetch=False), None)

        cache.remove('n1')
        self.assertEqual(cache.lookup('n1', fetch=False), None)
        # Not in cache, search returns no results (empty feed)
        emptyfeed = pvm_adp.Response('meth', 'path', 200, 'reason', {})
        emptyfeed.feed = mock.MagicMock()
        emptyfeed.feed.entries = []
        self.apt.read.return_value = emptyfeed
        self.assertRaises(exception.InstanceNotFound,
                          cache.lookup, 'n1', fetch=True)
        # Ensure removing one that doesn't exist is just ignored
        self.assertEqual(cache.remove('nonexistent'), None)

        cache.load_from_lpar_wraps(pvm_lpar.LPAR.wrap(self.resp))
        for lpar in LPAR_MAPPING:
            self.assertEqual(cache.lookup(lpar), LPAR_MAPPING[lpar].upper())

        # Test fetching. Start with a fresh cache and try to look it up
        cache = vm.UUIDCache(self.apt)

        self.apt.read.return_value = self.resp
        self.assertEqual(cache.lookup('z3-9-5-126-127-00000001'),
                         '089ffb20-5d19-4a8c-bb80-13650627d985'.upper())
        # Test it returns None even when we try to look it up
        self.assertEqual(cache.lookup('Nonexistent'), None)

        exc = pvm_exc.Error('Not found', response=FakeAdapterResponse(404))
        self.apt.read.side_effect = exc
        self.assertRaises(exception.InstanceNotFound,
                          cache.lookup, 'Nonexistent')

    def test_instance_info(self):

        # Test at least one state translation
        self.assertEqual(vm._translate_vm_state('running'),
                         power_state.RUNNING)

        inst_info = vm.InstanceInfo(self.apt, 'inst_name', '1234')
        # Test the static properties
        self.assertEqual(inst_info.id, '1234')
        self.assertEqual(inst_info.cpu_time_ns, 0)

        # Check that we raise an exception if the instance is gone.
        exc = pvm_exc.Error('Not found', response=FakeAdapterResponse(404))
        self.apt.read.side_effect = exc
        self.assertRaises(exception.InstanceNotFound,
                          inst_info.__getattribute__, 'state')

        # Reset the test inst_info
        inst_info = vm.InstanceInfo(self.apt, 'inst_name', '1234')

        class FakeResp2(object):
            def __init__(self, body):
                self.body = '"%s"' % body

        resp = FakeResp2('running')

        def return_resp(*args, **kwds):
            return resp

        self.apt.read.side_effect = return_resp
        self.assertEqual(inst_info.state, power_state.RUNNING)

        # Check the __eq__ method
        inst_info1 = vm.InstanceInfo(self.apt, 'inst_name', '1234')
        inst_info2 = vm.InstanceInfo(self.apt, 'inst_name', '1234')
        self.assertEqual(inst_info1, inst_info2)
        inst_info2 = vm.InstanceInfo(self.apt, 'name', '4321')
        self.assertNotEqual(inst_info1, inst_info2)

    def test_get_lpar_feed(self):
        self.apt.read.return_value = self.resp
        feed = vm.get_lpar_feed(self.apt, 'host_uuid')
        self.assertEqual(feed, self.resp.feed)

        exc = pvm_exc.Error('Not found', response=FakeAdapterResponse(404))
        self.apt.read.side_effect = exc
        feed = vm.get_lpar_feed(self.apt, 'host_uuid')
        self.assertEqual(feed, None)

    @mock.patch('nova_powervm.virt.powervm.vm.get_lpar_feed')
    def test_get_lpar_list(self, mock_feed):
        mock_feed.return_value = self.resp.feed
        lpar_list = vm.get_lpar_list(self.apt, 'host_uuid')
        # Check the first one in the feed and the length of the feed
        self.assertEqual(lpar_list[0], 'z3-9-5-126-127-00000001')
        self.assertEqual(len(lpar_list), 21)

    @mock.patch('pypowervm.tasks.vterm.close_vterm')
    def test_dlt_lpar(self, mock_vterm):
        """Performs a delete LPAR test."""
        vm.dlt_lpar(self.apt, '12345')
        self.assertEqual(1, self.apt.delete.call_count)
        # test failure due to open vterm
        self.apt.delete.side_effect = pvm_exc.JobRequestFailed(
            error='delete', operation_name='HSCL151B')
        # If failed due to vterm test close_vterm and delete are called
        self.apt.reset_mock()
        self.assertRaises(pvm_exc.JobRequestFailed,
                          vm.dlt_lpar, self.apt, '12345')
        self.assertEqual(1, mock_vterm.call_count)
        self.assertEqual(2, self.apt.delete.call_count)

    def test_build_attr(self):
        """Perform tests against _build_attrs."""
        instance = objects.Instance(**powervm.TEST_INSTANCE)
        flavor = instance.get_flavor()
        lpar_attrs = {'memory': 2048,
                      'name': 'instance-00000001',
                      'vcpu': 1}

        # Test dedicated procs
        flavor.extra_specs = {'powervm:dedicated_proc': 'true'}
        test_attrs = dict(lpar_attrs, **{'dedicated_proc': 'true'})
        self.assertEqual(vm._build_attrs(instance, flavor), test_attrs)

        # Test dedicated procs, min/max vcpu and sharing mode
        flavor.extra_specs = {'powervm:dedicated_proc': 'true',
                              'powervm:dedicated_sharing_mode':
                                  'share_idle_procs_active',
                              'powervm:min_vcpu': '1',
                              'powervm:max_vcpu': '3'}
        test_attrs = dict(lpar_attrs,
                          **{'dedicated_proc': 'true',
                             'sharing_mode': 'sre idle procs active',
                             'min_vcpu': '1', 'max_vcpu': '3'})
        self.assertEqual(vm._build_attrs(instance, flavor), test_attrs)

        # Test shared proc sharing mode
        flavor.extra_specs = {'powervm:uncapped': 'true'}
        test_attrs = dict(lpar_attrs, **{'sharing_mode': 'uncapped'})
        self.assertEqual(vm._build_attrs(instance, flavor), test_attrs)

        # Test availability priority
        flavor.extra_specs = {'powervm:availability_priority': '150'}
        test_attrs = dict(lpar_attrs, **{'avail_priority': '150'})
        self.assertEqual(vm._build_attrs(instance, flavor), test_attrs)

        # Test min, max proc units
        flavor.extra_specs = {'powervm:min_proc_units': '0.5',
                              'powervm:max_proc_units': '2.0'}
        test_attrs = dict(lpar_attrs, **{'min_proc_units': '0.5',
                                         'max_proc_units': '2.0'})
        self.assertEqual(vm._build_attrs(instance, flavor), test_attrs)

        # Test min, max mem
        flavor.extra_specs = {'powervm:min_mem': '1024',
                              'powervm:max_mem': '4096'}
        test_attrs = dict(lpar_attrs, **{'min_mem': '1024', 'max_mem': '4096'})
        self.assertEqual(vm._build_attrs(instance, flavor), test_attrs)

    @mock.patch('nova_powervm.virt.powervm.vm.UUIDCache')
    @mock.patch('pypowervm.wrappers.entry_wrapper.EntryWrapper.wrap')
    @mock.patch('pypowervm.utils.lpar_builder.DefaultStandardize')
    @mock.patch('pypowervm.utils.lpar_builder.LPARBuilder')
    def test_crt_lpar(self, mock_bldr, mock_stdz, mock_entrywrap, mock_cache):
        instance = objects.Instance(**powervm.TEST_INSTANCE)
        flavor = instance.get_flavor()
        flavor.extra_specs = {'powervm:dedicated_proc': 'true'}

        host_wrapper = mock.Mock()
        singleton = mock.Mock()
        mock_cache.get_cache.return_value = singleton
        vm.crt_lpar(self.apt, host_wrapper, instance, flavor)
        self.assertTrue(self.apt.create.called)
        singleton.add.assert_called_with(instance.name, mock.ANY)

        flavor.extra_specs = {'powervm:BADATTR': 'true'}
        host_wrapper = mock.Mock()
        self.assertRaises(exception.InvalidAttribute, vm.crt_lpar,
                          self.apt, host_wrapper, instance, flavor)

    @mock.patch('nova_powervm.virt.powervm.vm.get_pvm_uuid')
    @mock.patch('pypowervm.tasks.cna.crt_cna')
    def test_crt_vif(self, mock_crt_cna, mock_pvm_uuid):
        """Tests that a VIF can be created."""

        # Set up the mocks
        fake_vif = {'network': {'meta': {'vlan': 5}},
                    'address': 'aabbccddeeff'}

        def validate_of_crt(*kargs, **kwargs):
            self.assertEqual('fake_host', kargs[1])
            self.assertEqual(5, kargs[3])
            self.assertEqual('aabbccddeeff', kwargs['mac_addr'])
        mock_crt_cna.side_effect = validate_of_crt

        # Invoke
        vm.crt_vif(mock.MagicMock(), mock.MagicMock(), 'fake_host', fake_vif)

        # Validate (along with validate method above)
        self.assertEqual(1, mock_crt_cna.call_count)
