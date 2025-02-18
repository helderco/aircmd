#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#

"""This modules groups functions made to create reusable environments packaged in dagger containers."""
# Borrowed from and adopted from https://raw.githubusercontent.com/airbytehq/airbyte/75a7fc660d2fa2291d84d5aa78a8446fb7e32d55/tools/ci_connector_ops/ci_connector_ops/pipelines/actions/environments.py

from __future__ import annotations

import uuid
from typing import List, Optional, Tuple

from dagger import CacheSharingMode, CacheVolume, Container, Directory, File

from ..models.base import GlobalSettings, PipelineContext
from .pipelines import get_file_contents, get_repo_dir, with_exit_code
from .strings import slugify


def with_python_base(context: PipelineContext, python_image_name: str = "python:3.9-slim") -> Container:
    """Build a Python container with a cache volume for pip cache.

    Args:
        context (PipelineContext): The current test context, providing a dagger client and a repository directory.
        python_image_name (str, optional): The python image to use to build the python base environment. Defaults to "python:3.9-slim".

    Raises:
        ValueError: Raised if the python_image_name is not a python image.

    Returns:
        Container: The python base environment container.
    """
    if not python_image_name.startswith("python:3"):
        raise ValueError("You have to use a python image to build the python base environment")
    pip_cache: CacheVolume = context.dagger_client.cache_volume("pip_cache")

    base_container = (
        context.dagger_client.container()
        .from_(python_image_name)
        .with_mounted_cache("/root/.cache/pip", pip_cache)
        .with_exec(["pip", "install", "--upgrade", "pip"])
    )

    return base_container


def with_testing_dependencies(context: PipelineContext, pyproj_path: str, test_reqs: List[str]) -> Container:
    """Build a testing environment by installing testing dependencies on top of a python base environment.

    Args:
        context (PipelineContext): The current test context, providing a dagger client and a repository directory.

    Returns:
        Container: The testing environment container.
    """
    python_environment: Container = with_python_base(context)
    pyproject_toml_file = get_repo_dir(context, ".", include=[pyproj_path]).file(pyproj_path)
    return python_environment.with_exec(["pip", "install"] + test_reqs).with_file(
        f"/{test_reqs}", pyproject_toml_file
    )


def with_python_package(
    context: PipelineContext,
    settings: GlobalSettings,
    python_environment: Container,
    package_source_code_path: str,
    exclude: Optional[List[str]] = None,
) -> Container:
    """Load a python package source code to a python environment container.

    Args:
        context (PipelineContext): The current test context, providing the repository directory from which the python sources will be pulled.
        python_environment (Container): An existing python environment in which the package will be installed.
        package_source_code_path (str): The local path to the package source code.
        additional_dependency_groups (Optional[List]): extra_requires dependency of setup.py to install. Defaults to None.
        exclude (Optional[List]): A list of file or directory to exclude from the python package source code.

    Returns:
        Container: A python environment container with the python package source code.
    """
    if exclude:
        exclude = settings.DEFAULT_PYTHON_EXCLUDE + exclude
    else:
        exclude = settings.DEFAULT_PYTHON_EXCLUDE
    package_source_code_directory: Directory = get_repo_dir(context, settings, package_source_code_path, exclude=exclude)
    container = python_environment.with_mounted_directory("/" + package_source_code_path, package_source_code_directory).with_workdir(
        "/" + package_source_code_path
    )
    return container


async def with_installed_python_package(
    context: PipelineContext,
    settings: GlobalSettings,
    python_environment: Container,
    package_source_code_path: str,
    additional_dependency_groups: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
) -> Container:
    """Install a python package in a python environment container.

    Args:
        context (PipelineContext): The current test context, providing the repository directory from which the python sources will be pulled.
        python_environment (Container): An existing python environment in which the package will be installed.
        package_source_code_path (str): The local path to the package source code.
        additional_dependency_groups (Optional[List]): extra_requires dependency of setup.py to install. Defaults to None.
        exclude (Optional[List]): A list of file or directory to exclude from the python package source code.

    Returns:
        Container: A python environment container with the python package installed.
    """
    install_local_requirements_cmd = ["python", "-m", "pip", "install", "-r", "requirements.txt"]
    install_connector_package_cmd = ["python", "-m", "pip", "install", "."]

    container = with_python_package(context, settings, python_environment, package_source_code_path, exclude=exclude)
    if requirements_txt := await get_file_contents(container, "requirements.txt"):
        for line in requirements_txt.split("\n"):
            if line.startswith("-e ."):
                local_dependency_path = package_source_code_path + "/" + line[3:]
                container = container.with_mounted_directory(
                    "/" + local_dependency_path, get_repo_dir(context, settings, local_dependency_path, exclude=settings.DEFAULT_PYTHON_EXCLUDE)
                )
        container = container.with_exec(install_local_requirements_cmd)

    container = container.with_exec(install_connector_package_cmd)

    if additional_dependency_groups:
        container = container.with_exec(
            install_connector_package_cmd[:-1] + [install_connector_package_cmd[-1] + f"[{','.join(additional_dependency_groups)}]"]
        )

    return container


def with_alpine_packages(base_container: Container, packages_to_install: List[str]) -> Container:
    """Installs packages using apk-get.
    Args:
        context (Container): A alpine based container.

    Returns:
        Container: A container with the packages installed.

    """
    package_install_command = ["apk", "add"]
    return base_container.with_exec(package_install_command + packages_to_install)


def with_debian_packages(base_container: Container, packages_to_install: List[str]) -> Container:
    """Installs packages using apt-get.
    Args:
        context (Container): A alpine based container.

    Returns:
        Container: A container with the packages installed.

    """
    update_packages_command = ["apt-get", "update"]
    package_install_command = ["apt-get", "install", "-y"]
    return base_container.with_exec(update_packages_command).with_exec(package_install_command + packages_to_install)


def with_pip_packages(base_container: Container, packages_to_install: List[str]) -> Container:
    """Installs packages using pip
    Args:
        context (Container): A container with python installed

    Returns:
        Container: A container with the pip packages installed.

    """
    package_install_command = ["pip", "install"]
    return base_container.with_exec(package_install_command + packages_to_install)

def with_dockerd_service(
    context: PipelineContext, settings: GlobalSettings, shared_volume: Optional[Tuple[str, CacheVolume]] = None, docker_service_name: Optional[str] = None
) -> Container:
    """Create a container running dockerd, exposing its 2375 port, can be used as the docker host for docker-in-docker use cases.

    Args:
        context (PipelineContext): The current connector context.
        shared_volume (Optional, optional): A tuple in the form of (mounted path, cache volume) that will be mounted to the dockerd container. Defaults to None.
        docker_service_name (Optional[str], optional): The name of the docker service, appended to volume name, useful context isolation. Defaults to None.

    Returns:
        Container: The container running dockerd as a service.
    """
    docker_lib_volume_name = f"{shared_volume[0]}-docker-lib" if shared_volume is not None else "docker-lib"
    if docker_service_name:
        docker_lib_volume_name = f"{docker_lib_volume_name}-{slugify(docker_service_name)}"
    dind = (
        context.dagger_client.container()
        .from_(settings.DOCKER_DIND_IMAGE)
        .with_mounted_cache(
            "/var/lib/docker",
            context.dagger_client.cache_volume(docker_lib_volume_name),
            sharing=CacheSharingMode.SHARED,
        )
    )
    if shared_volume is not None:
        dind = dind.with_mounted_cache(*shared_volume)
    return dind.with_exposed_port(2375).with_exec(
        ["dockerd", "--log-level=error", "--host=tcp://0.0.0.0:2375", "--tls=false"], insecure_root_capabilities=True
    )


def with_bound_docker_host(
    context: PipelineContext,
    settings: GlobalSettings,
    container: Container,
    shared_volume: Optional[Tuple[str, CacheVolume]] = None,
    docker_service_name: Optional[str] = None,
) -> Container:
    """Bind a container to a docker host. It will use the dockerd service as a docker host.

    Args:
        context (PipelineContext): The current connector context.
        container (Container): The container to bind to the docker host.
        shared_volume (Optional, optional): A tuple in the form of (mounted path, cache volume) that will be both mounted to the container and the dockerd container. Defaults to None.
        docker_service_name (Optional[str], optional): The name of the docker service, useful context isolation. Defaults to None.

    Returns:
        Container: The container bound to the docker host.
    """
    dockerd = with_dockerd_service(context, settings, shared_volume, docker_service_name)
    docker_lib_volume_name = f"{shared_volume[0]}-docker-lib" if shared_volume is not None else "docker-lib"
    docker_hostname = docker_lib_volume_name
    if docker_service_name:
        docker_hostname = f"{docker_hostname}-{slugify(docker_service_name)}"
    bound = container.with_env_variable("DOCKER_HOST", f"tcp://{docker_hostname}:2375").with_service_binding(docker_hostname, dockerd)
    if shared_volume:
        bound = bound.with_mounted_cache(*shared_volume)
    return bound


def with_docker_cli(
    context: PipelineContext, settings: GlobalSettings, shared_volume: Optional[Tuple[str, CacheVolume]] = None, docker_service_name: Optional[str] = None
) -> Container:
    """Create a container with the docker CLI installed and bound to a persistent docker host.

    Args:
        context (PipelineContext): The current pipeline context.
        shared_volume (Optional, optional): A tuple in the form of (mounted path, cache volume) that will be both mounted to the container and the dockerd container. Defaults to None.
        docker_service_name (Optional[str], optional): The name of the docker service, useful context isolation. Defaults to None.

    Returns:
        Container: A docker cli container bound to a docker host.
    """
    docker_cli = context.dagger_client.container().from_(settings.DOCKER_CLI_IMAGE)
    return with_bound_docker_host(context, settings, docker_cli, shared_volume, docker_service_name)


def with_gradle(
    context: PipelineContext,
    settings: GlobalSettings,
    sources_to_include: Optional[List[str]] = None,
    bind_to_docker_host: bool = True,
    docker_service_name: Optional[str] = "gradle",
) -> Container:
    """Create a container with Gradle installed and bound to a persistent docker host.

    Args:
        context (PipelineContext): The current connector context.
        sources_to_include (List[str], optional): List of additional source path to mount to the container. Defaults to None.
        bind_to_docker_host (bool): Whether to bind the gradle container to a docker host.
        docker_service_name (Optional[str], optional): The name of the docker service, useful context isolation. Defaults to "gradle".

    Returns:
        Container: A container with Gradle installed and Java sources from the repository.
    """

    include = [
        ".root",
        ".env",
        "build.gradle",
        "deps.toml",
        "gradle.properties",
        "gradle",
        "gradlew",
        "LICENSE_SHORT",
        "publish-repositories.gradle",
        "settings.gradle",
        "build.gradle",
        "tools/gradle",
        "spotbugs-exclude-filter-file.xml",
        "buildSrc",
        "tools/bin/build_image.sh",
        "tools/lib/lib.sh",
    ]

    if sources_to_include:
        include += sources_to_include

    gradle_dependency_cache: CacheVolume = context.dagger_client.cache_volume("gradle-dependencies-caching")
    gradle_build_cache: CacheVolume = context.dagger_client.cache_volume("gradle-build-cache")

    shared_tmp_volume = ("/tmp", context.dagger_client.cache_volume("share-tmp-gradle"))

    openjdk_with_docker = (
        context.dagger_client.container()
        .from_("openjdk:17.0.1-jdk-slim")
        .with_exec(["apt-get", "update"])
        .with_exec(["apt-get", "install", "-y", "curl", "jq", "rsync"])
        .with_env_variable("VERSION", settings.DOCKER_VERSION)
        .with_exec(["sh", "-c", "curl -fsSL https://get.docker.com | sh"])
        .with_env_variable("GRADLE_HOME", "/root/.gradle")
        .with_exec(["mkdir", "/airbyte"])
        .with_workdir("/airbyte")
        .with_mounted_directory("/airbyte", get_repo_dir(context, settings, ".", include=include))
        .with_exec(["mkdir", "-p", settings.GRADLE_READ_ONLY_DEPENDENCY_CACHE_PATH])
        .with_mounted_cache(settings.GRADLE_BUILD_CACHE_PATH, gradle_build_cache, sharing=CacheSharingMode.LOCKED)
        .with_mounted_cache(settings.GRADLE_READ_ONLY_DEPENDENCY_CACHE_PATH, gradle_dependency_cache)
        .with_env_variable("GRADLE_RO_DEP_CACHE", settings.GRADLE_READ_ONLY_DEPENDENCY_CACHE_PATH)
    )

    if bind_to_docker_host:
        return with_bound_docker_host(context, settings, openjdk_with_docker, shared_tmp_volume, docker_service_name=docker_service_name)
    else:
        return openjdk_with_docker


async def load_image_to_docker_host(context: PipelineContext, 
                                    settings: GlobalSettings,
                                    tar_file: File, 
                                    image_tag: str, 
                                    docker_service_name: Optional[str] = None) -> None:
    """Load a docker image tar archive to the docker host.

    Args:
        context (PipelineContext): The current connector context.
        tar_file (File): The file object holding the docker image tar archive.
        image_tag (str): The tag to create on the image if it has no tag.
        docker_service_name (str): Name of the docker service, useful for context isolation.
    """
    # Hacky way to make sure the image is always loaded
    tar_name = f"{str(uuid.uuid4())}.tar"
    docker_cli = with_docker_cli(context, settings, docker_service_name=docker_service_name).with_mounted_file(tar_name, tar_file)

    # Remove a previously existing image with the same tag if any.
    docker_image_rm_exit_code = await with_exit_code(
        docker_cli.with_env_variable("CACHEBUSTER", tar_name).with_exec(["docker", "image", "rm", image_tag])
    )
    if docker_image_rm_exit_code == 0:
        context.logger.info(f"Removed an existing image tagged {image_tag}")
    image_load_output = await docker_cli.with_exec(["docker", "load", "--input", tar_name]).stdout()
    context.logger.info(image_load_output)
    # Not tagged images only have a sha256 id the load output shares.
    if "sha256:" in image_load_output:
        image_id = image_load_output.replace("\n", "").replace("Loaded image ID: sha256:", "")
        docker_tag_output = await docker_cli.with_exec(["docker", "tag", image_id, image_tag]).stdout()
        context.logger.info(docker_tag_output)


def with_poetry(context: PipelineContext) -> Container:
    """Install poetry in a python environment.

    Args:
        context (PipelineContext): The current test context, providing the repository directory from which the ci_credentials sources will be pulled.
    Returns:
        Container: A python environment with poetry installed.
    """
    python_base_environment: Container = with_python_base(context, "python:3.9")
    python_with_git = with_debian_packages(python_base_environment, ["git"])
    python_with_poetry = with_pip_packages(python_with_git, ["poetry"])

    poetry_cache: CacheVolume = context.dagger_client.cache_volume("poetry_cache")
    python_with_poetry.with_mounted_cache("/root/.cache/pypoetry", poetry_cache, sharing=CacheSharingMode.SHARED)

    return python_with_poetry


def with_poetry_module(context: PipelineContext, parent_dir: Directory, module_path: str) -> Container:
    """Sets up a Poetry module.

    Args:
        context (PipelineContext): The current test context, providing the repository directory from which the sources will be pulled.
    Returns:
        Container: A python environment with dependencies installed using poetry.
    """
    poetry_install_dependencies_cmd = ["poetry", "install"]

    python_with_poetry = with_poetry(context)
    return (
        python_with_poetry.with_mounted_directory("/src", parent_dir)
        .with_workdir(f"/src/{module_path}")
        .with_exec(poetry_install_dependencies_cmd)
        .with_env_variable("CACHEBUSTER", str(uuid.uuid4()))
    )

def with_crane(
    context: PipelineContext,
    settings: GlobalSettings
) -> Container:
    """Crane is a tool to analyze and manipulate container images.
    We can use it to extract the image manifest and the list of layers or list the existing tags on an image repository.
    https://github.com/google/go-containerregistry/tree/main/cmd/crane
    """

    # We use the debug image as it contains a shell which we need to properly use environment variables
    # https://github.com/google/go-containerregistry/tree/main/cmd/crane#images
    base_container = context.dagger_client.container().from_("gcr.io/go-containerregistry/crane/debug:v0.15.1")
    if settings.SECRET_DOCKER_HUB_USERNAME and settings.SECRET_DOCKER_HUB_PASSWORD:
        dockerhub_user = context.dagger_client.set_secret("docker_hub_username", settings.SECRET_DOCKER_HUB_USERNAME)
        dockerhub_password = context.dagger_client.set_secret("docker_hub_password", settings.SECRET_DOCKER_HUB_PASSWORD)
        base_container = (
            base_container
            .with_secret_variable("DOCKER_HUB_USERNAME", dockerhub_user)
            .with_secret_variable("DOCKER_HUB_PASSWORD", dockerhub_password)
            .with_exec(
                ["sh", "-c", "crane auth login index.docker.io -u $DOCKER_HUB_USERNAME -p $DOCKER_HUB_PASSWORD"], skip_entrypoint=True
                )
            )
            # We need to use skip_entrypoint=True to avoid the entrypoint to be overridden by the crane command
            # We use sh -c to be able to use environment variables in the command
            # This is a workaround as the default crane entrypoint doesn't support environment variables


    return base_container  

