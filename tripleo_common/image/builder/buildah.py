#   Copyright 2019 Red Hat, Inc.
#
#   Licensed under the Apache License, Version 2.0 (the "License"); you may
#   not use this file except in compliance with the License. You may obtain
#   a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#   WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#   License for the specific language governing permissions and limitations
#   under the License.
#


from concurrent import futures
import os
import six
import tenacity

from oslo_concurrency import processutils
from oslo_log import log as logging

from tripleo_common import constants
from tripleo_common.image.builder import base
from tripleo_common.utils import process

LOG = logging.getLogger(__name__)


class BuildahBuilder(base.BaseBuilder):
    """Builder to build container images with Buildah."""

    def __init__(self, work_dir, deps, base='fedora', img_type='binary',
                 tag='latest', namespace='master',
                 registry_address='127.0.0.1:8787', push_containers=True,
                 volumes=[], excludes=[]):
        """Setup the parameters to build with Buildah.

        :params work_dir: Directory where the Dockerfiles or Containerfiles
            are generated by Kolla.
        :params deps: Dictionary defining the container images
            dependencies.
        :params base: Base image on which the containers are built.
            Default to fedora.
        :params img_type: Method used to build the image. All TripleO images
            are built from binary method. Can be set to false to remove it
            from image name.
        :params tag: Tag used to identify the images that we build.
            Default to latest.
        :params namespace: Namespace used to build the containers.
            Default to master.
        :params registry_address: IP + port of the registry where we push
            the images. Default is 127.0.0.1:8787.
        :params push: Flag to bypass registry push if False. Default is True
        :params volumes: Bind mount volumes used during buildah bud.
            Default to [].
        :params excludes: List of images to skip. Default to [].
        """

        super(BuildahBuilder, self).__init__()
        self.build_timeout = constants.BUILD_TIMEOUT
        self.work_dir = work_dir
        self.deps = deps
        self.base = base
        self.img_type = img_type
        self.tag = tag
        self.namespace = namespace
        self.registry_address = registry_address
        self.push_containers = push_containers
        self.volumes = volumes
        self.excludes = excludes
        # Each container image has a Dockerfile or a Containerfile.
        # Buildah needs to know the base directory later.
        self.cont_map = {os.path.basename(root): root for root, dirs,
                         fnames in os.walk(self.work_dir)
                         if 'Dockerfile' in fnames or
                         'Containerfile' in fnames}
        # Building images with root so overlayfs is used, and not fuse-overlay
        # from userspace, which would be slower.
        self.buildah_cmd = ['sudo', 'buildah']

    def _find_container_dir(self, container_name):
        """Return the path of the Dockerfile/Containerfile directory.

        :params container_name: Name of the container.
        """

        if container_name not in self.cont_map:
            LOG.error('Container not found in Kolla '
                      'deps: %s' % container_name)
        return self.cont_map.get(container_name, '')

    def _get_destination(self, container_name):
        """Return the destination of a container image to push.

        :params container_name: Name of the container.
        """

        destination = "{}/{}/{}".format(
            self.registry_address,
            self.namespace,
            self.base,
        )
        if self.img_type:
            destination += '-' + self.img_type
        destination += '-' + container_name + ':' + self.tag
        return destination

    def _generate_container(self, container_name):
        """Generate a container image by building and pushing the image.

        :params container_name: Name of the container.
        """

        if container_name in self.excludes:
            return

        self.build(container_name, self._find_container_dir(container_name))
        if self.push_containers:
            self.push(self._get_destination(container_name))

    def build(self, container_name, container_build_path):
        """Build an image from a given directory.

        :params container_name: Name of the container.
        :params container_build_path: Directory where the Dockerfile or
            Containerfile and other files are located to build the image.
        """

        # 'buildah bud' is the command we want because Kolla uses Dockefile to
        # build images.
        # TODO(emilien): Stop ignoring TLS. The deployer should either secure
        # the registry or add it to insecure_registries.
        logfile = container_build_path + '/' + container_name + '-build.log'
        bud_args = ['bud']
        for v in self.volumes:
            bud_args.extend(['--volume', v])
        # TODO(aschultz): drop --format docker when oci format is properly
        # supported by the undercloud registry
        bud_args.extend(['--format', 'docker', '--tls-verify=False',
                         '--logfile', logfile, '-t',
                         self._get_destination(container_name),
                         container_build_path])
        args = self.buildah_cmd + bud_args
        print("Building %s image with: %s" % (container_name, ' '.join(args)))
        process.execute(
            *args,
            check_exit_code=True,
            run_as_root=False,
            use_standard_locale=True
        )

    @tenacity.retry(  # Retry up to 10 times with jittered exponential backoff
        reraise=True,
        wait=tenacity.wait_random_exponential(multiplier=1, max=15),
        stop=tenacity.stop_after_attempt(10)
    )
    def push(self, destination):
        """Push an image to a container registry.

        :params destination: URL to used to push the container. It contains
            the registry address, namespace, base, img_type (optional),
            container name and tag.
        """
        # TODO(emilien): Stop ignoring TLS. The deployer should either secure
        # the registry or add it to insecure_registries.
        # TODO(emilien) We need to figure out how we can push to something
        # else than a Docker registry.
        args = self.buildah_cmd + ['push', '--tls-verify=False', destination,
                                   'docker://' + destination]
        print("Pushing %s image with: %s" % (destination, ' '.join(args)))
        process.execute(*args, run_as_root=False, use_standard_locale=True)

    def build_all(self, deps=None):
        """Function that browse containers dependencies and build them.

        :params deps: Dictionary defining the container images
            dependencies.
        """

        if deps is None:
            deps = self.deps

        if isinstance(deps, (list,)):
            # Only a list of images can be multi-processed because they
            # are the last layer to build. Otherwise we could have issues
            # to build multiple times the same layer.
            # Number of workers will be based on CPU count with a min 2,
            # max 8. Concurrency in Buildah isn't that great so it's not
            # useful to go above 8.
            workers = min(8, max(2, processutils.get_worker_count()))
            with futures.ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_build = {executor.submit(self.build_all,
                                   container): container for container in
                                   deps}
                done, not_done = futures.wait(
                    future_to_build,
                    timeout=self.build_timeout,
                    return_when=futures.FIRST_EXCEPTION
                )
            # NOTE(cloudnull): Once the job has been completed all completed
            #                  jobs are checked for exceptions. If any jobs
            #                  failed a SystemError will be raised using the
            #                  exception information. If any job was loaded
            #                  but not executed a SystemError will be raised.
            for job in done:
                if job._exception:
                    raise SystemError("%(container)s raised %(exception)s" %
                                      {'container': future_to_build[job],
                                       'exception': job._exception})
            else:
                if not_done:
                    error_msg = ('The following jobs were '
                                 'incomplete: {}'.format(
                                     [future_to_build[job] for job
                                         in not_done]))

                    jobs_with_exceptions = [{
                        'container': future_to_build[job],
                        'exception': job._exception}
                        for job in not_done if job._exception]
                    if jobs_with_exceptions:
                        for job_with_exception in jobs_with_exceptions:
                            error_msg = error_msg + os.linesep + (
                                "%(container)s raised the following "
                                "exception: %(exception)s" %
                                job_with_exception)

                    raise SystemError(error_msg)

        elif isinstance(deps, (dict,)):
            for container in deps:
                self._generate_container(container)
                self.build_all(deps.get(container))
        elif isinstance(deps, six.string_types):
            self._generate_container(deps)
