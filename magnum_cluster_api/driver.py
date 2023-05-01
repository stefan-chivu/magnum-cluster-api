# Copyright (c) 2023 VEXXHOST, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import os

import keystoneauth1
import pkg_resources
from magnum import objects as magnum_objects
from magnum.drivers.common import driver
from oslo_log import log as logging

from magnum_cluster_api import (
    clients,
    exceptions,
    helm,
    monitor,
    objects,
    resources,
    utils,
)

LOG = logging.getLogger(__name__)


class BaseDriver(driver.Driver):
    def __init__(self):
        self.k8s_api = clients.get_pykube_api()

    def create_cluster(self, context, cluster, cluster_create_timeout):
        osc = clients.get_openstack_api(context)

        resources.Namespace(self.k8s_api).apply()

        credential = osc.keystone().client.application_credentials.create(
            user=cluster.user_id,
            name=cluster.uuid,
            description=f"Magnum cluster ({cluster.uuid})",
        )

        resources.CloudConfigSecret(
            self.k8s_api,
            cluster,
            osc.url_for(service_type="identity", interface="public"),
            osc.cinder_region_name(),
            credential,
        ).apply()

        resources.ApiCertificateAuthoritySecret(self.k8s_api, cluster).apply()
        resources.EtcdCertificateAuthoritySecret(self.k8s_api, cluster).apply()
        resources.FrontProxyCertificateAuthoritySecret(self.k8s_api, cluster).apply()
        resources.ServiceAccountCertificateAuthoritySecret(
            self.k8s_api, cluster
        ).apply()

        resources.apply_cluster_from_magnum_cluster(context, self.k8s_api, cluster)

    def _reconcile_api_address(
        self, magnum_cluster: magnum_objects.Cluster, capi_cluster: objects.Cluster
    ):
        """
        Reconcile the API address between Magnum and the Cluster API cluster.

        :param magnum_cluster: The Magnum cluster object.
        :param capi_cluster: The Cluster API cluster object.
        """
        if magnum_cluster.api_address != capi_cluster.api_address:
            magnum_cluster.api_address = capi_cluster.api_address
            magnum_cluster.save()

    def _reconcile_coe_version(
        self, magnum_cluster: magnum_objects.Cluster, capi_cluster: objects.Cluster
    ):
        """
        Reconcile the container orchestration engine version between Magnum and
        the Cluster API cluster.

        :param magnum_cluster: The Magnum cluster object.
        :param capi_cluster: The Cluster API cluster object.
        """
        if magnum_cluster.coe_version != capi_cluster.kubernetes_version:
            magnum_cluster.coe_version = capi_cluster.kubernetes_version
            magnum_cluster.save()

    def _reconcile_helm_chart(
        self,
        capi_cluster: objects.Cluster,
        release_name: str,
        chart_ref: str,
        values: dict,
    ):
        """
        This is a helper function to allow us to reconcile a Helm chart onto
        the workload cluster.

        :param capi_cluster: The Cluster API cluster object.
        :param release_name: The name of the Helm release.
        :param chart_ref: The chart reference to use.
        :param values: The values to reconcile.
        """
        with capi_cluster.config_file() as kubeconfig_file:
            try:
                get_values = helm.GetValuesReleaseCommand(
                    kubeconfig=kubeconfig_file.name,
                    namespace="kube-system",
                    release_name=release_name,
                )
                cluster_values = get_values()
            except exceptions.HelmReleaseNotFound:
                cluster_values = {}

            generated_values = values
            if cluster_values != generated_values:
                LOG.info("Updating cloud-controller-manager Helm chart")
                upgrade = helm.UpgradeReleaseCommand(
                    kubeconfig=kubeconfig_file.name,
                    namespace="kube-system",
                    release_name=release_name,
                    chart_ref=chart_ref,
                    values=generated_values,
                )
                upgrade()

    def _reconcile_cloud_controller_manager(self, capi_cluster: objects.Cluster):
        """
        Reconcile the OpenStack Cloud Controller Manager Helm chart into the
        workload cluster.

        :param capi_cluster: The Cluster API cluster object.
        """
        self._reconcile_helm_chart(
            capi_cluster,
            release_name="cloud-controller-manager",
            chart_ref=os.path.join(
                pkg_resources.resource_filename("magnum_cluster_api", "charts"),
                "openstack-cloud-controller-manager/",
            ),
            values=capi_cluster.cloud_controller_manager_values,
        )

    def _reconcile_cinder_csi(self, capi_cluster: objects.Cluster):
        """
        Reconcile the Cinder CSI Helm chart into the workload cluster.

        :param capi_cluster: The Cluster API cluster object.
        """
        # TODO: detect if Cinder is enabled
        self._reconcile_helm_chart(
            capi_cluster,
            release_name="openstack-cinder-csi",
            chart_ref=os.path.join(
                pkg_resources.resource_filename("magnum_cluster_api", "charts"),
                "openstack-cinder-csi/",
            ),
            values=capi_cluster.cinder_csi_values,
        )

    def update_cluster_status(self, context, cluster, use_admin_ctx=False):
        node_groups = [
            self.update_nodegroup_status(context, cluster, node_group)
            for node_group in cluster.nodegroups
        ]

        # TODO: watch for topology change instead
        osc = clients.get_openstack_api(context)

        capi_cluster = resources.Cluster(context, self.k8s_api, cluster).get_or_none()

        if cluster.status in (
            "CREATE_IN_PROGRESS",
            "UPDATE_IN_PROGRESS",
        ):
            # NOTE(mnaser): It's possible we run a cluster status update before
            #               the cluster is created. In that case, we don't want
            #               to update the cluster status.
            if capi_cluster is None:
                return

            capi_cluster.reload()

            try:
                self._reconcile_api_address(cluster, capi_cluster)
                self._reconcile_coe_version(cluster, capi_cluster)
                self._reconcile_cloud_controller_manager(capi_cluster)
                self._reconcile_cinder_csi(capi_cluster)
            except exceptions.ClusterNotReady as exc:
                LOG.debug("Cluster attribute not ready: %s", exc)
                return

            for condition in ("ControlPlaneReady", "InfrastructureReady", "Ready"):
                if capi_cluster.conditions[condition] is not True:
                    return

            for ng in node_groups:
                if not ng.status.endswith("_COMPLETE"):
                    return
                if ng.status == "DELETE_COMPLETE":
                    ng.destroy()

            # if cluster.status == "CREATE_IN_PROGRESS":
            #     cluster.status = "CREATE_COMPLETE"
            if cluster.status == "UPDATE_IN_PROGRESS":
                cluster.status = "UPDATE_COMPLETE"

            cluster.save()

        if cluster.status == "DELETE_IN_PROGRESS":
            if capi_cluster and capi_cluster.exists():
                return

            # NOTE(mnaser): We delete the application credentials at this stage
            #               to make sure CAPI doesn't lose access to OpenStack.
            try:
                osc.keystone().client.application_credentials.find(
                    name=cluster.uuid,
                    user=cluster.user_id,
                ).delete()
            except keystoneauth1.exceptions.http.NotFound:
                pass

            resources.CloudConfigSecret(self.k8s_api, cluster).delete()
            resources.ApiCertificateAuthoritySecret(self.k8s_api, cluster).delete()
            resources.EtcdCertificateAuthoritySecret(self.k8s_api, cluster).delete()
            resources.FrontProxyCertificateAuthoritySecret(
                self.k8s_api, cluster
            ).delete()
            resources.ServiceAccountCertificateAuthoritySecret(
                self.k8s_api, cluster
            ).delete()

            cluster.status = "DELETE_COMPLETE"
            cluster.save()

    def update_cluster(self, context, cluster, scale_manager=None, rollback=False):
        raise NotImplementedError()

    def resize_cluster(
        self,
        context,
        cluster,
        resize_manager,
        node_count,
        nodes_to_remove,
        nodegroup=None,
    ):
        if nodegroup is None:
            nodegroup = cluster.default_ng_worker

        if nodes_to_remove:
            machines = objects.Machine.objects(self.k8s_api).filter(
                namespace="magnum-system",
                selector={
                    "cluster.x-k8s.io/cluster-name": utils.get_or_generate_cluster_api_name(
                        self.k8s_api, cluster
                    ),
                    "topology.cluster.x-k8s.io/deployment-name": nodegroup.name,
                },
            )

            for machine in machines:
                instance_uuid = machine.obj["spec"]["providerID"].split("/")[-1]
                if instance_uuid in nodes_to_remove:
                    machine.obj["metadata"].setdefault("annotations", {})
                    machine.obj["metadata"]["annotations"][
                        "cluster.x-k8s.io/delete-machine"
                    ] = "yes"
                    machine.update()

        nodegroup.node_count = node_count
        nodegroup.save()

        resources.apply_cluster_from_magnum_cluster(context, self.k8s_api, cluster)

    def upgrade_cluster(
        self,
        context,
        cluster: magnum_objects.Cluster,
        cluster_template: magnum_objects.ClusterTemplate,
        max_batch_size,
        nodegroup: magnum_objects.NodeGroup,
        scale_manager=None,
        rollback=False,
    ):
        """
        Upgrade a cluster to a new version of Kubernetes.
        """
        # TODO: nodegroup?

        resources.apply_cluster_from_magnum_cluster(
            context, self.k8s_api, cluster, cluster_template=cluster_template
        )

    def delete_cluster(self, context, cluster):
        # NOTE(mnaser): This should be removed when this is fixed:
        #
        #               https://github.com/kubernetes-sigs/cluster-api-provider-openstack/issues/842
        #               https://github.com/kubernetes-sigs/cluster-api-provider-openstack/pull/990
        utils.delete_loadbalancers(context, cluster)

        resources.ClusterResourceSet(self.k8s_api, cluster).delete()
        resources.ClusterResourcesConfigMap(context, self.k8s_api, cluster).delete()
        resources.Cluster(context, self.k8s_api, cluster).delete()
        resources.ClusterAutoscalerHelmRelease(self.k8s_api, cluster).delete()

    def create_nodegroup(self, context, cluster, nodegroup):
        resources.apply_cluster_from_magnum_cluster(context, self.k8s_api, cluster)

    def update_nodegroup_status(self, context, cluster, nodegroup):
        action = nodegroup.status.split("_")[0]

        if nodegroup.role == "master":
            kcp = resources.get_kubeadm_control_plane(self.k8s_api, cluster)
            if kcp is None:
                return nodegroup

            generation = kcp.obj.get("status", {}).get("observedGeneration", 1)
            if generation > 1:
                action = "UPDATE"

            ready = kcp.obj["status"].get("ready", False)
            failure_message = kcp.obj["status"].get("failureMessage")

            updated_replicas = kcp.obj["status"].get("updatedReplicas")
            replicas = kcp.obj["status"].get("replicas")

            if updated_replicas != replicas:
                nodegroup.status = f"{action}_IN_PROGRESS"
            elif updated_replicas == replicas and ready:
                nodegroup.status = f"{action}_COMPLETE"
            nodegroup.status_reason = failure_message
        else:
            md = resources.get_machine_deployment(self.k8s_api, cluster, nodegroup)
            if md is None:
                if action == "DELETE":
                    nodegroup.status = f"{action}_COMPLETE"
                    nodegroup.save()
                    return nodegroup
                return nodegroup

            phase = md.obj["status"]["phase"]

            if phase in ("ScalingUp", "ScalingDown"):
                nodegroup.status = f"{action}_IN_PROGRESS"
            elif phase == "Running":
                nodegroup.status = f"{action}_COMPLETE"
            elif phase in ("Failed", "Unknown"):
                nodegroup.status = f"{action}_FAILED"

            # TODO(mnaser): We can remove this once we support Cluster API 1.4.0
            #               https://github.com/kubernetes-sigs/cluster-api/pull/7917
            resources.set_autoscaler_metadata_in_machinedeployment(
                context, self.k8s_api, cluster, nodegroup
            )

        nodegroup.save()

        return nodegroup

    def update_nodegroup(self, context, cluster, nodegroup):
        # TODO
        resources.apply_cluster_from_magnum_cluster(context, self.k8s_api, cluster)

    def delete_nodegroup(self, context, cluster, nodegroup):
        nodegroup.status = "DELETE_IN_PROGRESS"
        nodegroup.save()

        resources.apply_cluster_from_magnum_cluster(
            context,
            self.k8s_api,
            cluster,
        )

    def get_monitor(self, context, cluster):
        return monitor.Monitor(context, cluster)

    # def rotate_ca_certificate(self, context, cluster):
    #     raise exception.NotSupported(
    #         "'rotate_ca_certificate' is not supported by this driver.")

    def create_federation(self, context, federation):
        raise NotImplementedError()

    def update_federation(self, context, federation):
        raise NotImplementedError()

    def delete_federation(self, context, federation):
        raise NotImplementedError()


class UbuntuFocalDriver(BaseDriver):
    @property
    def provides(self):
        return [
            {"server_type": "vm", "os": "ubuntu-focal", "coe": "kubernetes"},
        ]
