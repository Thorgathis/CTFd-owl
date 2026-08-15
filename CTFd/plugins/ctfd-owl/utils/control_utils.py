import time

from flask import session
from sqlalchemy.sql import and_

from CTFd.models import Challenges, Users
from .db_utils import DBUtils
from .docker_utils import DockerUtils
from ..extensions import log
from ..models import DynamicCheckChallenge, MultiDynamicCheckChallenge


class ControlUtil:
    @staticmethod
    def is_multi(challenge) -> bool:
        return isinstance(challenge, MultiDynamicCheckChallenge) or (
            str(getattr(challenge, "type", "") or "") == "dynamic_check_docker_multi"
        )

    @staticmethod
    def multi_anchor_id(challenge) -> int:
        """Return the group anchor (parent) challenge id for a multitask challenge."""
        return int(getattr(challenge, "parent_id", None) or challenge.id)

    @staticmethod
    def get_multi_group_tasks(anchor_id):
        """Return all tasks (parent + children) of a multitask group, ordered by flag_index."""
        tasks = (
            MultiDynamicCheckChallenge.query.filter(
                (MultiDynamicCheckChallenge.id == anchor_id)
                | (MultiDynamicCheckChallenge.parent_id == anchor_id)
            ).all()
        )
        tasks.sort(key=lambda t: (int(t.flag_index or 1), int(t.id)))
        return tasks

    @staticmethod
    def new_container(user_id, challenge_id, prefix, instance_mode="personal"):
        challenge = DynamicCheckChallenge.query.filter_by(id=challenge_id).first()
        if ControlUtil.is_multi(challenge):
            return ControlUtil.new_multi_container(user_id, challenge, prefix, instance_mode)

        rq = DockerUtils.up_docker_compose(user_id=user_id, challenge_id=challenge_id)
        if isinstance(rq, tuple):
            for container in rq[1]:
                log(
                    "owl",
                    "[{date}] {msg}",
                    msg=f'Container name: {prefix.lower()}_user{user_id}_{rq[4]}_{container["service"]}_1',
                )
                DBUtils.new_container(user_id, challenge_id, flag=rq[2], port=container["port"], docker_id=rq[0],
                                      ip=rq[3], name=f'{prefix.lower()}_user{user_id}_{rq[4]}-{container["service"]}-1',
                                      instance_mode=instance_mode,
                                      labels=container.get("labels", "{}"))
            return True
        else:
            return rq

    @staticmethod
    def new_multi_container(user_id, challenge, prefix, instance_mode="personal"):
        """Deploy one container for a multitask group and create per-task rows.

        The compose is launched once against the anchor (parent) challenge with one
        dynamic flag per task (``FLAG<flag_index>``). Container service rows are then
        duplicated under every task's challenge id, each carrying its own flag, so the
        instance shows as running for all tasks and every task accepts its own flag.
        """
        anchor_id = ControlUtil.multi_anchor_id(challenge)
        # Materialize scalar (id, flag_index) up front: DBUtils.new_container closes the
        # session below, which would detach these ORM objects and raise
        # DetachedInstanceError on the next iteration.
        task_specs = [(int(t.id), int(t.flag_index or 1)) for t in ControlUtil.get_multi_group_tasks(anchor_id)]
        if not task_specs:
            return "Multitask group has no tasks."

        # Map each flag index to its own task challenge id so per-task flags
        # (static / semi-dynamic) are resolved from the right challenge.
        flag_specs = {idx: tid for (tid, idx) in task_specs}
        rq = DockerUtils.up_docker_compose(user_id=user_id, challenge_id=anchor_id, flag_specs=flag_specs)
        if not isinstance(rq, tuple):
            return rq

        flags_by_index = rq[2]
        for task_id, idx in task_specs:
            task_flag = flags_by_index.get(idx)
            for container in rq[1]:
                log(
                    "owl",
                    "[{date}] {msg}",
                    msg=f'Container name: {prefix.lower()}_user{user_id}_{rq[4]}_{container["service"]}_1 (task {task_id}, flag {idx})',
                )
                DBUtils.new_container(user_id, task_id, flag=task_flag, port=container["port"], docker_id=rq[0],
                                      ip=rq[3], name=f'{prefix.lower()}_user{user_id}_{rq[4]}-{container["service"]}-1',
                                      instance_mode=instance_mode,
                                      labels=container.get("labels", "{}"))
        return True

    @staticmethod
    def destroy_container(user_id):
        try:
            docker_result = DockerUtils.remove_current_docker_container(user_id)
            return True
        except Exception as e:
            import traceback
            print(traceback.format_exc())
            return False

    @staticmethod
    def destroy_container_for_challenge(user_id, challenge_id):
        challenge = DynamicCheckChallenge.query.filter_by(id=challenge_id).first()
        if ControlUtil.is_multi(challenge):
            return ControlUtil.destroy_multi_container(user_id, challenge)
        try:
            DockerUtils.remove_current_docker_container(user_id=user_id, challenge_id=challenge_id)
            return True
        except Exception:
            import traceback
            print(traceback.format_exc())
            return False

    @staticmethod
    def destroy_multi_container(user_id, challenge):
        """Tear down a multitask group's single container and remove all per-task rows."""
        try:
            anchor_id = ControlUtil.multi_anchor_id(challenge)
            task_ids = [t.id for t in ControlUtil.get_multi_group_tasks(anchor_id)]
            # The compose only exists under the anchor's dirname; down it once.
            DockerUtils.down_docker_compose(user_id, challenge_id=anchor_id)
            DBUtils.remove_current_container_for_challenges(user_id=user_id, challenge_ids=task_ids)
            return True
        except Exception:
            import traceback
            print(traceback.format_exc())
            return False

    @staticmethod
    def expired_container(user_id):
        DBUtils.renew_current_container(user_id=user_id)

    @staticmethod
    def expired_container_for_challenge(user_id, challenge_id):
        challenge = DynamicCheckChallenge.query.filter_by(id=challenge_id).first()
        if ControlUtil.is_multi(challenge):
            anchor_id = ControlUtil.multi_anchor_id(challenge)
            task_ids = [t.id for t in ControlUtil.get_multi_group_tasks(anchor_id)]
            DBUtils.renew_current_container_for_challenges(user_id=user_id, challenge_ids=task_ids)
            return
        DBUtils.renew_current_container_for_challenge(user_id=user_id, challenge_id=challenge_id)

    @staticmethod
    def get_container(user_id):
        return DBUtils.get_current_containers(user_id=user_id)

    @staticmethod
    def get_container_for_challenge(user_id, challenge_id):
        return DBUtils.get_current_containers_for_challenge(user_id=user_id, challenge_id=challenge_id)

    @staticmethod
    def check_challenge(challenge_id, user_id):
        user = Users.query.filter_by(id=user_id).first()

        if user.type == "admin":
            Challenges.query.filter(
                Challenges.id == challenge_id
            ).first_or_404()
        else:
            Challenges.query.filter(
                Challenges.id == challenge_id,
                and_(Challenges.state != "hidden", Challenges.state != "locked"),
            ).first_or_404()

    @staticmethod
    def frequency_limit():
        if "limit" not in session:
            session["limit"] = int(time.time())
            return False

        if int(time.time()) - session["limit"] < 1:
            return True

        session["limit"] = int(time.time())
        return False
