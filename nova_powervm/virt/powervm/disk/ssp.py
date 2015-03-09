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

from nova_powervm.virt.powervm.disk import driver as disk_drv


class SSPDiskAdapter(disk_drv.DiskAdapter):
    """Provides a disk adapter for Shared Storage Pools.

    Shared Storage Pools are a clustered file system technology that can link
    together Virtual I/O Servers.

    This adapter provides the connection for nova based storage (not Cinder)
    to connect to virtual machines.  A separate Cinder driver for SSPs may
    exist in the future.
    """

    def __init__(self, connection):
        """Initialize the SSPDiskAdapter

        :param connection: connection information for the underlying driver
        """
        super(SSPDiskAdapter, self).__init__(connection)

    @property
    def capacity(self):
        """Capacity of the storage in gigabytes."""
        raise NotImplementedError()

    @property
    def capacity_used(self):
        """Capacity of the storage in gigabytes that is used."""
        raise NotImplementedError()

    def disconnect_image_disk(self, context, instance, lpar_uuid,
                              disk_type=None):
        """Disconnects the storage adapters from the image disk.

        :param context: nova context for operation
        :param instance: instance to delete the image for.
        :param lpar_uuid: The UUID for the pypowervm LPAR element.
        :param disk_type: The list of disk types to remove or None which means
            to remove all disks from the VM.
        :return: A list of Mappings (either pypowervm VSCSIMappings or
                 VFCMappings)
        """
        raise NotImplementedError()

    def delete_disks(self, context, instance, mappings):
        """Removes the disks specified by the mappings.

        :param context: nova context for operation
        :param instance: instance to delete the image for.
        :param mappings: The mappings that had been used to identify the
                         backing storage.  List of pypowervm VSCSIMappings or
                         VFCMappings. Typically derived from
                         disconnect_image_disk.
        """
        raise NotImplementedError()

    def create_disk_from_image(self, context, instance, image, disk_size,
                               image_type=disk_drv.BOOT_DISK):
        """Creates a disk and copies the specified image to it.

        :param context: nova context used to retrieve image from glance
        :param instance: instance to create the disk for.
        :param image_id: image_id reference used to locate image in glance
        :param disk_size: The size of the disk to create in GB.  If smaller
                          than the image, it will be ignored (as the disk
                          must be at least as big as the image).  Must be an
                          int.
        :param image_type: the image type. See disk constants above.
        :returns: dictionary with the name of the created
                  disk device in 'device_name' key
        """
        raise NotImplementedError()

    def connect_disk(self, context, instance, disk_info, lpar_uuid, **kwds):
        raise NotImplementedError()

    def extend_disk(self, context, instance, disk_info, size):
        """Extends the disk.

        :param context: nova context for operation.
        :param instance: instance to extend the disk for.
        :param disk_info: dictionary with disk info.
        :param size: the new size in gb.
        """
        raise NotImplementedError()