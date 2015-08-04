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

import random

from oslo_config import cfg
import oslo_log.log as logging

from nova.i18n import _LI, _LE
from nova_powervm.virt.powervm.disk import driver as disk_drv
from nova_powervm.virt.powervm import vios
from nova_powervm.virt.powervm import vm

from pypowervm.tasks import scsi_mapper as tsk_map
from pypowervm.tasks import storage as tsk_stg
import pypowervm.util as pvm_u
import pypowervm.wrappers.cluster as pvm_clust
import pypowervm.wrappers.storage as pvm_stg
import pypowervm.wrappers.virtual_io_server as pvm_vios

from nova_powervm.virt.powervm import exception as npvmex

ssp_opts = [
    cfg.StrOpt('cluster_name',
               default='',
               help='Cluster hosting the Shared Storage Pool to use for '
                    'storage operations.  If none specified, the host is '
                    'queried; if a single Cluster is found, it is used.')
]


LOG = logging.getLogger(__name__)
CONF = cfg.CONF
CONF.register_opts(ssp_opts, group='powervm')


class SSPDiskAdapter(disk_drv.DiskAdapter):
    """Provides a disk adapter for Shared Storage Pools.

    Shared Storage Pools are a clustered file system technology that can link
    together Virtual I/O Servers.

    This adapter provides the connection for nova based storage (not Cinder)
    to connect to virtual machines.  A separate Cinder driver for SSPs may
    exist in the future.
    """

    def __init__(self, connection):
        """Initialize the SSPDiskAdapter.

        :param connection: connection information for the underlying driver
        """
        super(SSPDiskAdapter, self).__init__(connection)

        self._cluster = self._fetch_cluster(CONF.powervm.cluster_name)
        self.clust_name = self._cluster.name

        # _ssp @property method will fetch and cache the SSP.
        self.ssp_name = self._ssp.name

        LOG.info(_LI("SSP Storage driver initialized. "
                     "Cluster '%(clust_name)s'; SSP '%(ssp_name)s'"),
                 {'clust_name': self.clust_name, 'ssp_name': self.ssp_name})

    @property
    def capacity(self):
        """Capacity of the storage in gigabytes."""
        return float(self._ssp.capacity)

    @property
    def capacity_used(self):
        """Capacity of the storage in gigabytes that is used."""
        ssp = self._ssp
        return float(ssp.capacity) - float(ssp.free_space)

    def disconnect_image_disk(self, context, instance, tx_mgr=None,
                              disk_type=None):
        """Disconnects the storage adapters from the image disk.

        :param context: nova context for operation
        :param instance: instance to disconnect the image for.
        :param tx_mgr: (Optional) The pypowervm transaction FeedTask for
                       the I/O Operations.  If provided, the Virtual I/O Server
                       mapping updates will be added to the FeedTask.  This
                       defers the updates to some later point in time.  If the
                       FeedTask is not provided, the updates will be run
                       immediately when this method is executed.
        :param disk_type: The list of disk types to remove or None which means
            to remove all disks from the VM.
        :return: A list of all the backing storage elements that were
                 disconnected from the I/O Server and VM.
        """
        if tx_mgr is None:
            tx_mgr = vios.build_tx_feed_task(
                self.adapter, self.host_uuid, name='ssp',
                xag=[pvm_vios.VIOS.xags.SCSI_MAPPING])

        lpar_uuid = vm.get_pvm_uuid(instance)
        match_func = tsk_map.gen_match_func(pvm_stg.LU, prefixes=disk_type)

        # Delay run function to remove the mapping between the VM and the LU
        def rm_func(vios_w):
            LOG.info(_LI("Removing SSP disk connection between VM %(vm)s and "
                         "VIOS %(vios)s."),
                     {'vm': instance.name, 'vios': vios_w.name})
            return tsk_map.remove_maps(vios_w, lpar_uuid,
                                       match_func=match_func)

        # Add the mapping to *each* VIOS on the LPAR's host.
        # The LPAR's host has to be self.host_uuid, else the PowerVM API will
        # fail.
        #
        # Note - this may not be all the VIOSes on the system...just the ones
        # in the SSP cluster.
        #
        # The mappings will normally be the same on all VIOSes, unless a VIOS
        # was down when a disk was added.  So for the return value, we need to
        # collect the union of all relevant mappings from all VIOSes.
        lu_set = set()
        for vios_uuid in self.vios_uuids:
            # Add the remove for the VIO
            tx_mgr.wrapper_tasks[vios_uuid].add_functor_subtask(rm_func)

            # Find the active LUs so that a delete op knows what to remove.
            vios_w = tx_mgr.wrapper_tasks[vios_uuid].wrapper
            mappings = tsk_map.find_maps(vios_w.scsi_mappings, lpar_uuid,
                                         match_func=match_func)
            if mappings:
                lu_set.update([x.backing_storage for x in mappings])

        # Run the FeedTask if it was built locally
        if tx_mgr.name == 'ssp':
            tx_mgr.execute()

        return list(lu_set)

    def disconnect_disk_from_mgmt(self, vios_uuid, disk_name):
        """Disconnect a disk from the management partition.

        :param vios_uuid: The UUID of the Virtual I/O Server serving the
                          mapping.
        :param disk_name: The name of the disk to unmap.
        """
        tsk_map.remove_lu_mapping(self.adapter, vios_uuid, self.mp_uuid,
                                  disk_names=[disk_name])
        LOG.info(_LI(
            "Unmapped boot disk %(disk_name)s from the management partition "
            "from Virtual I/O Server %(vios_uuid)s."), {
                'disk_name': disk_name, 'mp_uuid': self.mp_uuid,
                'vios_uuid': vios_uuid})

    def delete_disks(self, context, instance, storage_elems):
        """Removes the disks specified by the mappings.

        :param context: nova context for operation
        :param instance: instance to delete the disk for.
        :param storage_elems: A list of the storage elements (LU
                              ElementWrappers) that are to be deleted.  Derived
                              from the return value from disconnect_image_disk.
        """
        tsk_stg.rm_ssp_storage(self._ssp, storage_elems)

    def create_disk_from_image(self, context, instance, img_meta, disk_size_gb,
                               image_type=disk_drv.DiskType.BOOT):
        """Creates a boot disk and links the specified image to it.

        If the specified image has not already been uploaded, an Image LU is
        created for it.  A Disk LU is then created for the instance and linked
        to the Image LU.

        :param context: nova context used to retrieve image from glance
        :param instance: instance to create the disk for.
        :param img_meta: image metadata dict:
                      { 'id': reference used to locate the image in glance,
                        'size': size in bytes of the image. }
        :param disk_size_gb: The size of the disk to create in GB.  If smaller
                             than the image, it will be ignored (as the disk
                             must be at least as big as the image).  Must be an
                             int.
        :param image_type: The image type. See disk_drv.DiskType.
        :return: The backing pypowervm LU storage object that was created.
        """
        LOG.info(_LI('SSP: Create %(image_type)s disk from image %(image_id)s '
                     'for instance %(instance_uuid)s.'),
                 dict(image_type=image_type, image_id=img_meta['id'],
                      instance_uuid=instance.uuid))

        # TODO(IBM): There's an optimization to be had here if we can create
        # both the image LU and the boot LU in the same ssp.update() call.
        # This will require some nontrivial refactoring, though, as the LUs are
        # created down inside of upload_new_lu and crt_lu_linked_clone.

        image_lu = self._get_or_upload_image_lu(context, img_meta)

        boot_lu_name = self._get_disk_name(image_type, instance)
        LOG.info(_LI('SSP: Disk name is %s'), boot_lu_name)

        ssp, boot_lu = tsk_stg.crt_lu_linked_clone(
            self._ssp, self._cluster, image_lu, boot_lu_name, disk_size_gb)

        return boot_lu

    def _get_or_upload_image_lu(self, context, img_meta):
        """Ensures our SSP has an LU containing the specified image.

        If an LU of type IMAGE corresponding to the input image metadata
        already exists in our SSP, return it.  Otherwise, create it, prime it
        with the image contents from glance, and return it.

        :param context: nova context used to retrieve image from glance
        :param img_meta: image metadata dict:
                      { 'id': reference used to locate the image in glance,
                        'size': size in bytes of the image. }
        :return: A pypowervm LU ElementWrapper representing the image.
        """
        # Key off of the name to see whether we already have the image
        luname = self._get_image_name(img_meta)
        ssp = self._ssp
        for lu in ssp.logical_units:
            if lu.lu_type == pvm_stg.LUType.IMAGE and lu.name == luname:
                LOG.info(_LI('SSP: Using already-uploaded image LU %s.'),
                         luname)
                return lu

        # We don't have it yet.  Create it and upload the glance image to it.
        # Make the image LU only as big as the image.
        stream = self._get_image_upload(context, img_meta)
        LOG.info(_LI('SSP: Uploading new image LU %s.'), luname)
        lu, f_wrap = tsk_stg.upload_new_lu(self._any_vios_uuid(), ssp, stream,
                                           luname, img_meta['size'])
        return lu

    def connect_disk(self, context, instance, disk_info, tx_mgr=None):
        """Connects the disk image to the Virtual Machine.

        :param context: nova context for the transaction.
        :param instance: nova instance to connect the disk to.
        :param disk_info: The pypowervm storage element returned from
                          create_disk_from_image.  Ex. VOptMedia, VDisk, LU,
                          or PV.
        :param tx_mgr: (Optional) The pypowervm transaction FeedTask for
                       the I/O Operations.  If provided, the Virtual I/O Server
                       mapping updates will be added to the FeedTask.  This
                       defers the updates to some later point in time.  If the
                       FeedTask is not provided, the updates will be run
                       immediately when this method is executed.
        """
        if tx_mgr is None:
            tx_mgr = vios.build_tx_feed_task(
                self.adapter, self.host_uuid, name='ssp',
                xag=[pvm_vios.VIOS.xags.SCSI_MAPPING])

        # Create the LU structure
        lu = pvm_stg.LU.bld_ref(self.adapter, disk_info.name, disk_info.udid)
        lpar_uuid = vm.get_pvm_uuid(instance)

        # This is the delay apply mapping
        def add_func(vios_w):
            LOG.info(_LI("Adding SSP disk connection between VM %(vm)s and "
                         "VIOS %(vios)s."),
                     {'vm': instance.name, 'vios': vios_w.name})
            mapping = tsk_map.build_vscsi_mapping(
                self.host_uuid, vios_w, lpar_uuid, lu)
            return tsk_map.add_map(vios_w, mapping)

        # Add the mapping to *each* VIOS on the LPAR's host.
        # The LPAR's host has to be self.host_uuid, else the PowerVM API will
        # fail.
        #
        # Note - this may not be all the VIOSes on the system...just the ones
        # in the SSP cluster.
        for vios_uuid in self.vios_uuids:
            tx_mgr.wrapper_tasks[vios_uuid].add_functor_subtask(add_func)

        # If the FeedTask was built locally, then run it immediately
        if tx_mgr.name == 'ssp':
            tx_mgr.execute()

    def extend_disk(self, context, instance, disk_info, size):
        """Extends the disk.

        :param context: nova context for operation.
        :param instance: instance to extend the disk for.
        :param disk_info: dictionary with disk info.
        :param size: the new size in gb.
        """
        raise NotImplementedError()

    def check_instance_shared_storage_local(self, context, instance):
        """Check if instance files located on shared storage.

        This runs check on the destination host, and then calls
        back to the source host to check the results.

        :param context: security context
        :param instance: nova.objects.instance.Instance object
        """

        # Get the SSP unique id and use that for the data to pass
        return {'ssp_uuid': self._cluster.ssp_uuid}

    def check_instance_shared_storage_remote(self, context, data):
        """Check if instance files located on shared storage.

        :param context: security context
        :param data: result of check_instance_shared_storage_local
        """

        # Check the data passed and see if we're in the same SSP
        try:
            if data:
                ssp_uuid = data.get('ssp_uuid')
                if ssp_uuid is not None:
                    return ssp_uuid == self._cluster.ssp_uuid
        except Exception as e:
            LOG.exception(_LE(u'Error checking for shared storage. '
                              'exception=%s'), e)
        return False

    def check_instance_shared_storage_cleanup(self, context, data):
        """Do cleanup on host after check_instance_shared_storage calls

        :param context: security context
        :param data: result of check_instance_shared_storage_local
        """

        # Nothing to cleanup since we just use the SSP UUID
        pass

    def _fetch_cluster(self, clust_name):
        """Bootstrap fetch the Cluster associated with the configured name.

        :param clust_name: The cluster_name from the config, used to perform a
        search query.  If '' or None (no cluster_name was specified in the
        config), we query all clusters on the host and, if exactly one is
        found, we use it.
        :return: The Cluster EntryWrapper.
        :raise ClusterNotFoundByName: If clust_name was nonempty but no such
                                      Cluster was found on the host.
        :raise TooManyClustersFound: If clust_name was nonempty but matched
                                     more than one Cluster on the host.
        :raise NoConfigNoClusterFound: If clust_name was empty and no Cluster
                                       was found on the host.
        :raise NoConfigTooManyClusters: If clust_name was empty, but more than
                                        one Cluster was found on the host.
        """
        try:
            # Did config provide a name?
            if clust_name:
                resp = pvm_clust.Cluster.search(self.adapter, name=clust_name)
                wraps = pvm_clust.Cluster.wrap(resp)
                if len(wraps) == 0:
                    raise npvmex.ClusterNotFoundByName(clust_name=clust_name)
                if len(wraps) > 1:
                    raise npvmex.TooManyClustersFound(clust_count=len(wraps),
                                                      clust_name=clust_name)
            else:
                # Otherwise, pull the entire feed of Clusters and, if
                # exactly one result, use it.
                resp = self.adapter.read(pvm_clust.Cluster.schema_type)
                wraps = pvm_clust.Cluster.wrap(resp)
                if len(wraps) == 0:
                    raise npvmex.NoConfigNoClusterFound()
                if len(wraps) > 1:
                    raise npvmex.NoConfigTooManyClusters(
                        clust_count=len(wraps))
            clust_wrap = wraps[0]
        except Exception as e:
            LOG.exception(e.message)
            raise e
        return clust_wrap

    def _refresh_cluster(self):
        """Refetch the Cluster from the host.

        This should be necessary only when the node list is needed and may have
        changed.

        :return: The refreshed self._cluster.
        """
        # TODO(IBM): If the Cluster doesn't exist when the driver is loaded, we
        # raise one of the custom exceptions; but if it gets removed at some
        # point while live, we'll (re)raise the 404 HttpError from the REST
        # API.  Do we need a crisper way to distinguish these two scenarios?
        # Do we want to trap the 404 and raise a custom "ClusterVanished"?
        self._cluster = self._cluster.refresh()
        return self._cluster

    @property
    def _ssp(self):
        """Fetch or refresh the SSP corresponding to the Cluster.

        This must be invoked after a successful _fetch_cluster.

        :return: The fetched or refreshed SSP EntryWrapper.
        """
        # TODO(IBM): Smarter refreshing (i.e. don't do it every time).
        if getattr(self, '_ssp_wrap', None) is None:
            resp = self.adapter.read_by_href(self._cluster.ssp_uri)
            self._ssp_wrap = pvm_stg.SSP.wrap(resp)
        else:
            self._ssp_wrap = self._ssp_wrap.refresh()
        return self._ssp_wrap

    @property
    def vios_uuids(self):
        """List the UUIDs of our cluster's VIOSes on this host.

        (If a VIOS is not on this host, we can't interact with it, even if its
        URI and therefore its UUID happen to be available in the pypowervm
        wrapper.)

        :return: A list of VIOS UUID strings.
        """
        ret = []
        for n in self._cluster.nodes:
            # Skip any nodes that we don't have the vios uuid or uri
            if not (n.vios_uuid and n.vios_uri):
                continue
            node_host_uuid = pvm_u.get_req_path_uuid(
                n.vios_uri, preserve_case=True, root=True)
            if self.host_uuid != node_host_uuid:
                continue
            ret.append(n.vios_uuid)
        return ret

    def _any_vios_uuid(self):
        """Pick one of the Cluster's VIOSes and return its UUID.

        Use when it doesn't matter which VIOS an operation is invoked against.
        Currently picks at random; may later be changed to use round-robin.

        :return: A single VIOS UUID string.
        """
        return random.choice(self.vios_uuids)

    def disk_match_func(self, disk_type, instance):
        """Return a matching function to locate the disk for an instance.

        :param disk_type: One of the DiskType enum values.
        :param instance: The instance whose disk is to be found.
        :return: Callable suitable for the match_func parameter of the
                 pypowervm.tasks.scsi_mapper.find_maps method, with the
                 following specification:
            def match_func(storage_elem)
                param storage_elem: A backing storage element wrapper (VOpt,
                                    VDisk, PV, or LU) to be analyzed.
                return: True if the storage_elem's mapping should be included;
                        False otherwise.
        """
        disk_name = self._get_disk_name(disk_type, instance)
        return tsk_map.gen_match_func(pvm_stg.LU, names=[disk_name])
