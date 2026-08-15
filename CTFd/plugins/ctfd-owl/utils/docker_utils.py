import os
import random
import re
import secrets
import subprocess
import uuid
from string import digits, ascii_lowercase
from typing import Union

import yaml

from CTFd.models import Flags
from .db_utils import DBUtils
from .labels_utils import LabelsUtils
from ..extensions import log
from ..models import DynamicCheckChallenge, OwlContainers


class DockerUtils:
    GEN_PLACEHOLDER = re.compile(r"\$\[!gen(?::(\d+))?!\]")

    @staticmethod
    def _get_plugin_root_dir() -> str:
        """Return absolute path to the ctfd-owl plugin root directory.

        This module lives in `ctfd-owl/utils/`. We resolve paths like `source/`
        relative to the plugin root (`ctfd-owl/`), not `utils/`.
        """
        return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    @staticmethod
    def gen_flag():
        """Generate a per-instance flag.

        Supports:
        - Fully dynamic flags: <prefix>{<random>} with configurable length
        - Semi-dynamic templates: replace $[!gen!] or $[!gen:06!] placeholders
        """
        configs = DBUtils.get_all_configs()
        prefix = (configs.get("docker_flag_prefix") or "").strip()
        try:
            dynamic_len = int(configs.get("docker_dynamic_flag_length", "32") or 32)
        except Exception:
            dynamic_len = 32

        alphabet = digits + ascii_lowercase

        def random_chunk(length: int) -> str:
            try:
                length = int(length)
            except Exception:
                length = dynamic_len
            if length < 0:
                length = 0
            if length > 4096:
                length = 4096
            return "".join(secrets.choice(alphabet) for _ in range(length))

        def gen_default_flag() -> str:
            return f"{prefix}{{{random_chunk(dynamic_len)}}}"

        flag = gen_default_flag()
        while OwlContainers.query.filter_by(flag=flag).first() is not None:
            flag = gen_default_flag()
        return flag

    @staticmethod
    def gen_flag_from_template(template: str):
        """Generate a per-instance flag from a template containing $[!gen!] placeholders."""
        configs = DBUtils.get_all_configs()
        try:
            default_len = int(configs.get("docker_semi_dynamic_flag_length", "6") or 6)
        except Exception:
            default_len = 6

        alphabet = digits + ascii_lowercase

        def random_chunk(length: int) -> str:
            try:
                length = int(length)
            except Exception:
                length = default_len
            if length < 0:
                length = 0
            if length > 4096:
                length = 4096
            return "".join(secrets.choice(alphabet) for _ in range(length))

        def repl(match: re.Match) -> str:
            length_str = match.group(1)
            length = int(length_str) if length_str is not None else default_len
            return random_chunk(length)

        flag = DockerUtils.GEN_PLACEHOLDER.sub(repl, template)
        while OwlContainers.query.filter_by(flag=flag).first() is not None:
            flag = DockerUtils.GEN_PLACEHOLDER.sub(repl, template)
        return flag

    @staticmethod
    def get_socket():
        configs = DBUtils.get_all_configs()
        socket = configs.get("docker_api_url")
        return socket

    @staticmethod
    def resolve_flag(challenge, challenge_id):
        """Resolve a single flag for a challenge based on its flag_type.

        For dynamic challenges each call yields a fresh unique flag; for
        semi-dynamic it renders the stored template; for static it returns the
        stored CTFd flag content.
        """
        if challenge.flag_type == 'static':
            return Flags.query.filter_by(challenge_id=challenge_id).first_or_404().content

        # Dynamic / semi-dynamic mode.
        # Semi-dynamic uses a flag template stored as a regular CTFd flag containing $[!gen!] placeholders.
        flag_record = Flags.query.filter_by(challenge_id=challenge_id).first()
        template = (flag_record.content if flag_record else "")

        if challenge.flag_type == 'semi-dynamic':
            if not template:
                raise ValueError("Semi-dynamic flag mode requires a flag template")
            if not DockerUtils.GEN_PLACEHOLDER.search(template):
                raise ValueError("Semi-dynamic flag template must contain $[!gen!] or $[!gen:N!] placeholder")
            return DockerUtils.gen_flag_from_template(template)

        # Backward compatible: allow semi-dynamic placeholders even when flag_type is just 'dynamic'.
        if template and DockerUtils.GEN_PLACEHOLDER.search(template):
            return DockerUtils.gen_flag_from_template(template)
        return DockerUtils.gen_flag()

    @staticmethod
    def up_docker_compose(user_id, challenge_id, flag_specs=None):
        """Launch a challenge's docker-compose.

        When ``flag_specs`` is provided (a multitask group) as a ``{index: challenge_id}``
        mapping, one flag is resolved per index from that task's own challenge id and
        exported as ``FLAG<index>``; the returned flag value is then a ``{index: flag}``
        dict instead of a single string. Resolving per task id makes static (and
        semi-dynamic) flags come from each task's own CTFd flag/template, while dynamic
        flags stay per-instance random. ``FLAG`` is always exported (set to the
        lowest-index flag) for backward compatibility.
        """
        try:
            configs = DBUtils.get_all_configs()
            plugin_root = DockerUtils._get_plugin_root_dir()
            challenge: DynamicCheckChallenge = DynamicCheckChallenge.query.filter_by(id=challenge_id).first_or_404()

            if flag_specs is not None:
                flag = {
                    int(i): DockerUtils.resolve_flag(challenge, task_cid)
                    for i, task_cid in flag_specs.items()
                }
            else:
                flag = DockerUtils.resolve_flag(challenge, challenge_id)

            socket = DockerUtils.get_socket()
            sname = os.path.join(plugin_root, "source", challenge.dirname)
            dirname = challenge.dirname.split("/")[-1]
            prefix = configs.get("docker_flag_prefix")
            name = "{}_user{}_{}".format(prefix, user_id, dirname).lower()
            problem_docker_run_dir = os.environ['PROBLEM_DOCKER_RUN_FOLDER']
            dname = os.path.join(problem_docker_run_dir, name)
            min_port, max_port = int(configs.get("frp_direct_port_minimum")), int(
                configs.get("frp_direct_port_maximum"))
            all_container = DBUtils.get_all_container()

            ports = []
            ports_list = [_.port for _ in all_container]
            compose_data: dict[str, Union[str, int, dict]] = yaml.safe_load(
                open(sname + '/docker-compose.yml', 'r').read())
            for service in compose_data["services"].keys():
                service_labels = compose_data["services"][service].get("labels")
                owl_meta = LabelsUtils.parse_owl_metadata(service_labels)
                if service_labels is not None and bool((owl_meta.get("proxy") or {}).get("enabled")) is True:
                    port = random.randint(min_port, max_port)
                    while port in ports_list or port in [x["port"] for x in ports]:
                        port = random.randint(min_port, max_port)
                    labels_json = LabelsUtils.dumps_labels(owl_meta)
                    ports.append(
                        {
                            "service": service,
                            "port": port,
                            "labels": labels_json,
                        })
        except Exception as e:
            try:
                log("owl",
                'Stdout: {out}\nStderr: {err}',
                out=e.stdout.decode(),
                err=e.stderr.decode()
                )
            except Exception:
                log("owl",
                'Stdout: {out}\nStderr: {err}',
                out=e,
                err=e
                )
            return e
        
        try:
            command = "cp -r '{}' '{}'".format(sname, dname)
            process = subprocess.run(command, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            command = "cd '{}' && cp docker-compose.yml run.yml".format(dname)
            process = subprocess.run(command, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            # up docker-compose
            if isinstance(flag, dict):
                # Multitask: export FLAG<index> for each flag, and FLAG=lowest index for compat.
                primary = flag[min(flag.keys())]
                exports = "export FLAG='{}'".format(primary)
                for i in sorted(flag.keys()):
                    exports += " && export FLAG{}='{}'".format(i, flag[i])
            else:
                exports = "export FLAG='{}'".format(flag)
            command = exports + " && cd " + dname + " && sed -i \'s/CTFD_PRIVATE_NETWORK/" + name + "/\' run.yml " + "&& export DOCKER_HOST='{}' && docker compose -f run.yml up -d".format(
                socket)
            process = subprocess.run(command, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            log(
                "owl",
                '[{date}] {msg}',
                msg=name + " up."
            )
            docker_id = str(uuid.uuid3(uuid.NAMESPACE_DNS, name)).replace("-", "")
            return docker_id, ports, flag, challenge.redirect_type, dirname
        except subprocess.CalledProcessError as e:
            log("owl",
                'Stdout: {out}\nStderr: {err}',
                out=e.stdout.decode(),
                err=e.stderr.decode()
                )
            return e.stderr.decode()

    @staticmethod
    def down_docker_compose(user_id, challenge_id):
        try:
            configs = DBUtils.get_all_configs()
            socket = DockerUtils.get_socket()
            challenge = DynamicCheckChallenge.query.filter_by(id=challenge_id).first_or_404()
            dirname = challenge.dirname.split("/")[-1]
            prefix = configs.get("docker_flag_prefix")
            name = "{}_user{}_{}".format(prefix, user_id, dirname).lower()
            problem_docker_run_dir = os.environ['PROBLEM_DOCKER_RUN_FOLDER']
            dname = os.path.join(problem_docker_run_dir, name)
        except Exception as e:
            try:
                log("owl",
                'Stdout: {out}\nStderr: {err}',
                out=e.stdout.decode(),
                err=e.stderr.decode()
                )
            except Exception as err:
                log("owl",
                'Stdout: {out}\nStderr: {err}',
                out=e,
                err=e
                )
            return str(e)

        try:
            command = "cd {} && export DOCKER_HOST='{}' && docker compose -f run.yml kill && docker compose -f run.yml down".format(dname, socket)
            process = subprocess.run(command, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            command = "rm -rf {}".format(dname)
            process = subprocess.run(command, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            log(
                "owl",
                "[{date}] {msg}",
                msg=name + " down.",
            )
            return True
        except subprocess.CalledProcessError as e:
            log("owl",
                'Stdout: {out}\nStderr: {err}',
                out=e.stdout.decode(),
                err=e.stderr.decode()
                )
            return str(e.stderr.decode())

    @staticmethod
    def remove_current_docker_container(user_id, challenge_id=None, is_retry=False):
        configs = DBUtils.get_all_configs()
        if challenge_id is None:
            containers = DBUtils.get_current_containers(user_id=user_id)
        else:
            containers = DBUtils.get_current_containers_for_challenge(user_id=user_id, challenge_id=challenge_id)

        if containers is None:
            return False
        try:
            challenge_ids = sorted({c.challenge_id for c in containers})
            for cid in challenge_ids:
                DockerUtils.down_docker_compose(user_id, challenge_id=cid)

            if challenge_id is None:
                DBUtils.remove_current_container(user_id)
            else:
                DBUtils.remove_current_container_for_challenge(user_id=user_id, challenge_id=challenge_id)
            return True
        except Exception as e:
            import traceback
            print(traceback.format_exc())
            # remove operation
            return False
