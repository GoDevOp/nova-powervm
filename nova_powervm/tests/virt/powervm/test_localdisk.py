# Copyright 2015 IBM Corp.
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

import mock

from nova import exception as nova_exc
from nova import objects
from nova import test
import os
from pypowervm import adapter as adpt
from pypowervm.tests.wrappers.util import pvmhttp
from pypowervm.wrappers import virtual_io_server as vios_w

from nova_powervm.tests.virt import powervm
from nova_powervm.virt.powervm import localdisk as ld


VOL_GRP_WITH_VIOS = 'fake_volume_group_with_vio_data.txt'
VIOS_WITH_VOL_GRP = 'fake_vios_with_volume_group_data.txt'


class TestLocalDisk(test.TestCase):
    """Unit Tests for the LocalDisk storage driver."""

    def setUp(self):
        super(TestLocalDisk, self).setUp()

        # Find directory for response file(s)
        data_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(data_dir, 'data')

        def resp(file_name):
            file_path = os.path.join(data_dir, file_name)
            return pvmhttp.load_pvm_resp(file_path).get_response()

        self.vg_to_vio = resp(VOL_GRP_WITH_VIOS)
        self.vio_to_vg = resp(VIOS_WITH_VOL_GRP)

    def get_ls(self, adpt):
        local = ld.LocalStorage({'adapter': adpt,
                                 'host_uuid': 'host_uuid',
                                 'vios_name': 'vios_name',
                                 'vios_uuid': 'vios_uuid'})
        return local

    @mock.patch('pypowervm.adapter.Adapter')
    @mock.patch('pypowervm.jobs.upload_lv.upload_new_vdisk')
    @mock.patch('nova_powervm.virt.powervm.localdisk.LocalStorage.'
                '_get_vg_uuid')
    @mock.patch('nova_powervm.virt.powervm.localdisk.LocalStorage.'
                '_get_disk_name')
    @mock.patch('nova_powervm.virt.powervm.localdisk.IterableToFileAdapter')
    @mock.patch('nova.image.API')
    def test_create_volume_from_image(self, mock_img_api, mock_file_adpt,
                                      mock_get_dname, mock_vg_uuid,
                                      mock_upload_lv, mock_adpt):
        mock_img = {'id': 'fake_id', 'size': 50}
        mock_get_dname.return_value = 'fake_vol'

        vol_name = self.get_ls(mock_adpt).create_volume_from_image(None, None,
                                                                   mock_img,
                                                                   20)
        mock_upload_lv.assert_called_with(mock.ANY, mock.ANY, mock.ANY,
                                          mock.ANY, 'fake_vol', 50,
                                          d_size=21474836480L)
        self.assertEqual('fake_vol', vol_name.get('device_name'))

    @mock.patch('pypowervm.wrappers.storage.VolumeGroup')
    @mock.patch('nova_powervm.virt.powervm.localdisk.LocalStorage.'
                '_get_vg_uuid')
    @mock.patch('nova_powervm.virt.powervm.localdisk.LocalStorage.'
                '_get_vg')
    @mock.patch('pypowervm.adapter.Adapter')
    def test_capacity(self, mock_adpt, mock_get_vg, mock_vg_uuid, mock_vg):
        """Tests the capacity methods."""

        # Set up the mock data.  This will simulate our vg wrapper
        wrap = mock.MagicMock(name='vg_wrapper')
        type(wrap).capacity = mock.PropertyMock(return_value='5120')
        type(wrap).available_size = mock.PropertyMock(return_value='2048')

        mock_vg.load_from_response.return_value = wrap
        local = self.get_ls(mock_adpt)

        self.assertEqual(5120.0, local.capacity)
        self.assertEqual(3072.0, local.capacity_used)

    @mock.patch('nova_powervm.virt.powervm.localdisk.LocalStorage.'
                '_get_vg_uuid')
    @mock.patch('pypowervm.adapter.Adapter')
    def test_disconnect_image_volume(self, mock_adpt, mock_vg_uuid):
        """Tests the disconnect_image_volume method."""

        # Set up the mock data.
        resp = mock.MagicMock(name='resp')
        type(resp).body = mock.PropertyMock(return_value='2')
        mock_vg_uuid.return_value = 'd5065c2c-ac43-3fa6-af32-ea84a3960291'

        mock_adpt.read.side_effect = [self.vio_to_vg, resp]

        def validate_update(*kargs, **kwargs):
            # Make sure that the mappings are only 1 (the remaining vopt)
            self.assertEqual([vios_w.XAG_VIOS_SCSI_MAPPING], kwargs['xag'])
            vio = vios_w.VirtualIOServer(adpt.Entry({}, kargs[0]))
            self.assertEqual(1, len(vio.scsi_mappings))
        mock_adpt.update.side_effect = validate_update

        local = self.get_ls(mock_adpt)
        local.disconnect_image_volume(mock.MagicMock(), mock.MagicMock(), '2')
        self.assertEqual(1, mock_adpt.update.call_count)

    @mock.patch('nova_powervm.virt.powervm.localdisk.LocalStorage.'
                '_get_vg_uuid')
    @mock.patch('pypowervm.adapter.Adapter')
    def test_delete_volumes(self, mock_adpt, mock_vg_uuid):
        # Mocks
        mock_adpt.side_effect = [self.vg_to_vio]

        # Find from the data the vDisk scsi mapping
        scsi_mapping = mock.MagicMock()
        scsi_mapping.udid = '0300025d4a00007a000000014b36d9deaf.1'

        # Invoke the call
        local = self.get_ls(mock_adpt)
        local.delete_volumes(mock.MagicMock(), mock.MagicMock(),
                             [scsi_mapping])

        # Validate the call
        self.assertEqual(1, mock_adpt.update.call_count)

    @mock.patch('pypowervm.wrappers.storage.VolumeGroup')
    @mock.patch('nova_powervm.virt.powervm.localdisk.LocalStorage.'
                '_get_vg_uuid')
    @mock.patch('nova_powervm.virt.powervm.localdisk.LocalStorage.'
                '_get_disk_name')
    @mock.patch('pypowervm.adapter.Adapter')
    def test_extend_volume(self, mock_adpt, mock_dsk_name,
                           mock_vg_uuid, mock_vg):
        local = self.get_ls(mock_adpt)

        inst = objects.Instance(**powervm.TEST_INSTANCE)

        vdisk = mock.Mock(name='vdisk')
        vdisk.name = 'disk_name'

        resp = mock.Mock(name='response')
        resp.virtual_disks = [vdisk]
        mock_vg.load_from_response.return_value = resp

        mock_dsk_name.return_value = 'NOMATCH'
        self.assertRaises(nova_exc.DiskNotFound, local.extend_volume,
                          'context', inst, dict(type='boot'), 10)

        mock_dsk_name.return_value = 'disk_name'
        local.extend_volume('context', inst, dict(type='boot'), 1000)

        # Validate the call
        self.assertEqual(1, mock_adpt.update.call_count)
        self.assertEqual(vdisk.capacity, 1000)
