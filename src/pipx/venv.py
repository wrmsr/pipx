import json
import logging
import pkgutil
from pathlib import Path
from subprocess import CompletedProcess
from typing import Dict, Generator, List, NamedTuple, Set

from pipx._vendor.packaging.utils import canonicalize_name

from pipx.animate import animate
from pipx.constants import PIPX_SHARED_PTH, ExitCode
from pipx.emojies import hazard
from pipx.interpreter import DEFAULT_PYTHON
from pipx.package_specifier import (
    fix_package_name,
    parse_specifier_for_install,
    parse_specifier_for_metadata,
)
from pipx.pipx_metadata_file import PackageInfo, PipxMetadata
from pipx.shared_libs import shared_libs
from pipx.util import (
    PipxError,
    exec_app,
    full_package_description,
    get_site_packages,
    get_venv_paths,
    pipx_wrap,
    rmdir,
    run_subprocess,
    subprocess_post_check,
)

logger = logging.getLogger(__name__)

venv_metadata_inspector_raw = pkgutil.get_data("pipx", "venv_metadata_inspector.py")
assert venv_metadata_inspector_raw is not None, (
    "pipx could not find required file venv_metadata_inspector.py. "
    "Please report this error at https://github.com/pipxproject/pipx. Exiting."
)
VENV_METADATA_INSPECTOR = venv_metadata_inspector_raw.decode("utf-8")


class VenvContainer:
    """A collection of venvs managed by pipx."""

    def __init__(self, root: Path):
        self._root = root

    def __repr__(self) -> str:
        return f"VenvContainer({str(self._root)!r})"

    def __str__(self) -> str:
        return str(self._root)

    def iter_venv_dirs(self) -> Generator[Path, None, None]:
        """Iterate venv directories in this container."""
        if not self._root.is_dir():
            return
        for entry in self._root.iterdir():
            if not entry.is_dir():
                continue
            yield entry

    def get_venv_dir(self, package: str) -> Path:
        """Return the expected venv path for given `package`."""
        return self._root.joinpath(canonicalize_name(package))

    def verify_shared_libs(self) -> None:
        for p in self.iter_venv_dirs():
            Venv(p)


class VenvMetadata(NamedTuple):
    apps: List[str]
    app_paths: List[Path]
    apps_of_dependencies: List[str]
    app_paths_of_dependencies: Dict[str, List[Path]]
    package_version: str
    python_version: str


class Venv:
    """Abstraction for a virtual environment with various useful methods for pipx"""

    def __init__(
        self, path: Path, *, verbose: bool = False, python: str = DEFAULT_PYTHON
    ) -> None:
        self.root = path
        self.python = python
        self.bin_path, self.python_path = get_venv_paths(self.root)
        self.pipx_metadata = PipxMetadata(venv_dir=path)
        self.verbose = verbose
        self.do_animation = not verbose
        try:
            self._existing = self.root.exists() and next(self.root.iterdir())
        except StopIteration:
            self._existing = False

        if self._existing and self.uses_shared_libs:
            if shared_libs.is_valid:
                if shared_libs.needs_upgrade:
                    shared_libs.upgrade(verbose=verbose)
            else:
                shared_libs.create(verbose)

            if not shared_libs.is_valid:
                raise PipxError(
                    pipx_wrap(
                        f"""
                        Error: pipx's shared venv {shared_libs.root} is invalid
                        and needs re-installation. To fix this, install or
                        reinstall a package. For example:
                        """
                    )
                    + f"\n  pipx install {self.root.name} --force",
                    wrap_message=False,
                )

    @property
    def name(self) -> str:
        if self.pipx_metadata.main_package.package is not None:
            venv_name = (
                f"{self.pipx_metadata.main_package.package}"
                f"{self.pipx_metadata.main_package.suffix}"
            )
        else:
            venv_name = self.root.name
        return venv_name

    @property
    def uses_shared_libs(self) -> bool:
        if self._existing:
            pth_files = self.root.glob("**/" + PIPX_SHARED_PTH)
            return next(pth_files, None) is not None
        else:
            # always use shared libs when creating a new venv
            return True

    @property
    def package_metadata(self) -> Dict[str, PackageInfo]:
        return_dict = self.pipx_metadata.injected_packages.copy()
        if self.pipx_metadata.main_package.package is not None:
            return_dict[
                self.pipx_metadata.main_package.package
            ] = self.pipx_metadata.main_package
        return return_dict

    @property
    def main_package_name(self) -> str:
        if self.pipx_metadata.main_package.package is None:
            # This is OK, because if no metadata, we are pipx < v0.15.0.0 and
            #   venv_name==main_package_name
            return self.root.name
        else:
            return self.pipx_metadata.main_package.package

    def create_venv(self, venv_args: List[str], pip_args: List[str]) -> None:
        with animate("creating virtual environment", self.do_animation):
            cmd = [self.python, "-m", "venv", "--without-pip"]
            venv_process = run_subprocess(cmd + venv_args + [str(self.root)])
        subprocess_post_check(venv_process)

        shared_libs.create(self.verbose)
        pipx_pth = get_site_packages(self.python_path) / PIPX_SHARED_PTH
        # write path pointing to the shared libs site-packages directory
        # example pipx_pth location:
        #   ~/.local/pipx/venvs/black/lib/python3.8/site-packages/pipx_shared.pth
        # example shared_libs.site_packages location:
        #   ~/.local/pipx/shared/lib/python3.6/site-packages
        #
        # https://docs.python.org/3/library/site.html
        # A path configuration file is a file whose name has the form 'name.pth'.
        # its contents are additional items (one per line) to be added to sys.path
        pipx_pth.write_text(f"{shared_libs.site_packages}\n", encoding="utf-8")

        self.pipx_metadata.venv_args = venv_args
        self.pipx_metadata.python_version = self.get_python_version()

    def safe_to_remove(self) -> bool:
        return not self._existing

    def remove_venv(self) -> None:
        if self.safe_to_remove():
            rmdir(self.root)
        else:
            logger.warning(
                pipx_wrap(
                    f"""
                    {hazard}  Not removing existing venv {self.root} because it
                    was not created in this session
                    """,
                    subsequent_indent=" " * 4,
                )
            )

    def upgrade_packaging_libraries(self, pip_args: List[str]) -> None:
        if self.uses_shared_libs:
            shared_libs.upgrade(verbose=self.verbose)
        else:
            # TODO: setuptools and wheel? Original code didn't bother
            # but shared libs code does.
            self._upgrade_package_no_metadata("pip", pip_args)

    def install_package(
        self,
        package: str,
        package_or_url: str,
        pip_args: List[str],
        include_dependencies: bool,
        include_apps: bool,
        is_main_package: bool,
        suffix: str = "",
    ) -> None:
        if pip_args is None:
            pip_args = []

        # package name in package specifier can mismatch URL due to user error
        package_or_url = fix_package_name(package_or_url, package)

        # check syntax and clean up spec and pip_args
        (package_or_url, pip_args) = parse_specifier_for_install(
            package_or_url, pip_args
        )

        with animate(
            f"installing {full_package_description(package, package_or_url)}",
            self.do_animation,
        ):
            cmd = ["install"] + pip_args + [package_or_url]
            pip_process = self._run_pip(cmd)
        subprocess_post_check(pip_process, raise_error=False)
        if pip_process.returncode:
            raise PipxError(
                f"Error installing {full_package_description(package, package_or_url)}."
            )

        self._update_package_metadata(
            package=package,
            package_or_url=package_or_url,
            pip_args=pip_args,
            include_dependencies=include_dependencies,
            include_apps=include_apps,
            is_main_package=is_main_package,
            suffix=suffix,
        )

        # Verify package installed ok
        if self.package_metadata[package].package_version is None:
            raise PipxError(
                f"Unable to install "
                f"{full_package_description(package, package_or_url)}.\n"
                f"Check the name or spec for errors, and verify that it can "
                f"be installed with pip.",
                wrap_message=False,
            )

    def install_package_no_deps(self, package_or_url: str, pip_args: List[str]) -> str:
        with animate(
            f"determining package name from {package_or_url!r}", self.do_animation
        ):
            old_package_set = self.list_installed_packages()
            cmd = ["install"] + ["--no-dependencies"] + pip_args + [package_or_url]
            pip_process = self._run_pip(cmd)
        subprocess_post_check(pip_process, raise_error=False)
        if pip_process.returncode:
            raise PipxError(
                f"""
                Cannot determine package name from spec {package_or_url!r}.
                Check package spec for errors.
                """
            )

        installed_packages = self.list_installed_packages() - old_package_set
        if len(installed_packages) == 1:
            package = installed_packages.pop()
            logger.info(f"Determined package name: {package}")
        else:
            logger.info(f"old_package_set = {old_package_set}")
            logger.info(f"install_packages = {installed_packages}")
            raise PipxError(
                f"""
                Cannot determine package name from spec {package_or_url!r}.
                Check package spec for errors.
                """
            )

        return package

    def get_venv_metadata_for_package(self, package: str) -> VenvMetadata:
        data = json.loads(
            run_subprocess(
                [
                    self.python_path,
                    "-c",
                    VENV_METADATA_INSPECTOR,
                    package,
                    self.bin_path,
                ],
                capture_stderr=False,
                log_cmd_str=" ".join(
                    [
                        str(self.python_path),
                        "-c",
                        "<contents of venv_metadata_inspector.py>",
                        package,
                        str(self.bin_path),
                    ]
                ),
            ).stdout
        )

        venv_metadata_traceback = data.pop("exception_traceback", None)
        if venv_metadata_traceback is not None:
            logger.error("Internal error with venv metadata inspection.")
            logger.info(
                f"venv_metadata_inspector.py Traceback:\n{venv_metadata_traceback}"
            )

        data["app_paths"] = [Path(p) for p in data["app_paths"]]
        for dep in data["app_paths_of_dependencies"]:
            data["app_paths_of_dependencies"][dep] = [
                Path(p) for p in data["app_paths_of_dependencies"][dep]
            ]

        return VenvMetadata(**data)

    def _update_package_metadata(
        self,
        package: str,
        package_or_url: str,
        pip_args: List[str],
        include_dependencies: bool,
        include_apps: bool,
        is_main_package: bool,
        suffix: str = "",
    ) -> None:
        venv_package_metadata = self.get_venv_metadata_for_package(package)
        package_info = PackageInfo(
            package=package,
            package_or_url=parse_specifier_for_metadata(package_or_url),
            pip_args=pip_args,
            include_apps=include_apps,
            include_dependencies=include_dependencies,
            apps=venv_package_metadata.apps,
            app_paths=venv_package_metadata.app_paths,
            apps_of_dependencies=venv_package_metadata.apps_of_dependencies,
            app_paths_of_dependencies=venv_package_metadata.app_paths_of_dependencies,
            package_version=venv_package_metadata.package_version,
            suffix=suffix,
        )
        if is_main_package:
            self.pipx_metadata.main_package = package_info
        else:
            self.pipx_metadata.injected_packages[package] = package_info

        self.pipx_metadata.write()

    def get_python_version(self) -> str:
        return run_subprocess([str(self.python_path), "--version"]).stdout.strip()

    def list_installed_packages(self) -> Set[str]:
        cmd_run = run_subprocess(
            [str(self.python_path), "-m", "pip", "list", "--format=json"]
        )
        pip_list = json.loads(cmd_run.stdout.strip())
        return set([x["name"] for x in pip_list])

    def run_app(self, app: str, app_args: List[str]) -> None:
        exec_app([str(self.bin_path / app)] + app_args)

    def _upgrade_package_no_metadata(self, package: str, pip_args: List[str]) -> None:
        with animate(
            f"upgrading {full_package_description(package, package)}", self.do_animation
        ):
            pip_process = self._run_pip(["install"] + pip_args + ["--upgrade", package])
        subprocess_post_check(pip_process)

    def upgrade_package(
        self,
        package: str,
        package_or_url: str,
        pip_args: List[str],
        include_dependencies: bool,
        include_apps: bool,
        is_main_package: bool,
        suffix: str = "",
    ) -> None:
        with animate(
            f"upgrading {full_package_description(package, package_or_url)}",
            self.do_animation,
        ):
            pip_process = self._run_pip(
                ["install"] + pip_args + ["--upgrade", package_or_url]
            )
        subprocess_post_check(pip_process)

        self._update_package_metadata(
            package=package,
            package_or_url=package_or_url,
            pip_args=pip_args,
            include_dependencies=include_dependencies,
            include_apps=include_apps,
            is_main_package=is_main_package,
            suffix=suffix,
        )

    def _run_pip(self, cmd: List[str]) -> CompletedProcess:
        cmd = [str(self.python_path), "-m", "pip"] + cmd
        if not self.verbose:
            cmd.append("-q")
        return run_subprocess(cmd)

    def run_pip_get_exit_code(self, cmd: List[str]) -> ExitCode:
        cmd = [str(self.python_path), "-m", "pip"] + cmd
        if not self.verbose:
            cmd.append("-q")
        returncode = run_subprocess(
            cmd, capture_stdout=False, capture_stderr=False
        ).returncode
        if returncode:
            cmd_str = " ".join(str(c) for c in cmd)
            logger.error(f"{cmd_str!r} failed")
        return ExitCode(returncode)
